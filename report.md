# Milestone 1 report

- Name: Jiahao Chen Zhou
- UNI: jc6980

Every run used the course runner (`./run_student_openai.sh 1`, mini-swe-agent with `openai/gpt-5.6-luna`, 
50 minute and $3 caps). After each run I re-ran my checks against the job's `artifacts/app/` in a clean Ubuntu 24.04
container (fresh `pip install -r requirements.txt`, `./start.sh`, then the checks). The counts
below come from that re-check, not from the agent's own output. In every run the counts matched
the last `RESULT` lines the agent saw.

## Approach 1

Single-file layout, A and B together. Spec: `milestone-1-specs/single-file/`.

### Run 1

- Component: both (A and B)
- Spec and checks version: v1
- Job folder: `jobs/2026-10-27__18-44-33/`
- My check results: `check_roles.sh` 63 passed, 0 failed; `check_channels.sh` 54 passed, 0 failed
- Cost and time: $0.88, 8m 45s, 69 agent steps, 0 exceptions, 0 format errors
- Problems:
  - The agent read `/spec/README.md` and both spec files first, then wrote an 18.7 KB `server.py`
    in a single heredoc, against the README's advice to write in smaller steps. After that it
    patched in small steps.
  - It built A first. `check_roles.sh` went 12/51 → 20/43 → 50/13 → 59/4 → 58/5 (a fix to
    invitations 8.7/8.8 briefly broke 8.4 to 8.6) → 63/0.
  - Then it built B. `check_channels.sh` went 23/31 → 47/7 → 63/0 and 54/0, using `ONLY=` to
    re-run single sections. Once it passed rule numbers (`ONLY="2.5 2.6 …"`) instead of section
    numbers and got `0 passed, 0 failed`; it noticed and moved on. Rule 3.6 needed three tries.
  - It submitted only after seeing `RESULT: 63 passed, 0 failed` and `RESULT: 54 passed, 0 failed`
    in the same step.

### Final `/app`

- Job folder: `jobs/2026-10-27__18-44-33/`
- My check results for A and B:

  | Check | Passed | Failed | Notes |
  | --- | --- | --- | --- |
  | `check_roles.sh` (A) | 63 | 0 | all sections 1 to 8 |
  | `check_channels.sh` (B) | 54 | 0 | all sections 1 to 8 |

## Approach 2

Single-file layout, one component at a time. Spec A and Spec B are the files in
`milestone-1-specs/single-file/`; the READMEs used are in `spec-variants/approach2-A/` and
`spec-variants/approach2-B/`.

### Run 1

- Component: A
- Spec and checks version: v1
- Job folder: `jobs/2026-10-27__18-42-16/`
- My check results: `check_roles.sh` 63 passed, 0 failed
- Cost and time: $0.05, 1m 53s, 10 agent steps, 0 exceptions, 0 format errors
- Problems:
  - A very short run. The agent read the README, `roles.md` and the check script's source, then
    wrote `server.py` in two large heredocs (230 lines).
  - First full check: 58 passed, 5 failed (1.2 WebSocket token, 4.3 private channel join, 5.6
    `role: owner` must be 400, 8.7/8.8 targeted invitations). One patch fixed all five; a subset
    run gave 36/0 and the full check 63/0, and it submitted.
  - Earlier, one check ran before the cluster was up.

### Run 2

- Component: B (Spec A frozen as in Run 1)
- Spec and checks version: v1
- Job folder: `jobs/2026-10-27__19-00-25/`
- My check results: `check_channels.sh` 54 passed, 0 failed
- Cost and time: $0.38, 5m 13s, 45 agent steps, 0 exceptions, 0 format errors
- Problems:
  - The agent wrote `server.py` in two heredocs (8.8 KB + 11.9 KB), including the minimal
    Component A routes listed in section 1 of `channels.md`. First full check: 18/36.
  - It then worked one section at a time with `ONLY=` (section 2: 5/9 → 10/4 → 12/2 → 14/0, then
    sections 3 to 8). Each section reached 0 failed on its own.
  - One patch in section 8 stopped the cluster from starting (`FAIL 0 cluster is not answering`).
    It fixed this on the next step.
  - The first full run after all sections passed alone showed **50 passed, 4 failed** (3.6 and
    events 8.5 to 8.7). This was a regression that only the whole-file check caught. One more
    patch gave 54/0, then it submitted.

