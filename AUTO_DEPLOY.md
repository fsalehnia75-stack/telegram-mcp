# Telegram Newsroom Auto deployment

The free Render plan can expose the manual and automatic connectors from the
same web service. `combined_server.py` serves two isolated MCP endpoints.

## Existing service settings

- Start command: `python combined_server.py`
- Manual MCP path: `/mcp`
- Automatic MCP path: `/auto-mcp`

## Environment variables

Keep the existing variables and add:

- `TELEGRAM_BOT_TOKEN` — secret; never commit it
- `TELEGRAM_DESTINATIONS` — same approved destination map
- `TELEGRAM_AUTO_PUBLISH_DESTINATION=main`
- `MCP_AUTH_ENABLED=true`
- `MCP_AUTH_ISSUER_URL` — same Auth0 issuer
- `AUTO_MCP_PUBLIC_URL` — existing service URL plus `/auto-mcp`
- `AUTO_AUTH_AUDIENCE` — same value as `AUTO_MCP_PUBLIC_URL`
- `AUTO_AUTH_REQUIRED_SCOPES=telegram:auto_publish`
- `TELEGRAM_SEND_RATE_LIMIT_PER_MINUTE=12`

## ChatGPT connector

Create a separate connector named `telegramnewsroom-auto` using the
`/auto-mcp` URL. Grant it full access only after verifying that its action list
contains exactly:

1. `list_telegram_destinations`
2. `auto_publish_news_package`

Keep the existing `telegramnewsroom` connector on **Always ask**. Never grant
full access to the manual connector.
