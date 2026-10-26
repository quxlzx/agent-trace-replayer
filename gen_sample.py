#!/usr/bin/env python3
"""gen_sample.py — synthesize a realistic agent-run JSONL trace.
Scenario: agent debugging a test failure; one web_fetch step hangs for 4 minutes.
Uses start/end pairs sharing an id, mixed key styles, to exercise the parser."""
import json, random, datetime

random.seed(7)
t = datetime.datetime(2026, 10, 26, 14, 2, 0).timestamp()
ev = []

def add(**kw):
    kw["ts"] = round(t, 3)
    ev.append(kw)

def step(name, typ, dur_s, **kw):
    global t
    cid = f"c{len(ev)}"
    add(type=typ, id=cid, name=name, **kw)
    t += dur_s
    add(type="tool_result" if typ == "tool_call" else "llm_result", id=cid)
    t += random.uniform(0.4, 1.6)  # model thinking between steps

add(type="user", name="user", input="fix the flaky test in tests/test_sync.py")
t += 2.1
step("read_file", "tool_call", 0.12, input="tests/test_sync.py")
step("terminal", "tool_call", 4.2, input="pytest tests/test_sync.py -q",
     output="FAILED test_sync_race::test_concurrent_writes — AssertionError")
step("thinking", "thinking", 3.0, input="race condition? need to check upstream issue tracker")
step("web_search", "tool_call", 1.8, input="aiohttp connection pool race condition github issue")
# the pathological one: fetch hangs ~4 minutes (retries under the hood)
step("web_fetch", "tool_call", 241.0,
     input="https://github.com/example/aiohttp/issues/7123",
     output="fetched after 3 retries (TLS handshake stalls behind proxy)")
step("terminal", "tool_call", 6.9, input="git bisect start HEAD HEAD~12 && git bisect run pytest tests/test_sync.py",
     output="bisect: first bad commit 9f2c1ab 'switch to shared connector pool'")
step("thinking", "thinking", 4.4, input="revert connector sharing for test client; add fixture isolation")
step("patch", "tool_call", 0.3, input="tests/test_sync.py: per-test ClientSession fixture")
step("terminal", "tool_call", 5.1, input="pytest tests/test_sync.py -q --count 20",
     output="20 passed in 4.87s")
step("terminal", "tool_call", 12.4, input="pytest -q  # full suite",
     output="412 passed, 3 skipped")
step("assistant", "llm_call", 3.2, output="fixed: flake was shared connector pool; fixture isolation added")

with open("sample.jsonl", "w") as f:
    for e in ev:
        f.write(json.dumps(e) + "\n")
print(f"wrote sample.jsonl ({len(ev)} events)")
