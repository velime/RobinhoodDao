"""One entry point for everything:

    python -m bottom                       Telegram bot
    python -m bottom check                 check .env and every data source
    python -m bottom tg login|scan|collect|search
                                           read Telegram channels through your account
    python -m bottom selftrain [--agy] [result.json ...]
                                           one-time self-training over all the history
    python -m bottom reference import|evaluate|stats|bundle ...
                                           the reference caller's calls
    python -m bottom claude setup|live     prepare claude_workspace/ for Claude Code
    python -m bottom mcp                   every tool as an MCP server (stdio)
"""

from __future__ import annotations

import importlib
import sys

COMMANDS = {
    "check": "bottom.check",
    "tg": "bottom.tg.__main__",
    "selftrain": "bottom.learning.selftrain",
    "reference": "bottom.learning.reference",
    "claude": "bottom.llm.claude_code",
    "mcp": "bottom.mcp_server",
}


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return
    if cmd not in COMMANDS:
        if cmd:
            sys.exit(f"неизвестная команда {cmd!r}\n{__doc__}")
        from .app import run

        run()
        return
    sys.argv = [f"bottom {cmd}", *sys.argv[2:]]
    importlib.import_module(COMMANDS[cmd]).main()


main()
