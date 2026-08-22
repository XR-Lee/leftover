"""Markdown -> Telegram HTML, plus code-fence-aware message splitting."""
from __future__ import annotations

import html
import re

TELEGRAM_LIMIT = 4096
SAFE_CHUNK = 3500

_FENCE = re.compile(r"```([\w+-]*)\n(.*?)(?:```|\Z)", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
_HEADING = re.compile(r"^#{1,6}\s*(.+)$", re.MULTILINE)


def to_html(text: str) -> str:
    """Convert a useful subset of Markdown to Telegram-flavoured HTML."""
    out: list[str] = []
    pos = 0
    for m in _FENCE.finditer(text):
        out.append(_inline(text[pos:m.start()]))
        lang, body = m.group(1), m.group(2)
        attr = f' class="language-{html.escape(lang)}"' if lang else ""
        out.append(f"<pre><code{attr}>{html.escape(body)}</code></pre>")
        pos = m.end()
    out.append(_inline(text[pos:]))
    return "".join(out)


def _inline(text: str) -> str:
    text = html.escape(text)
    text = _HEADING.sub(r"<b>\1</b>", text)
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _INLINE_CODE.sub(r"<code>\1</code>", text)
    return text


def split(text: str, limit: int = SAFE_CHUNK) -> list[str]:
    """Split into Telegram-sized pieces without cutting a code fence in half."""
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    in_fence = False

    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if size + len(line) > limit and buf:
            if in_fence:
                buf.append("```\n")
            chunks.append("".join(buf))
            buf = ["```\n"] if in_fence else []
            size = sum(len(x) for x in buf)
        while len(line) > limit:                  # single monstrous line
            chunks.append(line[:limit])
            line = line[limit:]
        buf.append(line)
        size += len(line)

    if buf:
        chunks.append("".join(buf))
    return [c for c in chunks if c.strip()]


def header(emoji: str, label: str, note: str = "") -> str:
    tail = f" <i>{html.escape(note)}</i>" if note else ""
    return f"<b>{html.escape(emoji)} {html.escape(label)}</b>{tail}"
