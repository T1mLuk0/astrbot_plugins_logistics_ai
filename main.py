"""
LogisticsAI 群消息采集插件。

逻辑结构：
    AstrBot 群消息事件
        -> 排除私聊和机器人自身消息
        -> 解析群、发送者、文本、图片和文件信息
        -> 构建 LogisticsAI 标准消息字典
        -> 创建异步上传任务
        -> 调用 ApiClient.upload(data)

设计原因：
    1. 本文件只负责监听、解析和调度，不直接执行 HTTP 请求。
    2. ApiClient 负责网络通信，便于独立维护认证、重试和超时策略。
    3. 后台任务避免上传过程阻塞 AstrBot 的消息事件循环。
    4. AstrBot 4.26.7 可能只向插件构造函数传入 Context，因此 config
       必须是可选参数，不能声明成必填参数。

扩展方向：
    1. AI、图片分析和 OCR 应在 LogisticsAI 后端处理。
    2. 数据库应由 ASP.NET Core API 统一访问。
    3. WebHook 和 WebSocket 可以在 ApiClient 或独立传输层扩展。
    4. 消息数据结构保持稳定时，新增处理能力无需修改监听流程。
"""

import asyncio
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Image, Plain
from astrbot.api.star import Context, Star, register

from .api import ApiClient

