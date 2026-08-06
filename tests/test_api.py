"""Backend HTTP contract regression tests."""

from __future__ import annotations

import json
import unittest
from typing import Any

try:
    from ._stubs import install_test_stubs
except ImportError:
    from _stubs import install_test_stubs

install_test_stubs()

from astrbot_plugins_logistics_ai.api import ApiClient


class RecordingApiClient(ApiClient):
    def __init__(self) -> None:
        super().__init__(
            {
                "enabled": True,
                "api_url": "https://eshinetong.com/api/messages",
                "retry_count": 0,
            }
        )
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    async def _request_with_retry(
        self,
        *,
        method: str,
        url: str,
        payload: dict[str, Any],
    ) -> str:
        self.requests.append((method, url, payload))
        if method == "POST":
            return json.dumps(
                {
                    "data": {
                        "id": 42,
                        "platform": "qq",
                        "messageId": "message-1",
                    },
                    "traceId": "trace-1",
                }
            )
        return ""


class ApiContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_upload_stays_on_post_and_reads_current_backend_receipt(
        self,
    ) -> None:
        client = RecordingApiClient()

        receipt = await client.upload(
            {
                "platform": "qq",
                "groupId": "group-1",
                "userId": "user-1",
                "messageId": "message-1",
                "messageType": "group_message",
                "content": "Schedule update",
                "images": [],
                "files": [],
                "receiveTime": "2026-08-06T01:00:00+00:00",
            }
        )

        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.database_id, 42)
        self.assertEqual(receipt.trace_id, "trace-1")
        self.assertEqual(
            client.requests[0][0:2],
            ("POST", "https://eshinetong.com/api/messages"),
        )

    async def test_analysis_uses_independent_put_endpoint(self) -> None:
        client = RecordingApiClient()
        receipt = await client.upload(
            {
                "platform": "qq",
                "groupId": "group-1",
                "userId": "user-1",
                "messageId": "message-1",
                "messageType": "group_message",
                "receiveTime": "2026-08-06T01:00:00+00:00",
            }
        )

        await client.upload_analysis(receipt, {"status": "succeeded"})

        self.assertEqual(
            client.requests[1][0:2],
            (
                "PUT",
                "https://eshinetong.com/api/messages/42/analysis",
            ),
        )


if __name__ == "__main__":
    unittest.main()
