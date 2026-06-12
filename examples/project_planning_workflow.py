#!/usr/bin/env python3
"""Emit a phased workflow plan for deep project planning from an architecture pack."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


WORKER_CONSTRAINTS = """\
Worker constraints:
- Work directly in this worker. Do not launch nested agents, subagents, background workers, or recursive coding-CLI sessions.
- Keep exploration bounded: read the most relevant corpus files yourself, then write the requested artifact.
"""


RUBRIC = """\
Rubric:
- Source grounding: cite and use the architecture corpus accurately.
- Coverage: product, runtime, security, market/accounting, operations, and implementation lanes are represented.
- Interface quality: concrete APIs, protocols, data models, ownership boundaries, and handoff artifacts.
- Error correction: review findings are accepted, fixed, rejected with rationale, or carried as explicit open decisions.
- Task quality: implementation tickets are small, dependency ordered, and testable.
- Consistency: no unresolved contradictions between product, architecture, security, economics, and operations.
- Validation gates: includes positive/negative tests, threat checks, operational readiness, and launch criteria.
- Artifact usability: final docs are navigable and sufficient for a follow-on implementation workflow.
"""


OUTPUT_CONTRACT = """\
Output contract:
- Use the artifact template: IDEA, GOAL, CONTEXT, ACCEPTANCE CRITERIA, NON-GOALS, FAILURE CRITERIA, SOURCE EVIDENCE, DECISIVE ISSUES OR REFUTATIONS, PLAN/TASKS, VALIDATION, OPEN QUESTIONS.
- Use statuses exactly: NON_REFUTED, REFUTED, UNVERIFIED, or BLOCKED.
- Treat UNVERIFIED as non-passing; state what evidence would settle it.
- Do not average disagreements. Accept valid criticisms and revise the plan.
- Prefer specific source files, interface names, task IDs, and validation commands over generic advice.
- Do not rewrite the source corpus. Create or update only files under the requested planning-output directory.
"""


def job(name: str, role: str, prompt: str) -> dict[str, str]:
    """Build one workflow job object."""
    return {"name": name, "role": role, "prompt": prompt.strip() + "\n"}


PROJECT_CORPUS = """\
Project corpus:
- Current working directory: `.`
- Use only cwd-relative paths such as `README.md`, `MANIFEST.md`, `docs/...`, `adr/...`, `models/...`, `review/...`, and `planning-output/...`.
- Do not use absolute paths when reading or writing files.
"""


def draft_prompt(name: str, focus: str, project_dir: Path, output_subdir: str) -> str:
    """Prompt one first-pass planner."""
    output_path = f"{output_subdir}/draft/{name}.md"
    return f"""\
You are a planning agent creating a criticizable conjecture, not a final truth.

{PROJECT_CORPUS}

Assigned slice:
{focus}

Read the relevant README, MANIFEST, docs, ADRs, models, diagrams, and review notes. Produce a concrete implementation-oriented plan for this slice, from architectural intent down to coding-agent-sized implementation tasks.

{WORKER_CONSTRAINTS}

{OUTPUT_CONTRACT}

Draft-specific requirements:
- Include a task table with columns: task_id, title, owner/module, depends_on, acceptance_criteria, positive_tests, negative_tests, risks.
- Include explicit assumptions and what would refute each major assumption.
- Include source evidence as file paths from the corpus.
- Save the full draft to `{output_path}`.
- Include the full draft in your final answer.
"""


def review_prompt(name: str, focus: str, project_dir: Path, output_subdir: str) -> str:
    """Prompt one adversarial reviewer."""
    output_path = f"{output_subdir}/review/{name}.md"
    return f"""\
You are an adversarial reviewer. Your job is to find decisive and almost-decisive criticisms, not to improve the draft yourself.

{PROJECT_CORPUS}

Draft artifacts to review:
{output_subdir}/draft/

Review focus:
{focus}

{WORKER_CONSTRAINTS}

{RUBRIC}

