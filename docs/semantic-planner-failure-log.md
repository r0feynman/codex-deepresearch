# Semantic Planner Failure Log

This is the living log for semantic planner, semantic release, public beta, E2E, and full-runner validation failures.

Purpose:

- Preserve failure evidence outside chat history.
- Separate facts from inferences and unknowns.
- Track repeated patterns before adding more case-specific patches.
- Support decisions about whether the semantic decomposition architecture needs structural changes.

Entry format:

- Date/time
- Prompt, issue, PR, suite, and command
- Run directory and key artifacts
- Directly observed failure
- Facts
- Inferences
- Unknowns
- Next diagnostic or fix direction

## 2026-07-23 - PR #148 / `sem-reg-011` Provider Coverage And Partition Failure

Prompt:

`Compare OAuth device-flow implementation guidance across official provider documentation.`

Context:

- Branch: `issue-133-report-section-uniqueness`
- PR: #148, `Harden semantic release planner contracts`
- Issue: #133 remains open
- Suite: `semantic-release-validation`
- Prompt id: `sem-reg-011`
- Command class: `codex-deepresearch prepare`

Run directory:

`/tmp/codex-dr-pr148-semreg011-budgetparse-b23aa56-20260723T072704Z/dr_20260723T072704`

Key artifacts:

- `semantic_plan.json`
- `semantic_plan_review.json`
- `semantic_planner_convergence.json`
- `evidence.json`
- `run_trace.jsonl`

Directly observed failure:

- Status: `blocked_semantic_review_failed`
- Reviewer verdict: `release_ineligible`
- Reviewer score: `7.8`
- Failure codes:
  - `locked_oracle_scope_alignment_failed`
  - `semantic_release_ineligible`
- Reviewer blockers:
  - `INCOMPLETE_SELECTED_PROVIDER_INITIATION_COVERAGE`
  - `TASK_PARTITION_CONSTRAINT_VIOLATION`

Facts:

- The final run failed after 3 convergence attempts.
- Attempt 1 failed deterministic validation with `REQ_003_COMPARISON_DELIVERABLE_INCOMPLETE`.
- Attempt 2 failed deterministic validation with repeated `bounded_task_requirement_exceeds_max_sources`.
- Attempt 3 passed deterministic validation but failed reviewer validation.
- The final reviewer said the selected-provider initiation coverage was incomplete: Microsoft, Google, and GitHub were covered, but Okta, Auth0, and Amazon Cognito lacked bounded initiation/support-status coverage.
- The final reviewer said the plan declared deterministic one-provider or two-provider task partitioning, but several tasks grouped three providers.
- The previously observed budget-parser defect was corrected in this run: `semantic_plan.json` and `evidence.json` both recorded `runner_source_budget.max_unique_sources=20`.

Inferences:

- This failure is not the same as the earlier budget parsing failure.
- The planner/retry loop can satisfy local deterministic validators while still violating higher-level semantic execution constraints expressed by the reviewer.
- Provider-set binding and provider-partition enforcement are likely under-specified as structured planner contracts.

Unknowns:

- Whether the selected-provider set should always be fixed by the oracle before planner execution, or whether planner may choose it when the prompt says only "official provider documentation."
- Whether one-provider/two-provider partitioning should be a hard schema field instead of prose inside `constraints`.
- Whether the planner adapter received enough machine-readable feedback from attempts 1 and 2 to repair provider coverage without inventing new provider groupings.

Next diagnostic or fix direction:

- Inspect the locked oracle for `sem-reg-011` and identify where selected providers and partition cardinality are represented.
- If selected providers and max providers per task are only prose, promote them to machine-readable fields such as `required_entities.providers[]` and `task_partition_contract.max_providers_per_task`.
- Add deterministic validation that every selected provider has coverage for each required comparison dimension or an explicit unsupported/unknown finding.
- Add deterministic validation that tasks do not exceed `max_providers_per_task` when a provider-partition contract exists.
- Avoid patching only the OAuth prompt; the fix should apply to any named-entity comparison plan.

## Structural Review: Current Evidence

Facts:

