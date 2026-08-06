"""Message contract regression tests."""

from __future__ import annotations

import unittest

from astrbot_plugins_logistics_ai.models import LogisticsMessage


class LogisticsMessageTests(unittest.TestCase):
    def test_legacy_raw_fields_remain_unchanged(self) -> None:
        source = {
            "platform": "qq",
            "groupId": "100",
            "groupName": "Operations",
            "userId": "200",
            "nickname": "Alice",
            "messageId": "300",
            "messageType": "group_message",
            "content": "ETD moved to the 16th",
            "images": ["https://example.com/a.jpg"],
            "files": ["https://example.com/a.pdf"],
            "receiveTime": "2026-08-06T01:00:00+00:00",
        }

        payload = LogisticsMessage.from_dict(source).to_dict()

        for field_name, expected in source.items():
            self.assertEqual(payload[field_name], expected)

    def test_reply_and_sender_role_are_serialized(self) -> None:
        payload = LogisticsMessage.from_dict(
            {
                "platform": "qq",
                "senderRole": "admin",
                "reply": {
                    "messageId": "quoted-1",
                    "userId": "quoted-user",
                    "nickname": "Bob",
                    "content": "Original rate",
                    "images": [" image-a ", "image-a"],
                    "files": [],
                    "receiveTime": "2026-08-06T00:00:00+00:00",
                },
            }
        ).to_dict()

        self.assertEqual(payload["senderRole"], "admin")
        self.assertEqual(payload["reply"]["messageId"], "quoted-1")
        self.assertEqual(payload["reply"]["images"], ["image-a"])

    def test_empty_reply_is_omitted(self) -> None:
        payload = LogisticsMessage.from_dict(
            {"platform": "qq", "reply": {}}
        ).to_dict()

        self.assertNotIn("reply", payload)


if __name__ == "__main__":
    unittest.main()
