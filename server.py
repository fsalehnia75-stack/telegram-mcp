import base64
import binascii
import json
import html
import os
import re
import threading
import time
from collections import deque
from typing import Any

import anyio
import httpx
import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from pydantic import AnyHttpUrl


BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
DESTINATIONS_RAW = os.environ.get("TELEGRAM_DESTINATIONS", "{}").strip()
AUTH_ENABLED = os.environ.get("MCP_AUTH_ENABLED", "true").strip().lower() == "true"
AUTH_ISSUER_URL = os.environ.get("MCP_AUTH_ISSUER_URL", "").strip()
MCP_PUBLIC_URL = os.environ.get("MCP_PUBLIC_URL", "").strip()
AUTH_AUDIENCE = os.environ.get("MCP_AUTH_AUDIENCE", MCP_PUBLIC_URL).strip()
AUTH_REQUIRED_SCOPES = [
    scope
    for scope in os.environ.get("MCP_AUTH_REQUIRED_SCOPES", "telegram:send").split()
    if scope
]
AUTO_PUBLISH_DESTINATION = os.environ.get(
    "TELEGRAM_AUTO_PUBLISH_DESTINATION", "main"
).strip()

MAX_IMAGE_BYTES = 10 * 1024 * 1024
SEND_RATE_LIMIT_PER_MINUTE = int(
    os.environ.get("TELEGRAM_SEND_RATE_LIMIT_PER_MINUTE", "12")
)

try:
    DESTINATIONS: dict[str, str] = json.loads(DESTINATIONS_RAW)
except json.JSONDecodeError as exc:
    raise RuntimeError(
        "TELEGRAM_DESTINATIONS must be valid JSON, for example: "
        '{"main":"-1001234567890"}'
    ) from exc

if not isinstance(DESTINATIONS, dict):
    raise RuntimeError("TELEGRAM_DESTINATIONS must be a JSON object.")

if not 1 <= SEND_RATE_LIMIT_PER_MINUTE <= 60:
    raise RuntimeError("TELEGRAM_SEND_RATE_LIMIT_PER_MINUTE must be between 1 and 60.")


class JWTTokenVerifier(TokenVerifier):
    """Verify Auth0-compatible RS256 access tokens using the issuer JWKS."""

    def __init__(self, issuer: str, audience: str) -> None:
        self.issuer = issuer.rstrip("/") + "/"
        self.audience = audience
        self.jwks = PyJWKClient(f"{self.issuer}.well-known/jwks.json")

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            signing_key = await anyio.to_thread.run_sync(
                self.jwks.get_signing_key_from_jwt,
                token,
            )
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iss", "sub"]},
            )
        except Exception:
            return None

        raw_scope = claims.get("scope", "")
        scopes = raw_scope.split() if isinstance(raw_scope, str) else []
        return AccessToken(
            token=token,
            client_id=str(claims.get("azp") or claims.get("client_id") or "unknown"),
            scopes=scopes,
            expires_at=int(claims["exp"]),
            subject=str(claims["sub"]),
        )


