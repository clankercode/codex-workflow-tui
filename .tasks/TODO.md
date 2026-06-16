# Workflow Dogfood Tasks

## DONE
- [x] Refactor: split workflow_tui.py (2035→567 LOC) into 4 modules
- [x] Refactor: split workflow_run_codex.py (1743→1411 LOC) into 2 modules
- [x] ccc-review-cx review of refactor — 6 drifts found and fixed
- [x] Bug fix: worktree lane deduplication
- [x] Bug fix: lazy worktree creation at launch time
- [x] Bug fix: dependent worktrees branch from dependency's branch
- [x] Bug fix: existing branch recovery (fallback to checkout)
- [x] Bug fix: stale worker detection + 3 retries with 30s grace
- [x] Bug fix: phase_jobs last-job-only inter-phase deps
- [x] Feat: workflow apply detaches by default
- [x] Feat: grid running agents, labeled live output
- [x] Feat: merge-lanes auto-resolves conflicts (UNTESTED)
- [x] #10: Run-level merged live output
- [x] #11: Compact live monitor/status command
- [x] 7 worktree lane tests + 35 tool-call parsing tests
- [x] Plan updated: parallel impls, reviews depend on impls

## IN PROGRESS
- [ ] Test for merge-lanes auto-resolve

## BACKLOG (from dogfood-backlog.md)
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
11. Merge conflict auto-resolution — `merge-lanes` should dispatch an agent to resolve conflicts instead of failing
12. Agent replacement/recovery — when a worker hits usage limits or crashes, should be able to add a replacement agent with same scope and have it auto-start (currently requires manual `workflow run --attach-run`)
13. `workflow run --attach-run` re-launches ALL jobs from the run, not just the specified `--job`. When retrying a single agent, it re-launches impl agents too, causing duplicate work and stale-worker confusion. Should only launch the specified jobs.
