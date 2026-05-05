#!/usr/bin/env python3
"""LLM proxy with live token/byte stats and slash commands.

Usage:
    python llm_proxy.py --host 192.168.1.10 [--to 11434] [--bind 0.0.0.0:11434]

In-program commands (type at the > prompt):
    /help                show commands
    /model <name>        rewrite the "model" field in outgoing JSON requests
    /model               clear model override
    /server <host[:port]>  change upstream destination
    /quit                exit
"""

import argparse
import asyncio
import collections
import json
import os
import re
import shutil
import sys
import threading
import time
from typing import Optional

IS_WIN = os.name == "nt"

# --------------------------------------------------------------------------- #
# Terminal setup
# --------------------------------------------------------------------------- #

def enable_vt_mode() -> None:
    if not IS_WIN:
        return
    import ctypes
    k = ctypes.windll.kernel32
    h = k.GetStdHandle(-11)  # STD_OUTPUT
    mode = ctypes.c_uint32()
    if k.GetConsoleMode(h, ctypes.byref(mode)):
        k.SetConsoleMode(h, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    h2 = k.GetStdHandle(-10)  # STD_INPUT
    if k.GetConsoleMode(h2, ctypes.byref(mode)):
        k.SetConsoleMode(h2, mode.value | 0x0200)  # ENABLE_VIRTUAL_TERMINAL_INPUT


def term_size():
    sz = shutil.get_terminal_size((100, 30))
    return sz.columns, sz.lines


# ANSI helpers
ESC = "\x1b["
SAVE = "\x1b7"
RESTORE = "\x1b8"
CLEAR_LINE = ESC + "2K"
HIDE_CURSOR = ESC + "?25l"
SHOW_CURSOR = ESC + "?25h"


def at(row: int, col: int = 1) -> str:
    return f"{ESC}{row};{col}H"


# tag → ANSI color. Anything not listed renders in default terminal color.
TAG_COLORS = {
    "conn": "\x1b[90m",          # bright black / grey
    "asst": "\x1b[32m",          # green
    "req-stats": "\x1b[32m",     # green
    "user": "\x1b[38;5;208m",    # 256-color orange
}
RESET = "\x1b[0m"


def _colorize(line: str) -> str:
    if not line.startswith("["):
        return line
    end = line.find("]")
    if end < 0:
        return line
    tag = line[1:end]
    color = TAG_COLORS.get(tag)
    if not color:
        return line
    return color + line + RESET


def set_scroll_region(top: int, bottom: int) -> str:
    return f"{ESC}{top};{bottom}r"


def reset_scroll_region() -> str:
    return f"{ESC}r"


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #

WINDOW_SECONDS = 300  # 5 minutes


class Stats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.bytes_in = 0   # client -> server
        self.bytes_out = 0  # server -> client
        self.tokens_in = 0
        self.tokens_out = 0
        # per-second buckets: deque of (epoch_sec, bytes_total, tokens_total)
        self.byte_hist = collections.deque(maxlen=WINDOW_SECONDS)
        self.tok_hist = collections.deque(maxlen=WINDOW_SECONDS)
        self._last_bytes = 0
        self._last_tokens = 0

    def add_bytes(self, n_in: int, n_out: int) -> None:
        with self.lock:
            self.bytes_in += n_in
            self.bytes_out += n_out

    def add_tokens(self, t_in: int, t_out: int) -> None:
        with self.lock:
            self.tokens_in += t_in
            self.tokens_out += t_out

    def tick(self) -> None:
        with self.lock:
            now = int(time.time())
            total_bytes = self.bytes_in + self.bytes_out
            total_tokens = self.tokens_in + self.tokens_out
            d_bytes = total_bytes - self._last_bytes
            d_toks = total_tokens - self._last_tokens
            self._last_bytes = total_bytes
            self._last_tokens = total_tokens
            self.byte_hist.append((now, d_bytes))
            self.tok_hist.append((now, d_toks))

    def snapshot(self):
        with self.lock:
            return (
                self.bytes_in,
                self.bytes_out,
                self.tokens_in,
                self.tokens_out,
                list(self.byte_hist),
                list(self.tok_hist),
            )


STATS = Stats()


class RequestInfo:
    """Last-seen request metadata + in-flight assistant response buffer."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.model = ""
        self.num_ctx: Optional[int] = None
        self.num_predict: Optional[int] = None
        self.temperature: Optional[float] = None
        self.endpoint = ""
        self.streaming = False
        self.partial = ""        # accumulating delta text (full message so far)
        self.log_cb = None       # set by main: callable(str) -> None
        self.req_body_bytes = 0
        self.req_start_time: Optional[float] = None
        # display toggles — defaults: hide outgoing prompt body, show responses
        self.show_from = False
        self.show_to = True

    def set_show_from(self, v: bool) -> None:
        with self.lock:
            self.show_from = v

    def set_show_to(self, v: bool) -> None:
        with self.lock:
            self.show_to = v

    def get_toggles(self):
        with self.lock:
            return self.show_from, self.show_to

    def set_log(self, cb) -> None:
        with self.lock:
            self.log_cb = cb

    def update_request(self, obj, endpoint: str, body_bytes: int = 0) -> None:
        with self.lock:
            # reset every per-request field — Ollama clients change these call to call.
            # Anything not present in this body shows as "(default)" in the UI.
            self.endpoint = endpoint
            self.model = ""
            self.num_ctx = None
            self.num_predict = None
            self.temperature = None
            self.streaming = False
            self.partial = ""
            self.req_body_bytes = body_bytes
            self.req_start_time = time.time()
            if not isinstance(obj, dict):
                return
            m = obj.get("model")
            if isinstance(m, str):
                self.model = m
            # Ollama nests these under "options"; OpenAI/llama.cpp use top-level
            opts = obj.get("options") if isinstance(obj.get("options"), dict) else {}
            ctx = opts.get("num_ctx")
            if not isinstance(ctx, int):
                ctx = obj.get("n_ctx")  # llama.cpp /completion
            if isinstance(ctx, int):
                self.num_ctx = ctx
            np = opts.get("num_predict")
            if not isinstance(np, int):
                np = obj.get("n_predict")  # llama.cpp
            if isinstance(np, int):
                self.num_predict = np
            mt = obj.get("max_tokens") or obj.get("max_completion_tokens")  # OpenAI
            if isinstance(mt, int):
                self.num_predict = mt
            t = opts.get("temperature")
            if not isinstance(t, (int, float)):
                t = obj.get("temperature")
            if isinstance(t, (int, float)):
                self.temperature = float(t)

    def log_request(self, obj) -> None:
        """Dump the outgoing request to the scroll log."""
        with self.lock:
            cb = self.log_cb
            show_from = self.show_from
        if not cb or not isinstance(obj, dict):
            return
        # the one-line [req] header is always shown — it just marks that a request
        # happened. The prompt bodies below are gated by /showfrom.
        bits = []
        if self.endpoint:
            bits.append(self.endpoint)
        if self.model:
            bits.append(f"model={self.model}")
        if self.num_ctx is not None:
            bits.append(f"ctx={self.num_ctx}")
        if self.num_predict is not None:
            bits.append(f"max={self.num_predict}")
        if self.temperature is not None:
            bits.append(f"temp={self.temperature}")
        if obj.get("stream") is False:
            bits.append("stream=false")
        cb("[req] " + " ".join(bits))
        if not show_from:
            # one-line summary instead of dumping the prompt
            in_text = _extract_input_text(obj)
            est = estimate_tokens(in_text)
            msgs = obj.get("messages")
            n_msgs = len(msgs) if isinstance(msgs, list) else 0
            sys_field = obj.get("system")
            sys_chars = 0
            if isinstance(sys_field, str):
                sys_chars = len(sys_field)
            elif isinstance(sys_field, list):
                for blk in sys_field:
                    if isinstance(blk, dict) and isinstance(blk.get("text"), str):
                        sys_chars += len(blk["text"])
            parts = [f"body={fmt_bytes(self.req_body_bytes)}", f"~{est}t in"]
            if n_msgs:
                parts.append(f"msgs={n_msgs}")
            if sys_chars:
                parts.append(f"system={sys_chars}c")
            cb("[req-stats] " + "  ".join(parts))
            return

        def emit(role: str, text: str) -> None:
            if not text:
                return
            for ln in text.splitlines() or [""]:
                cb(f"[{role}] {ln}")

        # Anthropic / Ollama / OpenAI all use "system" + "messages"
        sys_field = obj.get("system")
        if isinstance(sys_field, str):
            emit("system", sys_field)
        elif isinstance(sys_field, list):
            for blk in sys_field:
                if isinstance(blk, dict) and isinstance(blk.get("text"), str):
                    emit("system", blk["text"])

        msgs = obj.get("messages")
        if isinstance(msgs, list):
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                role = m.get("role") or "msg"
                c = m.get("content")
                if isinstance(c, str):
                    emit(role, c)
                elif isinstance(c, list):
                    for blk in c:
                        if not isinstance(blk, dict):
                            continue
                        t = blk.get("text")
                        if isinstance(t, str):
                            emit(role, t)
                        elif blk.get("type") == "image":
                            cb(f"[{role}] <image>")
                # tool calls / images attached at message level
                imgs = m.get("images")
                if isinstance(imgs, list) and imgs:
                    cb(f"[{role}] <{len(imgs)} image(s)>")

        # Ollama /api/generate and llama.cpp /completion
        prompt = obj.get("prompt")
        if isinstance(prompt, str):
            emit("prompt", prompt)

    def append_delta(self, text: str) -> None:
        with self.lock:
            self.streaming = True
            self.partial += text

    def mark_done(self, summary: str) -> None:
        with self.lock:
            cb = self.log_cb
            full = self.partial
            show_to = self.show_to
            start = self.req_start_time
            self.streaming = False
            self.partial = ""
        if not cb:
            return
        if full and show_to:
            # split multi-line assistant output into separate log lines
            for ln in full.splitlines() or [""]:
                cb(f"[asst] {ln}")
        elif full and not show_to:
            # one-line summary instead of dumping the response
            body_bytes = len(full.encode("utf-8"))
            est = estimate_tokens(full)
            dur = (time.time() - start) if start else 0.0
            tps = (est / dur) if dur > 0 else 0.0
            parts = [
                f"body={fmt_bytes(body_bytes)}",
                f"~{est}t out",
                f"dur={dur:.1f}s",
                f"tps={tps:.1f}",
            ]
            cb("[resp-stats] " + "  ".join(parts))
        if summary:
            cb(f"[done] {summary}")

    def snapshot(self):
        with self.lock:
            return {
                "model": self.model,
                "num_ctx": self.num_ctx,
                "num_predict": self.num_predict,
                "temperature": self.temperature,
                "endpoint": self.endpoint,
                "streaming": self.streaming,
                "partial": self.partial,
            }


REQ_INFO = RequestInfo()


# --------------------------------------------------------------------------- #
# Sparkline
# --------------------------------------------------------------------------- #

SPARK_CHARS = " ▁▂▃▄▅▆▇█"


def sparkline(values, width: int) -> str:
    if width <= 0:
        return ""
    if not values:
        return " " * width
    # bucket values into `width` columns (averaging)
    n = len(values)
    if n >= width:
        bucket = n / width
        out = []
        for i in range(width):
            lo = int(i * bucket)
            hi = max(lo + 1, int((i + 1) * bucket))
            chunk = values[lo:hi]
            out.append(sum(chunk) / len(chunk) if chunk else 0)
    else:
        # left-pad with zeros
        out = [0.0] * (width - n) + list(values)
    mx = max(out) if out else 0
    if mx <= 0:
        return " " * width
    s = []
    for v in out:
        idx = min(len(SPARK_CHARS) - 1, int(v / mx * (len(SPARK_CHARS) - 1)))
        s.append(SPARK_CHARS[idx])
    return "".join(s)


# --------------------------------------------------------------------------- #
# Renderer
# --------------------------------------------------------------------------- #

RESERVED_BOTTOM = 6  # sep+footer, req-info, response-preview, tok/s, B/s, input


class Renderer:
    def __init__(self) -> None:
        self.cols, self.rows = term_size()
        self.input_buf = ""
        self.message = ""  # transient command feedback
        self.lock = threading.Lock()
        self._setup()

    def _setup(self) -> None:
        sys.stdout.write(HIDE_CURSOR)
        sys.stdout.write("\x1b[2J")  # clear screen
        sys.stdout.write(set_scroll_region(1, max(1, self.rows - RESERVED_BOTTOM)))
        sys.stdout.write(at(1, 1))
        sys.stdout.flush()

    def teardown(self) -> None:
        sys.stdout.write(reset_scroll_region())
        sys.stdout.write(SHOW_CURSOR)
        sys.stdout.write(at(self.rows, 1))
        sys.stdout.write("\n")
        sys.stdout.flush()

    def maybe_resize(self) -> None:
        cols, rows = term_size()
        if (cols, rows) != (self.cols, self.rows):
            self.cols, self.rows = cols, rows
            sys.stdout.write(set_scroll_region(1, max(1, rows - RESERVED_BOTTOM)))
            sys.stdout.flush()

    def print_above(self, line: str) -> None:
        """Print a line into the scroll region above the status bar."""
        colored = _colorize(line)
        with self.lock:
            sys.stdout.write(SAVE)
            # Move into scroll region last visible row, write + newline (causes scroll)
            sys.stdout.write(at(max(1, self.rows - RESERVED_BOTTOM), 1))
            sys.stdout.write("\n" + colored)
            sys.stdout.write(RESTORE)
            sys.stdout.flush()

    def set_message(self, msg: str) -> None:
        self.message = msg

    def redraw_input(self) -> None:
        """Fast partial redraw of just the input row. Called per keystroke."""
        with self.lock:
            cols = self.cols
            rows = self.rows
            line = ("> " + self.input_buf)[:cols]
            sys.stdout.write(SAVE)
            sys.stdout.write(at(rows, 1) + CLEAR_LINE + line)
            sys.stdout.write(RESTORE)
            sys.stdout.flush()

    def draw_status(self, target_host: str, target_port: int, model_override: Optional[str]) -> None:
        with self.lock:
            self.maybe_resize()
            cols = self.cols
            rows = self.rows
            bin_, bout, tin, tout, bhist, thist = STATS.snapshot()

            # rates over last 1s
            byte_rate = bhist[-1][1] if bhist else 0
            tok_rate = thist[-1][1] if thist else 0

            byte_vals = [v for _, v in bhist]
            tok_vals = [v for _, v in thist]
            tok_peak = max(tok_vals) if tok_vals else 0
            byte_peak = max(byte_vals) if byte_vals else 0
            tok_avg = (sum(tok_vals) / len(tok_vals)) if tok_vals else 0
            byte_avg = (sum(byte_vals) / len(byte_vals)) if byte_vals else 0
            window_secs = len(tok_vals)  # seconds of data we have so far

            # fixed-width prefixes so the left │ aligns across both rows
            tok_prefix = f" tok/s {tok_rate:>6}  in {tin:>9}  out {tout:>9} "
            byte_prefix = f" B/s   {fmt_bytes(byte_rate):>6}  in {fmt_bytes(bin_):>9}  out {fmt_bytes(bout):>9} "
            tok_suffix = f" peak {tok_peak:>6}  avg {tok_avg:>6.1f}"
            byte_suffix = f" peak {fmt_bytes(byte_peak):>7}  avg {fmt_bytes(byte_avg):>7}"
            # equalize suffix widths so right edges align
            sw = max(len(tok_suffix), len(byte_suffix))
            tok_suffix = tok_suffix.ljust(sw)
            byte_suffix = byte_suffix.ljust(sw)

            spark_w = max(10, cols - len(tok_prefix) - sw - 2)  # 2 for │ │
            tok_spark = sparkline(tok_vals, spark_w)
            byte_spark = sparkline(byte_vals, spark_w)

            tok_line = (tok_prefix + f"│{tok_spark}│" + tok_suffix)[:cols]
            byte_line = (byte_prefix + f"│{byte_spark}│" + byte_suffix)[:cols]

            # time-axis hint placed inside the sparkline width so the user knows
            # what they're looking at: "5m ago ←─── now". Drawn between footer
            # and req row by piggy-backing on the message? Simpler: show window
            # length as part of the footer.
            window_hint = f"{window_secs:>3}s/{WINDOW_SECONDS}s"

            sep = "─" * cols
            override_str = model_override or "(none)"
            ri = REQ_INFO.snapshot()
            sf, st = REQ_INFO.get_toggles()
            footer = (
                f" → {target_host}:{target_port}   override={override_str}"
                f"   from={'on' if sf else 'off'} to={'on' if st else 'off'}"
                f"   graph={window_hint} (←5m..now→)"
            )
            if self.message:
                footer += "   " + self.message
            footer = footer[:cols]
            sep_line = (sep[: max(0, cols - len(footer))] + footer)[:cols]

            # request info row
            def _fmt(v, default="(default)"):
                return str(v) if v is not None and v != "" else default
            req_row = (
                f" req {ri['endpoint'] or '—':<14}"
                f" model={_fmt(ri['model'], '—'):<22}"
                f" ctx={_fmt(ri['num_ctx']):<7}"
                f" max={_fmt(ri['num_predict']):<7}"
                f" temp={_fmt(ri['temperature']):<5}"
            )[:cols]

            # streaming preview row: last (cols - prefix) chars of in-flight text,
            # newlines compressed to ⏎ so it stays single-line.
            prefix = " rsp │ " if ri["streaming"] else " rsp │ (idle) "
            avail = max(0, cols - len(prefix))
            preview = ri["partial"].replace("\r", "").replace("\n", " ⏎ ")
            if avail > 0 and len(preview) > avail:
                preview = "…" + preview[-(avail - 1):]
            rsp_row = (prefix + preview)[:cols]

            input_line = ("> " + self.input_buf)[:cols]

            sys.stdout.write(SAVE)
            sys.stdout.write(at(rows - 5, 1) + CLEAR_LINE + sep_line)
            sys.stdout.write(at(rows - 4, 1) + CLEAR_LINE + req_row)
            sys.stdout.write(at(rows - 3, 1) + CLEAR_LINE + rsp_row)
            sys.stdout.write(at(rows - 2, 1) + CLEAR_LINE + tok_line)
            sys.stdout.write(at(rows - 1, 1) + CLEAR_LINE + byte_line)
            sys.stdout.write(at(rows, 1) + CLEAR_LINE + input_line)
            sys.stdout.write(RESTORE)
            sys.stdout.flush()


def fmt_bytes(n: int) -> str:
    n = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}P"


# --------------------------------------------------------------------------- #
# HTTP / token sniffing
# --------------------------------------------------------------------------- #

# Rough token estimator: ~4 chars per token.
def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


HTTP_REQ_RE = re.compile(rb"^([A-Z]+) ([^ ]+) HTTP/1\.[01]\r\n")
CONTENT_LENGTH_RE = re.compile(rb"\r\nContent-Length: *(\d+)\r\n", re.IGNORECASE)
CONTENT_TYPE_RE = re.compile(rb"\r\nContent-Type: *([^\r\n]+)", re.IGNORECASE)
TRANSFER_ENC_RE = re.compile(rb"\r\nTransfer-Encoding: *([^\r\n]+)", re.IGNORECASE)


class RequestRewriter:
    """Buffers a client→server stream and rewrites the JSON model field."""

    def __init__(self, get_model_override):
        self.buf = bytearray()
        self.headers_done = False
        self.body_remaining = 0
        self.body_buf = bytearray()
        self.is_json = False
        self.passthrough = False  # once true, just forward
        self.get_model_override = get_model_override

    def feed(self, data: bytes) -> bytes:
        if self.passthrough:
            return data
        out = bytearray()
        self.buf.extend(data)
        while True:
            if not self.headers_done:
                idx = self.buf.find(b"\r\n\r\n")
                if idx < 0:
                    return bytes(out)  # need more
                headers = bytes(self.buf[: idx + 4])
                if not HTTP_REQ_RE.match(headers):
                    # not HTTP - bail to passthrough
                    self.passthrough = True
                    out.extend(self.buf)
                    self.buf.clear()
                    return bytes(out)
                m_cl = CONTENT_LENGTH_RE.search(headers)
                m_ct = CONTENT_TYPE_RE.search(headers)
                m_te = TRANSFER_ENC_RE.search(headers)
                m_req = HTTP_REQ_RE.match(headers)
                self._req_path = m_req.group(2).decode("latin-1") if m_req else ""
                if m_te and b"chunked" in m_te.group(1).lower():
                    # chunked request bodies: don't rewrite
                    self.passthrough = True
                    out.extend(self.buf)
                    self.buf.clear()
                    return bytes(out)
                self.body_remaining = int(m_cl.group(1)) if m_cl else 0
                self.is_json = bool(m_ct and b"json" in m_ct.group(1).lower())
                self._pending_headers = headers
                del self.buf[: idx + 4]
                self.headers_done = True
                self.body_buf.clear()

            need = self.body_remaining - len(self.body_buf)
            take = min(need, len(self.buf))
            if take > 0:
                self.body_buf.extend(self.buf[:take])
                del self.buf[:take]
            if len(self.body_buf) < self.body_remaining:
                return bytes(out)  # need more body

            # full request collected
            headers = self._pending_headers
            body = bytes(self.body_buf)
            override = self.get_model_override()
            if self.is_json and override and body:
                try:
                    obj = json.loads(body)
                    if isinstance(obj, dict) and "model" in obj:
                        obj["model"] = override
                        body = json.dumps(obj).encode("utf-8")
                        # update Content-Length
                        new_cl = f"Content-Length: {len(body)}".encode()
                        headers = re.sub(
                            rb"Content-Length: *\d+",
                            new_cl,
                            headers,
                            count=1,
                            flags=re.IGNORECASE,
                        )
                except Exception:
                    pass

            # token estimate for input + capture request metadata + log it
            if self.is_json and body:
                try:
                    obj = json.loads(body)
                    REQ_INFO.update_request(obj, getattr(self, "_req_path", ""), len(body))
                    REQ_INFO.log_request(obj)
                    in_text = _extract_input_text(obj)
                    if in_text:
                        STATS.add_tokens(estimate_tokens(in_text), 0)
                except Exception:
                    pass

            out.extend(headers)
            out.extend(body)
            # reset for next request on same connection
            self.headers_done = False
            self.body_remaining = 0
            self.body_buf.clear()
            if not self.buf:
                return bytes(out)


def _extract_input_text(obj) -> str:
    """Pull rough input text from OpenAI/Anthropic-style request bodies."""
    if not isinstance(obj, dict):
        return ""
    parts = []
    if isinstance(obj.get("system"), str):
        parts.append(obj["system"])
    msgs = obj.get("messages")
    if isinstance(msgs, list):
        for m in msgs:
            if not isinstance(m, dict):
                continue
            c = m.get("content")
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, list):
                for blk in c:
                    if isinstance(blk, dict) and isinstance(blk.get("text"), str):
                        parts.append(blk["text"])
    if isinstance(obj.get("prompt"), str):
        parts.append(obj["prompt"])
    return "\n".join(parts)


class ResponseSniffer:
    """Watches server→client for SSE/NDJSON/JSON. Handles chunked transfer.

    State machine per HTTP response:
      headers → (chunked decoder | length body | EOF body) → reset on completion.
    """

    MODE_NONE = 0
    MODE_SSE = 1
    MODE_NDJSON = 2
    MODE_JSON = 3

    def __init__(self):
        self.raw = bytearray()           # raw bytes from socket (pre-dechunk)
        self.headers_done = False
        self.chunked = False
        self.body_remaining = -1         # -1 = read until close
        self.body_bytes_seen = 0
        self.mode = self.MODE_NONE
        self.line_buf = bytearray()      # accumulates decoded body lines
        self.full_buf = bytearray()      # accumulates non-streaming JSON body
        self.passthrough = False
        # chunked decoder state
        self.chunk_remaining = -1        # -1 = need to read size line
        self.chunk_size_buf = bytearray()

    def feed(self, data: bytes, on_event) -> None:
        if self.passthrough:
            return
        self.raw.extend(data)
        # parse headers if needed
        if not self.headers_done:
            idx = self.raw.find(b"\r\n\r\n")
            if idx < 0:
                return
            headers = bytes(self.raw[: idx + 4])
            if not headers.startswith(b"HTTP/"):
                self.passthrough = True
                self.raw.clear()
                return
            m_ct = CONTENT_TYPE_RE.search(headers)
            m_cl = CONTENT_LENGTH_RE.search(headers)
            m_te = TRANSFER_ENC_RE.search(headers)
            ct = m_ct.group(1).lower() if m_ct else b""
            self.chunked = bool(m_te and b"chunked" in m_te.group(1).lower())
            self.body_remaining = int(m_cl.group(1)) if m_cl else -1
            if b"event-stream" in ct:
                self.mode = self.MODE_SSE
            elif b"x-ndjson" in ct or b"jsonl" in ct:
                self.mode = self.MODE_NDJSON
            elif b"json" in ct and self.chunked:
                # Ollama returns Content-Type: application/json with chunked NDJSON
                self.mode = self.MODE_NDJSON
            elif b"json" in ct:
                self.mode = self.MODE_JSON
            else:
                self.mode = self.MODE_NONE
            del self.raw[: idx + 4]
            self.headers_done = True
            self.body_bytes_seen = 0
            self.line_buf.clear()
            self.full_buf.clear()
            self.chunk_remaining = -1
            self.chunk_size_buf.clear()

        # extract body bytes (de-chunk if needed)
        body = self._drain_body()
        if body:
            self.body_bytes_seen += len(body)
            self._handle_body(body, on_event)

        # response complete?
        finished = False
        if self.chunked and self.chunk_remaining == 0 and not self.raw:
            # last-chunk terminator was consumed
            pass
        if self.body_remaining >= 0 and self.body_bytes_seen >= self.body_remaining:
            finished = True
        if self._chunked_done:
            finished = True
        if finished:
            if self.mode == self.MODE_JSON and self.full_buf:
                try:
                    obj = json.loads(bytes(self.full_buf))
                    _count_usage_tokens(obj, on_event)
                    _count_ollama_final(obj, on_event)
                except Exception:
                    pass
            self._reset_for_next()

    @property
    def _chunked_done(self) -> bool:
        return self.chunked and getattr(self, "_done_flag", False)

    def _reset_for_next(self) -> None:
        self.headers_done = False
        self.chunked = False
        self.body_remaining = -1
        self.body_bytes_seen = 0
        self.mode = self.MODE_NONE
        self.line_buf.clear()
        self.full_buf.clear()
        self.chunk_remaining = -1
        self.chunk_size_buf.clear()
        self._done_flag = False

    def _drain_body(self) -> bytes:
        """Pull body bytes out of self.raw, decoding chunked encoding if active."""
        if not self.chunked:
            out = bytes(self.raw)
            self.raw.clear()
            if self.body_remaining >= 0:
                # don't read past content-length (next response could be queued)
                want = self.body_remaining - self.body_bytes_seen
                if len(out) > want:
                    extra = out[want:]
                    out = out[:want]
                    self.raw[:0] = extra
            return out
        # chunked
        out = bytearray()
        while self.raw:
            if self.chunk_remaining < 0:
                # read size line
                nl = self.raw.find(b"\r\n")
                if nl < 0:
                    return bytes(out)
                size_line = bytes(self.raw[:nl]).split(b";", 1)[0].strip()
                del self.raw[: nl + 2]
                try:
                    self.chunk_remaining = int(size_line, 16)
                except ValueError:
                    self.passthrough = True
                    return bytes(out)
                if self.chunk_remaining == 0:
                    # consume trailers up to \r\n\r\n or just \r\n
                    end = self.raw.find(b"\r\n")
                    if end >= 0:
                        del self.raw[: end + 2]
                    self._done_flag = True
                    return bytes(out)
            else:
                if self.chunk_remaining > 0:
                    take = min(self.chunk_remaining, len(self.raw))
                    out.extend(self.raw[:take])
                    del self.raw[:take]
                    self.chunk_remaining -= take
                    if self.chunk_remaining > 0:
                        return bytes(out)
                # consume trailing \r\n after chunk data
                if len(self.raw) < 2:
                    return bytes(out)
                if bytes(self.raw[:2]) == b"\r\n":
                    del self.raw[:2]
                self.chunk_remaining = -1
        return bytes(out)

    def _handle_body(self, data: bytes, on_event) -> None:
        if self.mode == self.MODE_JSON:
            self.full_buf.extend(data)
            return
        if self.mode == self.MODE_NONE:
            return
        # SSE / NDJSON: line-buffered
        self.line_buf.extend(data)
        while True:
            nl = self.line_buf.find(b"\n")
            if nl < 0:
                break
            line = bytes(self.line_buf[:nl]).rstrip(b"\r")
            del self.line_buf[: nl + 1]
            if not line:
                continue
            if self.mode == self.MODE_SSE:
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == b"[DONE]":
                    continue
                try:
                    obj = json.loads(payload)
                except Exception:
                    continue
                _count_stream_chunk(obj, on_event)
            else:  # NDJSON
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                _count_ndjson_chunk(obj, on_event)


def _count_stream_chunk(obj, on_event) -> None:
    if not isinstance(obj, dict):
        return
    # Anthropic streaming
    t = obj.get("type")
    if t == "content_block_delta":
        d = obj.get("delta") or {}
        text = d.get("text") or d.get("partial_json") or ""
        if text:
            STATS.add_tokens(0, estimate_tokens(text))
            REQ_INFO.append_delta(text)
        return
    if t == "message_stop":
        REQ_INFO.mark_done("")
        return
    if t == "message_delta":
        usage = obj.get("usage") or {}
        ot = usage.get("output_tokens")
        if isinstance(ot, int):
            REQ_INFO.mark_done(f"output_tokens={ot}")
        return
    # OpenAI streaming
    choices = obj.get("choices")
    if isinstance(choices, list):
        for c in choices:
            if not isinstance(c, dict):
                continue
            d = c.get("delta") or {}
            text = d.get("content") or ""
            if text:
                STATS.add_tokens(0, estimate_tokens(text))
                REQ_INFO.append_delta(text)
            if c.get("finish_reason"):
                REQ_INFO.mark_done(f"finish={c.get('finish_reason')}")


def _count_ndjson_chunk(obj, on_event) -> None:
    """Handle one NDJSON line from Ollama or llama.cpp native /completion."""
    if not isinstance(obj, dict):
        return
    delta = ""
    msg = obj.get("message")
    if isinstance(msg, dict):  # Ollama /api/chat
        delta = msg.get("content") or ""
    if not delta:
        # Ollama /api/generate ("response"); llama.cpp /completion ("content")
        t = obj.get("response") or obj.get("content")
        if isinstance(t, str):
            delta = t
    if delta:
        STATS.add_tokens(0, estimate_tokens(delta))
        REQ_INFO.append_delta(delta)
    # final chunk: authoritative counts + flush message to log
    if obj.get("done") is True or obj.get("stop") is True:
        bits = []
        for k_disp, k_obj in (
            ("prompt_eval", "prompt_eval_count"),
            ("eval", "eval_count"),
            ("toks_eval", "tokens_evaluated"),
            ("toks_pred", "tokens_predicted"),
        ):
            v = obj.get(k_obj)
            if isinstance(v, int):
                bits.append(f"{k_disp}={v}")
        REQ_INFO.mark_done(" ".join(bits))


def _count_ollama_final(obj, on_event) -> None:
    if not isinstance(obj, dict):
        return
    bits = []
    pec = obj.get("prompt_eval_count")
    ec = obj.get("eval_count")
    te = obj.get("tokens_evaluated")  # llama.cpp
    tp = obj.get("tokens_predicted")  # llama.cpp
    if isinstance(pec, int):
        bits.append(f"prompt_eval={pec}")
    if isinstance(ec, int):
        bits.append(f"eval={ec}")
    if isinstance(te, int):
        bits.append(f"toks_eval={te}")
    if isinstance(tp, int):
        bits.append(f"toks_pred={tp}")
    if bits:
        on_event("done " + " ".join(bits))


def _count_usage_tokens(obj, on_event) -> None:
    if not isinstance(obj, dict):
        return
    u = obj.get("usage")
    if not isinstance(u, dict):
        return
    it = u.get("input_tokens") or u.get("prompt_tokens")
    ot = u.get("output_tokens") or u.get("completion_tokens")
    msg = []
    if isinstance(it, int):
        msg.append(f"in={it}")
    if isinstance(ot, int):
        msg.append(f"out={ot}")
    if msg:
        on_event("usage " + " ".join(msg))


# --------------------------------------------------------------------------- #
# Proxy
# --------------------------------------------------------------------------- #

class ProxyState:
    def __init__(self, host: str, port: int):
        self.lock = threading.Lock()
        self.host = host
        self.port = port
        self.model_override: Optional[str] = None

    def get_target(self):
        with self.lock:
            return self.host, self.port

    def set_target(self, host: str, port: int) -> None:
        with self.lock:
            self.host = host
            self.port = port

    def get_model(self) -> Optional[str]:
        with self.lock:
            return self.model_override

    def set_model(self, m: Optional[str]) -> None:
        with self.lock:
            self.model_override = m


async def pump(reader, writer, on_chunk):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            on_chunk(data)
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def handle_client(reader, writer, state: ProxyState, renderer: Renderer):
    peer = writer.get_extra_info("peername")
    host, port = state.get_target()
    try:
        u_reader, u_writer = await asyncio.open_connection(host, port)
    except Exception as e:
        renderer.print_above(f"[conn] failed {host}:{port} from {peer}: {e}")
        writer.close()
        return
    renderer.print_above(f"[conn] {peer} → {host}:{port}")

    rewriter = RequestRewriter(state.get_model)
    sniffer = ResponseSniffer()

    def on_client_chunk(data: bytes):
        STATS.add_bytes(len(data), 0)
        out = rewriter.feed(data)
        if out:
            u_writer.write(out)

    def on_server_chunk(data: bytes):
        STATS.add_bytes(0, len(data))
        sniffer.feed(data, lambda msg: renderer.print_above(f"[srv] {msg}"))

    # we override the pump for client→server so we can rewrite
    async def client_to_server():
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                STATS.add_bytes(len(data), 0)
                out = rewriter.feed(data)
                if out:
                    u_writer.write(out)
                    await u_writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            try:
                u_writer.close()
            except Exception:
                pass

    async def server_to_client():
        try:
            while True:
                data = await u_reader.read(65536)
                if not data:
                    break
                on_server_chunk(data)
                writer.write(data)
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    await asyncio.gather(client_to_server(), server_to_client())
    renderer.print_above(f"[conn] closed {peer}")


# --------------------------------------------------------------------------- #
# Input handling
# --------------------------------------------------------------------------- #

def input_loop(renderer: Renderer, state: ProxyState, stop_event: threading.Event):
    """Blocking key reader. Each keystroke immediately repaints the input row."""
    if IS_WIN:
        import msvcrt
        while not stop_event.is_set():
            try:
                ch = msvcrt.getwch()  # blocking — instant response
            except (KeyboardInterrupt, OSError):
                stop_event.set()
                return
            if ch in ("\x00", "\xe0"):
                # function/arrow key; consume scan code and ignore
                try:
                    msvcrt.getwch()
                except Exception:
                    pass
                continue
            if ch in ("\r", "\n"):
                cmd = renderer.input_buf
                renderer.input_buf = ""
                renderer.redraw_input()
                handle_command(cmd, renderer, state, stop_event)
            elif ch == "\b":
                if renderer.input_buf:
                    renderer.input_buf = renderer.input_buf[:-1]
                    renderer.redraw_input()
            elif ch == "\x03":  # Ctrl-C
                stop_event.set()
                return
            elif ch == "\x1b":  # ESC: clear pending
                renderer.input_buf = ""
                renderer.redraw_input()
            elif ch.isprintable():
                renderer.input_buf += ch
                renderer.redraw_input()
    else:
        import termios, tty, select
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while not stop_event.is_set():
                r, _, _ = select.select([sys.stdin], [], [], 0.2)
                if not r:
                    continue
                ch = sys.stdin.read(1)
                if ch in ("\r", "\n"):
                    cmd = renderer.input_buf
                    renderer.input_buf = ""
                    renderer.redraw_input()
                    handle_command(cmd, renderer, state, stop_event)
                elif ch in ("\x7f", "\b"):
                    if renderer.input_buf:
                        renderer.input_buf = renderer.input_buf[:-1]
                        renderer.redraw_input()
                elif ch == "\x03":
                    stop_event.set()
                    return
                elif ch == "\x1b":
                    renderer.input_buf = ""
                    renderer.redraw_input()
                elif ch.isprintable():
                    renderer.input_buf += ch
                    renderer.redraw_input()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


HELP_TEXT = (
    "commands: /help  /model [name]  /server <host[:port]>"
    "  /showfrom /hidefrom  /showto /hideto  /quit"
)


def handle_command(cmd: str, renderer: Renderer, state: ProxyState, stop_event: threading.Event):
    cmd = cmd.strip()
    if not cmd:
        return
    if not cmd.startswith("/"):
        renderer.set_message("(use /help)")
        return
    parts = cmd.split(None, 1)
    op = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    if op == "/help":
        renderer.print_above(HELP_TEXT)
        renderer.set_message("")
    elif op == "/quit" or op == "/exit":
        stop_event.set()
    elif op == "/model":
        if not arg:
            state.set_model(None)
            renderer.print_above("[cfg] model override cleared")
        else:
            state.set_model(arg)
            renderer.print_above(f"[cfg] model override → {arg}")
        renderer.set_message("")
    elif op == "/showfrom":
        REQ_INFO.set_show_from(True)
        renderer.print_above("[cfg] show client→server prompt content: ON")
        renderer.set_message("")
    elif op == "/hidefrom":
        REQ_INFO.set_show_from(False)
        renderer.print_above("[cfg] show client→server prompt content: OFF")
        renderer.set_message("")
    elif op == "/showto":
        REQ_INFO.set_show_to(True)
        renderer.print_above("[cfg] show server→client response content: ON")
        renderer.set_message("")
    elif op == "/hideto":
        REQ_INFO.set_show_to(False)
        renderer.print_above("[cfg] show server→client response content: OFF")
        renderer.set_message("")
    elif op == "/server":
        if not arg:
            renderer.set_message("usage: /server host[:port]")
            return
        host, _, p = arg.partition(":")
        try:
            port = int(p) if p else state.get_target()[1]
        except ValueError:
            renderer.set_message("bad port")
            return
        state.set_target(host, port)
        renderer.print_above(f"[cfg] upstream → {host}:{port} (existing conns unchanged)")
        renderer.set_message("")
    else:
        renderer.set_message(f"unknown: {op}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

async def run_server(args, renderer: Renderer, state: ProxyState, stop_event: threading.Event):
    # On wildcard binds, listen on both IPv4 and IPv6 so `localhost` works
    # regardless of which family the client picks first.
    if args.bind_host in ("0.0.0.0", "*", ""):
        bind_hosts = ["0.0.0.0", "::"]
    else:
        bind_hosts = args.bind_host
    server = await asyncio.start_server(
        lambda r, w: handle_client(r, w, state, renderer),
        host=bind_hosts,
        port=args.bind_port,
    )
    sockets = ", ".join(
        f"{s.getsockname()[0]}:{s.getsockname()[1]}" for s in server.sockets
    )
    renderer.print_above(f"[boot] listening on {sockets} → {args.host}:{args.port_to}")

    # ticker that updates per-second stats and redraws
    async def ticker():
        while not stop_event.is_set():
            STATS.tick()
            renderer.draw_status(*state.get_target(), state.get_model())
            await asyncio.sleep(1.0)

    async def stop_watcher():
        while not stop_event.is_set():
            await asyncio.sleep(0.2)
        server.close()

    async with server:
        await asyncio.gather(ticker(), stop_watcher(), server.serve_forever(), return_exceptions=True)


DEFAULT_PORT = 11434  # Ollama


def parse_bind(spec: str):
    """Parse 'host', 'host:port', ':port' or 'port' into (host, port)."""
    s = spec.strip()
    if not s:
        return "0.0.0.0", DEFAULT_PORT
    # bare integer = port only
    if s.isdigit():
        return "0.0.0.0", int(s)
    # IPv6 in brackets: [::1]:port
    if s.startswith("["):
        end = s.find("]")
        if end < 0:
            raise argparse.ArgumentTypeError(f"bad --bind: {spec}")
        host = s[1:end]
        rest = s[end + 1 :]
        port = int(rest[1:]) if rest.startswith(":") else DEFAULT_PORT
        return host, port
    # host[:port] or :port
    if ":" in s:
        host, _, p = s.rpartition(":")
        return (host or "0.0.0.0"), int(p) if p else DEFAULT_PORT
    return s, DEFAULT_PORT


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--bind",
        default=f"0.0.0.0:{DEFAULT_PORT}",
        help=f"local bind as host[:port] (default 0.0.0.0:{DEFAULT_PORT})",
    )
    ap.add_argument("--to", dest="port_to", type=int, default=None,
                    help=f"upstream port (default {DEFAULT_PORT}, or port from --host)")
    ap.add_argument("--host", required=True, help="upstream host as host or host:port")
    args = ap.parse_args()
    args.bind_host, args.bind_port = parse_bind(args.bind)

    # split --host into (host, optional port-from-host)
    h = args.host.strip()
    host_only, port_in_host = h, None
    if h.startswith("["):
        end = h.find("]")
        if end < 0:
            ap.error(f"bad --host: {args.host}")
        host_only = h[1:end]
        rest = h[end + 1 :]
        if rest.startswith(":"):
            try:
                port_in_host = int(rest[1:])
            except ValueError:
                ap.error(f"bad port in --host: {rest[1:]}")
        elif rest:
            ap.error(f"trailing junk after --host bracket: {rest}")
    elif h.count(":") == 1:
        host_only, _, p = h.partition(":")
        try:
            port_in_host = int(p)
        except ValueError:
            ap.error(f"bad port in --host: {p}")
    elif h.count(":") > 1:
        ap.error(f"--host has multiple colons (use [v6]:port for IPv6): {args.host}")

    # resolve port_to with precedence: --to > port-in-host > default
    if args.port_to is not None and port_in_host is not None and args.port_to != port_in_host:
        print(
            f"warning: --to {args.port_to} overrides port {port_in_host} from --host",
            file=sys.stderr,
        )
    if args.port_to is None:
        args.port_to = port_in_host if port_in_host is not None else DEFAULT_PORT
    args.host = host_only

    enable_vt_mode()
    renderer = Renderer()
    REQ_INFO.set_log(renderer.print_above)
    state = ProxyState(args.host, args.port_to)
    stop_event = threading.Event()

    t = threading.Thread(target=input_loop, args=(renderer, state, stop_event), daemon=True)
    t.start()

    try:
        asyncio.run(run_server(args, renderer, state, stop_event))
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        stop_event.set()
        renderer.teardown()


if __name__ == "__main__":
    main()
