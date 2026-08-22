# Telegram Newsroom Auto deployment

Deploy a second Render web service from this repository. Do not replace the
existing manual service.

## Service settings

- Start command: `python auto_server.py`
- Health/MCP path: `/mcp`
- Recommended service name: `telegram-newsroom-auto`

## Environment variables

Copy the values from the existing manual service unless noted otherwise:

- `TELEGRAM_BOT_TOKEN` — secret; never commit it
- `TELEGRAM_DESTINATIONS` — same approved destination map
- `TELEGRAM_AUTO_PUBLISH_DESTINATION=main`
- `MCP_AUTH_ENABLED=true`
- `MCP_AUTH_ISSUER_URL` — same Auth0 issuer
- `MCP_PUBLIC_URL` — exact HTTPS URL of the new service plus `/mcp`
- `MCP_AUTH_AUDIENCE` — same value as `MCP_PUBLIC_URL`
- `MCP_AUTH_REQUIRED_SCOPES=telegram:auto_publish`
- `TELEGRAM_SEND_RATE_LIMIT_PER_MINUTE=12`

## ChatGPT connector

Create a separate connector named `telegramnewsroom-auto` using the new `/mcp`
URL. Grant it full access only after verifying that its discovered action list
contains exactly:

1. `list_telegram_destinations`
2. `auto_publish_news_package`

Keep the existing `telegramnewsroom` connector on **Always ask**. Never grant
full access to the manual connector.
