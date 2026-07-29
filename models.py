"""LogisticsAI 消息数据模型。"""

from dataclasses import asdict, dataclass, field
from typing import Any

@dataclass(slots=True)
class LogisticsMessage:
    """发送到 LogisticsAI 后端的标准群消息结构。"""

    group_id: str = ""
    group_name: str = ""
    user_id: str = ""
    nickname: str = ""
    message_id: str = ""
    message_type: str = "group_message"
    content: str = ""
    images: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    receive_time: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LogisticsMessage":
        """从 main.py 生成的驼峰命名字典创建消息模型。"""
        return cls(
            group_id=str(data.get("groupId") or ""),
            group_name=str(data.get("groupName") or ""),
            user_id=str(data.get("userId") or ""),
            nickname=str(data.get("nickname") or ""),
            message_id=str(data.get("messageId") or ""),
            message_type=str(data.get("messageType") or "group_message"),
            content=str(data.get("content") or ""),
            images=cls._normalize_string_list(data.get("images")),
            files=cls._normalize_string_list(data.get("files")),
            receive_time=str(data.get("receiveTime") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        """转换成后端要求的驼峰命名 JSON 数据。"""
        data = asdict(self)

        return {
            "groupId": data["group_id"],
            "groupName": data["group_name"],
            "userId": data["user_id"],
            "nickname": data["nickname"],
            "messageId": data["message_id"],
            "messageType": data["message_type"],
            "content": data["content"],
            "images": data["images"],
            "files": data["files"],
            "receiveTime": data["receive_time"],
        }

    @staticmethod
    def _normalize_string_list(value: Any) -> list[str]:
        """规范化图片或文件 URL 列表，并保持原始顺序去重。"""
        if not isinstance(value, (list, tuple, set)):
            return []

        result: list[str] = []
        seen: set[str] = set()

        for item in value:
            text = str(item or "").strip()
            if not text or text in seen:
                continue

            seen.add(text)
            result.append(text)

        return result