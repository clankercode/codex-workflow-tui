# Workflow Dogfood Tasks

## DONE
- [x] Refactor: split workflow_tui.py (2035→567 LOC) into 4 modules
- [x] Refactor: split workflow_run_codex.py (1743→1411 LOC) into 2 modules
- [x] ccc-review-cx review of refactor — 6 drifts found and fixed
- [x] Bug fix: worktree lane deduplication (prepare_worktree_lanes called create per-job)
- [x] Bug fix: lazy worktree creation at launch time (not upfront)
- [x] Bug fix: dependent worktrees branch from dependency's worktree branch
- [x] Bug fix: existing branch recovery (fallback to checkout instead of create)

## IN PROGRESS
- [ ] Test cases for worktree behavior (subagent planning)
- [ ] Launch workflow for #10 + #11 (blocked on test cases)

## BACKLOG (from dogfood-backlog.md)
- [ ] #10: Run-level merged live output
- [ ] #11: Compact live monitor/status command
- [ ] #12: Event rollover visibility
- [ ] #13: First-class dogfood/backlog support
- [ ] #14: Ergonomic layered review workflows
- [ ] #15: Real multi-agent phase dogfood
- [ ] #16: Runs overview graph + attention notifications
- [ ] #17: TUI pane scrolling + run-list bounds
- [ ] #18: Runs detail layout for live output/running agents
- [ ] #19: Live throughput stats + smoothed counters
- [ ] #20: Finished-ago field for completed runs/agents
- [ ] #21: Add pi coding CLI as runner

## DOGFOOD FINDINGS (this session)
1. Worktree lane deduplication — prepare_worktree_lanes created same branch N times → fixed
2. Worktree creation should be lazy — upfront creation can't support dependency chains → fixed
3. Dependent worktrees need to branch from dependency's branch, not HEAD → fixed
4. Existing branch from prior run causes create failure → fallback to checkout → fixed
5. Don't cancel running workflows without asking — let progressing agents finish first
6. `workflow apply` blocks until completion — should launch and return immediately → fixed
7. Phase boundary adds unnecessary cross-task deps (review-10a waits for impl-11 too)
8. Stale worker detection — dead processes shown as running forever → fixed with retry + 30s grace
9. TUI shows `RUN!` in red for stale workers → fixed
10. Tool call gap: ccc `cache.read` tokens not captured (documented in test_codex_jsonl_cache_read_tokens_gap)
