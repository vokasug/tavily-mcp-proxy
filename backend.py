#!/usr/bin/env python3
import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
from aiohttp import web

CONFIG_DIR = Path(os.environ.get("TAVILY_MCP_CONFIG_DIR", "/etc/tavily-mcp"))
LISTEN_HOST = os.environ.get("TAVILY_MCP_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("TAVILY_MCP_PORT", "8741"))
UPSTREAM = os.environ.get("TAVILY_MCP_UPSTREAM", "https://mcp.tavily.com").rstrip("/")

ACCESS_KEYS_FILE = CONFIG_DIR / "access-keys.list"
ACTIVE_KEY_FILE = CONFIG_DIR / "active-key"
ACTIVE_KEY_STATUS = CONFIG_DIR / "active-key.status"

log = logging.getLogger("tavily-mcp")

_access_keys = {}


def load_access_keys():
    global _access_keys
    keys = {}
    if not ACCESS_KEYS_FILE.exists():
        log.error("access-keys file not found: %s", ACCESS_KEYS_FILE)
    else:
        try:
            text = ACCESS_KEYS_FILE.read_text()
        except OSError as e:
            log.error("cannot read %s: %s", ACCESS_KEYS_FILE, e)
            text = ""
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split(None, 1)
            if len(parts) != 2:
                log.warning("skipping malformed access-keys line: %r", line)
                continue
            name, key = parts
            keys[name] = key
    _access_keys = keys
    log.info("loaded %d access key(s): %s", len(keys), ", ".join(sorted(keys)) or "<none>")


def read_active_key():
    try:
        status = ACTIVE_KEY_STATUS.read_text().strip()
    except OSError as e:
        log.warning("active-key.status read failed: %s", e)
        return None, False
    if status != "ok":
        log.warning("active-key.status=%r (not ok)", status)
        return None, False
    try:
        key = ACTIVE_KEY_FILE.read_text().strip()
    except OSError as e:
        log.warning("active-key read failed: %s", e)
        return None, False
    if not key:
        log.warning("active-key empty")
        return None, False
    return key, True


def _client_ip(request):
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote


def _summarize_body(body, max_args=200):
    if not body:
        return ""
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return " body=<non-json %dB>" % len(body)
    items = data if isinstance(data, list) else [data]
    parts = []
    for item in items:
        if not isinstance(item, dict):
            continue
        s = str(item.get("method", "?"))
        params = item.get("params")
        if isinstance(params, dict):
            name = params.get("name")
            if name:
                s += ":%s" % name
            args = params.get("arguments")
            if args is not None:
                args_s = json.dumps(args, ensure_ascii=False)
                if len(args_s) > max_args:
                    args_s = args_s[:max_args] + "…"
                s += " %s" % args_s
        parts.append(s)
    return " rpc=" + ";".join(parts)


def _filter_response_headers(headers):
    return {
        k: v
        for k, v in headers.items()
        if k.lower() not in {"content-length", "transfer-encoding", "connection"}
    }


async def proxy_handler(request: web.Request):
    client = _client_ip(request)
    provided = request.query.get("accessKey")
    if not provided:
        log.info("reject: missing accessKey client=%s path=%s", client, request.path)
        return web.Response(status=401, text="unauthorized\n")
    if provided not in _access_keys.values():
        log.info("reject: wrong accessKey client=%s path=%s", client, request.path)
        return web.Response(status=401, text="unauthorized\n")

    key_name = next((n for n, k in _access_keys.items() if k == provided), "?")

    active, ok = read_active_key()
    if not ok:
        log.error("no active tavily key (status check failed); client=%s", client)
        return web.Response(status=503, text="no active key\n")

    query_pairs = [(k, v) for k, v in request.query.items() if k != "accessKey"]
    query_pairs.append(("tavilyApiKey", active))
    upstream_url = f"{UPSTREAM}{request.path}?{urlencode(query_pairs)}"

    fwd_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in {"host", "content-length", "authorization", "connection"}
    }
    body = await request.read() if request.body_exists or request.can_read_body else b""
    log.info(
        "req: key=%s client=%s method=%s path=%s%s",
        key_name, client, request.method, request.path, _summarize_body(body),
    )

    timeout = aiohttp.ClientTimeout(total=3600, connect=30, sock_read=3600)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.request(
                method=request.method,
                url=upstream_url,
                headers=fwd_headers,
                data=body if body else None,
                allow_redirects=False,
            ) as upstream_resp:
                content_type = upstream_resp.headers.get("Content-Type", "")
                is_stream = (
                    "text/event-stream" in content_type
                    or upstream_resp.headers.get("Transfer-Encoding", "").lower() == "chunked"
                    or upstream_resp.content_length is None
                )
                resp_headers = _filter_response_headers(upstream_resp.headers)
                if is_stream:
                    resp = web.StreamResponse(status=upstream_resp.status, headers=resp_headers)
                    await resp.prepare(request)
                    async for chunk in upstream_resp.content.iter_any():
                        await resp.write(chunk)
                    await resp.write_eof()
                    return resp
                payload = await upstream_resp.read()
                return web.Response(
                    status=upstream_resp.status,
                    body=payload,
                    headers=resp_headers,
                )
    except asyncio.TimeoutError:
        log.error("upstream timeout path=%s key=%s", request.path, key_name)
        return web.Response(status=504, text="upstream timeout\n")
    except aiohttp.ClientError as e:
        log.error("upstream error: %s path=%s key=%s", e, request.path, key_name)
        return web.Response(status=502, text="upstream error\n")


async def healthz(_request):
    return web.Response(text="ok\n")


def make_app():
    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_route("*", "/mcp", proxy_handler)
    app.router.add_route("*", "/mcp/", proxy_handler)
    app.router.add_route("*", "/mcp/{tail:.*}", proxy_handler)
    return app


def _install_signal_handlers(loop, stop_event):
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)


def _reload_handler(signum, frame):
    log.info("SIGHUP received, reloading access keys")
    load_access_keys()


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    load_access_keys()
    signal.signal(signal.SIGHUP, _reload_handler)

    app = make_app()
    runner = web.AppRunner(app, access_log=None)  # default access log leaks accessKey via query string
    await runner.setup()
    site = web.TCPSite(runner, LISTEN_HOST, LISTEN_PORT)
    await site.start()
    log.info("listening on http://%s:%d -> %s", LISTEN_HOST, LISTEN_PORT, UPSTREAM)

    stop = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop)
    try:
        await stop.wait()
    finally:
        log.info("shutting down")
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)