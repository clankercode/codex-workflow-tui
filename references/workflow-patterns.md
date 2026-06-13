# Workflow Patterns

The orchestration patterns that make a multi-agent workflow both *correct* and
*fast*. The lead agent composes these; the runner and native subagents execute
the lanes. Read this before designing a non-trivial fan-out.

These are ported from the Claude Code Workflow tool's operating model and adapted
to this skill's primitives (`workflow run --job`, phases, agents, decisions,
artifacts, `workflow verify`, lead synthesis). Pick by task and compose freely —
the list is a toolbox, not a checklist.

---

## 1. Concurrency discipline: pipeline by default, barrier only when needed

The default instinct — "fan out N lanes, **wait for all**, then synthesize" — is a
**barrier**. A barrier is only correct when the next stage genuinely needs *every*
prior result at once. Otherwise it wastes wall-clock: if five finders run and the
slowest takes 3× the fastest, a barrier idles the four fast lanes until the slow
one lands.

**Prefer a pipeline:** let each item flow through its stages independently. Item A
can be in *verify* while item B is still in *find*. Wall-clock becomes the slowest
single item's chain, not the sum of slowest-per-stage.

A barrier IS correct when, and only when, the next stage needs cross-item context:

- **Dedup / merge** across the full result set before expensive downstream work.
- **Early-exit** on an aggregate ("0 findings → skip the whole verify phase").
- The next prompt literally references "the other findings" for comparison.

A barrier is NOT justified by:

- "I need to flatten/filter the results first" — that's a cheap local transform;
  do it per-item, don't synchronize the whole fleet for it.
- "The stages are conceptually separate" — separate ≠ synchronized.
- "It's cleaner" — barrier latency is real wall-clock.

**Smell test:** if you wrote `run batch-A` → (cheap transform) → `run batch-B`,
and the transform has no cross-item dependency, you don't need the barrier. Start
each B-lane as soon as its A-lane returns.

**How to express it here.** The `workflow run` launcher is a *flat fan-out then
barrier* over one job set — that is one stage. To pipeline across stages, the lead
does NOT wait for the whole first batch before launching the second: as each
stage-1 agent's result lands (watch state / native subagent return), the lead
immediately launches that item's stage-2 lane and records it under the next phase.
Reserve a single `workflow run` barrier for the genuine cross-item steps above.
(Per-item pipelining inside one runner call is a current engine gap — see
`coding-cli-runners.md` on the `{"kind":"workflow-expansion"}` envelope.)

---

## 2. Scale to the request

Right-size the fan-out to what was actually asked. Over-orchestration is its own
cost.

- "find any bugs" → a few finders, single-vote verification.
- "thoroughly audit / be comprehensive" → larger finder pool, 3–5-vote
  adversarial verification, an explicit synthesis phase.
- A quick check → maybe no workflow at all; keep it inline.

When unsure, lean thorough for research/review/audit asks, brief for quick checks.

---

## 3. Adversarial verification (refute-by-default)

A plausible-but-wrong finding is the most expensive output a workflow can ship.
For each candidate finding, spawn N **independent** skeptics, each prompted to
*refute* it, defaulting to "refuted" under uncertainty. Keep the finding only if a
**majority fail to refute**.

```bash
# one refuter job per candidate; lead keeps survivors
workflow run --title "verify:finding-7" --runner ccc --ccc-runner @cx-reviewer \
  --max-agents 3 \
  --job "refute-a::Try to REFUTE this claim with code evidence. Default to refuted=true if uncertain: <claim>" \
  --job "refute-b::<same claim, independent reviewer>" \
  --job "refute-c::<same claim, independent reviewer>"
```

Record the verdict as a `workflow decision`; keep verified findings as artifacts.
Prevents the single-reviewer rubber-stamp.

---

## 4. Perspective-diverse verification

When a finding can fail in more than one way, give each verifier a **distinct
lens** instead of N identical refuters — diversity catches failure modes that
redundancy can't. Typical lenses: `correctness`, `security`, `performance`,
`does-it-actually-reproduce`. Use this *instead of* pattern 3 when the failure
surface is wide, or *layered with* it for high-stakes findings.

---

## 5. Judge panel

When the solution space is wide, generate N **independent attempts** from
different angles (e.g. MVP-first, risk-first, user-first), score them with
parallel judge agents, then synthesize from the winner while grafting the best
ideas from the runners-up. Beats one-attempt-iterated. Express as a `design` phase
(the attempts) + a `review` phase (the judges) + lead synthesis.

---

## 6. Loop-until-dry

For unknown-size discovery (bugs, edge cases, dead code, missing tests), a fixed
`while count < N` misses the tail. Instead keep spawning finder rounds until **K
consecutive rounds surface nothing new**.

Critical convergence rule: **dedup new findings against everything SEEN, not
against everything CONFIRMED.** If you dedup against confirmed-only, judge-rejected
candidates reappear every round and the loop never terminates.

---

## 7. Multi-modal sweep

Parallel agents that each search a *different way* — by container/structure, by
content/grep, by entity/identifier, by time/history. Each is blind to what the
others surface; useful when no single search angle finds everything. Each angle is
one job in a single fan-out; the lead merges and dedups.

---

## 8. Completeness critic

End an exhaustive task with one agent whose only job is to ask **"what's missing?"**
— a modality not run, a claim left unverified, a source left unread, a file the
sweep skipped. Whatever it finds becomes the next round of work. Cheap insurance
against a confident-but-partial result.

---

## 9. No silent caps

If a workflow bounds coverage — top-N only, no retry on failure, sampling instead
of full scan, `--max-agents` truncating a job list — **record what was dropped**
as a `workflow event` or `decision`. Silent truncation reads downstream as
"covered everything" when it didn't. One line of state turns a hidden gap into an
auditable choice.

---

## 10. Structured returns

A worker's final output is **data for the lead**, not prose for a human. Ask each
agent for a parseable shape — JSON, or markdown with fixed headings the lead can
split on — and state the exact schema in the prompt. The lead validates the shape
and **re-dispatches the lane on malformed output** rather than parsing slop. This
approximates the Claude Code Workflow tool's schema-forced returns, where a bad
shape forces an automatic retry. Persist the structured result as the agent's
`--result-file` so tooling and later phases can consume it.

---

## Composing patterns — exhaustive review

A high-assurance review chains several of the above:

1. **Multi-modal sweep** (7) + **scale-to-request** (2): a finder pool sized to
   "comprehensive", each lane a different search angle, each returning a
   **structured** finding list (10).
2. **Loop-until-dry** (6): repeat the sweep, dedup each round against `seen`
   (not `confirmed`), until two empty rounds.
3. **Perspective-diverse** (4) or **adversarial** (3) verify on every *fresh*
   finding, concurrently — and because verification is per-finding, **pipeline**
   it (1): verify finding A while the sweep is still surfacing finding B.
4. **Completeness critic** (8) before declaring done.
5. **No silent caps** (9): if any pool was capped, say so in state.
6. Run the verification commands, record them with `workflow verify`, and only
   then `workflow done`.

Each pattern earns its place by the failure mode it prevents — not by adding
agents. Add a lane only when it removes a way the answer could be wrong or slow.
