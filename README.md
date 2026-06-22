# Codex DeepResearch

Codex DeepResearch is a Codex plugin and skill project for high-quality text and visual research.

The goal is to provide a DeepResearch workflow that can run inside Codex like an installed workflow, while also supporting image discovery, screenshots, and VLM-based evidence analysis.

## Status

Pre-MVP scaffold.

Current contents:

- Product requirements in `docs/codex-deepresearch-prd.md`
- Development management plan in `docs/codex-deepresearch-project-management.md`
- Visual project-management explainer in `docs/codex-deepresearch-project-management.html`
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
python3 scripts/validate_repo.py
python3 /home/user/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py plugins/codex-deepresearch
```

## Codex Usage During Development

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