- Repeated failures have clustered around semantic plan contracts, not around basic test execution.
- Previously logged failures in `docs/semantic-regression-30-failure-summary.md` include source-budget contradictions, unbound jurisdiction placeholders, domain drift, materialization lineage gaps, and source-budget enforcement gaps.
- Recent PR #148 review found additional contract problems: request caps overriding stricter user caps, shared source-pool reuse hiding over-cap obligations, missing runner budget propagation, and search-result cap conflicts.
- The latest `sem-reg-011` failure shows another contract class: selected entity coverage and task partition constraints are not enforced deterministically before reviewer judgment.

Inferences:

- The repeated failures suggest a structural issue, not just isolated bad prompts.
- The current planner path still relies too much on natural-language constraints and post-hoc repair heuristics.
- Reviewer feedback catches real semantic gaps, but the system is using reviewer failures as the main enforcement mechanism rather than encoding the contract before fan-out.
- The likely structural fix is to move recurring semantic requirements from prose into typed contracts that deterministic validation can enforce.

Likely contract fields to promote:

- `selected_entities`: named providers, jurisdictions, products, standards, documents, or artifacts.
- `required_dimension_coverage`: required comparison dimensions per selected entity.
- `task_partition_contract`: max entities/providers per task, required partition key, and allowed grouping rules.
- `source_budget_contract`: runner-level unique source cap, per-task source cap, and whether caps are user constraints or runner defaults.
- `result_budget_contract`: per-search result cap and final-report surfaced-result cap.
- `coverage_matrix`: entity by dimension coverage status with `covered`, `unsupported`, `unknown`, or `not_applicable`.

Unknowns:

- How much of this should be produced by the oracle versus the planner.
- Whether semantic reviewer should remain a final judge only, or also produce typed repair directives.
- Whether existing manifests already encode enough oracle structure and the implementation is failing to consume it.

Current recommendation:

- Stop adding one-off natural-language parser exceptions after each failed prompt.
- Add a hardening issue for typed semantic decomposition contracts if one is not already open.
- Before running the full #133 release suite again, make provider/entity coverage and partition constraints deterministic for at least one representative class: named provider comparison.

## 2026-07-24 - Chat Research Invoke / Planner Adapter No-Artifact Stall

Prompt:

`Claude Deep Research and Anthropic multi-agent research architecture: how do they handle semantic task decomposition, worker orchestration, validation, and failure control compared with our planner validation failures?`

Context:

- Command class: `codex-deepresearch invoke`
- Selected mode: default `$deep-research` full-runner path
- Issue/PR context: not an issue acceptance run; this was a chat research attempt while answering a user investigation request

Run directory:

- None found before interruption.
- No `run_status.json` was found under recent `/tmp` deepresearch paths before interruption.

Directly observed failure:

- The command stayed inside semantic planner adapter execution with no user-visible output.
- Process tree showed `python3 ... codex-deepresearch invoke` waiting on `codex exec --json --ephemeral ... --output-schema ... semantic_adapter_schemas/planner.json`.
- The process was manually interrupted with Ctrl-C after several polling intervals.
- The Python traceback ended in `subprocess.run(...).communicate(...)` inside `_run_codex_semantic_adapter_command_with_capacity_retry`.

Facts:

- The invocation did not reach run directory creation or `run_status.json` materialization before interruption.
- No evidence bundle, search task file, report, or terminal blocked status was produced.
- This was not a validator rejection. It was an adapter execution stall or long-running planner call before observable run artifacts existed.

Inferences:

- The invoke path still has an observability gap before `run_status.json` exists.
- A stalled or long-running semantic planner adapter can leave the operator without a durable status artifact.
- This is a separate class from semantic reviewer failures such as provider coverage or task partition violations.

Unknowns:

- Whether the adapter would have eventually returned if allowed to keep running.
- Whether the delay was due to Codex model capacity, adapter prompt complexity, output-schema generation, network/service latency, or another subprocess condition.
- Whether timeout metadata would have been persisted if the configured timeout had elapsed naturally instead of manual interruption.

Next diagnostic or fix direction:

- Create run scaffolding and `run_status.json` before invoking the semantic planner adapter.
- Record adapter command start, last observed child process, elapsed time, and timeout/interruption state in a durable artifact.
- Consider a shorter operator-visible progress heartbeat for semantic adapter calls.

## 2026-07-24 - PR #148 / Typed Contract Schema Test Failure

