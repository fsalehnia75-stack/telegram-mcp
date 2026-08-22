import os
from contextlib import AsyncExitStack, asynccontextmanager

import uvicorn
from starlette.applications import Starlette

import auto_server
import server


manual_app = server.mcp.streamable_http_app(
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    max_request_body_size=15 * 1024 * 1024,
    host="0.0.0.0",
)
automatic_app = auto_server.mcp.streamable_http_app(
    streamable_http_path="/auto-mcp",
    stateless_http=True,
    json_response=True,
    max_request_body_size=15 * 1024 * 1024,
    host="0.0.0.0",
)


@asynccontextmanager
async def lifespan(app: Starlette):
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(manual_app.router.lifespan_context(app))
        await stack.enter_async_context(automatic_app.router.lifespan_context(app))
        yield


app = Starlette(
    routes=[*manual_app.routes, *automatic_app.routes],
    lifespan=lifespan,
)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
    )
