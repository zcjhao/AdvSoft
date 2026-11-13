# Milestone 2 report

- Name: Jiahao Chen Zhou
- UNI: jc6980

## Runs

One row per repeat of each run (22 in total). "Run" is the run in the run plan in README Setup.

| Run | Component | Repeat | Job folder | Check results | Cost | Ran checks (times) | Submitted with failures? | Problems |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1.1 | C | 1 | `2026-11-10__21-17-42` | C: 49 passed, 0 failed | $0.60 | 8 | no | read the check source 8 times and tested by hand with `curl` 16 times |
| 1.1 | C | 2 | `2026-11-10__21-44-29` | C: 49 passed, 0 failed | $0.30 | 12 | no | |
| 1.2 | D | 1 | `2026-11-10__21-34-33` | D: 22 passed, 0 failed | $0.33 | 5 | no | |
| 1.2 | D | 2 | `2026-11-10__21-54-41` | D: 22 passed, 0 failed | $0.31 | 6 | no | |
| 1.3 | E | 1 | `2026-11-10__21-16-22` | E: 51 passed, 0 failed | $1.59 | 37 | no | 116 steps; most of them on the E.5/E.6 UI checks, guessing what failed from the score |
| 1.3 | E | 2 | `2026-11-10__21-39-06` | E: 51 passed, 0 failed | $0.74 | 13 | no | |
| 2.1 | C | 1 | `2026-11-10__21-27-44` | C: 49 passed, 0 failed | $0.22 | 9 | no | |
| 2.1 | C | 2 | `2026-11-10__21-50-05` | C: 49 passed, 0 failed | $0.13 | 10 | no | |
| 2.2 | D | 1 | `2026-11-10__21-39-56` | D: 22 passed, 0 failed | $0.08 | 8 | no | |
| 2.2 | D | 2 | `2026-11-10__21-59-37` | D: 21 passed, 1 failed | $0.08 | 6 | no (the agent's last D.1 run was 11/0; the re-check failed D.1 rule 1.6, a cross-node WebSocket event) | timing-sensitive rule passed in the run and failed once in the fresh container |
| 2.3 | E | 1 | `2026-11-10__21-32-27` | E: 51 passed, 0 failed | $0.22 | 17 | no | |
| 2.3 | E | 2 | `2026-11-10__21-51-33` | E: 51 passed, 0 failed | $0.92 | 28 | no | |
| 3 | C, D, E | 1 | `2026-11-10__18-28-20` | C: 49 passed, 0 failed; D: 22 passed, 0 failed; E: 48 passed, 3 failed | $2.27 | 50 | no (all its own last runs passed; the re-check failed E.3 rules 4.3, 4.4, 4.6, IRC bridging) | D.1 took 10 check runs (steps 28 to 62) |
| 3 | C, D, E | 2 | `2026-11-11__01-56-08` | C: 49 passed, 0 failed; D: 22 passed, 0 failed; E: 50 passed, 1 failed | $1.95 | 59 | no (re-check failed E.6 rule 7.5, delete from the hover toolbar) | E.6 took 13 check runs |
| 4 | C, D, E | 1 | `2026-11-10__18-50-17` | C: 49 passed, 0 failed; D: 22 passed, 0 failed; E: 51 passed, 0 failed | $2.06 | 62 | no | E.6 took 17 check runs |
| 4 | C, D, E | 2 | `2026-11-11__01-43-19` | C: 49 passed, 0 failed; D: 22 passed, 0 failed; E: 51 passed, 0 failed | $1.76 | 60 | no | |
| 5.1 | A to E | 1 | `2026-11-10__18-27-40` | A and B: 1 broken (B 7.1); C: 48 passed, 1 failed; D: 22 passed, 0 failed; E: 51 passed, 0 failed | $3.01 | 84 | did not submit | ran out of budget at step 152 while driving the regression suite down (41, 32, 27, 21, 18, 13, 10, 7, 6) after building C, D and E |
| 5.1 | A to E | 2 | `2026-11-10__18-28-00` | A and B: 0 broken; C: 49 passed, 0 failed; D: 22 passed, 0 failed; E: 51 passed, 0 failed | $2.05 | 67 | no | |
| 5.2 | A to E | 1 | `2026-11-11__00-53-14` | A: 44 passed, 20 failed; B: 30 passed, 24 failed (44 broken); C: 49 passed, 0 failed; D: 22 passed, 0 failed; E: 47 passed, 4 failed | $2.94 | 52 | yes | ran the regression suite at step 14 and then only once more at step 174 (no output); submitted without ever seeing it pass, and with its last E.6 run showing no `RESULT` line. Invitations (A.4) were never built |
| 5.2 | A to E | 2 | `2026-11-11__01-17-19` | A: 64 passed, 0 failed; B: 33 passed, 21 failed (21 broken); C: 47 passed, 2 failed; D: 19 passed, 3 failed; E: 35 passed, 16 failed | $1.25 | 44 | yes | regression suite 88, 49, 31, 27 by step 18, then never again; submitted at $1.25 with E.5 showing no `RESULT` line and E.6 never run |
| 5.3 | A to E | 1 | `2026-11-11__00-52-47` | A: 64 passed, 0 failed; B: 54 passed, 0 failed; C: 49 passed, 0 failed; D: 22 passed, 0 failed; E: 48 passed, 3 failed | $2.28 | 76 | no (re-check failed E.6 rules 7.7 to 7.9, the emoji picker) | A and B passing by step 34 ($0.25) |
| 5.3 | A to E | 2 | `2026-11-10__21-16-56` | A: 64 passed, 0 failed; B: 50 passed, 4 failed; C: 48 passed, 1 failed; D: 22 passed, 0 failed; E: 14 passed, 37 failed | $3.02 | 70 | did not submit | A and B took until step 83 ($0.94), C.3/C.4 until step 134 ($2.25); out of budget at E.2 |

## Experiments

### 1) Feedback

> Do you find that adding detailed output to the checks helps the agent?

#### Runs compared and why

1.1 against 2.1, 1.2 against 2.2, and 1.3 against 2.3. Each pair builds the same single component
from the same spec and the same checks. The only difference is `score_only` (a `RESULT` line only)
against `verbose` (one `PASS`/`FAIL` line per rule, saying what was sent and what came back).

#### Supporting results

| Component | Run | Repeats | Result | Cost | Steps | Ran checks | Read check source | Manual `curl` tests |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| C | 1.1 score_only | 1 / 2 | 49/49, 49/49 | $0.60, $0.30 | 64, 38 | 8, 12 | 8, 5 | 16, 2 |
| C | 2.1 verbose | 1 / 2 | 49/49, 49/49 | $0.22, $0.13 | 38, 22 | 9, 10 | 0, 0 | 5, 1 |
| D | 1.2 score_only | 1 / 2 | 22/22, 22/22 | $0.33, $0.31 | 47, 44 | 5, 6 | 6, 4 | 4, 12 |
| D | 2.2 verbose | 1 / 2 | 22/22, 21/22 | $0.08, $0.08 | 16, 16 | 8, 6 | 2, 0 | 1, 0 |
| E | 1.3 score_only | 1 / 2 | 51/51, 51/51 | $1.59, $0.74 | 116, 69 | 37, 13 | 8, 10 | 8, 6 |
| E | 2.3 verbose | 1 / 2 | 51/51, 51/51 | $0.22, $0.92 | 31, 86 | 17, 28 | 3, 2 | 2, 9 |

"Read check source" counts commands that `cat`/`sed`/`grep` a check script or its rule engine.

#### Answer

On the final score detailed output made no difference: 11 of 12 runs passed every rule, and the one
lost rule (2.2 repeat 2, D.1 rule 1.6) is a timing-sensitive rule that passed in the agent's own last
run. It did change how the agent got there, and what it cost. With `score_only` output the agent
cannot see which rule failed, so it opened the check scripts (4 to 10 times per run against 0 to 3
with `verbose`) and tested by hand with `curl`. With `verbose` output, C cost 37% and 43% of the
`score_only` cost in the two repeats, and D 24% and 26%, with about half the steps. For E it was
less clear: one `verbose` repeat was the cheapest E run ($0.22), the other cost more than the
cheaper `score_only` repeat ($0.92 against $0.74). Both `verbose` E runs fixed failing rules one by
one, while `score_only` 1.3 repeat 1 needed 116 steps and $1.59, the most of any single-component
run, because the UI rules fail for reasons the score cannot show. So detailed output helps the agent
find a fault faster and more cheaply. Because our `score_only` checks were readable source, the agent
partly turned them into detailed feedback itself, which is why the scores are equal.

#### Consistency across repeats

Scores were identical across repeats. The cost difference pointed the same way in both repeats for C
and D and was large (2 to 4 times). For E the direction flipped between repeats, so the E result is
inconclusive with two repeats. E's cost seems to depend more on the agent's path through the UI
than on the feedback mode.

#### Other evidence

The transcripts show how the agent made up for `score_only` output. In 1.1 repeat 1 it read the C
check scripts 8 times and tested 16 requests by hand with `curl`, rebuilding the per-rule feedback
that `verbose` output gives for free; with `verbose` output (2.1) it read the check source 0 times in
both repeats. The E runs show the limit of this: the UI checks drive a browser, so their source
does not say which element failed, and 1.3 repeat 1 needed 37 check runs.

### 2) Tests as the spec

> Can the checks alone replace the written rules?

#### Runs compared and why

Run 3 against Run 4. Both build C, D and E with `verbose` checks. Run 3 also has the written Specs
C, D and E, and Run 4 has only `/spec/README.md` and the checks.

#### Supporting results

| Run | Repeat | C | D | E | Total | Cost | Steps | Ran checks | Read spec files (incl. README) | Read check source | Manual `curl` |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 3 (specs + checks) | 1 | 49/49 | 22/22 | 48/51 | 119/122 | $2.27 | 129 | 50 | 6 | 5 | 29 |
| 3 (specs + checks) | 2 | 49/49 | 22/22 | 50/51 | 121/122 | $1.95 | 119 | 59 | 14 | 2 | 5 |
| 4 (checks only) | 1 | 49/49 | 22/22 | 51/51 | 122/122 | $2.06 | 140 | 62 | 1 | 12 | 19 |
| 4 (checks only) | 2 | 49/49 | 22/22 | 51/51 | 122/122 | $1.76 | 112 | 60 | 1 | 12 | 12 |

#### Answer

Yes, for everything our checks test. Both checks-only repeats passed all 122 rules, and both
spec-plus-checks repeats lost 1 to 3 rules that their own last check runs had passed (IRC bridging in
E.3, a delete in E.6). Without specs the agent read the check scripts and their rule engines 12 times
in each repeat (against 2 and 5 with specs), so the checks became the spec. The cost was the same or
slightly lower without specs ($2.06 and $1.76 against $2.27 and $1.95). This does not mean the
written rules are useless. The hidden tests cover behaviour our checks do not test, and only the
written spec tells the agent about it.

#### Consistency across repeats

The direction was consistent: both Run 4 repeats scored at least as high as both Run 3
repeats, and the behaviour (0 spec reads beyond the README and 12 check-source reads, against 6 to 14
spec reads and 2 to 5 check-source reads) was the same in both repeats. The score gap is small (1 to 3
rules), so I read it as "no loss without
the specs", not as "better without them".

#### Other evidence

Run 3's lost rules all passed in the agent's own last run and failed in the fresh container of the
re-check, so they are flaky behaviour under a cold start, not rules the agent never met. Run 4 had
the same checks and did not lose them, so the written spec did not cause this either.

### 3) Scale

> Do you find that a component passes fewer of its checks when the agent builds it together with
> other components, rather than on its own?

#### Runs compared and why

The same component and the same `verbose` checks on its own (2.1, 2.2, 2.3), with C, D and E
together (Run 3), and in the whole app (5.1, 5.2, 5.3). All of these runs have the specs.

#### Supporting results

Rules passed in our re-check (repeat 1, repeat 2):

| Component | Alone (2.x) | With C, D, E (3) | Whole app 5.1 | Whole app 5.2 | Whole app 5.3 |
| --- | --- | --- | --- | --- | --- |
| C (49) | 49, 49 | 49, 49 | 48, 49 | 49, 47 | 49, 48 |
| D (22) | 22, 21 | 22, 22 | 22, 22 | 22, 19 | 22, 22 |
| E (51) | 51, 51 | 48, 50 | 51, 51 | 47, 35 | 48, 14 |
| Cost | $0.08 to $0.92 | $2.27, $1.95 | $3.01, $2.05 | $2.94, $1.25 | $2.28, $3.02 |

#### Answer

Yes, mostly for the component built last. On its own every component passed every rule in at least
one repeat and lost at most 1 rule in the other. With C, D and E together, E lost 1 to 3 rules. In the
whole app, E lost rules in four of six runs (3, 4, 16 and 37), and C or D lost 1 to 5 rules in three of
six.
The READMEs order D, then C, then E, and E (the IRC gateway and the web UI) is the most expensive
part, so it is what is unfinished when the budget runs out: 5.3 repeat 2 reached E with $0.46 left
and finished E.1 only. The cause is resources more than difficulty. A single component cost at most
$0.92 with `verbose` checks, the whole app $2 to $3, and two of six whole-app runs hit the $3 limit.

#### Consistency across repeats

Consistent in direction: no component did better in a bigger run than alone. The size varies a lot
between repeats (E in 5.3: 48 and 14), so a single repeat is a poor estimate of how a bigger run will
go.

#### Other evidence

Where a whole-app run spent its money shows the pressure. 5.3 repeat 1 had A and B passing at $0.25
and finished everything at $2.28. 5.3 repeat 2 needed $0.94 for A and B and $1.31 for C (C.4's `@here`
rule alone took 9 runs), and was cut off in E.2.

### 4) Regression

> a) Do you find that building components C, D, and E breaks features from A and B that were
> already working?

