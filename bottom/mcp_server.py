"""MCP server: every Bottom tool for Claude Code (or any MCP client) over stdio.

    python -m bottom.mcp_server

Claude Code starts it from claude_workspace/.mcp.json, so "connect everything" is one entry: exchanges, OI,
spreads, transfers, on-chain flows, the parsers' signals (read from their Telegram group), Telegram channels (read from the collector's db),
X and web search. JSON-RPC 2.0, one message per line; logs go to stderr only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

log = logging.getLogger("bottom.mcp")


class Server:
    def __init__(self, toolbox):
        self.toolbox = toolbox
        self._out = threading.Lock()

    def send(self, msg: dict) -> None:
        line = json.dumps(msg, ensure_ascii=False)
        with self._out:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def tools_list(self) -> list[dict]:
        return [{"name": t.name, "description": t.description, "inputSchema": t.parameters}
                for t in self.toolbox.tools.values()]

    async def handle(self, msg: dict) -> dict | None:
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:  # notification (initialized, cancelled, ...)
            return None
        try:
            if method == "initialize":
                want = params.get("protocolVersion")
                result = {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "bottom", "version": "0.2.0"},
                          "instructions": "Инструменты крипто-ресерчера Bottom: биржи, деривативы, OI, спреды, "
                                          "депозиты/выводы, ончейн-потоки, сигналы парсеров, Telegram, X, интернет."}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self.tools_list()}
            elif method == "tools/call":
                name, args = params.get("name"), params.get("arguments") or {}
                if name not in self.toolbox.tools:
                    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {name}"}}
                text = await self.toolbox.execute(name, args)
                result = {"content": [{"type": "text", "text": text}], "isError": text.startswith('{"error"')}
            elif method in ("resources/list", "prompts/list"):
                result = {method.split("/")[0]: []}
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
        except Exception as e:  # noqa: BLE001
            log.exception("request %s failed", method)
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)[:300]}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    async def serve(self, reader=None) -> None:
        loop = asyncio.get_running_loop()
        reader = reader or sys.stdin
        pending: set[asyncio.Task] = set()
        while True:
            line = await loop.run_in_executor(None, reader.readline)
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                continue

            async def run(m=msg):
                resp = await self.handle(m)
                if resp is not None:
                    self.send(resp)

            t = asyncio.create_task(run())  # tool calls run concurrently
            pending.add(t)
            t.add_done_callback(pending.discard)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


def build_toolbox():
    from .config import load_settings
    from .learning import Journal
    from .tools.registry import Toolbox
    from .vision import make_vision

    s = load_settings()
    tg_store = None
    if os.path.exists(s.db_path):
        try:
            from .tg.store import TgStore

            tg_store = TgStore(s.db_path)
        except Exception as e:  # noqa: BLE001
            log.info("telegram store unavailable: %s", e)
    vision = make_vision(s)
    return Toolbox(s, tg_store, vision=vision if vision.enabled else None, journal=Journal(s.db_path))


async def amain() -> None:
    toolbox = build_toolbox()
    try:
        await Server(toolbox).serve()
    finally:
        await toolbox.close()
        from .tools import http

        await http.close()


def main() -> None:
    os.chdir(ROOT)  # .env, data/ and bottom.db are relative to the project root
    sys.path.insert(0, ROOT)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(amain())


if __name__ == "__main__":
    main()