Prompt or issue:

- Issue #133 / PR #148 schema-test implementation for optional typed semantic contract fields.
- Test prompt: `Compare OAuth device-flow implementation guidance across official providers.`

Command or suite:

```bash
python3 -m unittest tests.test_semantic_planner.SemanticPlannerTests.test_planner_adapter_schema_accepts_optional_typed_contract_fields tests.test_semantic_planner.SemanticPlannerTests.test_codex_semantic_plan_preserves_optional_typed_contract_fields
```

Run directory:

- None. This was a unit-test failure before a real run artifact was produced.

Directly observed failure:

- `test_planner_adapter_schema_accepts_optional_typed_contract_fields` failed because the test validated the full fixture raw response against `planner.json`; the schema only allows top-level `candidate_plan` and `provenance`, so extra fixture metadata such as `planner_adapter`, `prompt_version`, `artifact_type`, and `schema_version` were rejected.
- `test_codex_semantic_plan_preserves_optional_typed_contract_fields` failed with `result["status"] == "blocked_semantic_planner_unavailable"` instead of `awaiting_search_results`.

Facts:

- The first failure is a test construction bug, not a product schema defect: the schema is intentionally stricter than the internal fixture response envelope.
- The second failure needs narrower inspection before changing production code; it occurred in the fake-adapter prepare path during the new preservation test.

Inferences:

- Schema acceptance should be tested against the exact output-schema envelope: `candidate_plan` plus `provenance`.
- The preservation test should either use the established fake-adapter path correctly or directly exercise the candidate-to-plan normalization boundary if the fake prepare path is too broad for this unit scope.

Unknowns:

- The exact cause of the `blocked_semantic_planner_unavailable` status in the new preservation test has not yet been inspected.

Next diagnostic or fix direction:

- Adjust the schema test to validate only the output-schema payload.
- Inspect the preservation test result diagnostics, then prefer a narrow normalization-boundary test over a full prepare test if no production defect is shown.

## 2026-07-24 - PR #148 / Typed Contract Test Retry Failure

Prompt or issue:

- Issue #133 / PR #148 schema-test implementation for optional typed semantic contract fields.
- Test prompt: `Compare OAuth device-flow implementation guidance across official providers.`

Command or suite:

```bash
python3 -m unittest tests.test_semantic_planner.SemanticPlannerTests.test_planner_adapter_schema_accepts_optional_typed_contract_fields tests.test_semantic_planner.SemanticPlannerTests.test_codex_semantic_plan_preserves_optional_typed_contract_fields
```

Run directory:

- None. The failure occurred in unit-test/fake-adapter setup before a durable research run was produced.

Directly observed failure:

- Schema test still failed because the fixture `candidate_plan` included internal helper-only fields such as `depth_preset`, `original_question`, and `model_or_surface` that are not part of `planner.json`.
- Preservation test errored in production code with `NameError: name '_materialize_candidate_typed_semantic_contracts' is not defined` from `plugins/codex-deepresearch/src/deepresearch/semantic_planner.py` inside `codex_semantic_candidate_plan`.

Facts:

- The schema failure is still a test construction issue: the output-schema contract should be tested with only schema-declared candidate fields plus the new optional typed contract fields.
- The `NameError` is a real current-worktree code defect, but it appears in `semantic_planner.py`, which this schema/test subtask was asked to avoid editing unless necessary.

Inferences:

- The preservation test should avoid the full prepare path until the main implementation agent resolves the undefined materialization function, or the schema/test subtask should be allowed to add the missing narrow helper if coordinating agents agree.
- A narrow candidate-plan preservation test can still prove `_candidate_plan_from_adapter_response` keeps typed fields without depending on the full convergence pipeline.

Unknowns:

- Whether another implementation agent already intends to define `_materialize_candidate_typed_semantic_contracts`.
- Whether the missing helper is part of the broader typed-contract implementation or an accidental stale call.

Next diagnostic or fix direction:

- Narrow the schema test to a schema-shaped candidate payload.
- Replace the full prepare preservation test with a direct candidate normalization boundary test unless the main agent assigns the undefined helper fix to this subtask.

## 2026-07-24 - PR #148 / Typed Contract Schema Nested Fixture Failure