> b) Does it make a difference whether the agent has Specs A and B or only their regression
> tests?

> c) Does it make a difference whether the A and B checks are given as a regression suite or as
> plain checks?

#### Runs compared and why

- a) The whole-app runs (5.1, 5.2, 5.3). From each transcript I took the order in which the agent ran
  the A/B checks and the C/D/E checks, so I can see whether A and B were working before C, D and E
  were built and whether they still worked afterwards.
- b) 5.1 (regression suite, no Specs A and B) against 5.2 (regression suite and Specs A and B). The
  only difference is the specs.
- c) 5.2 (Specs A and B, regression suite) against 5.3 (Specs A and B, plain staged checks). The only
  difference is the form of the A and B checks.

#### Supporting results

| Run | Repeat | A and B at the end (re-check) | A/B status during the run (from the transcript) | C/D/E rules | Cost |
| --- | --- | --- | --- | --- | --- |
| 5.1 (suite, no A/B specs) | 1 | 1 broken (A 64/64, B 53/54) | suite 85, 72 at steps 13 to 21; then D, C and E built (steps 24 to 125); then A/B fixed last: 41, 32, 27, 21, 18, 13, 10, 7, 6 until the budget ran out | 121/122 | $3.01 |
| 5.1 (suite, no A/B specs) | 2 | 0 broken (A 64/64, B 54/54) | suite 70 at step 11; D, C, E built (steps 16 to 89); A/B fixed last: 26, 21, 18, 12, 11, 8, 5, 3, 3, 0 | 122/122 | $2.05 |
| 5.2 (suite + A/B specs) | 1 | 44 broken (A 44/64, B 30/54) | built A/B from the specs, suite at step 14 (no output), then D, C, E; suite again only at step 174 (no output); submitted | 118/122 | $2.94 |
| 5.2 (suite + A/B specs) | 2 | 21 broken (A 64/64, B 33/54) | suite 88, 49, 31, 27 by step 18, then C and E built and never ran the suite again; submitted | 101/122 | $1.25 |
| 5.3 (plain checks + A/B specs) | 1 | 0 broken (A 64/64, B 54/54) | every A and B staged check passing by step 34; D, C, E built; `check_roles_all` and `check_channels_all` 64/0 and 54/0 at step 127 | 119/122 | $2.28 |
| 5.3 (plain checks + A/B specs) | 2 | 4 broken (A 64/64, B 50/54) | A and B staged checks passing by step 83; C and D built; out of budget in E without rerunning A/B | 84/122 | $3.02 |

