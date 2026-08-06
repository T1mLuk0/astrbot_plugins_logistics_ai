"""Multimodal analyzer tests."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

try:
    from ._stubs import install_test_stubs
except ImportError:
    from _stubs import install_test_stubs

install_test_stubs()

from astrbot_plugins_logistics_ai.analyzer import MultimodalAnalyzer
from astrbot_plugins_logistics_ai.exceptions import LogisticsAIAnalysisError


class FakeContext:
    def __init__(self, completion_text: str) -> None:
        self.completion_text = completion_text
        self.calls: list[tuple[str, object]] = []

    async def get_current_chat_provider_id(self, *, umo: str) -> str:
        self.calls.append(("provider", umo))
        return "mimo-v2-omni"

    async def llm_generate(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(("generate", kwargs))
        return SimpleNamespace(completion_text=self.completion_text)


class AnalyzerJsonTests(unittest.TestCase):
    def test_extracts_json_code_fence(self) -> None:
        result = MultimodalAnalyzer.extract_json_object(
            '```json\n{"status":"succeeded","events":[]}\n```'
        )
        self.assertEqual(result["status"], "succeeded")

    def test_extracts_json_surrounded_by_text(self) -> None:
        result = MultimodalAnalyzer.extract_json_object(
            'Result follows: {"summary":"delay"} end.'
        )
        self.assertEqual(result["summary"], "delay")

    def test_rejects_non_json_response(self) -> None:
        with self.assertRaises(LogisticsAIAnalysisError):
            MultimodalAnalyzer.extract_json_object("No structured result")

    def test_only_current_message_images_trigger_analysis(self) -> None:
        analyzer = MultimodalAnalyzer(
            context=object(),
            config={"multimodal_analysis_enabled": True},
        )
        self.assertFalse(
            analyzer.should_analyze(
                {
                    "images": [],
                    "reply": {"images": ["quoted-image.jpg"]},
                }
            )
        )
        self.assertTrue(analyzer.should_analyze({"images": ["current.jpg"]}))


class AnalyzerCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_calls_active_provider_without_visible_reply(self) -> None:
        response = {
            "schemaVersion": "1.0",
            "status": "succeeded",
            "summary": "Schedule update",
            "events": [{"eventType": "ScheduleDelayed"}],
        }
        context = FakeContext(json.dumps(response))
        analyzer = MultimodalAnalyzer(
            context=context,
            config={"multimodal_analysis_enabled": True},
        )

        result = await analyzer.analyze(
            {
                "messageId": "message-1",
                "images": ["https://example.com/current.jpg"],
            },
            "qq:group:100",
        )

        self.assertEqual(context.calls[0], ("provider", "qq:group:100"))
        generate_kwargs = context.calls[1][1]
        self.assertEqual(
            generate_kwargs["image_urls"],
            ["https://example.com/current.jpg"],
        )
        self.assertEqual(result["analyzer"]["providerId"], "mimo-v2-omni")
        self.assertTrue(result["requiresBackendContextResolution"])


if __name__ == "__main__":
    unittest.main()
