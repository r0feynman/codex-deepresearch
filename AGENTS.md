# AGENTS.md

## Project Purpose

This repository builds Codex DeepResearch as a Codex plugin plus reusable skill.

The product goal is described in `docs/codex-deepresearch-prd.md`. The development process is described in `docs/codex-deepresearch-project-management.md`.

## Working Rules

- Treat `/home/user/Projects/codex-deepresearch` as the project root.
- Do not add files from `/home/user/.codex`, `/home/user/.claude`, browser profiles, dumps, credentials, or unrelated home-directory files.
- Keep the plugin installable from `plugins/codex-deepresearch`.
- Keep the canonical skill at `plugins/codex-deepresearch/skills/deep-research/SKILL.md`.
- Prefer small issues and small PRs tied to the documented phase roadmap.
- For research features, preserve evidence metadata: source URL, retrieval time, modality, extracted claims, verifier results, and confidence.

## Implementation Coordination

When the user asks to implement work, the main Codex agent must act as a coordinator rather than the primary implementer.

- Create a dedicated local branch before implementation starts.
- Push the branch to the remote before implementation work begins.
- Use the GitHub issue as the implementation source of truth, including its scope, dependencies, and acceptance-test checklist.
- Update the issue checklist as acceptance-test items are implemented or verified.
- Keep the GitHub Project UI in sync during the work: move the project item to `In Progress` when implementation starts, to review state when the PR is opened, and to `Done` after the issue is closed or merged.
- Spawn an implementation subagent to make the scoped code or documentation changes.
- Have the implementation subagent take the work through PR creation.
- After the PR exists, spawn an adversarial review subagent to review for blockers, bugs, behavioral regressions, missing tests, and acceptance-criteria gaps.
- If the review subagent reports findings, send the work back through implementation and review until no blockers remain.
- After blockers are cleared, the main Codex coordinator performs the final review and reports the outcome to the user.
- When the user asks to merge a PR, complete the post-merge cleanup as part of the same workflow: merge the PR, verify the linked issue closed, update the GitHub Project item to `Done`, fetch/prune remotes, switch back to the target branch, fast-forward it to the remote, delete the merged local branch when safe, and report any remaining unrelated worktree changes.

## Validation

Before publishing or opening a PR, run:

```bash
python3 -m unittest discover -s tests
python3 scripts/validate_repo.py
python3 /home/user/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py plugins/codex-deepresearch
plugins/codex-deepresearch/scripts/codex-deepresearch validate-evidence --evidence tests/fixtures/evidence_schema/valid_evidence.json --search-results tests/fixtures/evidence_schema/search_results.jsonl --visual-observations tests/fixtures/evidence_schema/visual_observations.jsonl --verifier-votes tests/fixtures/evidence_schema/verifier_votes.jsonl
plugins/codex-deepresearch/scripts/codex-deepresearch ingest-manual --question "Manual validation" --runs-dir /tmp/codex-deepresearch-manual-validation --url https://example.com/manual-source
tmpdir=/tmp/codex-deepresearch-vision-validation; rm -rf "$tmpdir"; run_dir=$(plugins/codex-deepresearch/scripts/codex-deepresearch prepare "Vision adapter validation" --runs-dir "$tmpdir" --route visual_required | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_dir"])'); plugins/codex-deepresearch/scripts/codex-deepresearch ingest-vision --run "$run_dir" --provider codex-interactive
```

`python3 scripts/validate_repo.py` also runs a no-network `fetch-claims` smoke against a temporary local HTML source.

Keep this section current when new implementation test commands are added.

## Public Repository Safety

This repo is intended to be public. Before committing, check:

```bash
git status --short
git diff --cached --stat
git diff --cached
```

Never commit API keys, `.env` files, local sessions, minidumps, private screenshots, or generated evidence bundles containing personal data.
