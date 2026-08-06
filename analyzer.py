"""Silent multimodal analysis using AstrBot's active chat provider."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from astrbot.api import logger
from astrbot.api.star import Context

from .exceptions import LogisticsAIAnalysisError
from .prompts import ANALYSIS_SYSTEM_PROMPT, build_analysis_prompt


class MultimodalAnalyzer:
    """Analyze image-bearing messages without producing a visible bot reply."""

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
        if not image_urls:
            raise LogisticsAIAnalysisError(
                "Multimodal analysis requires at least one current-message image."
            )

        async with self._semaphore:
            provider_id = await self._context.get_current_chat_provider_id(
                umo=unified_msg_origin
            )
            prompt = build_analysis_prompt(message)

            try:
                response = await asyncio.wait_for(
                    self._context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=prompt,
                        image_urls=image_urls,
                        system_prompt=ANALYSIS_SYSTEM_PROMPT,
                        temperature=self._temperature,
                    ),
                    timeout=self._timeout,
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError as exc:
                raise LogisticsAIAnalysisError(
                    f"Multimodal analysis timed out after {self._timeout:.0f} seconds."
                ) from exc
            except Exception as exc:
                raise LogisticsAIAnalysisError(
                    f"AstrBot multimodal provider request failed: {exc}"
                ) from exc

            completion_text = str(
                getattr(response, "completion_text", "") or ""
            ).strip()
            if not completion_text:
                raise LogisticsAIAnalysisError(
                    "AstrBot multimodal provider returned an empty response."
                )

            analysis = self.extract_json_object(completion_text)
            self._normalize_analysis(analysis)
            analysis["analyzer"] = {
                "location": "astrbot",
                "providerId": provider_id,
                "mode": "visual_extraction",
            }
            analysis["sourceMessageId"] = str(
                message.get("messageId") or ""
            )
            analysis["analyzedAt"] = datetime.now(timezone.utc).isoformat()
            return analysis

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
            "summary": "AstrBot multimodal analysis failed.",
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

