# Read this first: work order and rules for this run

This run is about two components of the Slack clone: **A, roles and invitations** and
**B, channels and message history**. Each is written as four specs that build on each other.
Before you run `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`, you MUST have run both full
checks below and seen them pass. Do not submit while a check prints `FAIL`, unless the time
rule at the end says so.

The task description you were given is still the grading contract. The files in `/spec` do
not change it; they tell you what to build in this run, in what order, and how to prove it
works. Where `/spec` is more precise than the task description, follow `/spec`.

## Files in /spec

| File | What it is | Builds on |
|---|---|---|
| `README.md` | This file: scope, architecture, order of work, how to verify, when to submit | |
| `roles/roles-1.md` | A.1: conventions, cluster baseline, auth (11 rules) | nothing |
| `roles/roles-2.md` | A.2: workspaces, joining channels, join mode (15 rules) | A.1 |
| `roles/roles-3.md` | A.3: roles, ownership transfer, role-based channel permissions (24 rules) | A.1, A.2 |
| `roles/roles-4.md` | A.4: invitations (13 rules) | A.1 to A.3 |
| `channels/channels-1.md` | B.1: conventions, objects, paging rules, channel management (17 rules) | A.1, A.2 |
| `channels/channels-2.md` | B.2: messages, threads, history and paging (15 rules) | B.1 |
| `channels/channels-3.md` | B.3: events over WebSocket (7 rules) | B.1, B.2 |
| `channels/channels-4.md` | B.4: search, pins, mentions, read state (15 rules) | B.1 to B.3 |
| `roles/checks/check_roles_1.sh` … `_4.sh` | Print PASS or FAIL for each rule of one A spec | |
| `roles/checks/check_roles_all.sh` | All 63 A rules and one total | |
| `channels/checks/check_channels_1.sh` … `_4.sh` | Print PASS or FAIL for each rule of one B spec | |
| `channels/checks/check_channels_all.sh` | All 54 B rules and one total | |
| `roles/checks/roles_rules.sh`, `channels/checks/channels_rules.sh`, `channels/checks/check_channels.py` | The check engines the scripts above call; do not run or edit them directly | |

Each spec has the same parts: **Uses** (functions from earlier specs that you must call, not
rewrite), **Adds** (functions this spec introduces, with the names to use), and the rules,
numbered as the checks print them. Section 0 of `roles-1.md` and of `channels-1.md` applies to
every later spec of that component.

## Scope

Build only what the eight spec files describe. Do NOT build files, DMs, groups, slash
commands, the IRC gateway or the UI in this run. If `/app` already has code, improve it; do
not start over.

## Architecture that fits in 50 minutes

Use this unless `/app` already has something that works:

- **One Python file**, `/app/server.py`, using `aiohttp` (HTTP and WebSocket in one library).
  Run it as `python3 /app/server.py <node_id> <port>`. Put `aiohttp` in
  `/app/requirements.txt` and `pip install -r /app/requirements.txt`.
- **One SQLite file**, `/app/data/app.db`, opened by all three node processes, with
  `PRAGMA journal_mode=WAL` and `busy_timeout`. Every write that allocates an id or an
  `event_id` runs inside `BEGIN IMMEDIATE ... COMMIT`. Never keep state in Python globals,
  because a token or workspace created on `8000` must work on `8002`.
- **Cross-node events**: every node polls the events table every 50 ms for rows newer than
  the last one it saw and pushes them to its own WebSocket subscribers (`channels-3.md`).
- **`/app/start.sh`** stays in the foreground, starts the three nodes (`node_id` 0, 1, 2 on
  ports 8000, 8001, 8002), and restarts any node that dies, for example one
  `while true; do python3 /app/server.py $i 800$i; sleep 1; done &` loop per node followed by
  `wait`. Make it executable.

## Order of work

Work through the specs in this order. For each one: read it, build it using the functions
from the earlier specs, run its check until it prints `0 failed`, then run the component's
`_all` check to make sure nothing earlier broke.

1. `roles-1.md` → `check_roles_1.sh`
2. `roles-2.md` → `check_roles_2.sh`
3. `channels-1.md` → `check_channels_1.sh`
4. `channels-2.md` → `check_channels_2.sh`
5. `channels-3.md` → `check_channels_3.sh`
6. `roles-3.md` → `check_roles_3.sh`
7. `roles-4.md` → `check_roles_4.sh`
8. `channels-4.md` → `check_channels_4.sh`

Do not start a spec while a rule from an earlier spec in this list is failing. If a rule that
passed starts failing, fix that before anything else. Fix one failing rule at a time, lowest
number first. Write code in several smaller steps rather than one giant file write.

## How to verify

Run each as a single command, for example:

```bash
bash /spec/roles/checks/check_roles_2.sh
bash /spec/roles/checks/check_roles_all.sh
bash /spec/channels/checks/check_channels_3.sh
bash /spec/channels/checks/check_channels_all.sh
```

Each prints one line per rule and a final summary:

```text
PASS 3.1 create workspace returns workspace and #general
FAIL 4.7 stranger joining an invite_only workspace: expected 403, got 200
RESULT: 14 passed, 1 failed
```

- A check starts `/app/start.sh` itself if the cluster is not answering, and finishes in
  under 30 seconds. Each run uses fresh usernames, so you can rerun it any time.
- After you change code, restart the cluster before checking again: find the PID that owns
  each port with `ss -ltnp` and `kill` that PID (the `start.sh` loop brings the node back with
  the new code within a second or two). Never stop processes by name (`pkill`, `killall`).
- Server errors go to your log; if a rule returns `500`, read the log first.

## Time

Run `bash /app/timer.sh` to see how much time is left. Check it after each spec.

- Aim to finish steps 1 to 5 within 30 minutes and steps 6 to 8 within 45.
- If fewer than 5 minutes remain, stop adding features: make sure `start.sh` boots and the
  checks run, then submit.

## Before you submit

You may submit only when all of these are true, from output you actually saw:

- the last line of `check_roles_all.sh` is `RESULT: 63 passed, 0 failed`;
- the last line of `check_channels_all.sh` is `RESULT: 54 passed, 0 failed`;
- `/app/start.sh` and `/app/requirements.txt` exist, and `GET /api/health` returns `200` on
  ports `8000`, `8001`, and `8002`.

These rules are necessary, not sufficient. The full task in the grading contract still
applies.
