import base64
import binascii
import json
import mimetypes
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


@mcp.tool()
def send_telegram_photo(
    destination: str,
    photo_url: str = "",
    photo_base64: str = "",
    photo_filename: str = "image.jpg",
    caption: str = "",
    silent: bool = False,
) -> dict[str, Any]:
    """Send a photo to one approved Telegram destination.

    Exactly one image source must be supplied: ``photo_url`` or
    ``photo_base64``. Base64 input is uploaded as multipart form data.

    Args:
        destination: Approved destination name, such as "main".
        photo_url: Public HTTP(S) image URL Telegram can fetch.
        photo_base64: Base64-encoded image bytes.
        photo_filename: Filename used for base64 uploads, including extension.
        caption: Optional caption, up to 1024 characters.
        silent: Send without notification sound.
    """
    _require_config()

    if destination not in DESTINATIONS:
        raise ValueError(
            f"Unknown destination '{destination}'. "
            f"Allowed: {', '.join(sorted(DESTINATIONS))}"
        )

    has_url = bool(isinstance(photo_url, str) and photo_url.strip())
    has_base64 = bool(isinstance(photo_base64, str) and photo_base64.strip())
    if has_url == has_base64:
        raise ValueError("Exactly one of photo_url or photo_base64 must be provided.")

    if not isinstance(caption, str):
        raise ValueError("Caption must be a string.")
    if len(caption) > 1024:
        raise ValueError("Caption is longer than Telegram's 1024-character limit.")

    chat_id = DESTINATIONS[destination]
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    data = {
        "chat_id": chat_id,
        "caption": caption,
        "disable_notification": str(silent).lower(),
    }

    with httpx.Client(timeout=30.0) as client:
        if has_url:
            normalized_url = photo_url.strip()
            if not normalized_url.startswith(("http://", "https://")):
                raise ValueError("photo_url must start with http:// or https://.")
            response = client.post(
                url,
                data={**data, "photo": normalized_url},
            )
        else:
            encoded = photo_base64.strip()
            if encoded.startswith("data:"):
                if ";base64," not in encoded:
                    raise ValueError("photo_base64 data URL must contain ';base64,'.")
                encoded = encoded.split(";base64,", 1)[1]

            try:
                image_bytes = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("photo_base64 must be valid base64-encoded image data.") from exc

            if not image_bytes:
                raise ValueError("photo_base64 decoded to an empty file.")

            filename = os.path.basename(photo_filename.strip() or "image.jpg")
            mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            response = client.post(
                url,
                data=data,
                files={"photo": (filename, image_bytes, mime_type)},
            )

    try:
        result = response.json()
    except Exception:
        result = {"ok": False, "description": response.text}

    if response.status_code >= 400 or not result.get("ok"):
        description = result.get("description", "Telegram API request failed.")
        raise RuntimeError(f"Telegram error: {description}")

    message = result["result"]
    photos = message.get("photo") or []
    file_id = photos[-1].get("file_id") if photos else None
    return {
        "ok": True,
        "destination": destination,
        "message_id": message.get("message_id"),
        "chat_id": message.get("chat", {}).get("id"),
        "date": message.get("date"),
        "file_id": file_id,
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
