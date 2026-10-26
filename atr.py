#!/usr/bin/env python3
"""
atr — Agent Trace Replayer
==========================
Take a JSONL log of an agent run (tool calls, outputs, timings) and replay it
in the terminal as a scrub-able timeline with per-step latency bars.

Views:
  (default)   waterfall + summary + auto-diagnosis of slow steps / idle gaps
  --scrub     interactive playhead: n/p step, g N jump, f/b slow-steps, / text
  --play      replay in real time (or --speed xN)
  --top N     just the N slowest steps
  --grep STR  steps whose name/input/output match STR

Accepted JSONL (one event per line; keys are auto-detected):
  {"ts": 1729900000.12, "type": "tool_call", "name": "terminal",
   "input": "pytest -q", "duration_ms": 4210, "output": "...", "status": "ok"}
or start/end pairs sharing an "id":
  {"ts": ..., "type": "tool_call", "id": "t1", "name": "web_fetch", ...}
  {"ts": ..., "type": "tool_result", "id": "t1", "output": "..."}
Timestamps: epoch seconds/ms or ISO-8601. Durations: duration_ms / duration /
end_ts, or derived from paired events. Stdlib only.
"""
import argparse
import json
import math
import os
import re
import sys
import time
from datetime import datetime

# ---------------------------------------------------------------- utilities

def human(ms):
    if ms is None:
        return "?"
    if ms < 1:
        return "0ms"
    if ms < 1000:
        return f"{ms:.0f}ms"
    s = ms / 1000.0
    if s < 60:
        return f"{s:.2f}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{int(m)}m{s:04.1f}s"
    h, m = divmod(m, 60)
    return f"{int(h)}h{int(m):02d}m"