### Final `/app`

Component A and Component B come from separate runs, so they are separate `/app`s.

- Job folder for A: `jobs/2026-10-27__18-42-16/`
- Job folder for B: `jobs/2026-10-27__19-00-25/`
- My check results for A and B:

  | Check | Passed | Failed | Notes |
  | --- | --- | --- | --- |
  | `check_roles.sh` (A) | 63 | 0 | `/app` of the A run |
  | `check_channels.sh` (B) | 54 | 0 | `/app` of the B run |

## Approach 3

Staged layout, A and B together. Spec: `milestone-1-specs/staged/` (A.1 to A.4, B.1 to B.4).

### Run 1

- Component: both (A and B)
- Spec and checks version: v1
- Job folder: `jobs/2026-10-27__18-53-33/`
- My check results: `check_roles_all.sh` 63 passed, 0 failed; `check_channels_all.sh` 54 passed,
  0 failed
- Cost and time: $0.40, 5m 16s, 48 agent steps, 0 exceptions, 0 format errors
- Problems:
  - The agent read the README and the spec files, wrote a 25 KB `server.py` in one heredoc, and
    then followed the README's order one stage at a time. It ran each stage's check until it
    printed `0 failed` before moving on:
    - roles_1: 10/1 → 11/0
    - roles_2: 15/0 on the first try
    - channels_1: 8/9 → 16/1 → 17/0
    - channels_2: 9/6 → 13/2 → 15/0
    - channels_3: 4/3 → 7/0
    - roles_3: 24/0 on the first try
    - roles_4: 9/4 → 11/2 → 13/0
    - channels_4: 11/4 → 14/1 → 15/0
  - The README asks for the component's `_all` check after every stage. The agent ran the `_all`
    checks only once, at the end (63/0 and 54/0). Nothing had regressed, so it did not matter in
    this run.
  - It submitted only after both `_all` checks printed `0 failed`.

### Final `/app`

- Job folder: `jobs/2026-10-27__18-53-33/`
- My check results for A and B:

  | Check | Passed | Failed | Notes |
  | --- | --- | --- | --- |
  | `check_roles_all.sh` (A.1 to A.4) | 63 | 0 | stage counts 11 + 15 + 24 + 13 |
  | `check_channels_all.sh` (B.1 to B.4) | 54 | 0 | stage counts 17 + 15 + 7 + 15 |

## Approach 4

Staged layout, one component at a time. Spec A and Spec B are the files in
`milestone-1-specs/staged/`; the READMEs used are in `spec-variants/approach4-A/` and
`spec-variants/approach4-B/`.

### Run 1

- Component: A
- Spec and checks version: v1
- Job folder: `jobs/2026-10-27__19-05-52/`
- My check results: `check_roles_all.sh` 63 passed, 0 failed
- Cost and time: $0.10, 2m 35s, 18 agent steps, 0 exceptions, 0 format errors
- Problems:
  - The agent wrote one 14.6 KB `server.py`, then went through the stages in order:
    - roles_1: 10/1 (1.2 WebSocket token) → 11/0
    - roles_2: 15/0 and roles_3: 24/0, both on the first try
    - roles_4: 11/2 (targeted invitations 8.7 and 8.8) → 13/0
    - roles_all: 63/0, then it submitted
  - As in Approach 3, it ran `check_roles_all.sh` only at the end, not after each stage.
  - One early check ran before the cluster was up.

### Run 2