@register(
    "astrbot_plugin_logistics_ai",
    "LogisticsAI",
    "监听 QQ 群消息并上传至 LogisticsAI 数据平台",
    "0.0.1",
)
class LogisticsAIPlugin(Star):
    """监听并采集 QQ 群消息。"""

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | None = None,
    ) -> None:
        """
        初始化插件。

        AstrBot 4.26.7 在插件没有配置架构时可能只传入 Context，
        因此 config 必须允许为空，避免插件安装阶段实例化失败。
        """
        super().__init__(context)

        self.config: AstrBotConfig | dict[str, Any] = (
            config if config is not None else {}
        )
        self.api_client = ApiClient(self.config)
        self._upload_tasks: set[asyncio.Task[None]] = set()
        self._terminating = False

        logger.info("LogisticsAI 插件初始化完成")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent) -> None:
        """监听群消息，构建标准数据并调度异步上传。"""
        if self._terminating:
            return

        try:
            if self._is_self_message(event):
                return

            data = self._build_message_data(event)
            self._schedule_upload(data)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("LogisticsAI 解析群消息失败")

    def _schedule_upload(self, data: dict[str, Any]) -> None:
        """创建后台上传任务，避免阻塞 AstrBot 消息事件处理。"""
        message_id = data.get("messageId") or "unknown"
        task = asyncio.create_task(
            self._upload(data),
            name=f"logistics-ai-upload-{message_id}",
        )

        self._upload_tasks.add(task)
        task.add_done_callback(self._on_upload_task_done)

    def _on_upload_task_done(self, task: asyncio.Task[None]) -> None:
        """清理已经结束的上传任务。"""
        self._upload_tasks.discard(task)

        if task.cancelled():
            return

        try:
            task.exception()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("LogisticsAI 获取上传任务结果失败")

    async def _upload(self, data: dict[str, Any]) -> None:
        """通过 ApiClient 上传消息，不在 main.py 中执行 HTTP 操作。"""
        try:
            await self.api_client.upload(data)
            logger.info(
                "LogisticsAI 消息上传成功: group_id=%s, message_id=%s",
                data["groupId"],
                data["messageId"],
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "LogisticsAI 消息上传失败: group_id=%s, message_id=%s",
                data["groupId"],
                data["messageId"],
            )

    def _build_message_data(
        self,
        event: AstrMessageEvent,
    ) -> dict[str, Any]:
        """将 AstrBot 群消息转换为 LogisticsAI 标准消息结构。"""
        message_obj = event.message_obj
        sender = getattr(message_obj, "sender", None)
        raw_message = getattr(message_obj, "raw_message", None)

        text_parts: list[str] = []
        image_urls: list[str] = []
        file_urls: list[str] = []

        for component in event.get_messages():
            if isinstance(component, Plain):
                text = self._to_string(getattr(component, "text", None))
                if text:
                    text_parts.append(text)
                continue

            if isinstance(component, Image):
                image_url = self._extract_resource_url(component)
                if image_url:
                    image_urls.append(image_url)
                continue

            if isinstance(component, File):
                file_url = self._extract_resource_url(component)
                if file_url:
                    file_urls.append(file_url)

        group_id = self._first_non_empty_string(
            self._call_event_method(event, "get_group_id"),
            getattr(message_obj, "group_id", None),
            self._mapping_value(raw_message, "group_id"),
        )

        user_id = self._first_non_empty_string(
            self._call_event_method(event, "get_sender_id"),
            getattr(sender, "user_id", None),
            self._mapping_value(raw_message, "user_id"),
        )

        nickname = self._first_non_empty_string(
            getattr(sender, "card", None),
            self._call_event_method(event, "get_sender_name"),
            getattr(sender, "nickname", None),
            self._nested_mapping_value(raw_message, "sender", "card"),
            self._nested_mapping_value(raw_message, "sender", "nickname"),
        )

        return {
            "groupId": group_id,
            "groupName": self._extract_group_name(message_obj, raw_message),
            "userId": user_id,
            "nickname": nickname,
            "messageId": self._first_non_empty_string(
                getattr(message_obj, "message_id", None),
                self._mapping_value(raw_message, "message_id"),
            ),
            "messageType": self._extract_message_type(message_obj),
            "content": "".join(text_parts).strip(),
            "images": self._deduplicate(image_urls),
            "files": self._deduplicate(file_urls),
            "receiveTime": self._extract_receive_time(
                message_obj,
                raw_message,
            ),
        }

    @classmethod
    def _is_self_message(cls, event: AstrMessageEvent) -> bool:
        """判断消息是否由当前机器人账号发送。"""
        message_obj = event.message_obj
        sender = getattr(message_obj, "sender", None)
        raw_message = getattr(message_obj, "raw_message", None)

        sender_id = cls._first_non_empty_string(
            cls._call_event_method(event, "get_sender_id"),
            getattr(sender, "user_id", None),
            cls._mapping_value(raw_message, "user_id"),
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
        """从 AstrBot 消息对象或 OneBot 原始事件中提取群名称。"""
        return cls._first_non_empty_string(
            getattr(message_obj, "group_name", None),
            cls._mapping_value(raw_message, "group_name"),
            cls._nested_mapping_value(raw_message, "group", "group_name"),
            cls._nested_mapping_value(raw_message, "group", "name"),
        )

    @classmethod
    def _extract_message_type(cls, message_obj: Any) -> str:
        """把 AstrBot 消息类型转换为字符串。"""
        message_type = getattr(message_obj, "type", None)
        message_type_value = getattr(message_type, "value", message_type)

        return cls._first_non_empty_string(
            message_type_value,
            "group_message",
        )

    @classmethod
    def _extract_resource_url(cls, component: Any) -> str:
        """提取图片或文件组件中的资源 URL。"""
        return cls._first_non_empty_string(
            getattr(component, "url", None),
            getattr(component, "file", None),
        )

    @classmethod
    def _extract_receive_time(
        cls,
        message_obj: Any,
        raw_message: Any,
    ) -> str:
        """将消息接收时间转换为 UTC ISO 8601 字符串。"""
        raw_time = cls._first_non_empty_value(
            getattr(message_obj, "timestamp", None),
            getattr(message_obj, "time", None),
            cls._mapping_value(raw_message, "time"),
        )

        if isinstance(raw_time, datetime):
            message_time = raw_time
            if message_time.tzinfo is None:
                message_time = message_time.replace(tzinfo=timezone.utc)
            return message_time.astimezone(timezone.utc).isoformat()

        if raw_time is not None:
            try:
                return datetime.fromtimestamp(
                    float(raw_time),
                    tz=timezone.utc,
                ).isoformat()
            except (TypeError, ValueError, OSError):
                logger.warning(
                    "LogisticsAI 无法解析消息时间，将使用当前 UTC 时间: %r",
                    raw_time,
                )

        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _call_event_method(
        event: AstrMessageEvent,
        method_name: str,
    ) -> Any:
        """安全调用 AstrBot 事件对象提供的无参数读取方法。"""
        method = getattr(event, method_name, None)
        if not callable(method):
            return None

        try:
            return method()
        except Exception:
            logger.debug(
                "LogisticsAI 调用事件方法失败: %s",
                method_name,
                exc_info=True,
            )
            return None

    @staticmethod
    def _mapping_value(value: Any, key: str) -> Any:
        """从映射对象中读取字段。"""
        if isinstance(value, Mapping):
            return value.get(key)
        return None

    @classmethod
    def _nested_mapping_value(
        cls,
        value: Any,
        parent_key: str,
        child_key: str,
    ) -> Any:
        """从嵌套映射对象中读取字段。"""
        parent = cls._mapping_value(value, parent_key)
        return cls._mapping_value(parent, child_key)

    @classmethod
    def _first_non_empty_string(cls, *values: Any) -> str:
        """返回候选字段中第一个非空字符串。"""
        for value in values:
            text = cls._to_string(value)
            if text:
                return text
        return ""

    @staticmethod
    def _first_non_empty_value(*values: Any) -> Any:
        """返回候选字段中第一个非空原始值。"""
        for value in values:
            if value is not None and value != "":
                return value
        return None

    @staticmethod
    def _to_string(value: Any) -> str:
        """安全地把消息字段转换为字符串。"""
        if value is None:
            return ""
        return str(value).strip()

    @staticmethod
    def _deduplicate(values: list[str]) -> list[str]:
        """保持原始顺序并移除重复 URL。"""
        return list(dict.fromkeys(value for value in values if value))

    async def terminate(self) -> None:
        """终止插件并释放后台任务及 ApiClient 资源。"""
        self._terminating = True

        pending_tasks = tuple(self._upload_tasks)
        for task in pending_tasks:
            task.cancel()

        if pending_tasks:
            await asyncio.gather(
                *pending_tasks,
                return_exceptions=True,
            )

        self._upload_tasks.clear()

        close_method = getattr(self.api_client, "close", None)
        if callable(close_method):
            try:
                close_result = close_method()
                if asyncio.iscoroutine(close_result):
                    await close_result
            except Exception:
                logger.exception("LogisticsAI 关闭 ApiClient 失败")

        logger.info("LogisticsAI 插件已停止")