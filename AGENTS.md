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

## Validation

Before publishing or opening a PR, run:

```bash
python3 scripts/validate_repo.py
python3 /home/user/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py plugins/codex-deepresearch
```

When implementation code is added, extend this section with the project test command.

## Public Repository Safety

This repo is intended to be public. Before committing, check:

```bash
git status --short
git diff --cached --stat
git diff --cached
```

Never commit API keys, `.env` files, local sessions, minidumps, private screenshots, or generated evidence bundles containing personal data.
