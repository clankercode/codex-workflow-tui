# Worktree Lane Test Plan

## Overview

The worktree lane system manages isolated git worktrees for multi-agent coding workflows. Three key functions form the lifecycle:

- **`prepare_worktree_lanes`** (`workflow_apply.py`) -- records worktree metadata at plan-apply time (operation="planned"), defers creation.
- **`_ensure_worktree_lane`** (`workflow_run_codex.py`) -- lazily creates the worktree at worker launch time. Handles dependent jobs (branching from dependency's branch) and existing branch recovery.
- **`cmd_merge_lanes`** (`workflow_ops.py`) -- post-completion merge of lane branches back into the run cwd.

## Test Cases

| # | Test Name | Scenario | Key Assertion |
|---|-----------|----------|---------------|
| 1 | `test_worktree_lane_single_impl_creates_branch_and_path` | Single job with worktree | Branch, path, --cd flag all correct |
| 2 | `test_worktree_lane_shared_branch_across_phase_does_not_fail` | 3 jobs, same branch, 1 phase | No crash, distinct paths, all complete |
| 3 | `test_worktree_lane_dependent_reviewer_branches_from_impl` | impl -> review dependency | Reviewer branches from impl's branch |
| 4 | `test_worktree_lane_chain_dependencies_each_branches_from_previous` | impl -> review-a -> review-b | Each branches from prior dependency |
| 5 | `test_worktree_lane_parallel_independent_tasks_run_concurrently` | 2 independent jobs | Separate branches, concurrent completion |
| 6 | `test_worktree_lane_existing_branch_recovery_checkout_instead_of_create` | Pre-existing branch | Fallback checkout succeeds, prior commits visible |
| 7 | `test_worktree_lane_dry_run_plans_metadata_but_skips_creation` | Dry run mode | Planned event, no directory, no created event |
| 8 | `test_worktree_lane_merge_back_after_mock_completion` | Full lifecycle | merge-lanes merges branch to main |
| 9 | `test_worktree_lane_dependent_phases_execute_in_order_with_mock` | Async ordering | Timestamp ordering proves dependency gating |
| 10 | `test_worktree_lane_metadata_survives_phase_jobs_flattening` | Plan normalization | phase_jobs() preserves worktree dict |
