---
name: prd-evolution-review
description: Create or revise product requirements documents through a writer plus adversarial reviewer evolution loop. Use when Codex is asked to write a PRD, improve an existing PRD, turn vague product goals into implementation-ready requirements, run strict PRD review, score a PRD, or iterate until a quality threshold is reached.
---

# PRD Evolution Review

Use this skill to produce implementation-ready PRDs by alternating between two roles:

- `PRD writer`: expands requirements, clarifies product direction, defines scope, and decomposes work.
- `Adversarial reviewer`: attacks ambiguity, missing implementation detail, weak acceptance criteria, scope drift, and product-level gaps.

If the user explicitly asks for subagents, spawn separate writer/reviewer agents when the current environment supports it. If subagents are unavailable or not explicitly authorized, run the same roles sequentially in the main agent.

## Quick Workflow

1. Gather context.
2. Draft or revise the PRD.
3. Run adversarial review.
4. Apply findings.
5. Score the evolved PRD.
6. Repeat until the target score is reached.
7. Save the final PRD and, if useful, update implementation issues.

Default target score: `9.0 / 10`.

Default output format: Markdown PRD.

## Gather Context

Read existing source material before drafting:

- User's product goal and constraints.
- Existing PRD or planning docs.
- Repo `README.md`, `AGENTS.md`, issue templates, and relevant architecture docs if working in a repo.
- Existing GitHub/Linear issues only if the task asks to sync project management.
- Prior decisions from the current thread.

Ask at most 3 clarification questions only when missing answers would materially change the PRD. Otherwise make explicit assumptions and continue.

## Draft Or Revise

Use [references/prd-template.md](references/prd-template.md) when creating a PRD from scratch or when an existing PRD lacks structure.

The PRD must make these items explicit:

- Final product surface and primary user workflow.
- Non-goals.
- Execution modes or operating modes.
- Inputs and outputs.
- Storage format or schema.
- Acceptance criteria.
- Validation and test strategy.
- Work breakdown by phase.
- MVP scope and post-MVP roadmap.
- Risks, policy constraints, and failure modes.
- First implementable issue.

For plugin, skill, CLI, or API products, separate product UX from developer/debug UX. Do not let a helper CLI become the implicit product unless the PRD says that intentionally.

## Adversarial Review

Use [references/prd-rubric.md](references/prd-rubric.md) for scoring and review prompts.

The reviewer must lead with findings, not compliments. Each finding should include:

- Severity: `blocking`, `major`, or `minor`.
- Location or section.
- Why it matters.
- Concrete change required.

The reviewer should specifically check:

- Is the final product direction unmistakable?
- Can an engineer implement the next issue without guessing?
- Are schemas, interfaces, states, and handoff contracts specific enough?
- Are MVP and post-MVP phases separated?
- Are tasks decomposed into independently testable issues?
- Are acceptance criteria measurable?
- Are security, privacy, cost, and policy risks handled early enough?
- Does the PRD avoid drifting into a different product?

## Evolution Loop

After each review:

1. Group findings by theme.
2. Apply all `blocking` findings.
3. Apply `major` findings unless they conflict with user constraints.
4. Record deferred `minor` findings as risks or follow-up work.
5. Re-score the PRD.

Stop only when:

- score is at least the target score, and
- no blocking findings remain, and
- the next implementation step is clear.

If a loop reaches 3 iterations without meaningful score improvement, stop and explain the unresolved decision or missing input.

## Scoring

Use a 10-point score. The final answer should include a compact scorecard.

Default rubric:

- Product direction: 1.0
- Implementable execution contract: 1.5
- Mode/provider/interface separation: 1.0
- Data/schema specificity: 1.5
- MVP work breakdown: 1.0
- Acceptance and validation quality: 1.0
- Risk, policy, and cost handling: 1.0
- Roadmap beyond MVP: 1.0
- Goal alignment: 1.0

Adjust weights only when the user provides a different quality bar.

## Subagent Pattern

Use real subagents only when the user explicitly requests them.

Suggested writer prompt:

```text
You are the PRD writer. Create or revise the PRD so that requirements are detailed, unambiguous, product-level, and implementable. Preserve the user's product direction. Output concrete PRD edits or a replacement PRD.
```

Suggested reviewer prompt:

```text
You are the adversarial PRD reviewer. Be strict. Find ambiguity, missing implementation contracts, weak task breakdown, hidden product drift, weak acceptance criteria, and missing risks. Return findings ordered by severity and give a score out of 10.
```

The main agent is responsible for integration. Do not blindly paste subagent output. Resolve conflicts, update the PRD, and provide the final score.

## Output Requirements

When saving a PRD:

- Use Markdown.
- Keep stable section headings.
- Include implementation-ready tickets or issue candidates.
- Include a final self-review summary:
  - problems found during review
  - what changed because of review
  - risks remaining

When updating a repo:

- Use existing docs and issue naming conventions.
- Keep unrelated files untouched.
- Run available validation commands.
- If GitHub issues are updated, summarize created, edited, closed, or deferred issues.
