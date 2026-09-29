"""Convert the model's light markup to Telegram HTML and split long messages.

Supported markup (see the system prompt):
  **bold**, `code`, lines "> " → quote, lines ">! " → expandable quote,
  "- " / "* " bullets → "• ", "# heading" → bold line.
"""

from __future__ import annotations

import html
import re

TG_LIMIT = 4096
CHUNK = 3900

_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_CODE = re.compile(r"`([^`\n]+)`")


def _inline(text: str) -> str:
    t = html.escape(text, quote=False)
    t = _CODE.sub(r"<code>\1</code>", t)
    t = _BOLD.sub(r"<b>\1</b>", t)
    return t


def _line(raw: str) -> tuple[str, str]:
    """Return (kind, content): kind in {'text', 'quote', 'xquote'}."""
    s = raw.rstrip()
    if s.startswith(">!"):
        return "xquote", s[2:].lstrip()
    if s.startswith(">"):
        return "quote", s[1:].lstrip()
    return "text", s


def _text_line(s: str) -> str:
    m = re.match(r"^\s*#{1,6}\s+(.*)$", s)
    if m:
        return f"<b>{_inline(m.group(1).strip('* '))}</b>"
    m = re.match(r"^(\s*)[-*]\s+(.*)$", s)
    if m:
        return f"{m.group(1)}• {_inline(m.group(2))}"
    return _inline(s)


def to_html_blocks(text: str) -> list[str]:
    """Convert to a list of self-contained HTML blocks (safe to split between)."""
    blocks: list[str] = []
    buf: list[str] = []
    qkind: str | None = None
    qbuf: list[str] = []

    def flush_text():
        if buf:
            blocks.append("\n".join(buf))
            buf.clear()

    def flush_quote():
        nonlocal qkind
        if qbuf:
            tag = "<blockquote expandable>" if qkind == "xquote" else "<blockquote>"
            blocks.append(tag + "\n".join(_text_line(x) for x in qbuf) + "</blockquote>")
            qbuf.clear()
        qkind = None

    for raw in text.strip().splitlines():
        kind, content = _line(raw)
        if kind == "text":
            flush_quote()
            if not content.strip():
                flush_text()
                continue
            buf.append(_text_line(content))
        else:
            flush_text()
            if qkind not in (None, kind):
                flush_quote()
            qkind = kind
            qbuf.append(content)
    flush_text()
    flush_quote()
    return blocks


def to_telegram_chunks(text: str, limit: int = CHUNK) -> list[str]:
    chunks: list[str] = []
    cur = ""
    for b in to_html_blocks(text):
        if len(b) > limit:  # oversized block: hard split on lines, tags stay per line
            for piece in _hard_split(b, limit):
                if cur:
                    chunks.append(cur)
                    cur = ""
                chunks.append(piece)
            continue
        candidate = f"{cur}\n\n{b}" if cur else b
        if len(candidate) > limit:
            chunks.append(cur)
            cur = b
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks or [""]


def _hard_split(block: str, limit: int) -> list[str]:
    plain = re.sub(r"</?blockquote[^>]*>", "", block)
    out, cur = [], ""
    for line in plain.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = line[:limit]
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


def strip_markup(text: str) -> str:
    """Plain-text fallback if Telegram rejects the HTML."""
    t = _BOLD.sub(r"\1", text)
    t = re.sub(r"^>!?\s?", "", t, flags=re.M)
    return t
