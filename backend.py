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
BLOCKED_TOOLS = frozenset(
    t.strip()
    for t in os.environ.get(
        "TAVILY_MCP_BLOCKED_TOOLS", "tavily_crawl,tavily_research"
    ).split(",")
    if t.strip()
)

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


def _parse_rpc_items(body):
    if not body:
        return None
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    items = data if isinstance(data, list) else [data]
    return data, [i for i in items if isinstance(i, dict)]


def _is_blocked_call(item):
    params = item.get("params")
    return (
        item.get("method") == "tools/call"
        and isinstance(params, dict)
        and params.get("name") in BLOCKED_TOOLS
    )


def _filter_tools_in(obj):
    items = obj if isinstance(obj, list) else [obj]
    removed = []
    for item in items:
        if not isinstance(item, dict):
            continue
        result = item.get("result")
        if isinstance(result, dict) and isinstance(result.get("tools"), list):
            kept = [
                t
                for t in result["tools"]
                if not (isinstance(t, dict) and t.get("name") in BLOCKED_TOOLS)
            ]
            removed.extend(
                t.get("name")
                for t in result["tools"]
                if isinstance(t, dict) and t.get("name") in BLOCKED_TOOLS
            )
            result["tools"] = kept
    return removed


def _filter_sse_tools(payload):
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload
    out = []
    for event in text.split("\n\n"):
        lines = event.split("\n")
        data_lines = [l[5:].removeprefix(" ") for l in lines if l.startswith("data:")]
        if not data_lines:
            out.append(event)
            continue
        try:
            obj = json.loads("\n".join(data_lines))
        except ValueError:
            out.append(event)
            continue
        removed = _filter_tools_in(obj)
        if removed:
            log.info("tools/list: hid %s", ", ".join(sorted(set(removed))))
            lines = [l for l in lines if not l.startswith("data:")]
            lines.append("data: " + json.dumps(obj, ensure_ascii=False))
            out.append("\n".join(lines))
        else:
            out.append(event)
    return "\n\n".join(out).encode("utf-8")


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

    parsed = _parse_rpc_items(body)
    rpc_items = parsed[1] if parsed else []
    wants_tools_list = any(i.get("method") == "tools/list" for i in rpc_items)
    blocked = [i for i in rpc_items if _is_blocked_call(i)]
    if blocked and len(blocked) == len(rpc_items):
        for i in blocked:
            log.info(
                "block: key=%s client=%s tool=%s",
                key_name, client, i.get("params", {}).get("name"),
            )
        errors = [
            {
                "jsonrpc": "2.0",
                "id": i.get("id"),
                "error": {
                    "code": -32601,
                    "message": "Tool disabled by proxy: %s"
                    % i.get("params", {}).get("name"),
                },
            }
            for i in blocked
        ]
        payload = errors if isinstance(parsed[0], list) else errors[0]
        accept = request.headers.get("Accept", "")
        if "text/event-stream" in accept and "application/json" not in accept:
            sse = "event: message\ndata: %s\n\n" % json.dumps(payload, ensure_ascii=False)
            return web.Response(text=sse, content_type="text/event-stream")
        return web.json_response(payload)
    if blocked:
        log.warning(
            "batch mixes blocked tool(s) with other calls; passing through: key=%s client=%s",
            key_name, client,
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
                if wants_tools_list and upstream_resp.status == 200:
                    payload = await upstream_resp.read()
                    if "text/event-stream" in content_type:
                        payload = _filter_sse_tools(payload)
                    elif "application/json" in content_type:
                        try:
                            obj = json.loads(payload)
                            if _filter_tools_in(obj):
                                payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                        except (ValueError, UnicodeDecodeError):
                            pass
                    resp_headers = {
                        k: v
                        for k, v in resp_headers.items()
                        if k.lower() != "content-encoding"
                    }
                    return web.Response(
                        status=upstream_resp.status,
                        body=payload,
                        headers=resp_headers,
                    )
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