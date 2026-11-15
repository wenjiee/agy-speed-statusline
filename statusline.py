#!/usr/bin/env python3
"""Minimal Antigravity CLI statusLine: preserves native left/right, shows TTFT & TPS in the center."""

import json
import os
from pathlib import Path
import re
import sqlite3
import struct
import subprocess
import sys
import unicodedata

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
MODEL_EFFORT_RE = re.compile(r"^(.+?)\s*\((Low|Medium|High|XHigh|Max)\)\s*$", re.I)

RST = "\x1b[0m"
DIM = "\x1b[2m"
MUTED = "\x1b[38;2;154;160;166m"  # native built-in gray (#9AA0A6)
MODE_COLORS = {
    "auto": "\x1b[38;2;249;171;0m",          # native amber/gold (#F9AB00)
    "plan": "\x1b[38;2;138;180;248m",        # soft blue
    "accept-edits": "\x1b[38;2;129;201;149m",  # soft green
}
LATENCY = "\x1b[36m"  # light cyan
SPEED = "\x1b[97m"    # bright white


def disp_width(text: str) -> int:
    """Return terminal display width of a string (ignoring ANSI escapes)."""
    clean = ANSI_RE.sub("", text)
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in clean)


def _read_varint(data: bytes, idx: int):
    """Decode a protobuf varint starting at idx, returning (value, next_idx) or (None, idx)."""
    n = len(data)
    shift = 0
    val = 0
    while idx < n:
        b = data[idx]
        idx += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, idx
        shift += 7
        if shift > 63:
            return None, idx
    return None, idx


def parse_pb_fields(data: bytes):
    """Parse top-level wire fields of a protobuf blob, returning None if malformed."""
    i = 0
    n = len(data)
    out = []
    while i < n:
        tag, i = _read_varint(data, i)
        if tag is None:
            return None
        fn, wt = tag >> 3, tag & 7
        if fn == 0:
            return None
        if wt == 0:
            val, i = _read_varint(data, i)
            if val is None:
                return None
            out.append((fn, 0, val))
        elif wt == 1:
            if i + 8 > n:
                return None
            out.append((fn, 1, struct.unpack("<Q", data[i : i + 8])[0]))
            i += 8
        elif wt == 2:
            ln, i = _read_varint(data, i)
            if ln is None or i + ln > n:
                return None
            out.append((fn, 2, data[i : i + ln]))
            i += ln
        elif wt == 5:
            if i + 4 > n:
                return None
            out.append((fn, 5, struct.unpack("<I", data[i : i + 4])[0]))
            i += 4
        else:
            return None
    return out


def parse_pb_duration(blob: bytes) -> float:
    """Decode google.protobuf.Duration (seconds + nanos) into seconds."""
    sec = 0
    nanos = 0
    for fn, wt, val in parse_pb_fields(blob) or ():
        if fn == 1 and wt == 0:
            sec = val
        elif fn == 2 and wt == 0:
            nanos = val
    return sec + nanos / 1e9


def extract_metrics(conv_id: str):
    """Return (latest_ttft, latest_tps, avg_ttft, avg_tps) across turns in conv_id."""
    if not conv_id or not UUID_RE.fullmatch(conv_id):
        return None, None, None, None

    cli_dir = Path(os.environ.get("AGY_CLI_DIR") or Path.home() / ".gemini" / "antigravity-cli")
    db_path = cli_dir / "conversations" / f"{conv_id}.db"
    if not db_path.is_file():
        return None, None, None, None

    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.05)
        try:
            rows = conn.execute("SELECT data FROM gen_metadata ORDER BY idx DESC").fetchall()
        finally:
            conn.close()
    except Exception:
        return None, None, None, None

    latest_ttft = None
    latest_tps = None
    ttft_sum = 0.0
    ttft_cnt = 0
    tok_sum = 0
    stream_sum = 0.0

    for (blob,) in rows:
        for fn, wt, val in parse_pb_fields(blob) or ():
            if fn != 1 or wt != 2:
                continue
            ttft = None
            stream = None
            usage_blob = None
            retry_usage = None
            for cfn, cwt, cv in parse_pb_fields(val) or ():
                if cfn == 11 and cwt == 2:
                    ttft = parse_pb_duration(cv)
                elif cfn == 12 and cwt == 2:
                    stream = parse_pb_duration(cv)
                elif cfn == 4 and cwt == 2:
                    usage_blob = cv
                elif cfn == 17 and cwt == 2:
                    for rfn, rwt, rv in parse_pb_fields(cv) or ():
                        if rfn == 2 and rwt == 2:
                            retry_usage = rv
            ub = retry_usage or usage_blob
            out_tok = 0
            if ub:
                for ufn, uwt, uv in parse_pb_fields(ub) or ():
                    if ufn == 3 and uwt == 0:
                        out_tok = uv
                        break
            if ttft is not None:
                if latest_ttft is None:
                    latest_ttft = ttft
                ttft_sum += ttft
                ttft_cnt += 1
            if stream is not None and stream >= 0.2 and out_tok > 0:
                if latest_tps is None:
                    latest_tps = out_tok / stream
                tok_sum += out_tok
                stream_sum += stream

    avg_ttft = (ttft_sum / ttft_cnt) if ttft_cnt > 0 else None
    avg_tps = (tok_sum / stream_sum) if stream_sum > 0 else None
    return latest_ttft, latest_tps, avg_ttft, avg_tps


