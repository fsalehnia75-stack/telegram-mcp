import json
import os
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
DESTINATIONS_RAW = os.environ.get("TELEGRAM_DESTINATIONS", "{}").strip()

try:
    DESTINATIONS: dict[str, str] = json.loads(DESTINATIONS_RAW)
except json.JSONDecodeError as exc:
    raise RuntimeError(
        "TELEGRAM_DESTINATIONS must be valid JSON, for example: "
        '{"main":"-1001234567890"}'
    ) from exc

if not isinstance(DESTINATIONS, dict):
    raise RuntimeError("TELEGRAM_DESTINATIONS must be a JSON object.")

mcp = MCPServer("Telegram Sender")


def _require_config() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured.")
    if not DESTINATIONS:
        raise RuntimeError("No Telegram destinations are configured.")


@mcp.tool()
def list_telegram_destinations() -> dict[str, Any]:
    """List approved Telegram destinations available for sending."""
    return {
        "destinations": sorted(DESTINATIONS.keys()),
        "count": len(DESTINATIONS),
    }


@mcp.tool()
def send_telegram_message(
    destination: str,
    text: str,
    silent: bool = False,
    disable_link_preview: bool = False,
) -> dict[str, Any]:
    """Send a text message to one approved Telegram destination.

    Args:
        destination: Approved destination name, such as "main".
        text: Message text, up to 4096 characters.
        silent: Send without notification sound.
        disable_link_preview: Disable link previews.
    """
    _require_config()

    if destination not in DESTINATIONS:
        raise ValueError(
            f"Unknown destination '{destination}'. "
            f"Allowed: {', '.join(sorted(DESTINATIONS))}"
        )

    if not isinstance(text, str) or not text.strip():
        raise ValueError("Message text cannot be empty.")

    if len(text) > 4096:
        raise ValueError("Message is longer than Telegram's 4096-character limit.")

    chat_id = DESTINATIONS[destination]
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_notification": silent,
        "link_preview_options": {"is_disabled": disable_link_preview},
    }

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    with httpx.Client(timeout=20.0) as client:
        response = client.post(url, json=payload)

    try:
        data = response.json()
    except Exception:
        data = {"ok": False, "description": response.text}

    if response.status_code >= 400 or not data.get("ok"):
        description = data.get("description", "Telegram API request failed.")
        raise RuntimeError(f"Telegram error: {description}")

    message = data["result"]
    return {
        "ok": True,
        "destination": destination,
        "message_id": message.get("message_id"),
        "chat_id": message.get("chat", {}).get("id"),
        "date": message.get("date"),
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=port,
        stateless_http=True,
        json_response=True,
    )