Review protocol:
- Return PASS or FAIL. FAIL if any single meaningful issue remains.
- A decisive issue must contradict a stated goal, acceptance criterion, source fact, dependency, or validation requirement.
- For each issue include: ID, severity, target artifact/claim, contradicting observation, why it forces rejection, and concrete repair instruction.
- Label almost-decisive issues AD<n> and state the missing failure criterion or evidence that would make them decisive.
- Track repeated or related issues with stable IDs.
- Save the review to `{output_path}`.
- Include the full review in your final answer.
"""


def correction_prompt(name: str, focus: str, project_dir: Path, output_subdir: str) -> str:
    """Prompt one fixer/reviser."""
    output_path = f"{output_subdir}/corrected/{name}.md"
    return f"""\
You are a correction agent in a review-and-fix loop.

{PROJECT_CORPUS}

Inputs:
- Draft artifacts: {output_subdir}/draft/
- Review artifacts: {output_subdir}/review/

Correction focus:
{focus}

{WORKER_CONSTRAINTS}

Fix protocol:
- Diagnose the root cause of each relevant review issue before changing the plan.
- Address decisive findings directly while preserving the original project goals and constraints.
- Do not broaden scope. If a review asks for unjustified scope expansion, reject it with rationale.
- Include an "Issue Accounting" table with columns: issue_id, status, action_taken, artifact_section, remaining_risk.
- Status values: fixed, rejected-with-rationale, carried-as-open-decision, unverified.
- Include revised task IDs, dependencies, acceptance criteria, and tests.
- Save the corrected artifact to `{output_path}`.
- Include the full corrected artifact in your final answer.
"""


def synthesis_prompt(name: str, focus: str, project_dir: Path, output_subdir: str) -> str:
    """Prompt an integrated synthesizer."""
    output_path = f"{output_subdir}/synthesis/{name}.md"
    return f"""\
You are a synthesis agent combining corrected planning artifacts after disagreement.

{PROJECT_CORPUS}

Inputs:
- Corrected artifacts: {output_subdir}/corrected/
- Review artifacts: {output_subdir}/review/

Synthesis focus:
{focus}

{WORKER_CONSTRAINTS}

Synthesis protocol:
- Build from corrected artifacts, not from memory.
- Include a debate tree summary: root claim, criticisms, status, accepted changes, rejected criticisms, unresolved nodes.
- Classify each major disagreement as factual, logical, scope, or values.
- Produce integrated, navigable artifacts with stable task IDs and dependencies.
- Mark claims as NON_REFUTED, REFUTED, UNVERIFIED, or BLOCKED.
- Save the artifact to `{output_path}`.
- Include the full artifact in your final answer.
"""


def final_review_prompt(name: str, focus: str, project_dir: Path, output_subdir: str, round_name: str) -> str:
    """Prompt final independent reviewers."""
    output_path = f"{output_subdir}/{round_name}/{name}.md"
    synthesis_dir = "synthesis" if round_name == "final-review" else "final-fix"
    return f"""\
You are a final evaluator. Review the integrated project plan for benchmark quality.

{PROJECT_CORPUS}

Inputs:
- Integrated artifacts: {output_subdir}/{synthesis_dir}/
- Prior reviews and corrections: {output_subdir}/review/ and {output_subdir}/corrected/

Evaluation focus:
{focus}

{WORKER_CONSTRAINTS}

{RUBRIC}

Final review protocol:
- Return PASS or FAIL. FAIL if any critical issue remains or any rubric dimension is below 3/4.
- Score each rubric dimension from 0 to 4 and explain the score.
- Produce issue IDs FR<n> for decisive issues and AFR<n> for almost-decisive issues.
- Label prior issues as new, repeat, changed, resolved, or unverified when applicable.
- Include an error-budget gate: can success be evaluated, are failures repeating, is scope expanding?
- Save the review and scorecard to `{output_path}`.
- Include the full review in your final answer.
"""


def final_fix_prompt(project_dir: Path, output_subdir: str) -> str:
    """Prompt the final fixer after final reviews."""
    output_path = f"{output_subdir}/final-fix/final-synthesis-fixer.md"
    return f"""\
You are the final synthesis fixer in a review-and-fix loop.

{PROJECT_CORPUS}

Inputs:
- Integrated artifacts: {output_subdir}/synthesis/
- Final reviews: {output_subdir}/final-review/

Task:
Address all decisive final-review findings while preserving the project goals, source grounding, and implementation task usability.