Prompt or issue:

- Issue #133 / PR #148 schema-test implementation for optional typed semantic contract fields.

Command or suite:

```bash
python3 -m unittest tests.test_semantic_planner.SemanticPlannerTests.test_planner_adapter_schema_accepts_optional_typed_contract_fields tests.test_semantic_planner.SemanticPlannerTests.test_candidate_plan_normalization_preserves_optional_typed_contract_fields
```

Directly observed failure:

- `test_candidate_plan_normalization_preserves_optional_typed_contract_fields` passed.
- `test_planner_adapter_schema_accepts_optional_typed_contract_fields` failed because nested fixture records under the schema-shaped candidate still included internal fields such as `flags`, `quality_requirements`, `decision`, and `requires_official_or_primary` that `planner.json` intentionally rejects.

Facts:

- This is a test fixture shaping failure, not evidence that the new optional typed contract fields are rejected.
- The narrow normalization boundary already preserves the typed fields.

Inferences:

- The schema test should prune existing fixture data recursively according to `planner.json`, then add the new typed fields after pruning. That keeps the test focused on the new optional fields instead of unrelated fixture metadata.

Unknowns:

- None for this unit-test construction issue.

Next diagnostic or fix direction:

- Add a test-local schema-pruning helper and rerun the targeted tests.

## 2026-07-24 - PR #148 / Typed Contract Schema Fixture Source Policy Failure

Prompt or issue:

- Issue #133 / PR #148 schema-test implementation for optional typed semantic contract fields.

Command or suite:

```bash
python3 -m unittest tests.test_semantic_planner.SemanticPlannerTests.test_planner_adapter_schema_accepts_optional_typed_contract_fields tests.test_semantic_planner.SemanticPlannerTests.test_candidate_plan_normalization_preserves_optional_typed_contract_fields
```

Directly observed failure:

- Schema test failed because the reused fake candidate task did not satisfy `planner.json`'s nested `source_policy` required keys: `policy`, `allow_secondary`, and `required_source_quality`.
- The candidate normalization preservation test continued to pass.

Facts:

- The failure is unrelated to the new typed contract fields.
- Reusing the broad fake adapter fixture keeps exposing unrelated internal fixture/schema mismatches.

Inferences:

- The schema acceptance test should use a minimal hand-authored schema-valid candidate payload instead of pruning a broad internal fixture.

Unknowns:

- None for this unit-test construction issue.

Next diagnostic or fix direction:

- Replace the schema test fixture with a minimal `planner.json`-valid candidate and add the typed contract fields on top.

## 2026-07-24 - PR #148 / Typed Task Binding Materialization Diff Failure

Prompt or issue:

- Issue #133 / PR #148 typed semantic contract implementation.

Command or suite:

```bash
python3 -m unittest tests.test_semantic_planner tests.test_search_handoff
```

Directly observed failure:

- Initial run failed with 6 failures.
- The visible failure path was `semantic_materialization_diff.valid=false`.
- `research_tasks.json` and several materialized artifacts were missing the newly compared typed task binding fields:
  - `semantic_entity_refs`
  - `semantic_dimension_refs`
  - `final_deliverable_binding`

Facts:

- The typed fields had been added to `SEMANTIC_MATERIALIZATION_ALIGNMENT_FIELDS`.
- The diff checker then correctly expected those fields in materialized task artifacts.
- At the failure point, at least `research_tasks` records had `actual=None` while the accepted semantic plan expected empty lists or empty objects for those typed binding fields.
- A later rerun of the same two test files passed after shared-worktree implementation changes were present:

```bash
python3 -m unittest -v tests.test_semantic_planner tests.test_search_handoff
```

Result: `Ran 195 tests` with exit code 0.

Inferences:

- This was a propagation gap created by strengthening materialization diff before all downstream task artifacts carried the new typed binding fields.
- The failure was useful: it proved the diff now catches typed contract lineage loss instead of silently passing.

Unknowns:

- Which exact concurrent implementation edit fixed the propagation gap before the rerun. The test result proves the current worktree passes, but commit-level attribution is not yet separated.

Next diagnostic or fix direction:

