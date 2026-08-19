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

    def test_tool_annotations_require_confirmation_for_sends(self) -> None:
        import asyncio

        tools = asyncio.run(server.mcp.list_tools())
        annotations = {tool.name: tool.annotations for tool in tools}
        self.assertTrue(annotations["list_telegram_destinations"].read_only_hint)
        for name in ("send_telegram_message", "send_telegram_photo"):
            self.assertFalse(annotations[name].read_only_hint)
            self.assertTrue(annotations[name].destructive_hint)
            self.assertFalse(annotations[name].idempotent_hint)
            self.assertTrue(annotations[name].open_world_hint)


if __name__ == "__main__":
    unittest.main()
