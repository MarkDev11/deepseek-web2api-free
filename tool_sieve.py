"""
StreamSieve — streaming sieve engine

Mendeteksi tag pemanggilan alat DSML secara real-time dari aliran SSE dan
memisahkan teks biasa dari panggilan alat.

v2.2.0 fixes:
* 'feed()' tidak lagi rekursif pada ekor; memakai loop iteratif.
* '_capture_buf' punya batas keras (dapat diatur via DSML_MAX_BUFFER_BYTES)
  agar keluaran model yang rusak tidak menguras memori.

v3.3.20 — tag longgar (sejajar dengan dsh-plugin/src/dsml.js; memperbaiki DSML
yang bocor ke teks). Deteksi memakai regex, bukan string literal, sehingga:
* pipa fullwidth ｜, pipa ASCII |, pipa yang hilang, spasi di dalam tag, dan
  alias function_calls/calls untuk tool_calls ikut tertangkap;
* teks setelah blok tertutup dikembalikan utuh (dulu terbuang bersama blok);
* flush() mencoba memarse capture yang belum tertutup (model lupa tag tutup
  tetap berupa panggilan alat), lalu membersihkan sisa tag sebelum teks
  dilepas — marker DSML tidak bisa lagi muncul sebagai prosa.
"""
import os
import re
from dataclasses import dataclass
from typing import Any, Callable


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(1024, int(raw))  # never allow less than 1 KiB
    except ValueError:
        return default


MAX_CAPTURE_BUFFER = _env_int("DSML_MAX_BUFFER_BYTES", 1024 * 1024)  # 1 MiB

# --- Regex tag longgar (lihat docstring v3.3.20) ---
_PIPE = r"[|\uff5c]"
_NAMES = r"tool_calls|tool_call|function_calls|calls|invoke|parameter"
_WRAPPERS = r"tool_calls|tool_call|function_calls|calls"

_OPEN_RE = re.compile(
    r"<\s*" + _PIPE + r"{0,2}\s*DSML\s*" + _PIPE + r"{0,2}\s*(?:" + _NAMES + r")", re.IGNORECASE)
_OPEN_BARE_PIPE_RE = re.compile(
    r"(?<![|\uff5c/])[|\uff5c]{1,2}\s*DSML\s*" + _PIPE + r"{0,2}\s*(?:" + _NAMES + r")", re.IGNORECASE)
_OPEN_PLAIN_RE = re.compile(r"<\s*(?:" + _NAMES + r")\b", re.IGNORECASE)

_OPEN_INV_RE = re.compile(
    r"(?:<\s*" + _PIPE + r"{0,2}\s*DSML\s*" + _PIPE + r"{0,2}|(?<![|\uff5c/])[|\uff5c]{1,2}\s*DSML\s*" + _PIPE + r"{0,2})\s*invoke",
    re.IGNORECASE)
_OPEN_INV_PLAIN_RE = re.compile(r"<\s*invoke\b", re.IGNORECASE)
_CLOSE_INV_RE = re.compile(
    r"(?:</\s*(?:" + _PIPE + r"{0,2}\s*DSML\s*" + _PIPE + r"{0,2}\s*)?|(?<![|\uff5c/</])[|\uff5c]{1,2}\s*DSML\s*" + _PIPE + r"{0,2}\s*)invoke\s*>",
    re.IGNORECASE)
_OPEN_WRAP_RE = re.compile(
    r"<\s*" + _PIPE + r"{0,2}\s*DSML\s*" + _PIPE + r"{0,2}\s*(?:" + _WRAPPERS + r")"
    r"|(?<![|\uff5c/])[|\uff5c]{1,2}\s*DSML\s*" + _PIPE + r"{0,2}\s*(?:" + _WRAPPERS + r")"
    r"|<\s*(?:" + _WRAPPERS + r")\b",
    re.IGNORECASE)
