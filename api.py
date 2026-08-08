"""Asynchronous client for the LogisticsAI backend."""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Mapping
from typing import Any

import aiohttp
from astrbot.api import logger

from .exceptions import (
    LogisticsAIConfigurationError,
    LogisticsAIRequestError,
)
from .models import LogisticsMessage, UploadReceipt


class ApiClient:
    """Upload raw messages; all AI work stays in the backend."""

    RETRYABLE_STATUS_CODES = {
        408,
        425,
        429,
        500,
        502,
        503,
        504,
    }

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        """Read configuration and defer creation of the HTTP session."""
        self._config = config or {}
        self._enabled = self._get_bool("enabled", True)
        self._api_url = self._get_string(
            "api_url",
            "http://127.0.0.1:5000/api/messages",
        )
        self._api_token = self._get_string("api_token", "")
        self._token_header = self._get_string(
            "token_header",
            "Authorization",
        )
        configured_timeout = self._get_float("timeout", 30.0, minimum=5.0)
        self._timeout = configured_timeout
        self._assistant_reply_timeout = self._get_float(
            "assistant_reply_timeout",
            300.0,
            minimum=10.0,
        )
        self._retry_count = self._get_int(
            "retry_count",
            3,
            minimum=0,
            maximum=10,
        )
        self._retry_interval = self._get_float(
            "retry_interval",
            1.0,
            minimum=0.1,
        )
        self._verify_ssl = self._get_bool("verify_ssl", True)
        self._max_concurrency = self._get_int(
            "max_concurrency",
            4,
            minimum=1,
            maximum=100,
        )
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(self._max_concurrency)
        self._closed = False

        if self._enabled and not self._api_url:
            raise LogisticsAIConfigurationError(
                "api_url cannot be empty when LogisticsAI uploads are enabled."
            )

    @property
    def enabled(self) -> bool:
        """Return whether backend uploads are enabled."""
        return self._enabled

    async def upload(self, data: dict[str, Any]) -> UploadReceipt | None:
        """Upload one raw group message and return backend identifiers."""
        if not self._enabled:
            return None

        self._ensure_open()
        payload = LogisticsMessage.from_dict(data).to_dict()

        async with self._semaphore:
            response_body = await self._request_with_retry(
                method="POST",
                url=self._api_url,
                payload=payload,
            )

        return self._parse_upload_receipt(response_body, payload)

    async def upload_text_analysis(
        self,
        receipt: UploadReceipt,
        *,
        assistant_reply: str,
    ) -> None:
        """Forward a native AstrBot reply to the existing analysis endpoint.

        The backend keeps the original row, replaces its raw content with this
        native media description, and queues the same row for another analysis
        pass. The separate request never blocks initial message capture.
        """
        if not self._enabled or receipt.database_id is None:
            return

        normalized_reply = str(assistant_reply or "").strip()
        if not normalized_reply:
            logger.warning(
                "Skipping native assistant reply upload because the reply is empty. "
                "database_id=%s",
                receipt.database_id,
            )
            return

        self._ensure_open()
        analysis_url = (
            f"{self._api_url.rstrip('/')}/"
            f"{receipt.database_id}/text-analysis"
        )
        async with self._semaphore:
            await self._request_with_retry(
                method="POST",
                url=analysis_url,
                payload={"assistantReply": normalized_reply},
                timeout_seconds=self._assistant_reply_timeout,
            )

        logger.info(
            "Native assistant reply forwarded for backend analysis. database_id=%s",
            receipt.database_id,
        )

    async def _request_with_retry(
        self,
        *,
        method: str,
        url: str,
        payload: dict[str, Any],
        timeout_seconds: float | None = None,
    ) -> str:
        """Execute an HTTP request with bounded exponential backoff."""
        total_attempts = self._retry_count + 1
        last_error: Exception | None = None

        for attempt in range(1, total_attempts + 1):
            try:
                return await self._request(
                    method,
                    url,
                    payload,
                    timeout_seconds=timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except LogisticsAIRequestError as exc:
                last_error = exc
                if (
                    exc.status_code is not None
                    and exc.status_code not in self.RETRYABLE_STATUS_CODES
                ):
                    raise
                if attempt >= total_attempts:
                    raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt >= total_attempts:
                    break

            delay = self._calculate_retry_delay(attempt)
            logger.warning(
                "LogisticsAI request failed; retrying in %.2f seconds "
                "(%d/%d): %s",
                delay,
                attempt,
                total_attempts,
                last_error,
            )
            await asyncio.sleep(delay)

        raise LogisticsAIRequestError(
            f"LogisticsAI request failed after retries: {last_error}"
        ) from last_error

    async def _request(
        self,
        method: str,
        url: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> str:
        """Send one JSON request to the LogisticsAI backend."""
        session = await self._get_session()

        async with session.request(
            method,
            url,
            json=payload,
            headers=self._build_headers(),
            ssl=self._verify_ssl,
            timeout=(
                aiohttp.ClientTimeout(total=timeout_seconds)
                if timeout_seconds is not None
                else aiohttp.ClientTimeout(total=self._timeout)
            ),
        ) as response:
            response_body = await response.text()
            if 200 <= response.status < 300:
                return response_body

            safe_body = response_body[:1000]
            raise LogisticsAIRequestError(
                (
                    "LogisticsAI API returned a non-success status: "
                    f"{response.status}, body={safe_body}"
                ),
                status_code=response.status,
                response_body=safe_body,
            )

    def _parse_upload_receipt(
        self,
        response_body: str,
        payload: dict[str, Any],
    ) -> UploadReceipt:
        """Parse an ApiResponse upload receipt without requiring response data."""
        database_id: int | None = None
        trace_id = ""
        platform = str(payload.get("platform") or "")
        message_id = str(payload.get("messageId") or "")

        if response_body.strip():
            try:
                document = json.loads(response_body)
                if isinstance(document, dict):
                    trace_id = str(document.get("traceId") or "")
                    data = document.get("data")
                    if isinstance(data, dict):
                        raw_id = data.get("id")
                        if raw_id is not None:
                            database_id = int(raw_id)
                        platform = str(data.get("platform") or platform)
                        message_id = str(data.get("messageId") or message_id)
            except (json.JSONDecodeError, TypeError, ValueError):
                logger.debug(
                    "LogisticsAI upload succeeded but its response receipt "
                    "could not be parsed.",
                    exc_info=True,
                )

        return UploadReceipt(
            database_id=database_id,
            trace_id=trace_id,
            platform=platform,
            message_id=message_id,
        )

    async def _get_session(self) -> aiohttp.ClientSession:
        """Create and reuse one thread-safe HTTP session."""
        self._ensure_open()
        if self._session is not None and not self._session.closed:
            return self._session

        async with self._session_lock:
            if self._session is not None and not self._session.closed:
                return self._session

            timeout = aiohttp.ClientTimeout(total=self._timeout)
            connector = aiohttp.TCPConnector(
                limit=max(self._max_concurrency * 2, 10),
                enable_cleanup_closed=True,
            )
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                raise_for_status=False,
            )
            return self._session

    def _build_headers(self) -> dict[str, str]:
        """Build request headers without exposing the configured token."""
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "AstrBot-LogisticsAI/0.9.0",
        }
        if not self._api_token:
            return headers

        if self._token_header.lower() == "authorization":
            token = self._api_token
            if " " not in token.strip():
                token = f"Bearer {token}"
            headers[self._token_header] = token
        else:
            headers[self._token_header] = self._api_token

        return headers

    def _calculate_retry_delay(self, attempt: int) -> float:
        base_delay = self._retry_interval * (2 ** (attempt - 1))
        jitter = random.uniform(0, self._retry_interval * 0.25)
        return min(base_delay + jitter, 30.0)

    async def close(self) -> None:
        """Close the shared HTTP session."""
        self._closed = True
        async with self._session_lock:
            session = self._session
            self._session = None
            if session is not None and not session.closed:
                await session.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise LogisticsAIRequestError(
                "ApiClient is closed and cannot send more requests."
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
                    "Failed to read LogisticsAI setting %s.",
                    key,
                    exc_info=True,
                )

        try:
            return self._config[key]
        except (KeyError, TypeError):
            return default

    def _get_string(self, key: str, default: str) -> str:
        value = self._get_raw_value(key, default)
        return str(value if value is not None else default).strip()

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
            logger.warning(
                "Invalid LogisticsAI setting %s=%r; using %d.",
                key,
                value,
                default,
            )
            number = default
        return max(minimum, min(number, maximum))

    def _get_float(
        self,
        key: str,
        default: float,
        *,
        minimum: float,
    ) -> float:
        value = self._get_raw_value(key, default)
        try:
            number = float(value)
        except (TypeError, ValueError):
            logger.warning(
                "Invalid LogisticsAI setting %s=%r; using %.2f.",
                key,
                value,
                default,
            )
            number = default
        return max(minimum, number)

    def __repr__(self) -> str:
        """Return debug settings without credentials."""
        safe_config = {
            "enabled": self._enabled,
            "api_url": self._api_url,
            "timeout": self._timeout,
            "assistant_reply_timeout": self._assistant_reply_timeout,
            "retry_count": self._retry_count,
            "verify_ssl": self._verify_ssl,
            "max_concurrency": self._max_concurrency,
        }
        return f"ApiClient({json.dumps(safe_config, ensure_ascii=True)})"