{WORKER_CONSTRAINTS}

Fix protocol:
- For every FR/AFR finding, report fixed, rejected-with-rationale, carried-as-open-decision, or unverified.
- Produce updated final artifacts: MASTER_PLAN.md, INTERFACE_INDEX.md, TASK_DAG.md, RISK_REGISTER.md, MVP_MILESTONES.md, OPEN_QUESTIONS.md, and SCORECARD.json.
- Keep the plan criticizable: explicit goal/context, acceptance criteria, failure criteria, and source evidence.
- Save the full updated final package under `{output_subdir}/final-fix/`.
- Include a concise final answer naming every file created or updated.
"""


def build_phases(project_dir: Path, output_subdir: str) -> list[dict[str, object]]:
    """Return the phased benchmark workflow."""
    draft_jobs = [
        job("01-product-scope", "product-planner", draft_prompt("01-product-scope", "Product surface, user journeys, non-goals, trust tiers, and MVP boundary.", project_dir, output_subdir)),
        job("02-system-architecture", "system-architect", draft_prompt("02-system-architecture", "High-level architecture, runtime boundaries, session lifecycle, and cross-component data/control flows.", project_dir, output_subdir)),
        job("03-component-interfaces", "interface-designer", draft_prompt("03-component-interfaces", "Component APIs, protocol envelopes, filesystem/command broker interfaces, relay interfaces, and adapter contracts.", project_dir, output_subdir)),
        job("04-market-metering-models", "market-modeler", draft_prompt("04-market-metering-models", "Offer schemas, provider-model-token accounting, usage events, signed receipts, pricing, settlement, and disputes.", project_dir, output_subdir)),
        job("05-security-trust-abuse", "security-planner", draft_prompt("05-security-trust-abuse", "Threat mitigations, side-effect mediation, credential isolation, abuse handling, and validation gates.", project_dir, output_subdir)),
        job("06-runtime-adapters-ops", "runtime-ops-planner", draft_prompt("06-runtime-adapters-ops", "Agent-app adapters, host support, runtime trust tiers, operations workflows, observability, and kill switches.", project_dir, output_subdir)),
        job("07-mvp-roadmap-repo-plan", "roadmap-planner", draft_prompt("07-mvp-roadmap-repo-plan", "Staged MVP roadmap, repository layout, package boundaries, milestones, and acceptance criteria.", project_dir, output_subdir)),
        job("08-implementation-task-dag", "task-breakdown-planner", draft_prompt("08-implementation-task-dag", "Implementation task DAG from foundation through launch readiness, with parallel lanes and validation tasks.", project_dir, output_subdir)),
    ]
    review_jobs = [
        job("09-architecture-consistency-review", "architecture-reviewer", review_prompt("09-architecture-consistency-review", "Cross-slice architecture consistency, missing interfaces, dependency contradictions, and unclear ownership.", project_dir, output_subdir)),
        job("10-security-red-team-review", "security-reviewer", review_prompt("10-security-red-team-review", "Security, credential secrecy, side-effect envelopes, relay trust, abuse handling, and threat coverage.", project_dir, output_subdir)),
        job("11-market-economics-review", "market-reviewer", review_prompt("11-market-economics-review", "Market design, token accounting, pricing, receipts, settlement, disputes, and unit economics.", project_dir, output_subdir)),
        job("12-mvp-feasibility-review", "feasibility-reviewer", review_prompt("12-mvp-feasibility-review", "MVP scope, roadmap feasibility, launch gates, operational readiness, and overreach.", project_dir, output_subdir)),
        job("13-task-dag-review", "task-reviewer", review_prompt("13-task-dag-review", "Task granularity, dependencies, missing validation, untestable tickets, and implementation sequencing.", project_dir, output_subdir)),
    ]
    correction_jobs = [
        job("14-product-roadmap-fixer", "product-roadmap-fixer", correction_prompt("14-product-roadmap-fixer", "Product scope, user journeys, MVP milestones, launch gates, and open decisions.", project_dir, output_subdir)),
        job("15-architecture-interface-fixer", "architecture-interface-fixer", correction_prompt("15-architecture-interface-fixer", "Architecture, component contracts, protocols, repo/module boundaries, and handoff artifacts.", project_dir, output_subdir)),
        job("16-market-security-fixer", "market-security-fixer", correction_prompt("16-market-security-fixer", "Market, metering, receipts, trust tiers, credential secrecy, abuse, and operational controls.", project_dir, output_subdir)),
        job("17-task-dag-fixer", "task-dag-fixer", correction_prompt("17-task-dag-fixer", "Implementation task DAG, validation plan, positive/negative tests, and dependency ordering.", project_dir, output_subdir)),
    ]
    synthesis_jobs = [
        job("18-master-plan-synthesizer", "master-plan-synthesizer", synthesis_prompt("18-master-plan-synthesizer", "Produce MASTER_PLAN.md, INTERFACE_INDEX.md, TASK_DAG.md, RISK_REGISTER.md, MVP_MILESTONES.md, and SCORECARD.json.", project_dir, output_subdir)),
        job("19-open-questions-curator", "open-questions-curator", synthesis_prompt("19-open-questions-curator", "Produce OPEN_QUESTIONS.md with owner, timing, risk, decision trigger, and evidence needed for every unresolved item.", project_dir, output_subdir)),
    ]
    final_reviews = [
        job("20-final-source-grounding-review", "final-source-grounding-reviewer", final_review_prompt("20-final-source-grounding-review", "Source grounding, contradictions with corpus, unsupported claims, and unverified assumptions.", project_dir, output_subdir, "final-review")),
        job("21-final-implementation-readiness-review", "final-implementation-readiness-reviewer", final_review_prompt("21-final-implementation-readiness-review", "Task quality, dependencies, interface readiness, validation gates, and handoff usability.", project_dir, output_subdir, "final-review")),
        job("22-final-risk-security-review", "final-risk-security-reviewer", final_review_prompt("22-final-risk-security-review", "Security, abuse, operations, economics risk, unresolved blockers, and launch readiness.", project_dir, output_subdir, "final-review")),
    ]
    final_rereviews = [
        job("24-rereview-source-grounding", "final-rereviewer", final_review_prompt("24-rereview-source-grounding", "Verify final fixes for source grounding and unsupported claims.", project_dir, output_subdir, "final-rereview")),
        job("25-rereview-implementation-readiness", "final-rereviewer", final_review_prompt("25-rereview-implementation-readiness", "Verify final fixes for implementation task quality and interface readiness.", project_dir, output_subdir, "final-rereview")),
        job("26-rereview-risk-security", "final-rereviewer", final_review_prompt("26-rereview-risk-security", "Verify final fixes for risk, security, operations, and launch readiness.", project_dir, output_subdir, "final-rereview")),
    ]
    return [
        {"name": "draft", "title": "Draft Slice Plans", "jobs": draft_jobs},
        {"name": "review", "title": "Cross-Slice Reviews", "jobs": review_jobs},
        {"name": "correct", "title": "Correction Pass", "jobs": correction_jobs},
        {"name": "synthesize", "title": "Integrated Synthesis", "jobs": synthesis_jobs},
        {"name": "final-review", "title": "Final Review", "jobs": final_reviews},
        {"name": "final-fix", "title": "Final Fix", "jobs": [job("23-final-synthesis-fixer", "final-synthesis-fixer", final_fix_prompt(project_dir, output_subdir))]},
        {"name": "final-rereview", "title": "Final Rereview", "jobs": final_rereviews},
    ]


def build_plan(args: argparse.Namespace) -> dict[str, object]:
    """Return the workflow-plan object consumed by workflow runner-matrix."""
    project_dir = Path(args.project_dir).expanduser().resolve()
    return {
        "schema_version": 1,
        "kind": "workflow-plan",
        "title": args.title,
        "summary": "Phased architecture-to-ticket planning workflow with review-and-fix loops.",
        "goal": "Plan the full project from high-level architecture through individual implementation tasks, with error correction and final review.",
        "output_subdir": args.output_subdir,
        "phases": build_phases(project_dir, args.output_subdir),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--title", default="Agent Capacity Market Full Project Planning")
    parser.add_argument("--output-subdir", default="planning-output")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    print(json.dumps(build_plan(args), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
