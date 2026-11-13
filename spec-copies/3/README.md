# Read this first: work order and rules for this run

This run builds the Slack clone of the task description: **D, cluster robustness**; **C, features built on messages**; **E, IRC gateway, web UI and message edits**.
Each component is written as a few spec files that build on each other, each with its own check.
Before you run `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`, you MUST have seen every check named in
"Before you submit" pass. Do not submit while a check reports a failure, unless the time rule says so.

The task description you were given is still the grading contract. The files in `/spec` do not
change it; they tell you what to build in this run, in what order, and how to prove it works.
Where `/spec` is more precise than the task description, follow `/spec`.

## Files in /spec

| File | What it is |
|---|---|
| `README.md` | This file: scope, architecture, order of work, how to verify, when to submit |
| `d/d-1.md` | D.1: conventions, base, the supervisor and a killed Redis (11 rules) |
| `d/d-2.md` | D.2: a killed HTTP node (6 rules) |
| `d/d-3.md` | D.3: performance under concurrent writes (5 rules) |
| `d/checks/check_d_1.sh` … `_3.sh` | The check of each D spec, same number |
| `d/checks/lib.sh`, `chk.py`, `d_rules.py` | Helpers the D checks load; read them freely, but run only the `check_*.sh` scripts |
| `c/c-1.md` | C.1: conventions, base, reactions (14 rules) |
| `c/c-2.md` | C.2: DMs and profiles (10 rules) |
| `c/c-3.md` | C.3: files and slash commands (14 rules) |
| `c/c-4.md` | C.4: user groups, @group-handle, @here (11 rules) |
| `c/checks/check_c_1.sh` … `_4.sh` | The check of each C spec, same number |
| `c/checks/lib.sh`, `chk.py`, `c_rules.py` | Helpers the C checks load; read them freely, but run only the `check_*.sh` scripts |
| `e/e-1.md` | E.1: conventions, base, editing and deleting messages (13 rules) |
| `e/e-2.md` | E.2: IRC gateway, registration and commands (9 rules) |
| `e/e-3.md` | E.3: bridging IRC and the web (6 rules) |
| `e/e-4.md` | E.4: web UI shell, pinned hooks, button colours (6 rules) |
| `e/e-5.md` | E.5: web UI sign-up, workspace, channels (8 rules) |
| `e/e-6.md` | E.6: web UI messages, edits, threads, reactions (9 rules) |
| `e/checks/check_e_1.sh` … `_6.sh` | The check of each E spec, same number |
| `e/checks/lib.sh`, `chk.py`, `e_rules.py`, `e_ui.py` | Helpers the E checks load; read them freely, but run only the `check_*.sh` scripts |

Each spec file has the same parts: **Uses** (functions from earlier specs that you must call,
not rewrite), **Adds** (functions it introduces, with the names to use) and numbered rules,
numbered as its check prints them. Section 0 of the first spec of a component applies to all
of that component's specs.

## Scope