def _build_server() -> MCPServer:
    kwargs: dict[str, Any] = {}
    if AUTH_ENABLED:
        missing = [
            name
            for name, value in (
                ("MCP_AUTH_ISSUER_URL", AUTH_ISSUER_URL),
                ("MCP_PUBLIC_URL", MCP_PUBLIC_URL),
                ("MCP_AUTH_AUDIENCE", AUTH_AUDIENCE),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "OAuth is enabled but required settings are missing: "
                + ", ".join(missing)
            )

        kwargs = {
            "token_verifier": JWTTokenVerifier(AUTH_ISSUER_URL, AUTH_AUDIENCE),
            "auth": AuthSettings(
                issuer_url=AnyHttpUrl(AUTH_ISSUER_URL),
                resource_server_url=AnyHttpUrl(MCP_PUBLIC_URL),
                required_scopes=AUTH_REQUIRED_SCOPES,
            ),
        }

    return MCPServer(
        "Telegram Newsroom",
        version="1.6.1",
        instructions=(
            "This server is restricted to the user's scheduled newsroom workflow. "
            "It exposes no free-form send actions. Automatic publication is permitted "
            "only when the server-side editorial score, verification, temporal, decision, "
            "and blocking-gate checks pass; the destination is fixed by configuration."
        ),
        **kwargs,
    )


mcp = _build_server()
_send_times: deque[float] = deque()
_send_lock = threading.Lock()


def _require_config() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured.")
    if not DESTINATIONS:
        raise RuntimeError("No Telegram destinations are configured.")


def _require_destination(destination: str) -> str:
    if destination not in DESTINATIONS:
        raise ValueError(
            f"Unknown destination '{destination}'. "
            f"Allowed: {', '.join(sorted(DESTINATIONS))}"
        )
    return DESTINATIONS[destination]


def _enforce_send_rate_limit(slots: int = 1) -> None:
    if slots < 1:
        raise ValueError("Rate-limit slots must be at least 1.")
    now = time.monotonic()
    with _send_lock:
        while _send_times and now - _send_times[0] >= 60:
            _send_times.popleft()
        if len(_send_times) + slots > SEND_RATE_LIMIT_PER_MINUTE:
            raise RuntimeError("Telegram send rate limit exceeded. Try again later.")
        _send_times.extend([now] * slots)


def _decode_image(encoded: str) -> tuple[bytes, str, str]:
    if encoded.startswith("data:"):
        if ";base64," not in encoded:
            raise ValueError("photo_base64 data URL must contain ';base64,'.")
        encoded = encoded.split(";base64,", 1)[1]

    if len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4 + 4:
        raise ValueError("Image is larger than the 10 MB upload limit.")

    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("photo_base64 must be valid base64-encoded image data.") from exc

    if not image_bytes:
        raise ValueError("photo_base64 decoded to an empty file.")
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ValueError("Image is larger than the 10 MB upload limit.")

    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return image_bytes, "image/png", ".png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return image_bytes, "image/jpeg", ".jpg"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return image_bytes, "image/webp", ".webp"
    raise ValueError("Only PNG, JPEG, and WebP images are accepted.")


def _markdown_to_telegram_html(text: str) -> str:
    """Convert the newsroom's limited Markdown subset to safe Telegram HTML."""
    escaped = html.escape(text, quote=True)
    escaped = re.sub(
        r"\[([^\]\n]+)\]\((https://[^\s)]+)\)",
        r'<a href="\2">\1</a>',
        escaped,
    )
    escaped = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", escaped)
    return escaped


def _telegram_post(
    method: str,
    *,
    json_payload: dict[str, Any] | None = None,
    form_data: dict[str, Any] | None = None,
    files: dict[str, Any] | None = None,
    timeout: float,
) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                url,
                json=json_payload,
                data=form_data,
                files=files,
            )
    except httpx.HTTPError:
        raise RuntimeError("Telegram network request failed.") from None

    try:
        result = response.json()
    except Exception:
        result = {"ok": False, "description": "Telegram returned an invalid response."}

    if response.status_code >= 400 or not result.get("ok"):
        description = result.get("description", "Telegram API request failed.")
        raise RuntimeError(f"Telegram error: {description}")
    return result