#### Answer

a) Rarely, and only a little. The only runs where A and B were confirmed working before C, D and E
were built are the two 5.3 runs. In repeat 1 they still passed every rule afterwards, both in the
agent's own `_all` run and in our re-check. In repeat 2, 4 B rules failed at the end (2.12 archived
channels, 8.4 to 8.6 dense event ids and resume), which are exactly the shared events table and
channel state that C's reactions, files and slash commands also write to, so this is a real
regression, though the agent never reran A/B to see it because the budget ran out. In both 5.1 runs
the agent built C, D and E first and A and B last, so nothing of A/B existed to break. The large
A/B failures in 5.2 are features that were never built (5.2 repeat 1 has no invitation routes at
all), not features that broke.

b) Yes, and in a surprising direction. Without Specs A and B (5.1),
the suite was the agent's only source of the A and B rules, so it kept running it (17 and 16 times)
and drove it to 0 and 1 broken. With the specs (5.2), the agent built A and B from the text, treated
the suite as a final check, ran it 2 and 4 times, and submitted with 44 and 21 broken. Both 5.2
runs ignored the README's rule to rerun the suite after every step and its submit conditions.

c) Yes. With plain staged checks (5.3), A and B get the same step-by-step treatment as C, D and E:
both repeats had every A and B check passing before moving on, and ended with 0 and 4 broken. With
one suite (5.2) the agent skipped it and ended with 44 and 21 broken. The cost of plain checks is
time on A and B: 5.3 repeat 2 spent $0.94 on them, which left too little for E.

#### Consistency across repeats

a) Two runs answer it directly and they differ (0 and 4 broken), so the evidence is weak. b) and c)
are consistent across both repeats: 5.1 ends near 0 broken,
5.2 ends with many A/B failures, and 5.3 ends with A/B intact or nearly so.

#### Other evidence

Our regression suite has a design flaw that the 5.2 runs showed. Against a half-built app its
requests wait for timeouts, so a run can take longer than mini-swe-agent's 30-second command limit,
and then the agent gets no output at all (5.2 repeat 1 at steps 14 and 174). Every check prints
a first line so that a cut-off check is visible, and no agent wrapped a check in its own `timeout`,
but the suite itself still needs a global time budget and partial results to be useful
early in a run.
