# Read this first: work order and rules for this run

This run is about one component of the Slack clone: **B, channels and message history**. Before you run
`echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`, you MUST have run the check below and
seen all 54 rules pass. Do not submit while it prints `FAIL`, unless the time rule at the end
says so.

The task description you were given is still the grading contract. The files in `/spec` do
not change it; they tell you what to build in this run, in what order, and how to prove it
works. Where `/spec` is more precise than the task description, follow `/spec`.

## Files in /spec

| File | What it is |
|---|---|
| `README.md` | This file |
| `channels/channels.md` | Component B: 54 rules (channels, messages, paging, search, pins, mentions, read state, events) |
| `channels/checks/check_channels.sh` | Prints PASS or FAIL for each rule in `channels.md` (runs `check_channels.py`) |

## Scope

Build only what is described in `channels/channels.md`, including the Component A routes its section 1 lists (register, workspaces, channel join, role change): build those minimally first, exactly as `instruction.md` describes them. Do NOT build files, DMs, groups, slash
commands, the IRC gateway or the UI in this run. If `/app` already has code, improve it; do
not start over.

## Architecture that fits in 50 minutes

Use this unless `/app` already has something that works:

- **One Python file**, `/app/server.py`, using `aiohttp` (HTTP and WebSocket in one library).
  Run it as `python3 /app/server.py <node_id> <port>`. Put `aiohttp` in
  `/app/requirements.txt` and `pip install -r /app/requirements.txt`.
- **One SQLite file**, `/app/data/app.db`, opened by all three node processes, with
  `PRAGMA journal_mode=WAL` and `busy_timeout`. Every write that allocates an id or an `event_id` runs
  inside `BEGIN IMMEDIATE ... COMMIT`. Never keep state in Python globals, because a token or
  workspace created on `8000` must work on `8002`.
- **Cross-node events**: every node polls the events table every 50 ms for rows newer than
  the last one it saw and pushes them to its own WebSocket subscribers (`channels.md` section 8).
- **`/app/start.sh`** stays in the foreground, starts the three nodes (`node_id` 0, 1, 2 on
  ports 8000, 8001, 8002), and restarts any node that dies, for example one
  `while true; do python3 /app/server.py $i 800$i; sleep 1; done &` loop per node followed by
  `wait`. Make it executable.

## Order of work

1. Read `channels/channels.md` in full.
2. Build section 1 (baseline and the Component A routes it lists), then sections 2 to 8 in
   order. Run the check after each section and fix failures before going on.
3. Fix one failing rule at a time, lowest number first. If a rule that passed starts failing,
   fix that before anything else. Write code in several smaller steps, not one giant file write.

## How to verify

```bash
bash /spec/channels/checks/check_channels.sh
```

To see one section only: `ONLY="5" bash /spec/channels/checks/check_channels.sh`.

Each prints one line per rule and a final summary:

```text
PASS 2.1 ...
FAIL 2.2 ...: expected 409, got 201
RESULT: 10 passed, 1 failed
```

- A check starts `/app/start.sh` itself if the cluster is not answering, and finishes in
  under 30 seconds. Each run uses fresh usernames, so you can rerun it any time.
- After you change code, restart the cluster before checking again: find the PID that owns
  each port with `ss -ltnp` and `kill` that PID (the `start.sh` loop brings the node back with
  the new code within a second or two). Never stop processes by name (`pkill`, `killall`).
- Server errors go to your log; if a rule returns `500`, read the log first.

## Time

Run `bash /app/timer.sh` to see how much time is left. Check it after each section.
If fewer than 5 minutes remain, stop adding features: make sure `start.sh` boots and the
check runs, then submit.

## Before you submit

You may submit only when all of these are true, from output you actually saw:

- the last line of `check_channels.sh` is `RESULT: 54 passed, 0 failed`;
- `/app/start.sh` and `/app/requirements.txt` exist, and `GET /api/health` returns `200` on
  ports `8000`, `8001`, and `8002`.

These rules are necessary, not sufficient. The full task in the grading contract still
applies.
