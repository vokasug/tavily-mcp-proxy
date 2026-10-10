#!/usr/bin/env python3
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import aiohttp
from aiohttp import web

TOOLS = [
    {"name": "tavily_search", "description": "search"},
    {"name": "tavily_extract", "description": "extract"},
    {"name": "tavily_crawl", "description": "crawl"},
    {"name": "tavily_research", "description": "research"},
]
ACCESS_KEY = "tvmcp_testkey"

upstream_state = {"calls": 0, "as_json": False}


async def mock_mcp(request):
    body = await request.read()
    data = json.loads(body)
    items = data if isinstance(data, list) else [data]
    method = items[0].get("method", "")
    if method == "tools/call":
        upstream_state["calls"] += 1
    rid = items[0].get("id")
    if method == "initialize":
        result = {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "serverInfo": {"name": "mock-tavily", "version": "0"},
        }
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "upstream-ok"}], "isError": False}
    elif method.startswith("notifications/"):
        return web.Response(status=202)
    else:
        result = {}
    msg = {"jsonrpc": "2.0", "id": rid, "result": result}
    if request.headers.get("X-Mock-Json"):
        return web.json_response(msg)
    sse = "event: message\ndata: %s\n\n" % json.dumps(msg, ensure_ascii=False)
    return web.Response(text=sse, content_type="text/event-stream")


async def start_server(app):
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    return runner, port


async def rpc(session, url, payload, extra_headers=None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if extra_headers:
        headers.update(extra_headers)
    async with session.post(url, json=payload, headers=headers) as resp:
        body = await resp.read()
        ct = resp.headers.get("Content-Type", "")
        if "text/event-stream" in ct:
            for chunk in body.decode("utf-8").split("\n\n"):
                for line in chunk.split("\n"):
                    if line.startswith("data:"):
                        return resp.status, json.loads(line[5:].strip())
            return resp.status, None
        return resp.status, json.loads(body)


async def main():
    config_dir = Path(tempfile.mkdtemp(prefix="tavily-mcp-test-"))
    (config_dir / "access-keys.list").write_text("test\t%s\n" % ACCESS_KEY)
    (config_dir / "active-key").write_text("tvly-fake\n")
    (config_dir / "active-key.status").write_text("ok\n")

    mock_app = web.Application()
    mock_app.router.add_route("*", "/mcp/{tail:.*}", mock_mcp)
    mock_runner, mock_port = await start_server(mock_app)

    os.environ["TAVILY_MCP_CONFIG_DIR"] = str(config_dir)
    os.environ["TAVILY_MCP_UPSTREAM"] = "http://127.0.0.1:%d" % mock_port
    os.environ["TAVILY_MCP_HOST"] = "127.0.0.1"

    sys.path.insert(0, str(Path(__file__).parent))
    import backend

    assert backend.BLOCKED_TOOLS == {"tavily_crawl", "tavily_research"}, backend.BLOCKED_TOOLS
    backend.load_access_keys()
    backend_runner, proxy_port = await start_server(backend.make_app())
    url = "http://127.0.0.1:%d/mcp/?accessKey=%s" % (proxy_port, ACCESS_KEY)

    try:
        async with aiohttp.ClientSession() as session:
            # 1. initialize проходит
            status, msg = await rpc(session, url, {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "t", "version": "1"}},
            })
            assert status == 200 and msg["result"]["serverInfo"]["name"] == "mock-tavily", (status, msg)

            # 2. tools/list (SSE): заблокированные тулзы скрыты
            status, msg = await rpc(session, url, {
                "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            names = [t["name"] for t in msg["result"]["tools"]]
            assert status == 200, status
            assert names == ["tavily_search", "tavily_extract"], names

            # 3. tools/list (JSON-ответ от upstream): тоже фильтруется
            status, msg = await rpc(session, url, {
                "jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
                extra_headers={"X-Mock-Json": "1"})
            names = [t["name"] for t in msg["result"]["tools"]]
            assert status == 200 and names == ["tavily_search", "tavily_extract"], (status, names)

            # 4. вызов tavily_research блокируется, upstream не дергается
            before = upstream_state["calls"]
            status, msg = await rpc(session, url, {
                "jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "tavily_research", "arguments": {"input": "x"}}})
            assert status == 200, status
            assert "error" in msg and "result" not in msg, msg
            assert "tavily_research" in msg["error"]["message"], msg
            assert upstream_state["calls"] == before

            # 5. вызов tavily_crawl блокируется
            status, msg = await rpc(session, url, {
                "jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {"name": "tavily_crawl", "arguments": {"url": "x"}}})
            assert status == 200 and "error" in msg, (status, msg)
            assert upstream_state["calls"] == before

            # 6. разрешенный тулый проходит до upstream
            status, msg = await rpc(session, url, {
                "jsonrpc": "2.0", "id": 6, "method": "tools/call",
                "params": {"name": "tavily_search", "arguments": {"query": "x"}}})
            assert status == 200 and msg["result"]["content"][0]["text"] == "upstream-ok", (status, msg)
            assert upstream_state["calls"] == before + 1

            # 7. блокировка работает и без application/json в Accept (ответ как SSE)
            headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
            async with session.post(url, headers=headers, json={
                "jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {"name": "tavily_research", "arguments": {}},
            }) as resp:
                assert resp.status == 200
                assert "text/event-stream" in resp.headers.get("Content-Type", "")
                text = await resp.text()
                assert '"error"' in text and "tavily_research" in text, text

            # 8. неверный accessKey -> 401
            async with session.post(
                "http://127.0.0.1:%d/mcp/?accessKey=wrong" % proxy_port,
                json={"jsonrpc": "2.0", "id": 8, "method": "tools/list", "params": {}},
            ) as resp:
                assert resp.status == 401, resp.status
    finally:
        await backend_runner.cleanup()
        await mock_runner.cleanup()

    print("OK: all checks passed")


if __name__ == "__main__":
    asyncio.run(main())
