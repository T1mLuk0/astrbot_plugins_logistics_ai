"""LogisticsAI 后端 API 客户端。"""

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
from .models import LogisticsMessage

class ApiClient:
    """异步 LogisticsAI API 客户端。"""

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
        """读取配置，但延迟创建 aiohttp ClientSession。"""
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
        self._timeout = self._get_float("timeout", 15.0, minimum=1.0)
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
                "启用 LogisticsAI 上传后，api_url 不能为空"
            )

    async def upload(self, data: dict[str, Any]) -> None:
        """上传一条群消息，失败时按配置执行指数退避重试。"""
        if not self._enabled:
            return

        if self._closed:
            raise LogisticsAIRequestError("ApiClient 已关闭，无法继续上传")

        message = LogisticsMessage.from_dict(data)
        payload = message.to_dict()

        async with self._semaphore:
            await self._request_with_retry(payload)

    async def _request_with_retry(
        self,
        payload: dict[str, Any],
    ) -> None:
        """执行带重试策略的 POST 请求。"""
        total_attempts = self._retry_count + 1
        last_error: Exception | None = None

        for attempt in range(1, total_attempts + 1):
            try:
                await self._post(payload)
                return
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

                delay = self._calculate_retry_delay(attempt)
                logger.warning(
                    "LogisticsAI 上传失败，将在 %.2f 秒后重试 "
                    "(%d/%d): %s",
                    delay,
                    attempt,
                    total_attempts,
                    exc,
                )
                await asyncio.sleep(delay)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc

                if attempt >= total_attempts:
                    break

                delay = self._calculate_retry_delay(attempt)
                logger.warning(
                    "LogisticsAI 网络请求失败，将在 %.2f 秒后重试 "
                    "(%d/%d): %s",
                    delay,
                    attempt,
                    total_attempts,
                    exc,
                )
                await asyncio.sleep(delay)

        raise LogisticsAIRequestError(
            f"LogisticsAI 上传失败: {last_error}"
        ) from last_error

    async def _post(self, payload: dict[str, Any]) -> None:
        """向 LogisticsAI 后端发送 JSON 请求。"""
        session = await self._get_session()

        try:
            async with session.post(
                self._api_url,
                json=payload,
                headers=self._build_headers(),
                ssl=self._verify_ssl,
            ) as response:
                response_body = await response.text()

                if 200 <= response.status < 300:
                    return

                # 限制错误正文长度，避免后端异常页面污染日志。
                safe_body = response_body[:1000]
                raise LogisticsAIRequestError(
                    (
                        "LogisticsAI API 返回非成功状态码: "
                        f"{response.status}, body={safe_body}"
                    ),
                    status_code=response.status,
                    response_body=safe_body,
                )
        except asyncio.CancelledError:
            raise
        except LogisticsAIRequestError:
            raise
        except asyncio.TimeoutError:
            raise
        except aiohttp.ClientError:
            raise

    async def _get_session(self) -> aiohttp.ClientSession:
        """线程安全地延迟创建并复用 HTTP 会话。"""
        if self._closed:
            raise LogisticsAIRequestError("ApiClient 已关闭")

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
        """构建上传请求头。"""
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "AstrBot-LogisticsAI/0.0.1",
        }

        if not self._api_token:
            return headers

        if self._token_header.lower() == "authorization":
            token = self._api_token

            # 如果用户没有指定认证方案，默认使用 Bearer。
            if " " not in token.strip():
                token = f"Bearer {token}"

            headers[self._token_header] = token
        else:
            headers[self._token_header] = self._api_token

        return headers

    def _calculate_retry_delay(self, attempt: int) -> float:
        """计算带少量随机抖动的指数退避时间。"""
        base_delay = self._retry_interval * (2 ** (attempt - 1))
        jitter = random.uniform(0, self._retry_interval * 0.25)
        return min(base_delay + jitter, 30.0)

    async def close(self) -> None:
        """关闭 HTTP 会话并释放连接池。"""
        self._closed = True

        async with self._session_lock:
            session = self._session
            self._session = None

            if session is not None and not session.closed:
                await session.close()

    def _get_raw_value(self, key: str, default: Any) -> Any:
        """兼容普通字典和 AstrBotConfig 的配置读取。"""
        getter = getattr(self._config, "get", None)

        if callable(getter):
            try:
                return getter(key, default)
            except TypeError:
                value = getter(key)
                return default if value is None else value
            except Exception:
                logger.debug(
                    "LogisticsAI 读取配置失败: %s",
                    key,
                    exc_info=True,
                )

        try:
            return self._config[key]
        except (KeyError, TypeError):
            return default

    def _get_string(self, key: str, default: str) -> str:
        """读取字符串配置。"""
        value = self._get_raw_value(key, default)
        return str(value if value is not None else default).strip()

    def _get_bool(self, key: str, default: bool) -> bool:
        """读取布尔配置。"""
        value = self._get_raw_value(key, default)

        if isinstance(value, bool):
            return value

        if isinstance(value, str):
            normalized = value.strip().lower()

            if normalized in {"1", "true", "yes", "on", "是"}:
                return True

            if normalized in {"0", "false", "no", "off", "否"}:
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
        """读取并限制整数配置范围。"""
        value = self._get_raw_value(key, default)

        try:
            number = int(value)
        except (TypeError, ValueError):
            logger.warning(
                "LogisticsAI 配置 %s=%r 无效，使用默认值 %d",
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
        """读取并限制浮点数配置范围。"""
        value = self._get_raw_value(key, default)

        try:
            number = float(value)
        except (TypeError, ValueError):
            logger.warning(
                "LogisticsAI 配置 %s=%r 无效，使用默认值 %.2f",
                key,
                value,
                default,
            )
            number = default

        return max(minimum, number)

    def __repr__(self) -> str:
        """返回不包含认证令牌的客户端调试信息。"""
        safe_config = {
            "enabled": self._enabled,
            "api_url": self._api_url,
            "timeout": self._timeout,
            "retry_count": self._retry_count,
            "verify_ssl": self._verify_ssl,
            "max_concurrency": self._max_concurrency,
        }
        return f"ApiClient({json.dumps(safe_config, ensure_ascii=False)})"