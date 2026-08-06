"""Reply capture and workflow-order tests."""

from __future__ import annotations

import unittest

try:
    from ._stubs import install_test_stubs
except ImportError:
    from _stubs import install_test_stubs

install_test_stubs()

from astrbot.api.message_components import File, Image, Plain, Reply

from astrbot_plugins_logistics_ai.main import LogisticsAIPlugin
from astrbot_plugins_logistics_ai.models import UploadReceipt


class ReplyCaptureTests(unittest.TestCase):
    def test_extracts_explicit_reply_snapshot(self) -> None:
        components = [
            Reply(
                id="quoted-1",
                sender_id="user-1",
                sender_nickname="Operator",
                time=1_786_000_000,
                message_str="Quoted sailing update",
                chain=[
                    Plain("Fallback quoted text"),
                    Image("https://example.com/quoted.jpg"),
                    File("https://example.com/quoted.pdf"),
                ],
            ),
            Plain("Current update"),
        ]

        reply = LogisticsAIPlugin._extract_reply_context(components)

        self.assertIsNotNone(reply)
        self.assertEqual(reply["messageId"], "quoted-1")
        self.assertEqual(reply["content"], "Quoted sailing update")
        self.assertEqual(
            reply["images"],
            ["https://example.com/quoted.jpg"],
        )
        self.assertEqual(
            reply["files"],
            ["https://example.com/quoted.pdf"],
        )


class FakeApiClient:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.analysis: dict[str, object] | None = None

    async def upload(self, _: dict[str, object]) -> UploadReceipt:
        self.calls.append("raw-upload")
        return UploadReceipt(database_id=42, platform="qq", message_id="m-1")

    async def upload_analysis(
        self,
        _: UploadReceipt,
        analysis: dict[str, object],
    ) -> None:
        self.calls.append("analysis-upload")
        self.analysis = analysis


class FakeAnalyzer:
    def __init__(
        self,
        calls: list[str],
        *,
        enabled_for_message: bool,
        error: Exception | None = None,
    ) -> None:
        self.calls = calls
        self.enabled_for_message = enabled_for_message
        self.error = error

    def should_analyze(self, _: dict[str, object]) -> bool:
        return self.enabled_for_message

    async def analyze(
        self,
        _: dict[str, object],
        __: str,
    ) -> dict[str, object]:
        self.calls.append("model-call")
        if self.error is not None:
            raise self.error
        return {"status": "succeeded"}

    @staticmethod
    def failure_payload(
        message_id: str,
        error: Exception,
    ) -> dict[str, object]:
        return {
            "status": "failed",
            "sourceMessageId": message_id,
            "warnings": [str(error)],
        }


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def build_plugin(
        api_client: FakeApiClient,
        analyzer: FakeAnalyzer,
    ) -> LogisticsAIPlugin:
        plugin = LogisticsAIPlugin.__new__(LogisticsAIPlugin)
        plugin.api_client = api_client
        plugin.analyzer = analyzer
        return plugin

    async def test_raw_upload_happens_before_model_and_analysis_upload(self) -> None:
        calls: list[str] = []
        api_client = FakeApiClient(calls)
        analyzer = FakeAnalyzer(calls, enabled_for_message=True)
        plugin = self.build_plugin(api_client, analyzer)

        await plugin._process_message(
            {
                "platform": "qq",
                "groupId": "g-1",
                "messageId": "m-1",
                "images": ["current.jpg"],
            },
            "qq:group:g-1",
        )

        self.assertEqual(
            calls,
            ["raw-upload", "model-call", "analysis-upload"],
        )

    async def test_text_only_message_never_calls_model(self) -> None:
        calls: list[str] = []
        api_client = FakeApiClient(calls)
        analyzer = FakeAnalyzer(calls, enabled_for_message=False)
        plugin = self.build_plugin(api_client, analyzer)

        await plugin._process_message(
            {
                "platform": "qq",
                "groupId": "g-1",
                "messageId": "m-1",
                "images": [],
            },
            "qq:group:g-1",
        )

        self.assertEqual(calls, ["raw-upload"])

    async def test_model_failure_does_not_remove_raw_upload(self) -> None:
        calls: list[str] = []
        api_client = FakeApiClient(calls)
        analyzer = FakeAnalyzer(
            calls,
            enabled_for_message=True,
            error=RuntimeError("provider unavailable"),
        )
        plugin = self.build_plugin(api_client, analyzer)

        await plugin._process_message(
            {
                "platform": "qq",
                "groupId": "g-1",
                "messageId": "m-1",
                "images": ["current.jpg"],
            },
            "qq:group:g-1",
        )

        self.assertEqual(
            calls,
            ["raw-upload", "model-call", "analysis-upload"],
        )
        self.assertEqual(api_client.analysis["status"], "failed")


if __name__ == "__main__":
    unittest.main()
