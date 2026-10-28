# Read this first: work order and rules for this run

This run is about one component of the Slack clone: **B, channels and message history**. It is written as four specs that build on each other. Before you run
`echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`, you MUST have run the full check below and
seen all 54 rules pass. Do not submit while it prints `FAIL`, unless the time rule at the end
says so.

The task description you were given is still the grading contract. The files in `/spec` do
not change it; they tell you what to build in this run, in what order, and how to prove it
works. Where `/spec` is more precise than the task description, follow `/spec`.

## Files in /spec

| File | What it is | Builds on |
|---|---|---|
| `README.md` | This file | |
| `channels/channels-1.md` | B.1: conventions, objects, paging rules, the Component A routes B needs, channel management (17 rules) | nothing |
| `channels/channels-2.md` | B.2: messages, threads, history and paging (15 rules) | B.1 |
| `channels/channels-3.md` | B.3: events over WebSocket (7 rules) | B.1, B.2 |
| `channels/channels-4.md` | B.4: search, pins, mentions, read state (15 rules) | B.1 to B.3 |
| `channels/checks/check_channels_1.sh` … `_4.sh` | PASS or FAIL for each rule of one spec | |
| `channels/checks/check_channels_all.sh` | All 54 rules and one total | |
| `channels/checks/channels_rules.sh`, `check_channels.py` | The engine the checks call; do not run or edit them directly | |

## Scope

Build only what is described in the four `channels-N.md` files, including the Component A routes its section 1 lists (register, workspaces, channel join, role change): build those minimally first, exactly as `instruction.md` describes them. Do NOT build files, DMs, groups, slash
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
  the last one it saw and pushes them to its own WebSocket subscribers (`channels-3.md`).
- **`/app/start.sh`** stays in the foreground, starts the three nodes (`node_id` 0, 1, 2 on
  ports 8000, 8001, 8002), and restarts any node that dies, for example one
  `while true; do python3 /app/server.py $i 800$i; sleep 1; done &` loop per node followed by
  `wait`. Make it executable.

## Order of work

Each spec has **Uses** (functions from earlier specs that you must call, not rewrite), **Adds**
(functions it introduces, with the names to use), and the rules, numbered as the checks print them.
For each spec: read it, build it, run its check until it prints `0 failed`, then run
`check_channels_all.sh` to make sure nothing earlier broke.

1. `channels-1.md` → `check_channels_1.sh`
2. `channels-2.md` → `check_channels_2.sh`
3. `channels-3.md` → `check_channels_3.sh`
4. `channels-4.md` → `check_channels_4.sh`

Do not start a spec while a rule from an earlier spec is failing. Fix one failing rule at a time,
lowest number first. Write code in several smaller steps, not one giant file write.

## How to verify

```bash
bash /spec/channels/checks/check_channels_1.sh
bash /spec/channels/checks/check_channels_2.sh
bash /spec/channels/checks/check_channels_3.sh
bash /spec/channels/checks/check_channels_4.sh
bash /spec/channels/checks/check_channels_all.sh
```

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

Run `bash /app/timer.sh` to see how much time is left. Check it after each spec.
If fewer than 5 minutes remain, stop adding features: make sure `start.sh` boots and the
check runs, then submit.

## Before you submit

You may submit only when all of these are true, from output you actually saw:

- the last line of `check_channels_all.sh` is `RESULT: 54 passed, 0 failed`;
- `/app/start.sh` and `/app/requirements.txt` exist, and `GET /api/health` returns `200` on
  ports `8000`, `8001`, and `8002`.

These rules are necessary, not sufficient. The full task in the grading contract still
applies.
