"""Collect LogisticsAI group messages and optional MiMo visual analysis."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Image, Plain, Reply
from astrbot.api.star import Context, Star, register

from .analyzer import MultimodalAnalyzer
from .api import ApiClient


@register(
    "astrbot_plugin_logistics_ai",
    "LogisticsAI",
    "Collect group logistics intelligence and upload it to LogisticsAI",
    "0.1.0",
)
class LogisticsAIPlugin(Star):
    """Capture group messages without blocking AstrBot event processing."""

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | None = None,
    ) -> None:
        """Initialize clients while remaining compatible with optional config."""
        super().__init__(context)
        self.config: AstrBotConfig | dict[str, Any] = (
            config if config is not None else {}
        )
        self.api_client = ApiClient(self.config)
        self.analyzer = MultimodalAnalyzer(context, self.config)
        self._tasks: set[asyncio.Task[None]] = set()
        self._terminating = False

        logger.info(
            "LogisticsAI plugin initialized. multimodal_analysis_enabled=%s",
            self.analyzer.enabled,
        )

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent) -> None:
        """Capture one group message and schedule the backend workflow."""
        if self._terminating:
            return

        try:
            if self._is_self_message(event):
                return

            data = self._build_message_data(event)
            if not data["messageId"]:
                logger.warning(
                    "LogisticsAI ignored a group message without a message ID. "
                    "group_id=%s",
                    data["groupId"],
                )
                return

            unified_msg_origin = str(
                getattr(event, "unified_msg_origin", "") or ""
            )
            self._schedule_processing(data, unified_msg_origin)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("LogisticsAI failed to capture a group message.")

    def _schedule_processing(
        self,
        data: dict[str, Any],
        unified_msg_origin: str,
    ) -> None:
        """Schedule raw upload followed by optional silent visual analysis."""
        if self._terminating:
            return

        message_id = data.get("messageId") or "unknown"
        task = asyncio.create_task(
            self._process_message(data, unified_msg_origin),
            name=f"logistics-ai-process-{message_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        """Release a completed task and observe any unexpected exception."""
        self._tasks.discard(task)
        if task.cancelled():
            return

        try:
            exception = task.exception()
        except asyncio.CancelledError:
            return

        if exception is not None:
            logger.error(
                "LogisticsAI background task ended unexpectedly: %s",
                exception,
            )

    async def _process_message(
        self,
        data: dict[str, Any],
        unified_msg_origin: str,
    ) -> None:
        """Persist raw data first, then upload an independent visual result."""
        try:
            receipt = await self.api_client.upload(data)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "LogisticsAI raw message upload failed. "
                "platform=%s group_id=%s message_id=%s",
                data.get("platform", "qq"),
                data.get("groupId", ""),
                data.get("messageId", ""),
            )
            return

        if receipt is None:
            return

        logger.info(
            "LogisticsAI raw message uploaded. platform=%s group_id=%s "
            "message_id=%s database_id=%s reply_to=%s",
            data.get("platform", "qq"),
            data.get("groupId", ""),
            data.get("messageId", ""),
            receipt.database_id,
            self._reply_message_id(data),
        )

        if not self.analyzer.should_analyze(data):
            return

        try:
            analysis = await self.analyzer.analyze(
                data,
                unified_msg_origin,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "LogisticsAI multimodal analysis failed. message_id=%s",
                data.get("messageId", ""),
            )
            analysis = self.analyzer.failure_payload(
                str(data.get("messageId") or ""),
                exc,
            )

        try:
            await self.api_client.upload_analysis(receipt, analysis)
            logger.info(
                "LogisticsAI multimodal analysis uploaded. "
                "message_id=%s database_id=%s status=%s",
                data.get("messageId", ""),
                receipt.database_id,
                analysis.get("status", "unknown"),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "LogisticsAI analysis upload failed, but the raw message "
                "remains stored. message_id=%s database_id=%s",
                data.get("messageId", ""),
                receipt.database_id,
            )

    def _build_message_data(
        self,
        event: AstrMessageEvent,
    ) -> dict[str, Any]:
        """Convert an AstrBot group event to the LogisticsAI payload."""
        message_obj = event.message_obj
        sender = getattr(message_obj, "sender", None)
        raw_message = getattr(message_obj, "raw_message", None)
        components = tuple(event.get_messages() or ())

        text_parts, image_urls, file_urls = self._extract_chain_content(
            components,
            include_replies=False,
        )
        reply = self._extract_reply_context(components)

        group_id = self._first_non_empty_string(
            self._call_event_method(event, "get_group_id"),
            getattr(message_obj, "group_id", None),
            self._mapping_value(raw_message, "group_id"),
        )
        user_id = self._first_non_empty_string(
            self._call_event_method(event, "get_sender_id"),
            getattr(sender, "user_id", None),
            self._mapping_value(raw_message, "user_id"),
            self._nested_mapping_value(raw_message, "sender", "user_id"),
        )
        nickname = self._first_non_empty_string(
            getattr(sender, "card", None),
            self._call_event_method(event, "get_sender_name"),
            getattr(sender, "nickname", None),
            self._nested_mapping_value(raw_message, "sender", "card"),
            self._nested_mapping_value(raw_message, "sender", "nickname"),
        )

        data: dict[str, Any] = {
            "platform": self._extract_platform(event, message_obj),
            "groupId": group_id,
            "groupName": self._extract_group_name(message_obj, raw_message),
            "userId": user_id,
            "nickname": nickname,
            "senderRole": self._extract_sender_role(event),
            "messageId": self._first_non_empty_string(
                getattr(message_obj, "message_id", None),
                self._mapping_value(raw_message, "message_id"),
            ),
            "messageType": self._extract_message_type(message_obj),
            "content": "".join(text_parts).strip(),
            "images": self._deduplicate(image_urls),
            "files": self._deduplicate(file_urls),
            "receiveTime": self._extract_receive_time(message_obj, raw_message),
        }
        if reply is not None:
            data["reply"] = reply

        return data

    @classmethod
    def _extract_reply_context(
        cls,
        components: Sequence[Any],
    ) -> dict[str, Any] | None:
        """Capture the first explicit Reply component and its quoted chain."""
        for component in components:
            if not isinstance(component, Reply):
                continue

            reply_chain = tuple(getattr(component, "chain", None) or ())
            text_parts, image_urls, file_urls = cls._extract_chain_content(
                reply_chain,
                include_replies=False,
            )
            content = cls._first_non_empty_string(
                getattr(component, "message_str", None),
                "".join(text_parts),
                getattr(component, "text", None),
            )
            reply_id = cls._normalize_identifier(
                getattr(component, "id", None)
            )

            reply = {
                "messageId": reply_id,
                "userId": cls._normalize_identifier(
                    getattr(component, "sender_id", None)
                ),
                "nickname": cls._first_non_empty_string(
                    getattr(component, "sender_nickname", None)
                ),
                "content": content,
                "images": cls._deduplicate(image_urls),
                "files": cls._deduplicate(file_urls),
                "receiveTime": cls._format_utc_time(
                    getattr(component, "time", None)
                ),
            }

            if any(
                (
                    reply["messageId"],
                    reply["content"],
                    reply["images"],
                    reply["files"],
                )
            ):
                return reply

        return None

    @classmethod
    def _extract_chain_content(
        cls,
        components: Sequence[Any],
        *,
        include_replies: bool,
    ) -> tuple[list[str], list[str], list[str]]:
        """Extract text, images, and files from one message component chain."""
        text_parts: list[str] = []
        image_urls: list[str] = []
        file_urls: list[str] = []

        for component in components:
            if isinstance(component, Plain):
                text = cls._to_string(getattr(component, "text", None))
                if text:
                    text_parts.append(text)
                continue

            if isinstance(component, Image):
                image_url = cls._extract_resource_url(component)
                if image_url:
                    image_urls.append(image_url)
                continue

            if isinstance(component, File):
                file_url = cls._extract_resource_url(component)
                if file_url:
                    file_urls.append(file_url)
                continue

            if include_replies and isinstance(component, Reply):
                reply_chain = tuple(getattr(component, "chain", None) or ())
                nested_text, nested_images, nested_files = (
                    cls._extract_chain_content(
                        reply_chain,
                        include_replies=False,
                    )
                )
                text_parts.extend(nested_text)
                image_urls.extend(nested_images)
                file_urls.extend(nested_files)

        return text_parts, image_urls, file_urls

    @classmethod
    def _extract_platform(
        cls,
        event: AstrMessageEvent,
        message_obj: Any,
    ) -> str:
        """Normalize common QQ adapter names to the backend platform code."""
        raw_message = getattr(message_obj, "raw_message", None)
        platform = cls._first_non_empty_string(
            cls._call_event_method(event, "get_platform_name"),
            getattr(message_obj, "platform", None),
            cls._mapping_value(raw_message, "platform"),
        ).lower()

        if not platform or any(
            keyword in platform
            for keyword in ("qq", "onebot", "napcat", "llonebot")
        ):
            return "qq"

        return platform

    @classmethod
    def _is_self_message(cls, event: AstrMessageEvent) -> bool:
        """Return whether the event was sent by the active bot account."""
        message_obj = event.message_obj
        sender = getattr(message_obj, "sender", None)
        raw_message = getattr(message_obj, "raw_message", None)
        sender_id = cls._first_non_empty_string(
            cls._call_event_method(event, "get_sender_id"),
            getattr(sender, "user_id", None),
            cls._mapping_value(raw_message, "user_id"),
            cls._nested_mapping_value(raw_message, "sender", "user_id"),
        )
        self_id = cls._first_non_empty_string(
            cls._call_event_method(event, "get_self_id"),
            getattr(message_obj, "self_id", None),
            cls._mapping_value(raw_message, "self_id"),
        )
        return bool(sender_id and self_id and sender_id == self_id)

    @classmethod
    def _extract_group_name(
        cls,
        message_obj: Any,
        raw_message: Any,
    ) -> str:
        return cls._first_non_empty_string(
            getattr(message_obj, "group_name", None),
            cls._mapping_value(raw_message, "group_name"),
            cls._nested_mapping_value(raw_message, "group", "group_name"),
            cls._nested_mapping_value(raw_message, "group", "name"),
        )

    @classmethod
    def _extract_message_type(cls, message_obj: Any) -> str:
        raw_message = getattr(message_obj, "raw_message", None)
        message_type = cls._first_non_empty_string(
            getattr(message_obj, "message_type", None),
            cls._mapping_value(raw_message, "message_type"),
            cls._mapping_value(raw_message, "post_type"),
        ).lower()
        if message_type in {
            "group",
            "group_message",
            "message",
            "message_event",
        }:
            return "group_message"
        return message_type or "group_message"

    @classmethod
    def _extract_sender_role(cls, event: AstrMessageEvent) -> str:
        role = cls._first_non_empty_string(getattr(event, "role", None))
        if role:
            return role.lower()
        return "admin" if bool(cls._call_event_method(event, "is_admin")) else "member"

    @classmethod
    def _extract_receive_time(
        cls,
        message_obj: Any,
        raw_message: Any,
    ) -> str:
        raw_time = cls._first_non_empty_value(
            getattr(message_obj, "timestamp", None),
            getattr(message_obj, "time", None),
            cls._mapping_value(raw_message, "timestamp"),
            cls._mapping_value(raw_message, "time"),
        )
        return cls._format_utc_time(raw_time) or datetime.now(
            timezone.utc
        ).isoformat()

    @classmethod
    def _format_utc_time(cls, raw_time: Any) -> str:
        """Normalize datetime, Unix timestamp, or ISO input to UTC ISO 8601."""
        if isinstance(raw_time, datetime):
            value = raw_time
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc).isoformat()

        if raw_time is not None:
            try:
                timestamp = float(raw_time)
                if timestamp <= 0:
                    return ""
                if timestamp > 10_000_000_000:
                    timestamp /= 1000
                return datetime.fromtimestamp(
                    timestamp,
                    tz=timezone.utc,
                ).isoformat()
            except (TypeError, ValueError, OverflowError, OSError):
                raw_text = cls._to_string(raw_time)
                if raw_text:
                    try:
                        parsed = datetime.fromisoformat(
                            raw_text.replace("Z", "+00:00")
                        )
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=timezone.utc)
                        return parsed.astimezone(timezone.utc).isoformat()
                    except ValueError:
                        return ""

        return ""

    @classmethod
    def _extract_resource_url(cls, component: Any) -> str:
        """Read a URL or local file reference from a media component."""
        for attribute in ("url", "file", "path", "src", "download_url"):
            value = getattr(component, attribute, None)
            if isinstance(value, Mapping):
                value = cls._first_non_empty_string(
                    value.get("url"),
                    value.get("file"),
                    value.get("path"),
                )
            text = cls._to_string(value)
            if text:
                return text
        return ""

    @staticmethod
    def _call_event_method(
        event: AstrMessageEvent,
        method_name: str,
    ) -> Any:
        method = getattr(event, method_name, None)
        if not callable(method):
            return None
        try:
            return method()
        except Exception:
            return None

    @staticmethod
    def _mapping_value(data: Any, key: str) -> Any:
        return data.get(key) if isinstance(data, Mapping) else None

    @classmethod
    def _nested_mapping_value(
        cls,
        data: Any,
        parent_key: str,
        child_key: str,
    ) -> Any:
        return cls._mapping_value(
            cls._mapping_value(data, parent_key),
            child_key,
        )

    @classmethod
    def _first_non_empty_string(cls, *values: Any) -> str:
        for value in values:
            text = cls._to_string(value)
            if text:
                return text
        return ""

    @staticmethod
    def _first_non_empty_value(*values: Any) -> Any:
        for value in values:
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            return value
        return None

    @staticmethod
    def _to_string(value: Any) -> str:
        return "" if value is None else str(value).strip()

    @classmethod
    def _normalize_identifier(cls, value: Any) -> str:
        text = cls._to_string(value)
        return "" if text in {"", "0", "None"} else text

    @staticmethod
    def _deduplicate(values: Sequence[str]) -> list[str]:
        return list(dict.fromkeys(value for value in values if value))

    @staticmethod
    def _reply_message_id(data: dict[str, Any]) -> str:
        reply = data.get("reply")
        return str(reply.get("messageId") or "") if isinstance(reply, dict) else ""

    async def terminate(self) -> None:
        """Cancel background work and release the shared HTTP client."""
        self._terminating = True
        pending_tasks = tuple(self._tasks)
        for task in pending_tasks:
            task.cancel()

        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        self._tasks.clear()

        try:
            await self.api_client.close()
        except Exception:
            logger.exception("LogisticsAI failed to close the API client.")

        logger.info("LogisticsAI plugin stopped.")