Build Components D, C and E.
Do **not** build Components A and B in full: build only the base that the first spec of each
component lists in its section 1 (health, register/login/me, workspaces, channels, joining,
messages, replies, WebSocket subscribe/resume). No roles, invitations, search, pins or read state.
Shared pieces: the IRC gateway is built in E (D's rules do not need it); E's web UI needs reactions and
threads, which E.1 asks for if C is not in this run.
If `/app` already has code, improve it; do not start over.

## Architecture that fits in 50 minutes

Use this unless `/app` already has something that works:

- **One Python file**, `/app/server.py`, using `aiohttp` (HTTP and WebSocket in one library). It runs
  as an HTTP node with `python3 /app/server.py node <node_id> <port>` and, if the IRC gateway is in this
  run, as the gateway with `python3 /app/server.py irc 6667`. Put `aiohttp` in `/app/requirements.txt`
  and `pip install -r /app/requirements.txt`. Create the app with
  `web.Application(client_max_size=11 * 1024 * 1024)`.
- **One SQLite file**, `/app/data/app.db`, opened once per process with `PRAGMA journal_mode=WAL`,
  `PRAGMA synchronous=NORMAL` and `PRAGMA busy_timeout=5000`. Every write that allocates an id or an
  `event_id` runs inside `BEGIN IMMEDIATE ... COMMIT`. Never keep state in Python globals (except each
  process's own open sockets), because a token or message created on `8000` must work on `8002`.
- **Events**: one `events` table (`channel_id`, `event_id`, the JSON frame). Every node (and the IRC
  gateway) polls it every 50 ms for rows newer than the last one it saw and pushes them to its own
  sockets. This keeps working when Redis or another node dies. Start the poller so that it never blocks
  startup (aiohttp awaits every `on_startup` callback before it listens, so never await the endless loop):

  ```python
  async def start_pump(app):
      app['pump'] = asyncio.create_task(event_pump(app))   # schedule it, do not await it
  app.on_startup.append(start_pump)
  ```

  Inside `event_pump`, catch and log every exception per iteration so one bad row cannot stop delivery.
- **`/app/start.sh`** stays in the foreground and runs every process in its own restart loop:

  ```bash
  #!/bin/bash
  cd /app && mkdir -p /app/data
  ( while true; do redis-server --port 6379 --bind 127.0.0.1 --save '' --appendonly no; sleep 0.5; done ) &
  for i in 0 1 2; do ( while true; do python3 /app/server.py node $i 800$i; sleep 0.5; done ) & done
  ( while true; do python3 /app/server.py irc 6667; sleep 0.5; done ) &   # only if the IRC gateway is in this run
  wait
  ```

  Make it executable. `GET /` serves `/app/static/index.html` if the web UI is in this run.
- Write code in several smaller steps (one route group per command) rather than one giant file write.

## Order of work

1. `d/d-1.md` → `bash /spec/d/checks/check_d_1.sh`
2. `d/d-2.md` → `bash /spec/d/checks/check_d_2.sh`
3. `d/d-3.md` → `bash /spec/d/checks/check_d_3.sh`
4. `c/c-1.md` → `bash /spec/c/checks/check_c_1.sh`
5. `c/c-2.md` → `bash /spec/c/checks/check_c_2.sh`
6. `c/c-3.md` → `bash /spec/c/checks/check_c_3.sh`
7. `c/c-4.md` → `bash /spec/c/checks/check_c_4.sh`
8. `e/e-1.md` → `bash /spec/e/checks/check_e_1.sh`
9. `e/e-2.md` → `bash /spec/e/checks/check_e_2.sh`
10. `e/e-3.md` → `bash /spec/e/checks/check_e_3.sh`
11. `e/e-4.md` → `bash /spec/e/checks/check_e_4.sh`
12. `e/e-5.md` → `bash /spec/e/checks/check_e_5.sh`
13. `e/e-6.md` → `bash /spec/e/checks/check_e_6.sh`

For each step: read the spec, build it using the functions from the earlier specs, and run its
check until it reports no failures.
Do not start a step while a check of an earlier step fails (except as the time rule below allows).
If something that passed starts failing, fix it first. Fix one failing rule at a time, lowest number first.

## How to verify

Run each check as one command, for example:

```bash
bash /spec/d/checks/check_d_1.sh
bash /spec/c/checks/check_c_1.sh
bash /spec/e/checks/check_e_1.sh
```

Every check ends with a line like `RESULT: 12 passed, 0 failed`; only `0 failed` means done.

- A check starts `/app/start.sh` itself if the cluster is not answering, and finishes in under
  30 seconds. Each run uses fresh users, so you can rerun it any time.
- Some checks kill a process on purpose (the one that owns port `8001` or `6379`); `start.sh` must
  bring it back. Wait a few seconds before the next check if one just did that.
- After you change code, restart the cluster before checking again: find the PID that owns each
  port with `ss -ltnp` and `kill` that PID; the `start.sh` loop brings the process back with the new
  code within a second or two. **Never stop processes by name** (`pkill`, `killall`, `pgrep -f`,
  parsing `ps`): that can kill you, the agent.
- Server errors go to your log; if a rule returns `500`, read the log first.
- **Do not edit any file in `/spec`**, including the checks. If a check fails, fix the code, not the check.
- Every check first prints `CHECK STARTED` and ends with a `RESULT` line. **Run it as it is, never under
  `timeout`, `|| true` or `2>/dev/null`**: it stops on its own in under 30 seconds. If you see no `RESULT`
  line, the check did not finish and **counts as failed**. Empty output is never a pass.

## Budget: about 3 US dollars of model calls

Every command you run adds its output to everything you read afterwards, so long outputs early on
are paid for again at every later step. A run that hits the budget stops on the spot, finished or not.

- Read one spec file when you start it, not all of them up front. Do not `cat` the check scripts:
  the `FAIL` lines already say what was sent and what came back.
- Run checks as `bash /spec/<x>/checks/check_<x>_N.sh 2>&1 | grep -v '^PASS'` so passing rules
  do not fill the screen (the `RESULT` line still shows).
- Write a whole route group (or a whole spec's routes) per command instead of one route per command.

## Time

You have 50 minutes. Run `bash /app/timer.sh` to see how much is left. Check it after each step.

- There are 13 steps. Spend at most about 3 minutes per step on average, and do not get stuck on one rule:
  if a single rule still fails after three attempts, move on and come back to it at the end.
- Keep checking the timer: if fewer than 5 minutes remain, stop adding features: make sure `start.sh` boots,
  run the checks once more, then submit. Before that point, do not submit while any step is unbuilt.

## Before you submit

You may submit only when all of these are true, from output you actually saw:

- every D check (`check_d_1.sh` … `_3.sh`) ends with `0 failed`;
- every C check (`check_c_1.sh` … `_4.sh`) ends with `0 failed`;
- every E check (`check_e_1.sh` … `_6.sh`) ends with `0 failed`;
- `/app/start.sh` and `/app/requirements.txt` exist, and `GET /api/health` returns `200` on ports
  `8000`, `8001` and `8002`.

These rules are necessary, not sufficient. The full task in the grading contract still applies.