def detect_mode(payload: dict) -> str:
    """Detect permission/execution mode label matching built-in statusLine."""
    for key in ("mode", "permission_mode"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    try:
        pid = str(os.getppid())
        for _ in range(4):
            out = subprocess.check_output(
                ["ps", "-o", "ppid=,args=", "-p", pid],
                text=True,
                timeout=0.3,
                stderr=subprocess.DEVNULL,
            ).strip()
            if not out:
                break
            parts = out.split(None, 1)
            next_pid = parts[0]
            args = parts[1] if len(parts) > 1 else ""
            if "--dangerously-skip-permissions" in args:
                return "auto"
            m = re.search(r"--mode(?:=|\s+)(\S+)", args)
            if m:
                return m.group(1)
            if "agy-bin" in args or next_pid in ("0", "1", pid):
                break
            pid = next_pid
    except Exception:
        pass
    return ""


def format_right(payload: dict) -> str:
    """Format the built-in right-side status text: `<mode> · <model> · <effort>`."""
    mode = detect_mode(payload)
    tail_parts = []

    model_info = payload.get("model")
    if not isinstance(model_info, dict):
        model_info = {}
    model_name = str(model_info.get("display_name") or model_info.get("id") or "").strip()
    if model_name:
        m = MODEL_EFFORT_RE.match(model_name)
        if m:
            tail_parts.append(m.group(1).strip())
            tail_parts.append(m.group(2).lower())
        else:
            tail_parts.append(model_name)

    tail = " · ".join(tail_parts)
    if mode and tail:
        mcol = MODE_COLORS.get(mode, MUTED)
        return f"{mcol}{mode}{RST}{MUTED} · {tail}{RST}"
    if mode:
        mcol = MODE_COLORS.get(mode, MUTED)
        return f"{mcol}{mode}{RST}"
    return f"{MUTED}{tail}{RST}" if tail else ""


def render_mid(l_ttft, l_tps, a_ttft=None, a_tps=None, full=True) -> str:
    """Render the middle segment, colored by layer: separators dim, latency cyan, speed bright white."""
    fmt_s = lambda v: f"{v:.1f}s" if v is not None else "--"
    fmt_t = lambda v: f"{v:.0f} t/s" if v is not None else "-- t/s"

    segs = [
        (LATENCY, fmt_s(l_ttft)),
        (DIM, " · "),
        (SPEED, fmt_t(l_tps)),
    ]
    if full:
        segs += [
            (DIM, " │ 均 "),
            (LATENCY, fmt_s(a_ttft)),
            (DIM, " · "),
            (SPEED, fmt_t(a_tps)),
        ]
    return RST.join(f"{style}{text}" for style, text in segs) + RST


def main():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    conv_id = str(payload.get("conversation_id") or "").strip()
    if not conv_id:
        tpath = str(payload.get("transcript_path") or "")
        m = UUID_RE.search(tpath)
        if m:
            conv_id = m.group(0)

    l_ttft, l_tps, a_ttft, a_tps = extract_metrics(conv_id)

    left = "? for shortcuts"
    right = format_right(payload)
    try:
        width = max(20, int(payload.get("terminal_width") or 80))
    except (TypeError, ValueError):
        width = 80

    lw = disp_width(left)
    rw = disp_width(right)
    gap = width - lw - rw

    full_mid = render_mid(l_ttft, l_tps, a_ttft, a_tps, full=True)
    short_mid = render_mid(l_ttft, l_tps, full=False)

    mid = full_mid if gap >= disp_width(full_mid) + 2 else short_mid
    mw = disp_width(mid)

    if gap >= mw + 2:
        lpad = (gap - mw) // 2
        rpad = gap - mw - lpad
        sys.stdout.write(f"{MUTED}{left}{RST}{' ' * lpad}{mid}{' ' * rpad}{right}")
    elif width >= mw + rw + 2:
        lpad = max(1, (width - mw - rw) // 2)
        sys.stdout.write(f"{' ' * lpad}{mid}{' ' * max(1, width - lpad - mw - rw)}{right}")
    else:
        sys.stdout.write(mid)


if __name__ == "__main__":
    main()
