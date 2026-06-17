# Multi-Agent Phase Fan-out (Real Dogfood)

Run several implementation agents **in the same phase**, each on a disjoint scope
and its own worktree, then synthesize and review. This is the plan-file
expression of the fan-out → synthesis → review shape referenced throughout
[workflow-patterns.md](workflow-patterns.md), declared once and launched with a
single `workflow apply`. It is the canonical way to dogfood the workflow system
on itself: N real parallel writers, real merge lanes, real review.

> **Read first:** [workflow-patterns.md](workflow-patterns.md) §1 (when a barrier
> is justified — synthesis over the full set is one), §2 (scale to the request),
> and §10 (structured returns — each lane's output is data for synthesis).

---

## When to fan out within a phase vs. use sequential phases

Put jobs in the **same phase** when the work is genuinely independent — different
files or modules, no data flowing between them — so they can run concurrently.
Use **sequential phases** when each stage needs the prior stage's output.

| Situation                                                     | Shape                         |
|---------------------------------------------------------------|-------------------------------|
| N disjoint modules, each implementable standalone             | one fan-out phase             |
| impl, then a review that needs the impl result                | two sequential phases         |
| parallel impl, then a merge/integration step over all of them | fan-out phase → synthesis phase |
| parallel impl, then independent per-lane tests                | one phase (tests in same lane) |

**Smell test:** if lane B's prompt starts with "read lane A's output", those are
two phases, not one. If every lane can be described without referencing a sibling,
they belong in one phase.

---

## Why worktree lanes prevent merge conflicts

Each fan-out job declares `"worktree": true` (or a worktree object). At apply
time the runner creates a **separate git worktree per job**, sets that job's
`cwd` to the lane path, and records the lane in run state. Because each writer
checks out its own working tree on its own branch, two agents never edit the same
files concurrently — there is nothing to conflict until merge.

Pair each lane with a `write_scope` that documents the file ownership, so the
synthesis phase (and any human reviewer) can see at a glance which lanes touched
which paths. `write_scope` is documentation of intent; the worktree is the
enforcement.

---

## The dependency graph: phase N+1 waits on all of phase N

Phase order is positional. At flatten time, **every job in phase N becomes an
implicit dependency of every job in phase N+1**. So:

- Jobs **within** the fan-out phase have no implicit deps → they run in parallel.
- The synthesis phase's job depends on **all** fan-out jobs (a genuine barrier —
  it merges the full set, which [workflow-patterns.md](workflow-patterns.md) §1
  says is the one case a barrier is justified).
- The review phase depends on the synthesis job.

Two consequences worth knowing: the run-state `phase_id` is **computed** from each
phase's `name` (`phase-<slugified-name>`), not declared; and jobs within a phase
are parallel by default — add a job-level `depends_on` only when one sibling must
wait for another.

---

## Example: fan-out → synthesis → review

```json
{
  "kind": "workflow-plan",
  "title": "multi-agent-fanout",
  "summary": "Three parallel impl agents on disjoint scopes, then synthesis, then review.",
  "cwd": ".",
  "runner": "ccc",
  "max_agents": 3,
  "phases": [
    {
      "name": "impl-fanout",
      "title": "Parallel Implementation",
      "summary": "Three disjoint implementation lanes in one phase.",
      "jobs": [
        {
          "name": "impl-a",
          "prompt": "Implement module A. Edit only src/a/. Return a JSON summary of files touched.",
          "write_scope": ["src/a"],
          "worktree": true
        },
        {
          "name": "impl-b",
          "prompt": "Implement module B. Edit only src/b/. Return a JSON summary of files touched.",
          "write_scope": ["src/b"],
          "worktree": true
        },
        {
          "name": "impl-c",
          "prompt": "Implement module C. Edit only src/c/. Return a JSON summary of files touched.",
          "write_scope": ["src/c"],
          "worktree": true
        }
      ]
    },
    {
      "name": "synthesis",
      "title": "Synthesis",
      "summary": "Lead merges the three lanes and writes integration glue.",
      "jobs": [
        {
          "name": "integrate",
          "prompt": "Read the impl-a, impl-b, and impl-c results. Write the integration glue in src/integration. Return a JSON summary."
        }
      ]
    },
    {
      "name": "review",
      "title": "Review",
      "summary": "Cross-cutting review of the integrated result.",
      "ccc_runner": "@glm51",
      "jobs": [
        {
          "name": "review",
          "prompt": "Review the integrated result for correctness and coherence. Return findings as JSON: {severity, file, issue}."
        }
      ]
    }
  ]
}
```

After `normalize_workflow` + flatten, the dependency graph is:

```text
impl-a (worktree, src/a)  ─┐
impl-b (worktree, src/b)  ─┼─> integrate     depends_on: impl-a, impl-b, impl-c
impl-c (worktree, src/c)  ─┘        │
                                    └─> review (@glm51)   depends_on: integrate
```

The three `impl-*` jobs have **no** `depends_on`, so they run concurrently. Set
`max_agents >= 3` at the root so all three launch at once; otherwise the runner
serializes the fan-out and you lose the parallelism. The synthesis `integrate`
job is the fan-in barrier; `review` chains after it on a different model.

---

## Launching and merging

```bash
workflow apply workflows/multi-agent-fanout.workflow.json \
  --runner ccc \
  --max-agents 3
```

After the run completes, merge each worktree lane into its `merge_target`
(`main` by default), or let the lead drive the merge from the synthesis phase.
Use `--dry-run` to confirm the three worktree lanes are planned and the
synthesis barrier is wired before spending model calls.

Keep the lead's synthesis mandatory: parallel lanes can each look correct yet
fail to integrate. Read every lane's structured summary and run the integration
tests before declaring the run `completed`.
