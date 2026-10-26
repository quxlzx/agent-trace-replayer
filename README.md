# atr — Agent Trace Replayer

Take a JSONL log of an agent run (tool calls, outputs, timings) and replay it
in the terminal as a **scrub-able timeline with per-step latency bars**.
Debug why your agent took 4 minutes on one step.

Single file, stdlib-only Python ≥3.9. No dependencies.

```
$ atr trace.jsonl
── trace.jsonl ──
wall 4m52.3s  steps 12  attributed 4m42.4s  unattributed/idle 9.86s
  time by kind: tool 4m31.8s | think 7.40s | llm 3.20s | user 0ms

waterfall:
  0 👤 user                 █       0ms
  1 ⚙ read_file            █     120ms
  2 ⚙ terminal             █     4.20s
  3 💭 thinking             █     3.00s
  4 ⚙ web_search           █     1.80s
  5 ⚙ web_fetch            ███████████████████████████████████████   4m01.0s
  6 ⚙ terminal             █     6.90s
  ...

diagnosis
  SLOW #5 web_fetch took 4m01.0s (57× median 4.20s, p95 4m01.0s)
        in : https://github.com/example/aiohttp/issues/7123
        out: fetched after 3 retries (TLS handshake stalls behind proxy)
```

## Install

```bash
curl -O https://raw.githubusercontent.com/quxlzx/agent-trace-replayer/main/atr.py
chmod +x atr.py && mv atr.py ~/.local/bin/atr   # or anywhere on PATH
```

## Usage

| Command | What it does |
|---|---|
| `atr trace.jsonl` | waterfall + summary + auto-diagnosis |
| `atr trace.jsonl --scrub` | interactive playhead (see keys below) |
| `atr trace.jsonl --play --speed 30` | timed replay with animated bars |
| `atr trace.jsonl --top 5` | the 5 slowest steps |
| `atr trace.jsonl --grep pytest` | filter steps by regex |
| `atr trace.jsonl --from 2026-10-26T14:00 --to 2026-10-26T15:00` | time-window filter (epoch/ISO) |
| `atr --warn-ms 5000 --bad-ms 30000 ...` | bar color thresholds |

Scrub keys: `n`/`p` next/prev · `g N` jump · `f`/`b` hop between slow steps ·
`/text` search · `d` detail · `w` waterfall · `q` quit. Scrub also reads
commands from stdin, so it scripts cleanly.

## Trace format

One JSON event per line; keys are auto-detected:

```json
{"ts": 1729900000.12, "type": "tool_call", "name": "terminal",
 "input": "pytest -q", "duration_ms": 4210, "output": "...", "status": "ok"}
```

- **Timestamps:** `ts` / `timestamp` / `time` — epoch seconds, epoch ms, or ISO-8601
- **Durations:** `duration_ms` / `duration` / `ms`, or `end_ts`, or start/end
  events sharing an `id` / `call_id` / `tool_use_id`
- **Payloads:** `input` / `args` / `arguments`, `output` / `result` / `content`
- **Errors:** `status`, `error`, `is_error`
- Missing timestamps are derived from neighboring events.

Run `python3 gen_sample.py` to generate a demo trace (a flaky-test debugging
session with one pathological 4-minute fetch).

## Hermes adapter

If you run [Hermes Agent](https://hermes-agent.nousresearch.com), `atr` reads
sessions straight from `~/.hermes/state.db` (read-only):

```bash
atr hermes:              # list recent sessions
atr hermes:last          # replay the most recent run
atr hermes:<id-or-title-substring>
atr hermes:last --export run.jsonl   # normalized JSONL for sharing
```

Tool durations come from assistant→tool message timestamps; LLM generation
time is materialized as its own `🧠 generate` steps, so "the model was slow"
and "the tool was slow" stay distinguishable in the `time by kind` split.

## Reading the diagnosis

- `SLOW #N` — step ≥ max(bad threshold, 3× median): the latency culprit
- `GAP` — unattributed idle before a step (model queueing / user wait), not tool time
- `time by kind` — answers "was it the tools or the model?" at a glance

## License

MIT