- Before PR merge, inspect the final diff and ensure task artifact generation always writes `semantic_entity_refs`, `semantic_dimension_refs`, and `final_deliverable_binding` when those fields are present or defaulted in the accepted semantic plan.

## 2026-07-24 - PR #148 / Typed Contract Canary Blocked By Missing Adapter

Prompt:

`Compare OAuth device-flow implementation guidance across official provider documentation.`

Command or suite:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch prepare "Compare OAuth device-flow implementation guidance across official provider documentation." --runs-dir /tmp/codex-dr-pr148-typed-contract-canary-20260723T235315Z --route text_only --suite-id typed-contract-canary --prompt-id sem-reg-011 --allow-release-ineligible-materialization-for-tests
```

Run directory:

`/tmp/codex-dr-pr148-typed-contract-canary-20260723T235315Z/dr_20260723T235315`

Directly observed failure:

- Final status: `blocked_semantic_planner_unavailable`
- Planner mode: `blocked`
- `semantic_release_eligible=false`
- `semantic_planner_raw/planner_response.json` recorded `adapter_response_received=false`.
- Diagnostics said: `Codex semantic planner adapter is not configured; refusing to materialize local heuristic output as codex_semantic.`

Facts:

- The run created durable status and semantic artifacts.
- The run did not exercise a real Codex semantic planner adapter response.
- The run did not fail due to typed contract parser, validator, materialization diff, or evidence propagation exceptions.
- The blocked semantic plan contains empty typed contract fields because true semantic planning did not run.

Inferences:

- This canary proves the blocked-adapter path remains explicit and does not falsely claim semantic release eligibility.
- It does not prove the new typed contract behavior works with a live Codex semantic adapter.

Unknowns:

- Whether the current shell intentionally lacks the default semantic adapter enablement environment, or whether adapter discovery regressed.

Next diagnostic or fix direction:

- For release evidence, rerun with the configured Codex semantic planner adapter enabled.
- Keep this blocked run out of release numerator counts.

## 2026-07-24 - PR #148 / Typed Contract Test Import Failure

Prompt or issue:

- Issue #133 / PR #148 typed semantic decomposition contract hardening.

Command or suite:

```bash
python3 -m unittest tests.test_semantic_planner.SemanticPlannerTests.test_typed_contracts_materialize_entity_dimension_matrix tests.test_semantic_planner.SemanticPlannerTests.test_typed_coverage_matrix_catches_missing_entity_dimension tests.test_semantic_planner.SemanticPlannerTests.test_typed_partition_contract_catches_too_many_entities_per_task tests.test_semantic_planner.SemanticPlannerTests.test_sem_reg_011_like_provider_gap_fails_before_reviewer tests.test_search_handoff.SearchHandoffTests.test_prepare_propagates_runner_source_budget_to_evidence
```

Directly observed failure:

- Four new semantic planner tests errored before exercising product logic.
- Python raised `NameError: name 'SEMANTIC_PLANNER_SCHEMA_VERSION' is not defined` from `tests/test_semantic_planner.py`.
- The search handoff propagation test passed.

Facts:

- This is a test import/setup failure.
- It does not prove the typed semantic contract materialization or validation logic is incorrect.

Inferences:

- The new tests reference `SEMANTIC_PLANNER_SCHEMA_VERSION` but the test module import list did not include it.

Unknowns:

- Whether the typed contract logic passes after the import is corrected.

Next diagnostic or fix direction:

- Import `SEMANTIC_PLANNER_SCHEMA_VERSION` in `tests/test_semantic_planner.py` and rerun the same targeted tests.

## 2026-07-24 - PR #148 / Typed Entity Inference Over-Extraction

Prompt or issue:

- Issue #133 / PR #148 typed semantic decomposition contract hardening.

Command or suite:

```bash
python3 -m unittest tests.test_semantic_planner.SemanticPlannerTests.test_typed_contracts_materialize_entity_dimension_matrix tests.test_semantic_planner.SemanticPlannerTests.test_typed_coverage_matrix_catches_missing_entity_dimension tests.test_semantic_planner.SemanticPlannerTests.test_typed_partition_contract_catches_too_many_entities_per_task tests.test_semantic_planner.SemanticPlannerTests.test_sem_reg_011_like_provider_gap_fails_before_reviewer tests.test_search_handoff.SearchHandoffTests.test_prepare_propagates_runner_source_budget_to_evidence
```

Directly observed failure:

- `test_typed_contracts_materialize_entity_dimension_matrix` failed because `selected_entities` contained 20 entries instead of the expected 4.

Facts:

- The candidate had four provider entities in `domain_entities`.
- The typed materializer also scanned capitalized text from angle/task prose and over-extracted non-provider tokens as selected entities.

Inferences:

- Capitalized-name fallback is too broad when structured domain entities or known provider names already provide a usable selected entity set.

Unknowns:

- Whether stricter entity inference causes any existing real prompt to lose necessary non-provider comparison entities.

Next diagnostic or fix direction:

- Prefer typed/domain/known-provider entities and only use capitalized fallback when fewer than two comparison entities are available.

## 2026-07-24 - PR #148 / Typed Dimension Inference Over-Extraction

Prompt or issue:

- Issue #133 / PR #148 typed semantic decomposition contract hardening.

Command or suite:

```bash
python3 -m unittest tests.test_semantic_planner.SemanticPlannerTests.test_typed_contracts_materialize_entity_dimension_matrix tests.test_semantic_planner.SemanticPlannerTests.test_typed_coverage_matrix_catches_missing_entity_dimension tests.test_semantic_planner.SemanticPlannerTests.test_typed_partition_contract_catches_too_many_entities_per_task tests.test_semantic_planner.SemanticPlannerTests.test_sem_reg_011_like_provider_gap_fails_before_reviewer tests.test_search_handoff.SearchHandoffTests.test_prepare_propagates_runner_source_budget_to_evidence
```

Directly observed failure:

- `test_typed_contracts_materialize_entity_dimension_matrix` failed because `required_dimensions` contained 5 entries instead of the expected 2.

Facts:

- The candidate prompt/constraints had two concrete comparison axes: initiation endpoint and request parameters.
- The materializer also promoted supporting requirements such as source quality/official evidence into comparison dimensions.

Inferences:

- Requirement fallback should not add broad evidence-quality requirements as entity-by-dimension comparison axes when concrete comparison dimensions already exist.

Unknowns:

- Whether some prompts need source-quality as an explicit matrix dimension. That should be requested by planner/oracle explicitly rather than inferred as a default when concrete dimensions are present.

Next diagnostic or fix direction:

- Prefer concrete dimension terms from prompt/tasks. Only add requirement-derived dimensions when no concrete comparison dimensions were found.

## 2026-07-24 - PR #148 / Typed Contract Broad Related Test Regression

Prompt or issue:

- Issue #133 / PR #148 typed semantic decomposition contract hardening.

Command or suite:

```bash
python3 -m unittest tests.test_semantic_planner tests.test_search_handoff
```

Directly observed failure:

- The targeted typed-contract tests passed after import/entity/dimension fixes.
- The broader related suite failed with many tests returning `blocked_semantic_planner_unavailable`, followed by missing `search_tasks.json` or missing reviewer artifact errors.
- The command output was truncated and the temporary run directories were cleaned by test cleanup before inspection.

Facts:

- At least 32 failures and 18 errors were reported in the broader suite.
- Many errors are downstream artifact-missing symptoms after planner blocking, not independent root causes.
- No durable candidate validation artifact was available from that run because the test temp directories were removed.

Inferences:

- A common early validation/schema/provenance issue is likely causing fake adapter candidates to block before reviewer/materialization.
- The directly observed suite output is insufficient to identify the exact root cause.

Unknowns:

- Which new field or validation rule first converts previously accepted fixture candidates into blocked planner responses.

Next diagnostic or fix direction:

- Rerun one representative failing test with focused output, then inspect the result object or preserved artifact before making further fixes.

## 2026-07-24 - PR #148 / Typed Contract Empty-Field Regression

Prompt or issue:

- Issue #133 / PR #148 typed semantic decomposition contract hardening after adversarial review.

Command or suite:

```bash
python3 -m unittest tests.test_semantic_planner tests.test_search_handoff
```

Directly observed failure:

- The broader suite still failed with many `blocked_semantic_planner_unavailable` results.
- The output also showed materialization diff failures where `semantic_entity_refs`, `semantic_dimension_refs`, and `final_deliverable_binding` were missing from materialized artifacts even though the expected bounded task values were empty.

Facts:

- The test helper adapter now emits the six required plan-level typed contract fields.
- The typed materializer still treated existing empty `selected_entities` / `required_dimensions` as absent and inferred comparison contracts from prose.
- The materialization diff treated missing empty typed task-binding fields as field mismatches.

Inferences:

- Empty typed contract fields from an adapter must be treated as authoritative empty contracts for non-comparison or untyped fixture plans.
- Materialization diff should require typed task-binding fields only when the semantic bounded task has non-empty bindings; otherwise old artifacts without empty-list placeholders should remain compatible.

Unknowns:

- Whether any remaining blocked planner failures come from adapter fixtures that still omit the six typed fields entirely.

Next diagnostic or fix direction:

- Do not infer selected entities or dimensions when the adapter already emitted those fields, even if they are empty.
- In materialization diff, allow absent task-level typed binding fields when the expected value is empty.

## 2026-07-24 - PR #148 / Source Budget Contract Over-Blocking

Prompt or issue:

- Issue #133 / PR #148 typed semantic decomposition contract hardening after adversarial review.

Command or suite:

```bash
python3 -m unittest tests.test_semantic_planner tests.test_search_handoff
```

Directly observed failure:

- The related suite failed with 37 failures and 18 errors.
- Multiple failures returned `blocked_semantic_planner_unavailable` before reviewer artifacts or `search_tasks.json` were produced.
- Several direct validation failures included only `typed_source_budget_contract_missing`.

Facts:

- The new validation rejected plans whenever `runner_source_budget` existed but `source_budget_contract` was absent or empty.
- Existing source-budget repair paths materialized `runner_source_budget` but did not also materialize the new typed `source_budget_contract`.
- Focused source-budget tests failed until the typed source-budget contract was generated from the repaired runner budget.

Inferences:

- The validator was enforcing the right contract too early for legacy/internal repair objects, while the materializer had not yet connected the existing runner-budget data into the new typed contract field.

Unknowns:

- None for the directly observed source-budget over-blocking path after the focused repro and fix.

Resolution:

- Materialize `source_budget_contract` from `runner_source_budget` in budget-cap and source-cap repair paths.
- Keep empty `source_budget_contract` invalid when the field is explicitly present on a typed contract plan.
- Re-ran the focused typed-contract/source-budget tests and `python3 -m unittest tests.test_semantic_planner tests.test_search_handoff`; both passed.

## 2026-07-24 - PR #148 / Release Gates Still Missing Real Run Bundles

Prompt or issue:

- Issue #133 / PR #148 validation after typed semantic contract hardening.

Command or suite:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch public-beta-validation --runs-dir /tmp/codex-deepresearch-public-beta-validation-pr148 --suite-id public-beta-validation --clean --allow-blocked
plugins/codex-deepresearch/scripts/codex-deepresearch semantic-release-validation --runs-dir /tmp/codex-deepresearch-semantic-release-validation-pr148 --suite-id semantic-release-validation --clean --allow-blocked
```

Directly observed failure:

- Public Beta validation status was `blocked`, with `release_gate_ready=false`.
- Semantic release validation status was `failed`, with `valid=false` and `release_gate_ready=false`.

Facts:

- Public Beta validation counted 20 blocked runs: 10 `artifact_handoff_failure` and 10 `provider_failure`.
- Public Beta validation reported `passed=0`, `failed=0`, `blocked=20`.
- Semantic release validation reported missing run entries for all 30 semantic regression prompts, all 20 Public Beta semantic prompts, and all 12 blind holdout prompts.
- Semantic release validation also reported `manual_trace_audit_manifest_missing`.
- These failures came from missing release-counted run bundles, not from a Python exception in the typed contract changes.

Inferences:

- The typed contract implementation can be merged as a hardening slice only if the PR explicitly does not claim #133 release signoff.
- Full #133 closure still requires generating and supplying the release-counted run directories plus manual trace audit manifest.

Unknowns:

- Whether all 62 release-counted semantic runs will pass once generated on the latest code.

Resolution:

- No validator threshold was lowered.
- The failures were preserved as release-gate failures and left for the remaining #133 signoff work.
