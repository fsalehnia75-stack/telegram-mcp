import base64
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_DESTINATIONS", '{"main":"-100123"}')
os.environ.setdefault("MCP_AUTH_ENABLED", "false")

import server


class TelegramServerTests(unittest.TestCase):
    def setUp(self) -> None:
        server._send_times.clear()

    def test_image_validation_accepts_png(self) -> None:
        payload = base64.b64encode(b"\x89PNG\r\n\x1a\ncontent").decode()
        data, mime, extension = server._decode_image(payload)
        self.assertEqual(data, b"\x89PNG\r\n\x1a\ncontent")
        self.assertEqual(mime, "image/png")
        self.assertEqual(extension, ".png")

    def test_image_validation_rejects_non_image(self) -> None:
        payload = base64.b64encode(b"not-an-image").decode()
        with self.assertRaisesRegex(ValueError, "Only PNG"):
            server._decode_image(payload)

    def test_send_result_does_not_expose_chat_id(self) -> None:
        fake_result = {
            "ok": True,
            "result": {
                "message_id": 7,
                "chat": {"id": -100123},
                "date": 123456,
            },
        }
        with patch.object(server, "_telegram_post", return_value=fake_result):
            result = server.send_telegram_message("main", "hello")
        self.assertEqual(result, {"ok": True, "destination": "main", "message_id": 7})

    def test_network_error_hides_bot_token(self) -> None:
        client = MagicMock()
        client.__enter__.return_value.post.side_effect = server.httpx.ConnectError("boom")
        with patch.object(server.httpx, "Client", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "network request failed") as caught:
                server._telegram_post("sendMessage", json_payload={}, timeout=1.0)
        self.assertNotIn("test-token", str(caught.exception))

    def test_public_tools_are_restricted_to_gated_auto_publish(self) -> None:
        import asyncio

        tools = asyncio.run(server.mcp.list_tools())
        annotations = {tool.name: tool.annotations for tool in tools}
        self.assertTrue(annotations["list_telegram_destinations"].read_only_hint)
        self.assertEqual(
            set(annotations),
            {"list_telegram_destinations", "auto_publish_news_package"},
        )
        auto_annotations = annotations["auto_publish_news_package"]
        self.assertFalse(auto_annotations.read_only_hint)
        self.assertTrue(auto_annotations.destructive_hint)
        self.assertFalse(auto_annotations.idempotent_hint)
        self.assertTrue(auto_annotations.open_world_hint)

    def test_publish_news_package_sends_photo_then_article(self) -> None:
        payload = base64.b64encode(b"\x89PNG\r\n\x1a\ncontent").decode()
        photo_result = {"ok": True, "result": {"message_id": 10}}
        article_result = {"ok": True, "result": {"message_id": 11}}

        with patch.object(
            server,
            "_telegram_post",
            side_effect=[photo_result, article_result],
        ) as telegram_post:
            result = server.publish_news_package(
                "main",
                "final article",
                photo_base64=payload,
                photo_filename="featured.png",
            )

        self.assertEqual(
            result,
            {
                "ok": True,
                "status": "complete",
                "destination": "main",
                "photo_message_id": 10,
                "article_message_id": 11,
            },
        )
        self.assertEqual(
            [call.args[0] for call in telegram_post.call_args_list],
            ["sendPhoto", "sendMessage"],
        )

    def test_publish_news_package_reports_partial_send(self) -> None:
        payload = base64.b64encode(b"\x89PNG\r\n\x1a\ncontent").decode()
        photo_result = {"ok": True, "result": {"message_id": 12}}

        with patch.object(
            server,
            "_telegram_post",
            side_effect=[photo_result, RuntimeError("Telegram network request failed.")],
        ):
            result = server.publish_news_package(
                "main",
                "final article",
                photo_base64=payload,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["photo_message_id"], 12)
        self.assertIsNone(result["article_message_id"])

    def test_auto_publish_accepts_only_qualified_news(self) -> None:
        expected = {
            "ok": True,
            "status": "complete",
            "destination": "main",
            "photo_message_id": 20,
            "article_message_id": 21,
        }
        with patch.object(
            server,
            "publish_news_package",
            return_value=expected.copy(),
        ) as publish:
            result = server.auto_publish_news_package(
                article_text="verified article",
                editorial_score=72,
                verification_status="VERIFIED",
                temporal_validation_status="PASS",
                editorial_decision="HIGH_PRIORITY",
                blocking_gates=[],
                photo_url="https://example.com/news.jpg",
            )

        publish.assert_called_once_with(
            destination="main",
            article_text="verified article",
            photo_url="https://example.com/news.jpg",
            photo_base64="",
            photo_filename="news-image.jpg",
            photo_caption="",
            silent=False,
            disable_link_preview=False,
        )
        self.assertEqual(result["mode"], "automatic")
        self.assertEqual(result["editorial_score"], 72)

    def test_auto_publish_rejects_score_below_threshold(self) -> None:
        with patch.object(server, "publish_news_package") as publish:
            with self.assertRaisesRegex(ValueError, "editorial_score >= 55"):
                server.auto_publish_news_package(
                    article_text="conditional article",
                    editorial_score=54,
                    verification_status="VERIFIED",
                    temporal_validation_status="PASS",
                    editorial_decision="CONDITIONAL",
                    blocking_gates=[],
                    photo_url="https://example.com/news.jpg",
                )
        publish.assert_not_called()

    def test_auto_publish_rejects_failed_gates(self) -> None:
        invalid_cases = (
            {"verification_status": "UNVERIFIED"},
            {"temporal_validation_status": "FAIL"},
            {"editorial_decision": "CONDITIONAL"},
            {"blocking_gates": ["SOURCE_GATE"]},
        )
        defaults = {
            "article_text": "article",
            "editorial_score": 70,
            "verification_status": "VERIFIED",
            "temporal_validation_status": "PASS",
            "editorial_decision": "PUBLISHABLE",
            "blocking_gates": [],
            "photo_url": "https://example.com/news.jpg",
        }
        with patch.object(server, "publish_news_package") as publish:
            for changes in invalid_cases:
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    server.auto_publish_news_package(**(defaults | changes))
        publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