_CLOSE_WRAP_RE = re.compile(
    r"</\s*(?:" + _PIPE + r"{0,2}\s*DSML\s*" + _PIPE + r"{0,2}\s*)?(?:" + _WRAPPERS + r")\s*>"
    r"|(?<![|\uff5c/</])[|\uff5c]{1,2}\s*DSML\s*" + _PIPE + r"{0,2}\s*(?:" + _WRAPPERS + r")\s*>",
    re.IGNORECASE)
_RESIDUE_RE = re.compile(
    r"</?\s*" + _PIPE + r"{0,2}\s*DSML\s*" + _PIPE + r"{0,2}\s*(?:" + _NAMES + r")(?:[^>\"']|\"[^\"]*\"|'[^']*')*>?"
    r"|(?<![</|\uff5c])/?\s*[|\uff5c]{1,2}\s*DSML\s*" + _PIPE + r"{0,2}\s*(?:" + _NAMES + r")(?:[^>\"']|\"[^\"]*\"|'[^']*')*>?",
    re.IGNORECASE)

# Sisa teks yang masih bisa tumbuh menjadi tag pembuka (untuk menahan ekor).
_NAME_PREFIXES = set()
for _n in ("tool_calls", "tool_call", "function_calls", "calls", "invoke", "parameter", "dsml"):
    for _k in range(len(_n) + 1):
        _NAME_PREFIXES.add(_n[:_k].upper())

_PARTIAL_RE = re.compile(
    r"^<?\s*(?:(?:[|｜])\s*){0,2}(DSML)?\s*(?:(?:[|｜])\s*){0,2}\s*([A-Za-z_]*)$", re.IGNORECASE)


def _could_open(tail: str) -> bool:
    m = _PARTIAL_RE.match(tail)
    if not m:
        return False
    return (m.group(2) or "").upper() in _NAME_PREFIXES


@dataclass
class SieveEvent:
    type: str  # 'text' | 'tool_calls'
    data: Any


