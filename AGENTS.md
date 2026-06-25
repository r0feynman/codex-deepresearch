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

## GitHub Issue Creation

When the user asks to create, register, publish, split, or bulk-create GitHub issues, use `.agents/skills/github-issue-quality-gate` before creating any issue.

- Draft issues locally first; do not run `gh issue create` until the draft passes the quality gate.
- Spawn an adversarial issue-review subagent to review the local draft for blockers, ambiguity, duplicate risk, PRD/roadmap mismatch, missing dependencies, unsafe content, weak acceptance criteria, and metadata problems.
- Revise the local draft and repeat adversarial review until no blockers remain.
- After blockers are cleared, the main Codex coordinator scores the local draft using the skill rubric. A draft must score at least 9/10 before it can be registered as a GitHub issue.
- Only after the local draft has no blockers and scores at least 9/10, create the GitHub issue.
- After creation, add the registered issue to the GitHub Project, set Project fields, and verify `projectItems` plus `gh project item-list` before reporting completion.
- Report the created issue URL, final score, review result, and Project field status to the user.

## Validation

Before publishing or opening a PR, run:

```bash
python3 -m unittest discover -s tests
python3 scripts/validate_repo.py
python3 /home/user/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py plugins/codex-deepresearch
plugins/codex-deepresearch/scripts/codex-deepresearch mvp-smoke --runs-dir /tmp/codex-deepresearch-mvp-smoke --suite-id mvp-smoke --clean --invoke '$deep-research: MVP smoke text-only fixture'
plugins/codex-deepresearch/scripts/codex-deepresearch fresh-session-e2e --runs-dir /tmp/codex-deepresearch-fresh-session-e2e --suite-id fresh-session-e2e --clean --real-codex-exec skip
tmpdir=/tmp/codex-deepresearch-fresh-session-visual-e2e; rm -rf "$tmpdir"; plugins/codex-deepresearch/scripts/codex-deepresearch fresh-session-visual-e2e --runs-dir "$tmpdir" --suite-id fresh-session-visual-e2e --clean --real-codex-interactive skip
plugins/codex-deepresearch/scripts/codex-deepresearch public-beta-validation --runs-dir /tmp/codex-deepresearch-public-beta-validation --suite-id public-beta-validation --clean --allow-blocked
tmpdir=/tmp/codex-deepresearch-parallel-validation; rm -rf "$tmpdir"; run_dir=$(plugins/codex-deepresearch/scripts/codex-deepresearch prepare "Parallel orchestration validation" --runs-dir "$tmpdir" --route text_only | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_dir"])'); plugins/codex-deepresearch/scripts/codex-deepresearch orchestrate-parallel --run "$run_dir" --adapter fixture --min-tasks 3
plugins/codex-deepresearch/scripts/codex-deepresearch validate-evidence --evidence tests/fixtures/evidence_schema/valid_evidence.json --search-results tests/fixtures/evidence_schema/search_results.jsonl --visual-observations tests/fixtures/evidence_schema/visual_observations.jsonl --verifier-votes tests/fixtures/evidence_schema/verifier_votes.jsonl
plugins/codex-deepresearch/scripts/codex-deepresearch ingest-manual --question "Manual validation" --runs-dir /tmp/codex-deepresearch-manual-validation --url https://example.com/manual-source
tmpdir=/tmp/codex-deepresearch-vision-validation; rm -rf "$tmpdir"; run_dir=$(plugins/codex-deepresearch/scripts/codex-deepresearch prepare "Vision adapter validation" --runs-dir "$tmpdir" --route visual_required | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_dir"])'); plugins/codex-deepresearch/scripts/codex-deepresearch ingest-vision --run "$run_dir" --provider codex-interactive
tmpdir=/tmp/codex-deepresearch-visual-acquisition-validation; rm -rf "$tmpdir"; run_dir=$(plugins/codex-deepresearch/scripts/codex-deepresearch prepare "Visual acquisition validation" --runs-dir "$tmpdir" --route visual_required | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_dir"])'); plugins/codex-deepresearch/scripts/codex-deepresearch acquire-visual --run "$run_dir" --provider local-page --provider local-image-fixture --provider local-screenshot-fixture --screenshot-mode all; plugins/codex-deepresearch/scripts/codex-deepresearch ingest-vision --run "$run_dir" --provider codex-interactive
tmpdir=/tmp/codex-deepresearch-guardrail-validation; rm -rf "$tmpdir"; run_dir=$(plugins/codex-deepresearch/scripts/codex-deepresearch prepare "Guardrail validation" --runs-dir "$tmpdir" --route text_only | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_dir"])'); python3 - "$run_dir" <<'PY'
import json, sys
from pathlib import Path
run_dir = Path(sys.argv[1])
evidence = json.loads((run_dir / "evidence.json").read_text())
source = {
    "id": "src_guardrail_validation",
    "type": "web",
    "url": "https://example.com/guardrail",
    "title": "Guardrail Validation Source",
    "published_at": None,
    "accessed_at": "2026-06-22T00:00:00Z",
    "quality": "secondary",
    "retrieval_status": "fetched",
    "local_artifact_path": "sources/src_guardrail_validation.html",
    "license_policy": "allowed",
    "robots_policy": "disallowed",
    "policy_decision": "allowed",
    "policy_flags": [],
}
evidence["sources"] = [source]
evidence["claims"] = [{
    "id": "claim_guardrail_validation",
    "text": "A legal compliance claim needs guardrail review.",
    "claim_type": "text",
    "supporting_sources": ["src_guardrail_validation"],
    "supporting_images": [],
    "quote_spans": [{"source_id": "src_guardrail_validation", "quote": "A legal compliance claim needs guardrail review.", "location": "paragraph 1"}],
    "votes": [],
    "verification_status": "supported",
    "review_status": "human_accepted",
    "promotion_status": "promoted_memory",
    "confidence": "high",
    "caveats": [],
}]
(run_dir / "evidence.json").write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
PY
plugins/codex-deepresearch/scripts/codex-deepresearch enforce-guardrails --run "$run_dir"
tmpdir=/tmp/codex-deepresearch-verify-validation; rm -rf "$tmpdir"; run_dir=$(plugins/codex-deepresearch/scripts/codex-deepresearch prepare "Verification matrix validation" --runs-dir "$tmpdir" --route text_only | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_dir"])'); page="$run_dir/source.html"; printf '<html><body><p>The verification matrix validation command extracts a source linked claim.</p></body></html>' > "$page"; python3 - "$run_dir" "$page" <<'PY'
import json, sys
from pathlib import Path
run_dir = Path(sys.argv[1])
page = Path(sys.argv[2])
evidence = json.loads((run_dir / "evidence.json").read_text())
source = {
    "id": "src_verify_validation",
    "type": "web",
    "url": page.resolve().as_uri(),
    "title": "Verification Validation Source",
    "published_at": None,
    "accessed_at": "2026-06-22T00:00:00Z",
    "quality": "primary",
    "retrieval_status": "fetched",
    "local_artifact_path": "sources/src_verify_validation.html",
    "license_policy": "allowed",
    "robots_policy": "allowed",
    "policy_decision": "allowed",
    "policy_flags": [],
    "route": "text_only",
    "angle_id": "angle_001",
}
evidence["sources"] = [source]
evidence["claims"] = [{
    "id": "claim_verify_validation",
    "text": "The verification matrix validation command extracts a source linked claim.",
    "claim_type": "text",
    "supporting_sources": ["src_verify_validation"],
    "supporting_images": [],
    "quote_spans": [{"source_id": "src_verify_validation", "quote": "The verification matrix validation command extracts a source linked claim.", "location": "paragraph 1"}],
    "votes": [],
    "verification_status": "unverified",
    "review_status": "not_reviewed",
    "promotion_status": "not_eligible",
    "confidence": "low",
    "caveats": [],
}]
(run_dir / "evidence.json").write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
PY
plugins/codex-deepresearch/scripts/codex-deepresearch verify-claims --run "$run_dir"
plugins/codex-deepresearch/scripts/codex-deepresearch synthesize --run "$run_dir"
```

`python3 scripts/validate_repo.py` also runs no-network `fetch-claims`, `enforce-guardrails`, `synthesize`, CI-safe visual E2E, and public beta validation smokes against temporary local artifacts.

Keep this section current when new implementation test commands are added.

## Public Repository Safety

This repo is intended to be public. Before committing, check:

```bash
git status --short
git diff --cached --stat
git diff --cached
```

Never commit API keys, `.env` files, local sessions, minidumps, private screenshots, or generated evidence bundles containing personal data.
