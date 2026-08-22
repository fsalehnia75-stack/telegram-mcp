import os
from typing import Any

from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from pydantic import AnyHttpUrl

import server


def _build_auto_server() -> MCPServer:
    kwargs: dict[str, Any] = {}
    if server.AUTH_ENABLED:
        missing = [
            name
            for name, value in (
                ("MCP_AUTH_ISSUER_URL", server.AUTH_ISSUER_URL),
                ("MCP_PUBLIC_URL", server.MCP_PUBLIC_URL),
                ("MCP_AUTH_AUDIENCE", server.AUTH_AUDIENCE),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "OAuth is enabled but required settings are missing: "
                + ", ".join(missing)
            )
        kwargs = {
            "token_verifier": server.JWTTokenVerifier(
                server.AUTH_ISSUER_URL,
                server.AUTH_AUDIENCE,
            ),
            "auth": AuthSettings(
                issuer_url=AnyHttpUrl(server.AUTH_ISSUER_URL),
                resource_server_url=AnyHttpUrl(server.MCP_PUBLIC_URL),
                required_scopes=server.AUTH_REQUIRED_SCOPES,
            ),
        }

    return MCPServer(
        "Telegram Newsroom Auto",
        version="1.0.0",
        instructions=(
            "This server is restricted to the user's scheduled newsroom workflow. "
            "It exposes no free-form manual send tools. Auto-publication is allowed "
            "only when the server-side editorial gates pass and the destination is "
            "the configured fixed destination."
        ),
        **kwargs,
    )


mcp = _build_auto_server()

mcp.tool(
    title="List Telegram destinations",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)(server.list_telegram_destinations)

mcp.tool(
    title="Auto-publish verified Telegram news package",
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)(server.auto_publish_news_package)


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
