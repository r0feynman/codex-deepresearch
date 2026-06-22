# Codex DeepResearch Session Handoff

Last updated: 2026-06-22

Use this document to continue implementation without relying on prior chat history.

## Start Here

Run Codex from the project root:

```bash
cd /home/user/Projects/codex-deepresearch
codex
```

Do not implement from `/home/user` directly. If a session starts elsewhere, set command working directories to `/home/user/Projects/codex-deepresearch`.

## Project

- Repository: `r0feynman/codex-deepresearch`
- Local path: `/home/user/Projects/codex-deepresearch`
- GitHub repo: <https://github.com/r0feynman/codex-deepresearch>
- GitHub Project board: <https://github.com/users/r0feynman/projects/1>
- Current `origin/main` at handoff time: `8ff8a22` (`Document merge cleanup workflow`)
- M1 merge commit: `adc8f24` (`Add plugin scaffold smoke runner (#14)`)

## Read First

Read these files before implementing:

1. `AGENTS.md`
2. `README.md`
3. `docs/session-handoff.md`
4. `docs/codex-deepresearch-prd.md`
5. `docs/codex-deepresearch-project-management.md`

The PRD is the product source of truth. GitHub Issues are the implementation source of truth.

Repo-local skills available in this repository:

- `deep-research`: plugin skill scaffold at `plugins/codex-deepresearch/skills/deep-research/SKILL.md`.
- `prd-evolution-review`: reusable PRD writer/reviewer workflow at `.agents/skills/prd-evolution-review/SKILL.md`.

## Current Product Direction

Codex DeepResearch ships as a Codex plugin, not as a standalone product.

Primary UX:

- Installed Codex plugin.
- `$deep-research` Skill invocation inside Codex.

Supporting UX:

- CLI runner for development, automation, and smoke tests.
- Manual source mode for fallback and controlled tests.

The PRD separates these execution modes:

- `codex-plugin`: Codex session drives search/VLM through handoff artifacts.
- `automated-cli`: direct provider/API automation for reproducible runs.
- `manual-sources`: user-provided URLs, PDFs, image URLs, and local images only.

## Important Design Constraint

Codex can search and inspect images during an interactive session, but the plugin runner must not assume a hidden callable API such as:

```ts
codex.search(...)
codex.vlm.analyze(...)
```

For `codex-plugin` mode, the runner should use explicit handoff artifacts:

1. `prepare`
2. Codex fills `search_results.jsonl` and `visual_observations.jsonl`
3. `ingest`
4. `verify`
5. `synthesize`

The Skill wording and smoke UX need to guide Codex toward valid handoff files.

## GitHub Issues

Phase 1 MVP is represented by one epic and twelve canonical MVP task issues.

Epic:

- #1 `[Epic] Phase 1 MVP vertical slice backlog`

Canonical MVP issues:

- #2 `[M1] Plugin Scaffold and Manifest` - completed by PR #14.
- #3 `[M2] Execution Mode Resolver` - next recommended implementation candidate.
- #4 `[M3] Evidence Schema Validator`
- #5 `[M4] Codex Search Handoff Slice`
- #6 `[M5] Manual Sources Slice`
- #7 `[M6] Modality Router Slice`
- #8 `[M7] Fetch and Claim Extraction Slice`
- #9 `[M8] VLM Handoff and Vision Adapter Slice`
- #10 `[M9] Verification Matrix Slice`
- #11 `[M10] Report Generation Slice`
- #12 `[M11] Guardrail Enforcement Slice`
- #13 `[M12] MVP Smoke Suite`

Current recommended starting point:

- Start with #3 `[M2] Execution Mode Resolver`.
- Keep M2 scoped to mode/provider/budget config normalization and invalid-combination errors.
- Do not implement M3+ evidence, fetch, VLM, verification, or report behavior as part of M2.

## Recent Completion Summary

Completed:

- PR #14 closed #2 `[M1] Plugin Scaffold and Manifest`.
- Added the plugin scaffold under `plugins/codex-deepresearch/`.
- Added the canonical DeepResearch skill scaffold at `plugins/codex-deepresearch/skills/deep-research/SKILL.md`.
- Added repo-local marketplace metadata at `.agents/plugins/marketplace.json`.
- Added install/update smoke tooling and validation coverage.
- Added implementation coordination rules in `AGENTS.md`.
- Added merge cleanup workflow notes in commit `8ff8a22`.

Earlier planning completed:

- Created and refined `docs/codex-deepresearch-prd.md`.
- Synced GitHub Issues to PRD tickets M1-M12.
- Added all MVP issues to the GitHub Project board.

## Current Repo Shape

Key paths:

```text
AGENTS.md
README.md
docs/codex-deepresearch-prd.md
docs/codex-deepresearch-project-management.md
docs/session-handoff.md
.agents/plugins/marketplace.json
.agents/skills/prd-evolution-review/SKILL.md
plugins/codex-deepresearch/.codex-plugin/plugin.json
plugins/codex-deepresearch/skills/deep-research/SKILL.md
plugins/codex-deepresearch/scripts/
scripts/validate_repo.py
.github/workflows/ci.yml
```

## Validation Commands

Run these after relevant changes:

```bash
python3 scripts/validate_repo.py
python3 /home/user/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py plugins/codex-deepresearch
git diff --check
```

For changes to `.agents/skills/prd-evolution-review`, also run:

```bash
python3 /home/user/.codex/skills/.system/skill-creator/scripts/quick_validate.py .agents/skills/prd-evolution-review
```

If GitHub-facing changes are pushed, check CI:

```bash
gh run list --limit 5
```

## Suggested First Prompt For New Session

```text
We are continuing the Codex DeepResearch project.

Project path:
/home/user/Projects/codex-deepresearch

First read:
- AGENTS.md
- README.md
- docs/session-handoff.md
- docs/codex-deepresearch-prd.md
- docs/codex-deepresearch-project-management.md

Then inspect GitHub issue #3:
[M2] Execution Mode Resolver

Implement only #3. Keep the change narrow. Before editing, run git status. After editing, run:
- python3 scripts/validate_repo.py
- python3 /home/user/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py plugins/codex-deepresearch
- git diff --check
```

## Working Rules

- Keep implementation scoped to the current GitHub issue.
- Do not rewrite the PRD during implementation unless the issue requires it.
- Do not treat the CLI as the final product; the final product is the Codex plugin.
- Do not rely on hidden Codex-native search/VLM APIs.
- Preserve public-repo safety:
  - no credentials
  - no raw sessions
  - no minidumps
  - no private screenshots
  - no `.env` files

## Known Risks

- `codex-plugin` search/VLM depends on agent-mediated handoff, so Skill text and smoke tests must be precise.
- The MVP must prove that text-only tasks perform zero VLM work.
- Visual-required tasks must produce valid `VisualEvidence` and visual verifier records before high-confidence visual claims are allowed.
- Guardrails are part of MVP, not a later cleanup phase.
