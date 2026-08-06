"""Data contracts used by the LogisticsAI AstrBot plugin."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class ReplyContext:
    """A normalized snapshot of the message quoted by a reply component."""

    message_id: str = ""
    user_id: str = ""
    nickname: str = ""
    content: str = ""
    images: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    receive_time: str = ""

    @classmethod
    def from_dict(cls, data: Any) -> "ReplyContext | None":
        """Create a reply context from a camelCase dictionary."""
        if not isinstance(data, dict):
            return None

        normalized = cls(
            message_id=str(data.get("messageId") or "").strip(),
            user_id=str(data.get("userId") or "").strip(),
            nickname=str(data.get("nickname") or "").strip(),
            content=str(data.get("content") or "").strip(),
            images=LogisticsMessage.normalize_string_list(data.get("images")),
            files=LogisticsMessage.normalize_string_list(data.get("files")),
            receive_time=str(data.get("receiveTime") or "").strip(),
        )

        if not any(
            (
                normalized.message_id,
                normalized.content,
                normalized.images,
                normalized.files,
            )
        ):
            return None

        return normalized

    def to_dict(self) -> dict[str, Any]:
        """Serialize the reply context using the backend camelCase contract."""
        return {
            "messageId": self.message_id,
            "userId": self.user_id,
            "nickname": self.nickname,
            "content": self.content,
            "images": list(self.images),
            "files": list(self.files),
            "receiveTime": self.receive_time,
        }


@dataclass(slots=True)
class LogisticsMessage:
    """A normalized group message sent to the LogisticsAI backend."""

    platform: str = "qq"
    group_id: str = ""
    group_name: str = ""
    user_id: str = ""
    nickname: str = ""
    sender_role: str = "member"
    message_id: str = ""
    message_type: str = "group_message"
    content: str = ""
    images: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    reply: ReplyContext | None = None
    receive_time: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LogisticsMessage":
        """Create a message from the camelCase dictionary built by main.py."""
        return cls(
            platform=str(data.get("platform") or "qq").strip(),
            group_id=str(data.get("groupId") or "").strip(),
            group_name=str(data.get("groupName") or "").strip(),
            user_id=str(data.get("userId") or "").strip(),
            nickname=str(data.get("nickname") or "").strip(),
            sender_role=str(data.get("senderRole") or "member").strip(),
            message_id=str(data.get("messageId") or "").strip(),
            message_type=str(
                data.get("messageType") or "group_message"
            ).strip(),
            content=str(data.get("content") or ""),
            images=cls.normalize_string_list(data.get("images")),
            files=cls.normalize_string_list(data.get("files")),
            reply=ReplyContext.from_dict(data.get("reply")),
            receive_time=str(data.get("receiveTime") or "").strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the message using the backend camelCase contract."""
        data = asdict(self)
        payload: dict[str, Any] = {
            "platform": data["platform"],
            "groupId": data["group_id"],
            "groupName": data["group_name"],
            "userId": data["user_id"],
            "nickname": data["nickname"],
            "senderRole": data["sender_role"],
            "messageId": data["message_id"],
            "messageType": data["message_type"],
            "content": data["content"],
            "images": data["images"],
            "files": data["files"],
            "receiveTime": data["receive_time"],
        }

        if self.reply is not None:
            payload["reply"] = self.reply.to_dict()

        return payload

    @staticmethod
    def normalize_string_list(value: Any) -> list[str]:
        """Normalize and de-duplicate a sequence of URLs or file references."""
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


@dataclass(frozen=True, slots=True)
class UploadReceipt:
    """The identifiers returned after a raw message upload succeeds."""

    database_id: int | None
    trace_id: str = ""
    platform: str = ""
    message_id: str = ""

