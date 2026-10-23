#!/usr/bin/env python3
import os
import sys
import json

from aiohttp import ClientSession, ClientTimeout, web


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


def authorized(request: web.Request, token: str) -> bool:
    auth = request.headers.get("authorization", "")
    x_api_key = request.headers.get("x-api-key", "")
    return auth == f"Bearer {token}" or x_api_key == token


async def health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def handle(request: web.Request) -> web.StreamResponse:
    token = request.app["token"]
    if not authorized(request, token):
        return web.json_response(
            {
                "error": {
                    "message": "Unauthorized",
                    "type": "invalid_request_error",
                    "code": "unauthorized",
                }
            },
            status=401,
            headers={"WWW-Authenticate": "Bearer"},
        )

    upstream = request.app["upstream"]
    url = f"{upstream}{request.rel_url}"
    headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower() not in HOP_BY_HOP_HEADERS
        and name.lower() not in {"authorization", "x-api-key"}
    }
    body = await request.read()
    content_type = request.headers.get("content-type", "")
    if body and "application/json" in content_type.lower():
        body = rewrite_json_body(body)

    session: ClientSession = request.app["session"]
    async with session.request(
        request.method,
        url,
        headers=headers,
        data=body if body else None,
        allow_redirects=False,
    ) as upstream_response:
        response_headers = {
            name: value
            for name, value in upstream_response.headers.items()
            if name.lower() not in HOP_BY_HOP_HEADERS
        }
        downstream = web.StreamResponse(
            status=upstream_response.status,
            reason=upstream_response.reason,
            headers=response_headers,
        )
        await downstream.prepare(request)
        async for chunk in upstream_response.content.iter_chunked(65536):
            await downstream.write(chunk)
        await downstream.write_eof()
        return downstream


async def on_startup(app: web.Application) -> None:
    app["session"] = ClientSession(
        timeout=ClientTimeout(total=None, sock_connect=30, sock_read=None)
    )


def rewrite_json_body(body: bytes) -> bytes:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body

    if isinstance(payload, dict):
        # Cursor may send this by default; the Azure gpt-5.6-sol deployment rejects it.
        payload.pop("temperature", None)
        if "max_tokens" in payload and "max_completion_tokens" not in payload:
            payload["max_completion_tokens"] = payload.pop("max_tokens")
        return json.dumps(payload, separators=(",", ":")).encode("utf-8")

    return body


async def on_cleanup(app: web.Application) -> None:
    await app["session"].close()


def main() -> int:
    token = os.environ.get("SWITCHYARD_CURSOR_API_KEY")
    if not token:
        print("SWITCHYARD_CURSOR_API_KEY is required", file=sys.stderr)
        return 2

    upstream = os.environ.get("SWITCHYARD_PROXY_UPSTREAM", "http://127.0.0.1:4000")
    app = web.Application(client_max_size=1024**3)
    app["token"] = token
    app["upstream"] = upstream.rstrip("/")
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/health", health)
    app.router.add_route("*", "/{tail:.*}", handle)

    host = os.environ.get("SWITCHYARD_PROXY_HOST", "127.0.0.1")
    port = int(os.environ.get("SWITCHYARD_PROXY_PORT", "4100"))
    web.run_app(app, host=host, port=port, access_log=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