class StreamSieve:
    """Real-time sieve for DSML tool calls inside an SSE stream (loose tags)."""

    def __init__(self, parse_fn: Callable | None = None,
                 max_capture_buffer: int | None = None):
        self.parse_fn = parse_fn
        self._pending = ""
        self._capture_buf = ""
        self._capturing = False
        self._max_capture_buffer = (
            max_capture_buffer if max_capture_buffer is not None
            else MAX_CAPTURE_BUFFER
        )

    # ---------------- public API ----------------

    def feed(self, chunk: str) -> list[SieveEvent]:
        events: list[SieveEvent] = []
        if not chunk:
            return events
        self._pending += chunk
        self._drain(events)
        return events

    def flush(self) -> list[SieveEvent]:
        events: list[SieveEvent] = []
        if self._capturing:
            result = self._try_finish_capture()
            if result is None and self.parse_fn and self._capture_buf:
                # Blok belum tertutup; model sering lupa tag penutup, tapi
                # isinya tetap panggilan alat yang sah — coba parse dulu.
                try:
                    tcs, _cleaned = self.parse_fn(self._capture_buf)
                except Exception:
                    tcs = []
                if tcs:
                    events.append(SieveEvent("tool_calls", tcs))
                    result = ("", None, "")
            if result is None:
                if self._capture_buf:
                    events.append(SieveEvent("text", self._strip(self._capture_buf)))
            else:
                prefix, tool_calls, suffix = result
                if tool_calls:
                    events.append(SieveEvent("tool_calls", tool_calls))
                if prefix:
                    events.append(SieveEvent("text", self._strip(prefix)))
                if suffix:
                    events.append(SieveEvent("text", self._strip(suffix)))
            self._capture_buf = ""
            self._capturing = False
        if self._pending:
            events.append(SieveEvent("text", self._strip(self._pending)))
            self._pending = ""
        return events

    # ---------------- internals ----------------

    @staticmethod
    def _strip(text: str) -> str:
        return _RESIDUE_RE.sub("", text)

    def _drain(self, events: list[SieveEvent]) -> None:
        while self._pending:
            if self._capturing:
                self._capture_buf += self._pending
                self._pending = ""
                if len(self._capture_buf) > self._max_capture_buffer:
                    events.append(SieveEvent("text", self._strip(self._capture_buf)))
                    self._capture_buf = ""
                    self._capturing = False
                    return
                result = self._try_finish_capture()
                if result is None:
                    return
                prefix, tool_calls, suffix = result
                self._capture_buf = ""
                self._capturing = False
                if prefix:
                    events.append(SieveEvent("text", self._strip(prefix)))
                if tool_calls:
                    events.append(SieveEvent("tool_calls", tool_calls))
                if suffix:
                    self._pending = suffix
                continue
            start_idx = self._find_real_start(self._pending)
            if start_idx >= 0:
                if start_idx:
                    events.append(SieveEvent("text", self._strip(self._pending[:start_idx])))
                self._capture_buf = self._pending[start_idx:]
                self._pending = ""
                self._capturing = True
                result = self._try_finish_capture()
                if result is not None:
                    prefix, tool_calls, suffix = result
                    self._capture_buf = ""
                    self._capturing = False
                    if prefix:
                        events.append(SieveEvent("text", self._strip(prefix)))
                    if tool_calls:
                        events.append(SieveEvent("tool_calls", tool_calls))
                    if suffix:
                        self._pending = suffix
                    continue
                return
            safe, hold = self._split_safe(self._pending)
            if safe:
                events.append(SieveEvent("text", self._strip(safe)))
            self._pending = hold
            return

    @staticmethod
    def _find_real_start(text: str) -> int:
        best = -1
        for rx in (_OPEN_RE, _OPEN_BARE_PIPE_RE, _OPEN_PLAIN_RE):
            m = rx.search(text)
            if m and (best < 0 or m.start() < best):
                best = m.start()
        return best

    @staticmethod
    def _split_safe(text: str) -> tuple[str, str]:
        """Return (safe, hold) with safe+hold == text; hold is the smallest
        tail that could still grow into a DSML tool-call opening tag."""
        if not text:
            return "", ""
        lo = max(0, len(text) - 64)
        for i in range(lo, len(text)):
            if text[i] in "<|" and _could_open(text[i:i + 48]):
                return text[:i], text[i:]
        return text, ""

    def _try_finish_capture(self):
        if not self._capture_buf or not self.parse_fn:
            return None
        if not self._is_capture_complete():
            return None
        cut = self._capture_cut()
        head = self._capture_buf[:cut] if cut > 0 else self._capture_buf
        tail = self._capture_buf[cut:] if cut > 0 else ""
        tool_calls, _cleaned = self.parse_fn(head)
        if tool_calls:
            return ("", tool_calls, tail)
        return (self._capture_buf, None, "")

    def _capture_cut(self) -> int:
        buf = self._capture_buf
        cut = 0
        last_wrap = None
        for m in _CLOSE_WRAP_RE.finditer(buf):
            last_wrap = m
        if last_wrap:
            cut = last_wrap.end()
        last_inv = None
        for m in _CLOSE_INV_RE.finditer(buf):
            last_inv = m
        if last_inv and last_inv.end() > cut:
            cut = last_inv.end()
        return cut

    def _is_capture_complete(self) -> bool:
        buf = self._capture_buf
        # Blok ber-wrapper tunggu penutup wrapper — sama seperti expectFc pada
        # plugin. Menutup lebih awal membuat potongan '</…calls>' yang terpotong
        # antar-chunk lolos sebagai teks. Blok invoke tanpa wrapper cukup
        # penutup invoke yang seimbang; yang tak tertutup diparse saat flush.
        if _OPEN_WRAP_RE.search(buf):
            return bool(_CLOSE_WRAP_RE.search(buf))
        opens = len(_OPEN_INV_RE.findall(buf)) + len(_OPEN_INV_PLAIN_RE.findall(buf))
        if opens:
            closes = len(_CLOSE_INV_RE.findall(buf))
            if closes >= opens:
                return True
        return False