- Component: B (Spec A frozen as in Run 1)
- Spec and checks version: v1
- Job folder: `jobs/2026-10-27__19-08-30/`
- My check results: `check_channels_all.sh` 54 passed, 0 failed
- Cost and time: $0.16, 3m 11s, 24 agent steps, 0 exceptions, 0 format errors
- Problems:
  - channels_1 took six check runs (1/16 → 3/14 → 7/10 → 13/4 → 17/0). It covers the minimal
    Component A routes plus channel management, so most of the setup work landed in this stage.
  - channels_2 (15/0) and channels_3 (7/0) passed on the first try.
  - channels_4 went 5/10 → 15/0 in one patch. channels_all gave 54/0, then it submitted.
  - `_all` ran only at the end.

### Final `/app`

Component A and Component B come from separate runs, so they are separate `/app`s.

- Job folder for A: `jobs/2026-10-27__19-05-52/`
- Job folder for B: `jobs/2026-10-27__19-08-30/`
- My check results for A and B:

  | Check | Passed | Failed | Notes |
  | --- | --- | --- | --- |
  | `check_roles_all.sh` (A.1 to A.4) | 63 | 0 | `/app` of the A run |
  | `check_channels_all.sh` (B.1 to B.4) | 54 | 0 | `/app` of the B run |

## Comparison of the approaches

| Approach | Run(s) | Cost | Wall time | Agent steps | A result | B result |
| --- | --- | --- | --- | --- | --- | --- |
| 1 single-file, together | `2026-10-27__18-44-33` | $0.88 | 8m 45s | 69 | 63/0 | 54/0 |
| 2 single-file, one at a time | A: `2026-10-27__18-42-16`, B: `2026-10-27__19-00-25` | $0.05 + $0.38 | 1m 53s + 5m 13s | 10 + 45 | 63/0 | 54/0 |
| 3 staged, together | `2026-10-27__18-53-33` | $0.40 | 5m 16s | 48 | 63/0 | 54/0 |
| 4 staged, one at a time | A: `2026-10-27__19-05-52`, B: `2026-10-27__19-08-30` | $0.10 + $0.16 | 2m 35s + 3m 11s | 18 + 24 | 63/0 | 54/0 |

Both layouts were enough for a full pass of both components: Approaches 1 and 3 each
reached 63/63 and 54/54, so with these specs the layout did not change the
final score. It did change how the agent got there.

- **Single-file (Approach 1):** the agent worked from one large check per component. It saw long
  failure lists (51, then 43, then 13 failing rules) and fixed them in big batches. One batch
  fixed invitation rules 8.7/8.8 but broke 8.4 to 8.6, and it relied on `ONLY=` to narrow the
  output.
- **Staged (Approach 3):** each check covered 7 to 24 rules, so the agent saw at most 9 failures
  at a time. It finished in fewer steps (48 against 69), less time (5m 16s against 8m 45s) and at
  less than half the cost ($0.40 against $0.88), with no regressions.

The one weakness I saw in the staged run was the skipped `_all` checks between stages. A
regression in an earlier stage could slip through until the end, so a future version of the
README should make the `_all` check part of each stage's check output.

The Approaches 2 and 4 point the same way:

- **Component A** was easy in both layouts. Single-file took 10 steps and $0.05; staged took 18
  steps and $0.10, spending its extra steps on stage checks.
- **Component B** was where staging helped:
  - **Single-file (Approach 2):** 45 steps, $0.38. Every section passed on its own with `ONLY=`,
    but the whole-file check then showed 4 new failures (3.6 and events 8.5 to 8.7). One patch
    also stopped the cluster from starting.
  - **Staged (Approach 4):** 24 steps, $0.16. Stages 2 and 3 passed on the first try and there
    were no regressions.

Across the four approaches:

- Doing both components in one run cost more than the two separate runs added together: about 2×
  for single-file ($0.88 against $0.43) and about 1.5× for staged ($0.40 against $0.26).
- The staged layout was cheaper and steadier in both settings.
