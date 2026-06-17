# Layered Review Workflows

Declare an implementation lane **and** an ordered multi-model review stack in a
single `workflow-plan` JSON, so one `workflow apply` runs the whole pipeline and
records every phase in run state. This is the ergonomic way to express the
review-pattern compositions described in
[workflow-patterns.md](workflow-patterns.md) (adversarial verify, completeness
critic) as a durable, re-runnable plan.

> **Read first:** [workflow-patterns.md](workflow-patterns.md) §1 (pipeline vs
> barrier), §3 (adversarial verify), and the SKILL.md "Delegation Rules". A layered
> review is a *barrier chain* — each review stage needs the prior stage's full
> output — so the sequential phase model below is the correct shape.

---

## The preferred pattern: phase-level `ccc_runner` overrides

Execution fields (`runner`, `ccc_runner`, `model`, `max_agents`, …) resolve with
**root plan < phase < job** precedence. Assign one `ccc_runner` per **phase** and
every job in that phase inherits it. The implementation phase and each review
phase then run on a different model without per-job repetition:

- **impl** phase → `@mimo25p` (fast, write-capable implementation model)
- **semantic-review** phase → `@glm51` (correctness/intent lens)
- **standards-review** phase → `@mm3` (style/tests/standards lens)

Using distinct models per review *layer* is the cheap form of
[perspective-diverse verification](workflow-patterns.md#4-perspective-diverse-verification):
different model families notice different failure modes, and rotating which model
sits at which layer prevents a single model's blind spot from being permanently
last-word.

---

## How phase ordering works (so you write it correctly)

Phase order is **positional** — phases run in the order listed in the `phases`
array. You do **not** declare `depends_on` on phases. At flatten time
(`workflow apply`), **all jobs in phase N become implicit dependencies for every
job in phase N+1**, so a later phase cannot start until the entire prior phase
completes. This is exactly the barrier a review stack needs.

Two consequences worth knowing:

- The run-state `phase_id` is **computed** from each phase's `name`
  (`phase-<slugified-name>`); it is not a field you declare.
- Jobs *within* a phase run in **parallel** by default. Add a job-level
  `depends_on` only when one job in the same phase must wait for a sibling.

---

## Example: impl → semantic-review → standards-review

```json
{
  "kind": "workflow-plan",
  "title": "layered-review",
  "summary": "Implementation followed by an ordered multi-model review stack.",
  "cwd": ".",
  "runner": "ccc",
  "phases": [
    {
      "name": "impl",
      "title": "Implementation",
      "summary": "Implement the bounded change.",
      "ccc_runner": "@mimo25p",
      "jobs": [
        {
          "name": "impl-feature",
          "prompt": "Implement <feature>. Edit only src/feature/. Return a JSON summary of files touched and decisions made.",
          "write_scope": ["src/feature"],
          "worktree": true
        }
      ]
    },
    {
      "name": "semantic-review",
      "title": "Semantic Review",
      "summary": "Review for correctness and intent.",
      "ccc_runner": "@glm51",
      "jobs": [
        {
          "name": "semantic-review",
          "prompt": "Review the impl-feature output for correctness and intent regressions. Return findings as JSON: {severity, file, issue}. Default to skepticism."
        }
      ]
    },
    {
      "name": "standards-review",
      "title": "Standards Review",
      "summary": "Review for style, tests, and standards.",
      "ccc_runner": "@mm3",
      "jobs": [
        {
          "name": "standards-review",
          "prompt": "Review the impl-feature output for test coverage, naming, and project standards. Return gaps as JSON: {severity, file, issue}."
        }
      ]
    }
  ]
}
```

After `normalize_workflow` + flatten, the dependency graph is:

```text
impl-feature (@mimo25p)
   └─> semantic-review (@glm51)        depends_on: impl-feature
          └─> standards-review (@mm3)   depends_on: semantic-review
```

Reviewers are read-only (no `write_scope`/`worktree`), per the SKILL.md rule:
prefer read-only agents for review; give write access only to the owning lane.

---

## ⚠️ Layered models can still miss edge cases — keep lead review mandatory

A stack of strong reviewers is **defense in depth, not a guarantee**. Models
share training-time blind spots, and any one reviewer can rubber-stamp a
plausible-but-wrong finding. The lead agent is still the final gate:

1. **Read every review summary yourself**, not just the top-line verdict. A
   completed review phase with only a "looks good" is a red flag — re-dispatch
   the lane for concrete evidence (file paths, line numbers).
2. **Run a completeness critic** ([workflow-patterns.md](workflow-patterns.md) §8)
   as a final lead-local pass: "what did all three layers miss?"
3. **Run the verification commands** and record them with `workflow verify`
   before marking the run `completed` — model agreement is not evidence; passing
   tests are.

Treat the layered stack as *filters that reduce* the lead's review surface, never
as a replacement for it.

---

## Rotating the review stack between runs

Because `ccc_runner` is a per-phase field, you diversify across runs by
reassigning which preset each review layer uses. Keep the *impl* model fixed for
consistency and rotate the review models so no single family is permanently
last-word:

| run   | impl        | semantic-review | standards-review |
|-------|-------------|-----------------|------------------|
| run 1 | `@mimo25p`  | `@glm51`        | `@mm3`           |
| run 2 | `@mimo25p`  | `@mm3`          | `@glm51`         |
| run 3 | `@mimo25p`  | `@glm51`        | `@mm3` (re-run)  |

Rotation is the plan-level equivalent of
[perspective-diverse verification](workflow-patterns.md#4-perspective-diverse-verification):
it spreads blind spots across runs instead of baking one ordering in. Record the
chosen ordering as a `workflow decision` so a later run can deliberately differ.

---

## Launching

```bash
workflow apply workflows/layered-review.workflow.json \
  --runner ccc \
  --max-agents 3
```

`--max-agents` caps simultaneous workers; since the phases are sequential, one
worker per active phase is typical. Use `--dry-run` to confirm the flattened
phases and per-phase `ccc_runner` assignments before spending model calls.
