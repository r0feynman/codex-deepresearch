# Codex DeepResearch

Codex DeepResearch is a Codex plugin and skill project for high-quality text and visual research.

The goal is to provide a DeepResearch workflow that can run inside Codex like an installed workflow, while also supporting image discovery, screenshots, and VLM-based evidence analysis.

## Status

Pre-MVP scaffold.

Current contents:

- Product requirements in `docs/codex-deepresearch-prd.md`
- Phase 3 install, quickstart, troubleshooting, and examples in `docs/codex-deepresearch-plugin-guide.md`
- Development management plan in `docs/codex-deepresearch-project-management.md`
- Visual project-management explainer in `docs/codex-deepresearch-project-management.html`
- Session handoff notes in `docs/session-handoff.md`
- Repo-local PRD evolution skill in `.agents/skills/prd-evolution-review`
- Codex plugin scaffold in `plugins/codex-deepresearch`
- DeepResearch skill scaffold in `plugins/codex-deepresearch/skills/deep-research`

## Repository Layout

```text
.
├── AGENTS.md
├── docs/
├── plugins/
│   └── codex-deepresearch/
│       ├── .codex-plugin/plugin.json
│       ├── assets/
│       ├── scripts/
│       └── skills/deep-research/SKILL.md
├── scripts/
│   └── validate_repo.py
├── src/
└── tests/
```

## Local Validation

Run:

```bash
python3 -m unittest discover -s tests
python3 scripts/validate_repo.py
python3 /home/user/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py plugins/codex-deepresearch
plugins/codex-deepresearch/scripts/codex-deepresearch mvp-smoke --runs-dir /tmp/codex-deepresearch-mvp-smoke --suite-id mvp-smoke --clean --invoke '$deep-research: MVP smoke text-only fixture'
tmpdir=/tmp/codex-deepresearch-parallel-validation; rm -rf "$tmpdir"; run_dir=$(plugins/codex-deepresearch/scripts/codex-deepresearch prepare "Parallel orchestration validation" --runs-dir "$tmpdir" --route text_only | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_dir"])'); plugins/codex-deepresearch/scripts/codex-deepresearch orchestrate-parallel --run "$run_dir" --adapter fixture --min-tasks 3
plugins/codex-deepresearch/scripts/codex-deepresearch validate-evidence --evidence tests/fixtures/evidence_schema/valid_evidence.json --search-results tests/fixtures/evidence_schema/search_results.jsonl --visual-observations tests/fixtures/evidence_schema/visual_observations.jsonl --verifier-votes tests/fixtures/evidence_schema/verifier_votes.jsonl
plugins/codex-deepresearch/scripts/codex-deepresearch ingest-manual --question "Manual validation" --runs-dir /tmp/codex-deepresearch-manual-validation --url https://example.com/manual-source
```

`scripts/validate_repo.py` also exercises no-network fetch, guardrail, verification, vision, report-generation, and MVP release-gate smokes against temporary local artifacts. The standalone MVP release gate requires the `codex` CLI on `PATH` by default for plugin install/update smoke. In CI environments without Codex CLI, `validate_repo.py` runs `mvp-smoke --skip-codex-cli-install-check` and accepts only an honest recorded skip where all non-install checks pass and install/update is not marked passed. See `AGENTS.md` for the full pre-PR validation command block.

### Fixture vs Real Parallel E2E

The parallel fixture command in the validation block is a deterministic no-network merge test:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch orchestrate-parallel --run "$run_dir" --adapter fixture --min-tasks 3
```

Fixture success proves task planning, shard validation, dedupe, and merge mechanics. It writes deterministic `example.com` fixture evidence and must not be cited as real Codex child execution.

Real-use Phase 2 E2E is a separate gate. When Codex auth/runtime is available, prepare a real-use run and execute:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch orchestrate-parallel --run "$run_dir" --adapter codex-exec --no-degrade
```

For real-use E2E acceptance, `parallel_orchestration_status.json` and `merge_status.json` must show `adapter=codex-exec`, `evidence_source.type=real_child_execution`, and `accepted_shards > 0`. If a real child run accepts no shards, the run is not successful: `--no-degrade` reports `ok=false` with `status=failed_parallel_no_accepted_shards`, and `evidence_source.type=failed_real_child_execution` with `real_use_e2e_eligible=false`.

If the run explicitly degrades or fails, do not count it as passing real-use E2E. Report `status`, `ok`, `parallel_degraded`, `needs_serial_handoff`, `degraded_reason`, `evidence_source`, `failure_counts`, `diagnostics`, and `merge_status.json` `failed_tasks` / `rejected_shards` / `blocked_tasks` entries instead.

Manual source runs are a third provenance path. `ingest-manual` writes `evidence_source.type=manual_handoff`; this can validate manual handoff mechanics but does not satisfy fixture or real `codex-exec` E2E.

Use the PRD's "Parallel status matrix" and "Report quality gate" sections when interpreting Phase 2 results.

## GitHub Project Management

Milestones, labels, and starter issues can be seeded with:

```bash
python3 scripts/bootstrap_github.py
```

The visual GitHub Projects board needs the GitHub CLI `project` scope once:

```bash
gh auth refresh -s project
python3 scripts/bootstrap_project_board.py
```

The board script creates or reuses the `Codex DeepResearch` project, links it to this repository, adds open issues, and sets these custom fields:

- `Phase`
- `Work Type`
- `Component`
- `Priority`
- `Workflow Status`

## Codex Usage During Development

For local plugin install, update, remove, first-run verification, troubleshooting, and beta examples, see `docs/codex-deepresearch-plugin-guide.md`.

From this repository, Codex can use the repo-local skill mirror:

```text
$deep-research: research the current topic with text and visual evidence
```

For plugin testing, add the repository marketplace from the repo root if Codex does not show it automatically:

```bash
codex plugin marketplace add .
```

Then restart Codex, open `/plugins`, install `codex-deepresearch`, and start a new thread.

## Public Repo Safety

Do not commit local Codex or Claude sessions, credentials, minidumps, raw browser profiles, private screenshots, or `.env` files. This repository is intended to be safe for a public GitHub repo.
