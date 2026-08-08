"""Silent text and multimodal analysis using AstrBot's active provider."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
from astrbot.api import logger
from astrbot.api.star import Context

from .exceptions import LogisticsAIAnalysisError
from .prompts import ANALYSIS_SYSTEM_PROMPT, build_analysis_prompt


class MultimodalAnalyzer:
    """Analyze logistics messages without producing a visible bot reply.

    ``multimodal_analysis_enabled`` is intentionally reserved for image-aware
    extraction. Text-only extraction is delegated to the backend DeepSeek
    provider so AstrBot's single multimodal provider is not overloaded.
    """

    _CODE_FENCE_PATTERN = re.compile(
        r"^\s*```(?:json)?\s*(.*?)\s*```\s*$",
        re.IGNORECASE | re.DOTALL,
    )
    _ARRAY_FIELDS = (
        "sailings",
        "freightRates",
        "events",
        "unmappedFacts",
        "evidence",
        "warnings",
    )

    def __init__(
        self,
        context: Context,
        config: Mapping[str, Any] | None = None,
    ) -> None:
        self._context = context
        self._config = config or {}
        self.enabled = self._get_bool("multimodal_analysis_enabled", False)
        self._timeout = self._get_float(
            "multimodal_analysis_timeout",
            120.0,
            minimum=10.0,
        )
        max_concurrency = self._get_int(
            "multimodal_analysis_concurrency",
            1,
            minimum=1,
            maximum=4,
        )
        self._temperature = self._get_float(
            "multimodal_analysis_temperature",
            0.1,
            minimum=0.0,
            maximum=1.0,
        )
        self._semaphore = asyncio.Semaphore(max_concurrency)

    def should_analyze(self, message: dict[str, Any]) -> bool:
        """Return whether a captured message needs AstrBot-side vision."""
        return self.enabled and bool(message.get("images"))

    async def analyze(
        self,
        message: dict[str, Any],
        unified_msg_origin: str,
    ) -> dict[str, Any]:
        """Call the active provider and return a normalized analysis object."""
        image_urls = self._normalize_string_list(message.get("images"))
        provider_image_urls, temporary_files = await self._prepare_images(
            image_urls
        )

        try:
            async with self._semaphore:
                provider_id = await self._context.get_current_chat_provider_id(
                    umo=unified_msg_origin
                )
                prompt = build_analysis_prompt(message)
                generate_kwargs: dict[str, Any] = {
                    "chat_provider_id": provider_id,
                    "prompt": prompt,
                    "system_prompt": ANALYSIS_SYSTEM_PROMPT,
                    "temperature": self._temperature,
                }
                if provider_image_urls:
                    generate_kwargs["image_urls"] = provider_image_urls

                try:
                    response = await asyncio.wait_for(
                        self._context.llm_generate(**generate_kwargs),
                        timeout=self._timeout,
                    )
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError as exc:
                    raise LogisticsAIAnalysisError(
                        f"AI analysis timed out after {self._timeout:.0f} seconds."
                    ) from exc
                except Exception as exc:
                    raise LogisticsAIAnalysisError(
                        f"AstrBot AI provider request failed: {exc}"
                    ) from exc

                completion_text = str(
                    getattr(response, "completion_text", "") or ""
                ).strip()
                if not completion_text:
                    raise LogisticsAIAnalysisError(
                        "AstrBot AI provider returned an empty response."
                    )

                analysis = self.extract_json_object(completion_text)
                self._normalize_analysis(analysis)
                analysis["analyzer"] = {
                    "location": "astrbot",
                    "providerId": provider_id,
                    "mode": "multimodal_extraction"
                    if image_urls
                    else "text_extraction",
                }
                analysis["sourceMessageId"] = str(
                    message.get("messageId") or ""
                )
                analysis["analyzedAt"] = datetime.now(timezone.utc).isoformat()
                return analysis
        finally:
            self._cleanup_temporary_files(temporary_files)

    async def _prepare_images(
        self,
        image_urls: list[str],
    ) -> tuple[list[str], list[str]]:
        """Make provider image inputs usable without relying on QQ URL access."""
        prepared: list[str] = []
        temporary_files: list[str] = []

        for image_url in image_urls:
            local_path = self._existing_local_path(image_url)
            if local_path is not None:
                prepared.append(local_path)
                continue

            if not image_url.startswith(("http://", "https://")):
                prepared.append(image_url)
                continue

            try:
                downloaded_path = await self._download_image(image_url)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "LogisticsAI could not download image for AI analysis; "
                    "the provider will receive the original URL: %s",
                    exc,
                )
                prepared.append(image_url)
                continue

            prepared.append(downloaded_path)
            temporary_files.append(downloaded_path)

        return prepared, temporary_files

    async def _download_image(self, image_url: str) -> str:
        """Download one remote image to a temporary local file."""
        timeout = aiohttp.ClientTimeout(
            total=min(max(self._timeout, 10.0), 30.0)
        )
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                image_url,
                allow_redirects=True,
            ) as response:
                if response.status < 200 or response.status >= 300:
                    raise LogisticsAIAnalysisError(
                        f"image download returned HTTP {response.status}"
                    )

                content = await response.read()
                if not content:
                    raise LogisticsAIAnalysisError(
                        "image download returned an empty body"
                    )
                if len(content) > 15 * 1024 * 1024:
                    raise LogisticsAIAnalysisError(
                        "image download exceeded the 15 MiB limit"
                    )

                content_type = response.headers.get("Content-Type", "")
                suffix = mimetypes.guess_extension(
                    content_type.split(";", 1)[0].strip()
                ) or Path(image_url).suffix[:8]
                suffix = suffix if suffix and len(suffix) <= 8 else ".img"

                with tempfile.NamedTemporaryFile(
                    prefix="logistics-ai-image-",
                    suffix=suffix,
                    delete=False,
                ) as temporary_file:
                    temporary_file.write(content)
                    return temporary_file.name

    @staticmethod
    def _existing_local_path(value: str) -> str | None:
        try:
            path = Path(value).expanduser()
            return str(path.resolve()) if path.is_file() else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _cleanup_temporary_files(paths: list[str]) -> None:
        for path in paths:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                logger.debug(
                    "LogisticsAI could not remove temporary image file: %s",
                    path,
                    exc_info=True,
                )

    @classmethod
    def extract_json_object(cls, value: str) -> dict[str, Any]:
        """Extract the first valid JSON object from a provider response."""
        normalized = value.lstrip("\ufeff").strip()
        fence_match = cls._CODE_FENCE_PATTERN.match(normalized)
        if fence_match:
            normalized = fence_match.group(1).strip()

        try:
            parsed = json.loads(normalized)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        decoder = json.JSONDecoder()
        for index, character in enumerate(normalized):
            if character != "{":
                continue
            try:
                parsed, _ = decoder.raw_decode(normalized[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed

        excerpt = normalized[:300].replace("\n", " ")
        raise LogisticsAIAnalysisError(
            "Multimodal provider did not return a JSON object. "
            f"Response excerpt: {excerpt}"
        )

    @classmethod
    def failure_payload(
        cls,
        message_id: str,
        error: Exception,
    ) -> dict[str, Any]:
        """Create a bounded failure result for backend observability."""
        return {
            "schemaVersion": "1.0",
            "status": "failed",
            "documentType": "unknown",
            "summary": "AstrBot AI analysis failed.",
            "extractedText": "",
            "sailings": [],
            "freightRates": [],
            "events": [],
            "unmappedFacts": [],
            "evidence": [],
            "warnings": [str(error)[:500]],
            "requiresBackendContextResolution": False,
            "sourceMessageId": message_id,
            "analyzedAt": datetime.now(timezone.utc).isoformat(),
        }

    @classmethod
    def _normalize_analysis(cls, analysis: dict[str, Any]) -> None:
        analysis.setdefault("schemaVersion", "1.0")
        analysis["status"] = str(analysis.get("status") or "succeeded")
        analysis["documentType"] = str(
            analysis.get("documentType") or "unknown"
        )
        analysis["summary"] = str(analysis.get("summary") or "")
        analysis["extractedText"] = str(
            analysis.get("extractedText") or ""
        )

        for field_name in cls._ARRAY_FIELDS:
            if not isinstance(analysis.get(field_name), list):
                analysis[field_name] = []

        analysis["requiresBackendContextResolution"] = bool(
            analysis.get("requiresBackendContextResolution")
            or analysis["events"]
        )

    @staticmethod
    def _normalize_string_list(value: Any) -> list[str]:
        if not isinstance(value, (list, tuple, set)):
            return []
        return list(
            dict.fromkeys(
                text
                for item in value
                if (text := str(item or "").strip())
            )
        )

    def _get_raw_value(self, key: str, default: Any) -> Any:
        getter = getattr(self._config, "get", None)
        if callable(getter):
            try:
                return getter(key, default)
            except TypeError:
                value = getter(key)
                return default if value is None else value
            except Exception:
                logger.debug(
                    "Failed to read LogisticsAI analysis setting %s.",
                    key,
                    exc_info=True,
                )

        try:
            return self._config[key]
        except (KeyError, TypeError):
            return default

    def _get_bool(self, key: str, default: bool) -> bool:
        value = self._get_raw_value(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        return bool(value)

    def _get_int(
        self,
        key: str,
        default: int,
        *,
        minimum: int,
        maximum: int,
    ) -> int:
        value = self._get_raw_value(key, default)
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = default
        return max(minimum, min(number, maximum))

    def _get_float(
        self,
        key: str,
        default: float,
        *,
        minimum: float,
        maximum: float | None = None,
    ) -> float:
        value = self._get_raw_value(key, default)
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = default
        number = max(minimum, number)
        return min(number, maximum) if maximum is not None else number