@mcp.tool(
    title="List Telegram destinations",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
def list_telegram_destinations() -> dict[str, Any]:
    """List approved destination labels. This tool never sends a message."""
    return {
        "destinations": sorted(DESTINATIONS.keys()),
        "count": len(DESTINATIONS),
    }


def send_telegram_message(
    destination: str,
    text: str,
    silent: bool = False,
    disable_link_preview: bool = False,
) -> dict[str, Any]:
    """Send text to an approved Telegram destination after fresh user confirmation.

    This is an irreversible external write. The client must show the exact
    destination and final text and ask the user to approve this specific send.

    Args:
        destination: Approved destination label, such as "main".
        text: Final message text, up to 4096 characters.
        silent: Send without a notification sound.
        disable_link_preview: Disable link previews.
    """
    _require_config()
    chat_id = _require_destination(destination)
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Message text cannot be empty.")
    formatted_text = _markdown_to_telegram_html(text.strip())
    if len(formatted_text) > 4096:
        raise ValueError("Message is longer than Telegram's 4096-character limit.")

    _enforce_send_rate_limit()
    result = _telegram_post(
        "sendMessage",
        json_payload={
            "chat_id": chat_id,
            "text": formatted_text,
            "parse_mode": "HTML",
            "disable_notification": silent,
            "link_preview_options": {"is_disabled": disable_link_preview},
        },
        timeout=20.0,
    )
    return {
        "ok": True,
        "destination": destination,
        "message_id": result["result"].get("message_id"),
    }


def send_telegram_photo(
    destination: str,
    photo_url: str = "",
    photo_base64: str = "",
    photo_filename: str = "image.jpg",
    caption: str = "",
    silent: bool = False,
) -> dict[str, Any]:
    """Send a photo after fresh user confirmation of image, caption, and destination.

    Exactly one image source is required: ``photo_url`` or ``photo_base64``.
    This is an irreversible external write and must be approved for every call.

    Args:
        destination: Approved destination label, such as "main".
        photo_url: Public HTTPS image URL Telegram can fetch.
        photo_base64: Base64-encoded PNG, JPEG, or WebP bytes.
        photo_filename: Filename for base64 uploads; its extension is normalized.
        caption: Final caption, up to 1024 characters.
        silent: Send without a notification sound.
    """
    _require_config()
    chat_id = _require_destination(destination)

    has_url = bool(isinstance(photo_url, str) and photo_url.strip())
    has_base64 = bool(isinstance(photo_base64, str) and photo_base64.strip())
    if has_url == has_base64:
        raise ValueError("Exactly one of photo_url or photo_base64 must be provided.")
    if not isinstance(caption, str):
        raise ValueError("Caption must be a string.")
    if len(caption) > 1024:
        raise ValueError("Caption is longer than Telegram's 1024-character limit.")

    _enforce_send_rate_limit()
    form_data: dict[str, Any] = {
        "chat_id": chat_id,
        "caption": caption,
        "disable_notification": str(silent).lower(),
    }
    files = None
    if has_url:
        normalized_url = photo_url.strip()
        if not normalized_url.startswith("https://"):
            raise ValueError("photo_url must start with https://.")
        form_data["photo"] = normalized_url
    else:
        image_bytes, mime_type, extension = _decode_image(photo_base64.strip())
        base_name = os.path.splitext(os.path.basename(photo_filename.strip()))[0]
        filename = (base_name or "image") + extension
        files = {"photo": (filename, image_bytes, mime_type)}

    result = _telegram_post(
        "sendPhoto",
        form_data=form_data,
        files=files,
        timeout=30.0,
    )
    return {
        "ok": True,
        "destination": destination,
        "message_id": result["result"].get("message_id"),
    }


def publish_news_package(
    destination: str,
    article_text: str,
    photo_url: str = "",
    photo_base64: str = "",
    photo_filename: str = "news-image.jpg",
    photo_caption: str = "",
    silent: bool = False,
    disable_link_preview: bool = False,
) -> dict[str, Any]:
    """Publish one approved news package with one fresh confirmation.

    The package sends the full article first, then sends the image as a direct
    reply to the article. The client must show the exact destination, image,
    caption, and article text, then obtain fresh user confirmation for this
    specific package. One approval covers this single two-message publication only.

    Exactly one image source is required: ``photo_url`` or ``photo_base64``.
    If Telegram accepts the article but rejects the image, the result is returned
    with ``status=partial`` and the article message ID so callers can retry only
    the image as a reply without duplicating the article.
    """
    _require_config()
    chat_id = _require_destination(destination)

    if not isinstance(article_text, str) or not article_text.strip():
        raise ValueError("Article text cannot be empty.")
    formatted_article = _markdown_to_telegram_html(article_text.strip())
    if len(formatted_article) > 4096:
        raise ValueError("Article text is longer than Telegram's 4096-character limit.")
    if not isinstance(photo_caption, str):
        raise ValueError("Photo caption must be a string.")
    if len(photo_caption) > 1024:
        raise ValueError("Photo caption is longer than Telegram's 1024-character limit.")

    has_url = bool(isinstance(photo_url, str) and photo_url.strip())
    has_base64 = bool(isinstance(photo_base64, str) and photo_base64.strip())
    if has_url == has_base64:
        raise ValueError("Exactly one of photo_url or photo_base64 must be provided.")

    form_data: dict[str, Any] = {
        "chat_id": chat_id,
        "caption": photo_caption,
        "disable_notification": str(silent).lower(),
    }
    files = None
    if has_url:
        normalized_url = photo_url.strip()
        if not normalized_url.startswith("https://"):
            raise ValueError("photo_url must start with https://.")
        form_data["photo"] = normalized_url
    else:
        image_bytes, mime_type, extension = _decode_image(photo_base64.strip())
        base_name = os.path.splitext(os.path.basename(photo_filename.strip()))[0]
        filename = (base_name or "news-image") + extension
        files = {"photo": (filename, image_bytes, mime_type)}

    # Reserve both Telegram operations before the irreversible first send.
    _enforce_send_rate_limit(slots=2)
    article_result = _telegram_post(
        "sendMessage",
        json_payload={
            "chat_id": chat_id,
            "text": formatted_article,
            "parse_mode": "HTML",
            "disable_notification": silent,
            "link_preview_options": {"is_disabled": disable_link_preview},
        },
        timeout=20.0,
    )
    article_message_id = article_result["result"].get("message_id")
    form_data["reply_parameters"] = json.dumps(
        {"message_id": article_message_id}
    )

    try:
        photo_result = _telegram_post(
            "sendPhoto",
            form_data=form_data,
            files=files,
            timeout=30.0,
        )
    except RuntimeError as exc:
        return {
            "ok": False,
            "status": "partial",
            "destination": destination,
            "photo_message_id": None,
            "article_message_id": article_message_id,
            "photo_reply_to_message_id": article_message_id,
            "error": str(exc),
        }

    return {
        "ok": True,
        "status": "complete",
        "destination": destination,
        "photo_message_id": photo_result["result"].get("message_id"),
        "article_message_id": article_message_id,
        "photo_reply_to_message_id": article_message_id,
    }


@mcp.tool(
    title="Auto-publish verified Telegram news package",
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
def auto_publish_news_package(
    article_text: str,
    editorial_score: int,
    verification_status: str,
    temporal_validation_status: str,
    editorial_decision: str,
    blocking_gates: list[str],
    photo_url: str = "",
    photo_base64: str = "",
    photo_filename: str = "news-image.jpg",
    photo_caption: str = "",
    silent: bool = False,
    disable_link_preview: bool = False,
) -> dict[str, Any]:
    """Auto-publish a newsroom-qualified package under standing authorization.

    Use only in the user's scheduled newsroom workflow. This tool does not accept
    a caller-selected destination. It rejects scores below 55, non-verified news,
    failed temporal validation, non-publishable decisions, and blocking gates.
    Manual or ad-hoc sends must use the confirmation-gated manual tools instead.
    """
    if isinstance(editorial_score, bool) or not isinstance(editorial_score, int):
        raise ValueError("editorial_score must be an integer between 0 and 100.")
    if not 55 <= editorial_score <= 100:
        raise ValueError("Automatic publication requires editorial_score >= 55.")
    if verification_status.strip().upper() != "VERIFIED":
        raise ValueError("Automatic publication requires verification_status=VERIFIED.")
    if temporal_validation_status.strip().upper() != "PASS":
        raise ValueError(
            "Automatic publication requires temporal_validation_status=PASS."
        )

    allowed_decisions = {"BREAKING", "HIGH_PRIORITY", "PUBLISHABLE"}
    normalized_decision = editorial_decision.strip().upper()
    if normalized_decision not in allowed_decisions:
        raise ValueError(
            "Automatic publication requires BREAKING, HIGH_PRIORITY, or PUBLISHABLE."
        )
    if not isinstance(blocking_gates, list) or any(
        not isinstance(gate, str) for gate in blocking_gates
    ):
        raise ValueError("blocking_gates must be a list of strings.")
    active_gates = [gate.strip() for gate in blocking_gates if gate.strip()]
    if active_gates:
        raise ValueError("Automatic publication is blocked by active editorial gates.")
    if not AUTO_PUBLISH_DESTINATION:
        raise RuntimeError("TELEGRAM_AUTO_PUBLISH_DESTINATION is not configured.")

    result = publish_news_package(
        destination=AUTO_PUBLISH_DESTINATION,
        article_text=article_text,
        photo_url=photo_url,
        photo_base64=photo_base64,
        photo_filename=photo_filename,
        photo_caption=photo_caption,
        silent=silent,
        disable_link_preview=disable_link_preview,
    )
    result["mode"] = "automatic"
    result["editorial_score"] = editorial_score
    result["editorial_decision"] = normalized_decision
    return result


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=port,
        stateless_http=True,
        json_response=True,
        max_request_body_size=15 * 1024 * 1024,
    )
