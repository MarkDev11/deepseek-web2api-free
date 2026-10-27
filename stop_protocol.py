"""Stop protocol shared with the dsh-plugin adapter (parity layer).

The web upstream has no native stop semantics for agent loops: it sometimes
ends a turn with a half-finished sentence instead of a tool call. The dsh-plugin
solved this with an explicit protocol - every reply must end with either a
tool block or the token [done]; without one the server keeps asking, up to a
cap. server.py applies the same rules here so the hosted proxy behaves like
the plugin.
"""
from __future__ import annotations

import os
import re

STOP_PROTOCOL_TEXT = (
    "STOP PROTOCOL: If any action is still needed, emit the complete tool block. "
    "If everything for this request is truly finished, end your final reply with "
    "the exact token [done]. Never stop without one of the two."
)

REMINDER_TEXT = (
    "Reminder: this reply MUST end with a tool block or the exact token [done]; "
    "you stopped without either. If an action is still needed, reply with ONLY "
    "the complete tool block now. If everything is finished, reply [done]."
)

DONE_RE = re.compile(r"\[\s*done\s*\]", re.I)

# suffixes that could still grow into "[done]" across stream deltas
_PARTIAL = re.compile(r"\[\s*(?:d(?:o(?:n(?:e)?)?)?)?\s*$", re.I)


def auto_continue_max() -> int:
    return int(os.environ.get("PROXY_AUTO_CONTINUE_MAX") or 20)


def has_done(text: str) -> bool:
    return bool(DONE_RE.search(text or ""))


def strip_done(text: str) -> str:
    return DONE_RE.sub(" ", text or "").strip()


def scrub_stream(text: str, pending: str) -> tuple[str, str]:
    """Split text into (emit, held) so '[done]' never reaches the client,
    even when it arrives split across deltas."""
    buf = (pending or "") + (text or "")
    buf = DONE_RE.sub(" ", buf)
    m = _PARTIAL.search(buf)
    if m and (len(buf) - m.start()) <= 8:
        return buf[: m.start()], buf[m.start():]
    return buf, ""