def parse_ts(v):
    """epoch s / epoch ms / ISO-8601 -> epoch seconds (float) or None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v / 1000.0 if v > 1e11 else float(v)
    s = str(v).strip()
    try:
        f = float(s)
        return f / 1000.0 if f > 1e11 else f
    except ValueError:
        pass
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None

def parse_dur(ev):
    for k in ("duration_ms", "durationMs", "ms", "elapsed_ms"):
        if isinstance(ev.get(k), (int, float)):
            return float(ev[k])
    for k in ("duration", "elapsed", "latency", "latency_ms"):
        if isinstance(ev.get(k), (int, float)):
            v = float(ev[k])
            return v if v > 100 else v * 1000.0  # heuristic: <100 means seconds
    return None

def first(ev, *keys, default=None):
    for k in keys:
        if ev.get(k) not in (None, ""):
            return ev[k]
    return default

TOOLISH = {"tool_call", "tool_use", "tool_result", "tool", "function_call", "mcp"}
LLMISH = {"llm", "llm_call", "model", "assistant", "generation", "completion", "turn"}
THINKISH = {"thinking", "reasoning", "plan"}

def classify(kind):
    k = (kind or "").lower()
    if k in TOOLISH or "tool" in k or "function" in k or k.startswith("mcp"):
        return "tool"
    if k in LLMISH or "llm" in k or "model" in k:
        return "llm"
    if k in THINKISH:
        return "think"
    if "user" in k or "human" in k:
        return "user"
    return "other"

# ---------------------------------------------------------------- ingest

class Step:
    __slots__ = ("i", "start", "dur", "kind", "name", "input", "output",
                 "status", "raw", "gap_before")
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

def load_trace(path):
    steps, by_id, pending_ts = [], {}, None
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for ln, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"  ! line {ln}: bad JSON ({e})", file=sys.stderr)
                continue
            if not isinstance(ev, dict):
                continue
            ts = parse_ts(first(ev, "ts", "timestamp", "time", "start_time",
                                "started_at", "created_at"))
            if ts is not None:
                pending_ts = ts
            eid = first(ev, "id", "call_id", "tool_use_id", "span_id")
            kind = str(first(ev, "type", "event", "kind", "role", "op",
                             default="event"))
            name = str(first(ev, "name", "tool", "function", "label",
                             default=kind))
            inp = first(ev, "input", "args", "arguments", "params", "command")
            out = first(ev, "output", "result", "content", "response",
                        "stdout", "text")
            status = first(ev, "status", default=None)
            if ev.get("error") or ev.get("is_error"):
                status = "error"
            end = parse_ts(first(ev, "end_ts", "end_time", "finished_at",
                                 "completed_at"))
            dur = parse_dur(ev)

            if eid and eid in by_id:  # pair: result closes the call
                st = by_id[eid]
                if out is not None:
                    st.output = out
                if status:
                    st.status = status
                if dur is not None:
                    st.dur = dur
                elif end is not None and st.start is not None:
                    st.dur = (end - st.start) * 1000.0
                elif ts is not None and st.start is not None:
                    st.dur = (ts - st.start) * 1000.0
                continue

            if dur is None and end is not None and ts is not None:
                dur = (end - ts) * 1000.0
            st = Step(i=len(steps), start=ts, dur=dur or 0.0,
                      kind=classify(kind), name=name,
                      input=inp, output=out, status=status, raw=ev,
                      gap_before=None)
            steps.append(st)
            if eid:
                by_id[eid] = st

    # fill missing timestamps by walking forward from the first known ts
    t = None
    for st in steps:
        if st.start is None:
            st.start = (t + st.dur / 1000.0) if t is not None else 0.0
        t = st.start + st.dur / 1000.0
    steps.sort(key=lambda s: s.start)
    for i, st in enumerate(steps):
        st.i = i
        st.gap_before = ((st.start - (steps[i - 1].start + steps[i - 1].dur / 1000.0)) * 1000.0
                         if i else 0.0)
    return steps

# ---------------------------------------------------------------- hermes adapter

def load_hermes(query, list_only=False):
    """Build steps directly from ~/.hermes/state.db sessions.
    query: 'last' | session id | title/display-name substring. Empty -> list."""
    import sqlite3
    dbp = os.environ.get("HERMES_STATE_DB",
                         os.path.expanduser("~/.hermes/state.db"))
    db = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    if list_only or query in ("", "list"):
        rows = db.execute(
            "SELECT id, title, started_at, tool_call_count, message_count "
            "FROM sessions WHERE archived=0 AND tool_call_count>0 "
            "ORDER BY last_activity_at DESC LIMIT 15").fetchall()
        out = []
        for r in rows:
            ts = datetime.fromtimestamp(r["started_at"]).strftime("%m-%d %H:%M")
            out.append(f"  {ts}  {r['id']:<38} tools:{r['tool_call_count']:>3}  "
                       f"{clip(r['title'] or '', 40)}")
        return "\n".join(out)
    if query == "last":
        r = db.execute("SELECT id FROM sessions WHERE archived=0 AND "
                       "tool_call_count>0 ORDER BY last_activity_at DESC "
                       "LIMIT 1").fetchone()
    else:
        r = db.execute("SELECT id FROM sessions WHERE id=? LIMIT 1",
                       (query,)).fetchone()
        if not r:
            rows = db.execute("SELECT id, title FROM sessions WHERE archived=0 "
                              "AND (title LIKE ? OR display_name LIKE ?) "
                              "ORDER BY last_activity_at DESC LIMIT 5",
                              (f"%{query}%", f"%{query}%")).fetchall()
            if not rows:
                sys.exit(f"no Hermes session matches {query!r} "
                         f"(try 'atr hermes:' to list)")
            if len(rows) > 1:
                sys.exit("ambiguous — pick one id:\n" + "\n".join(
                    f"  {x['id']}  {clip(x['title'] or '', 40)}" for x in rows))
            r = rows[0]
    sid = r["id"]
    msgs = db.execute(
        "SELECT role, content, tool_calls, tool_call_id, tool_name, "
        "timestamp, reasoning_content FROM messages "
        "WHERE session_id=? AND role!='session_meta' ORDER BY id",
        (sid,)).fetchall()
    steps, prev_end = [], None
    for m in msgs:
        ts = float(m["timestamp"])
        if m["role"] == "user":
            steps.append(Step(i=0, start=ts, dur=0.0, kind="user",
                              name="user", input=m["content"], output=None,
                              status=None, raw={}, gap_before=None))
            prev_end = ts
            continue
        if m["role"] == "assistant":
            gen = (ts - prev_end) * 1000.0 if prev_end is not None else 0.0
            steps.append(Step(i=0, start=prev_end if prev_end is not None else ts,
                              dur=max(0.0, gen), kind="llm", name="generate",
                              input=None, output=m["content"] or None,
                              status=None, raw={}, gap_before=None))
            calls = []
            if m["tool_calls"]:
                try:
                    calls = json.loads(m["tool_calls"])
                except json.JSONDecodeError:
                    calls = []
            for call in calls:
                fn = call.get("function", {})
                steps.append(Step(i=0, start=ts, dur=0.0, kind="tool",
                                  name=fn.get("name", "tool"),
                                  input=fn.get("arguments"), output=None,
                                  status="pending", raw={}, gap_before=None))
            prev_end = ts
            continue
        if m["role"] == "tool":
            # close the matching pending call (same tool name, LIFO)
            target = None
            for st in reversed(steps):
                if st.kind == "tool" and st.status == "pending" \
                        and st.name == (m["tool_name"] or st.name):
                    target = st
                    break
            if target is None:
                target = Step(i=0, start=ts, dur=0.0, kind="tool",
                              name=m["tool_name"] or "tool", input=None,
                              output=None, status="pending", raw={},
                              gap_before=None)
                steps.append(target)
            target.dur = max(0.0, (ts - target.start) * 1000.0)
            target.output = m["content"]
            target.status = ("error" if '"error":' in (m["content"] or "")[:200]
                             and '"error": null' not in (m["content"] or "")[:200]
                             else "ok")
            prev_end = max(prev_end or ts, ts)
            continue
    steps = [s for s in steps if not (s.kind == "llm" and s.dur < 1
                                      and not s.output)]
    steps.sort(key=lambda s: s.start)
    for i, st in enumerate(steps):
        st.i = i
        st.gap_before = ((st.start - (steps[i - 1].start + steps[i - 1].dur / 1000.0)) * 1000.0
                         if i else 0.0)
    return steps

# ---------------------------------------------------------------- render

class C:
    def __init__(self, on):
        self.on = on
    def __call__(self, code, s):
        return f"\033[{code}m{s}\033[0m" if self.on else s
    def dim(self, s):  return self("2", s)
    def red(self, s):  return self("31", s)
    def yel(self, s):  return self("33", s)
    def grn(self, s):  return self("32", s)
    def cyn(self, s):  return self("36", s)
    def mag(self, s):  return self("35", s)
    def bold(self, s): return self("1", s)

def term_w():
    return os.get_terminal_size().columns if sys.stdout.isatty() else 100

def clip(v, n):
    if v is None:
        return ""
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = s.replace("\n", "\\n")
    return s if len(s) <= n else s[: n - 1] + "…"

def bar_color(c, ms, warn, bad):
    if ms >= bad:   return c.red
    if ms >= warn:  return c.yel
    return c.grn

KIND_ICON = {"tool": "⚙", "llm": "🧠", "think": "💭", "user": "👤", "other": "·"}

def step_line(c, st, width, warn, bad, marker="  "):
    col = bar_color(c, st.dur, warn, bad)
    label = f"{st.i:>3} {KIND_ICON[st.kind]} {clip(st.name, 22):<22}"
    tstr = (datetime.fromtimestamp(st.start).strftime("%H:%M:%S")
            if st.start else "--:--:--")
    dur = col(f"{human(st.dur):>9}")
    err = c.red(" ✗") if st.status and "err" in str(st.status).lower() else ""
    info = clip(st.input if st.input is not None else st.output, width - 48)
    return f"{marker}{c.dim(tstr)} {label} {dur}{err}  {c.dim(info)}"

def waterfall(c, steps, t0, t1, warn, bad, width=None, limit=None):
    width = width or term_w()
    track = max(20, width - 52)
    total = max(1e-9, t1 - t0)
    rows = []
    shown = steps if limit is None else steps[:limit]
    for st in shown:
        off = int((st.start - t0) / total * track)
        ln = max(1, int(st.dur / 1000.0 / total * track))
        if off + ln > track:
            ln = track - off
        col = bar_color(c, st.dur, warn, bad)
        bar = "█" * ln
        rows.append(f"{c.dim(f'{st.i:>3}')} {KIND_ICON[st.kind]} "
                    f"{clip(st.name, 20):<20} {col(bar)} "
                    f"{col(f'{human(st.dur):>9}')}")
    return "\n".join(rows)

def summary(c, steps, warn, bad):
    t0, t1 = steps[0].start, max(s.start + s.dur / 1000.0 for s in steps)
    wall = (t1 - t0) * 1000.0
    by_kind = {}
    for s in steps:
        by_kind[s.kind] = by_kind.get(s.kind, 0.0) + s.dur
    attributed = sum(by_kind.values())
    idle = wall - attributed
    lines = [c.bold(f"wall {human(wall)}  steps {len(steps)}  "
                    f"attributed {human(attributed)}  "
                    f"unattributed/idle {human(max(0, idle))}")]
    parts = [f"{k} {human(v)}" for k, v in
             sorted(by_kind.items(), key=lambda kv: -kv[1])]
    lines.append(c.dim("  time by kind: " + " | ".join(parts)))
    return "\n".join(lines)

def diagnose(c, steps, warn, bad):
    """Flag slow steps and big idle gaps — the 'why did it take 4 minutes' view."""
    durs = sorted(s.dur for s in steps if s.dur > 0)
    if not durs:
        return ""
    p50 = durs[len(durs) // 2]
    p95 = durs[min(len(durs) - 1, int(len(durs) * 0.95))]
    out = [c.bold("diagnosis")]
    hits = [s for s in steps if s.dur >= max(bad, 3 * p50)]
    if hits:
        for s in sorted(hits, key=lambda s: -s.dur)[:5]:
            ratio = s.dur / p50 if p50 else float("inf")
            out.append(f"  {c.red('SLOW')} #{s.i} {s.name} took "
                       f"{c.red(human(s.dur))} "
                       f"({ratio:.0f}× median {human(p50)}, p95 {human(p95)})")
            if s.input is not None:
                out.append(c.dim(f"        in : {clip(s.input, 90)}"))
            if s.output is not None:
                out.append(c.dim(f"        out: {clip(s.output, 90)}"))
    else:
        out.append(c.dim("  no step exceeds 3× median duration"))
    gaps = [(s, s.gap_before) for s in steps if s.gap_before and
            s.gap_before >= max(bad, 3 * p50)]
    for s, g in sorted(gaps, key=lambda x: -x[1])[:3]:
        out.append(f"  {c.yel('GAP')} {human(g)} idle before #{s.i} "
                   f"{s.name} — model thinking / queueing, not tool time")
    errs = [s for s in steps if s.status and "err" in str(s.status).lower()]
    for s in errs[:3]:
        out.append(f"  {c.red('ERR ')} #{s.i} {s.name}: {clip(s.output, 80)}")
    return "\n".join(out)

def detail(c, st, width):
    col = bar_color(c, st.dur, 2000, 10000)
    head = (f"#{st.i}  {KIND_ICON[st.kind]} {c.bold(st.name)}  "
            f"{col(human(st.dur))}"
            + (f"  {c.dim('gap before: ' + human(st.gap_before))}"
               if st.gap_before and st.gap_before > 50 else ""))
    lines = [head]
    if st.input is not None:
        lines.append(c.cyn("  in : ") + clip(st.input, width - 8))
    if st.output is not None:
        lines.append(c.mag("  out: ") + clip(st.output, width - 8))
    if st.status:
        lines.append(c.dim(f"  status: {st.status}"))
    return "\n".join(lines)

def strip(c, steps, cur, t0, t1, warn, bad, half=4):
    """mini timeline around the playhead."""
    rows = []
    lo, hi = max(0, cur - half), min(len(steps), cur + half + 1)
    for st in steps[lo:hi]:
        mark = c.bold("▶") if st.i == cur else " "
        col = bar_color(c, st.dur, warn, bad)
        rows.append(f"{mark} {c.dim(f'{st.i:>3}')} {KIND_ICON[st.kind]} "
                    f"{clip(st.name, 24):<24} {col(human(st.dur)):>10}")
    return "\n".join(rows)

# ---------------------------------------------------------------- modes

def mode_default(c, steps, args):
    t0 = steps[0].start
    t1 = max(s.start + s.dur / 1000.0 for s in steps)
    print(c.bold(f"── {args.file} ──"))
    print(summary(c, steps, args.warn, args.bad))
    print()
    if args.top:
        print(c.bold(f"top {args.top} slowest:"))
        for s in sorted(steps, key=lambda s: -s.dur)[: args.top]:
            print(step_line(c, s, term_w(), args.warn, args.bad))
    else:
        print(c.bold("waterfall:"))
        print(waterfall(c, steps, t0, t1, args.warn, args.bad))
    print()
    print(diagnose(c, steps, args.warn, args.bad))

def mode_top(c, steps, args):
    for s in sorted(steps, key=lambda s: -s.dur)[: args.top]:
        print(step_line(c, s, term_w(), args.warn, args.bad))

def mode_grep(c, steps, args):
    rx = re.compile(args.grep, re.I)
    n = 0
    for s in steps:
        blob = f"{s.name} {s.input or ''} {s.output or ''}"
        if rx.search(blob):
            n += 1
            print(step_line(c, s, term_w(), args.warn, args.bad))
            if args.verbose:
                print(detail(c, s, term_w()))
    print(c.dim(f"{n} match(es)"))

def mode_play(c, steps, args):
    speed = args.speed or 1.0
    t0 = steps[0].start
    print(c.bold(f"▶ replay @ {speed:g}×  ({human((steps[-1].start + steps[-1].dur/1000 - t0)*1000)} wall)"))
    for s in steps:
        due = (s.start - t0) / speed
        now = time.monotonic() - start_clock
        wait = due - now
        if wait > 0:
            if wait > 2:
                print(c.dim(f"  …{human(wait*1000)}"))
                wait = min(wait, 2.0)
            time.sleep(wait)
        print(step_line(c, s, term_w(), args.warn, args.bad))
        if s.dur / speed > 1.5:  # animate long steps
            t_end = time.monotonic() + min(s.dur / 1000.0 / speed, 3.0)
            w = 30
            while time.monotonic() < t_end:
                frac = 1 - (t_end - time.monotonic()) / (s.dur / 1000.0 / speed)
                frac = max(0.0, min(1.0, frac))
                col = bar_color(c, s.dur, args.warn, args.bad)
                sys.stdout.write("\r  " + col("█" * int(frac * w)) +
                                 "░" * (w - int(frac * w)) +
                                 f" {human(s.dur * frac):>9}")
                sys.stdout.flush()
                time.sleep(0.08)
            sys.stdout.write("\r" + " " * (w + 14) + "\r")
    print(c.bold("■ replay complete"))

HELP = """scrub keys: n/p next·prev | g N jump | f/b next·prev slow | / text search
            d detail | w waterfall | q quit  (Enter = next)"""

def mode_scrub(c, steps, args):
    t0 = steps[0].start
    t1 = max(s.start + s.dur / 1000.0 for s in steps)
    cur, interactive = 0, sys.stdin.isatty()
    slow = sorted((s.i for s in steps if s.dur >= args.warn),
                  key=lambda i: -steps[i].dur)
    if interactive:
        print(c.bold("scrub mode — ") + c.dim(HELP))
    while True:
        st = steps[cur]
        print("\x1b[2J\x1b[H" if interactive else "\n" + "=" * 60)
        print(summary(c, steps, args.warn, args.bad))
        print()
        print(strip(c, steps, cur, t0, t1, args.warn, args.bad))
        print()
        print(detail(c, st, term_w()))
        if interactive:
            try:
                cmd = input(c.dim("\nscrub> ")).strip()
            except EOFError:
                return
        else:
            line = sys.stdin.readline()
            if not line:
                return
            cmd = line.strip()
            print(c.dim(f"scrub> {cmd}"))
        if not cmd or cmd in ("n", "l"):
            cur = min(len(steps) - 1, cur + 1)
        elif cmd in ("p", "h"):
            cur = max(0, cur - 1)
        elif cmd.startswith("g"):
            try:
                cur = max(0, min(len(steps) - 1, int(cmd[1:].strip())))
            except ValueError:
                pass
        elif cmd == "f":
            nxt = [i for i in slow if i > cur]
            cur = nxt[-1] if nxt else cur
        elif cmd == "b":
            prv = [i for i in slow if i < cur]
            cur = prv[0] if prv else cur
        elif cmd.startswith("/"):
            q = cmd[1:].lower()
            hits = [s.i for s in steps
                    if q in f"{s.name} {s.input or ''} {s.output or ''}".lower()]
            if hits:
                cur = next((h for h in hits if h > cur), hits[0])
                print(c.dim(f"  {len(hits)} hit(s), jumped to #{cur}"))
        elif cmd == "d":
            print(detail(c, st, term_w()))
        elif cmd == "w":
            print(waterfall(c, steps, t0, t1, args.warn, args.bad))
        elif cmd in ("q", "quit", "exit"):
            return

# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        prog="atr", description="Agent Trace Replayer — scrub-able JSONL "
        "timeline with per-step latency bars.")
    ap.add_argument("file", help="JSONL trace file")
    ap.add_argument("--scrub", action="store_true", help="interactive scrubber")
    ap.add_argument("--play", action="store_true", help="replay in real time")
    ap.add_argument("--speed", type=float, default=None, help="playback speed (e.g. 10)")
    ap.add_argument("--top", type=int, default=0, help="show N slowest steps")
    ap.add_argument("--grep", metavar="STR", help="filter steps by regex")
    ap.add_argument("--from", dest="t_from", help="start time (epoch/ISO)")
    ap.add_argument("--to", dest="t_to", help="end time (epoch/ISO)")
    ap.add_argument("--warn-ms", dest="warn", type=float, default=2000,
                    help="yellow threshold ms (default 2000)")
    ap.add_argument("--bad-ms", dest="bad", type=float, default=10000,
                    help="red threshold ms (default 10000)")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--export", metavar="FILE",
                    help="dump normalized JSONL (works with hermes: sources)")
    args = ap.parse_args()

    if args.file.startswith("hermes:"):
        query = args.file[len("hermes:"):]
        listing = load_hermes(query, list_only=(query == ""))
        if not isinstance(listing, list):
            print(listing)
            return
        steps = listing
        print(f"hermes session: {query}")
    else:
        steps = load_trace(args.file)
    if not steps:
        sys.exit("no events parsed")
    if args.export:
        with open(args.export, "w", encoding="utf-8") as fh:
            for s in steps:
                fh.write(json.dumps({
                    "ts": s.start, "type": s.kind, "name": s.name,
                    "duration_ms": s.dur, "input": s.input,
                    "output": s.output, "status": s.status},
                    ensure_ascii=False) + "\n")
        print(f"exported {len(steps)} steps -> {args.export}")
        if args.file.startswith("hermes:") and not (
                args.scrub or args.play or args.top or args.grep):
            pass  # fall through to summary too
    tf, tt = parse_ts(args.t_from), parse_ts(args.t_to)
    if tf is not None or tt is not None:
        steps = [s for s in steps
                 if (tf is None or s.start >= tf) and (tt is None or s.start <= tt)]
        if not steps:
            sys.exit("no events in time window")
    color = (not args.no_color and sys.stdout.isatty()
             and not os.environ.get("NO_COLOR"))
    c = C(color)

    global start_clock
    if args.play:
        start_clock = time.monotonic()
        mode_play(c, steps, args)
    elif args.scrub:
        mode_scrub(c, steps, args)
    elif args.grep:
        mode_grep(c, steps, args)
    elif args.top:
        mode_top(c, steps, args)
    else:
        mode_default(c, steps, args)

if __name__ == "__main__":
    main()
