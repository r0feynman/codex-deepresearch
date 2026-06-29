# Codex DeepResearch PRD

## 제품 목표

Claude Code `/deep-research`의 텍스트 중심 조사 파이프라인을 clean-room 방식으로 재구현하되, Codex에서는 VLM 에이전트를 추가해 웹페이지, 논문, 스크린샷, 상품 이미지, 차트, UI, 사진 증거까지 함께 조사한다.

## 핵심 차별점

Claude식 구조:

```text
질문 -> 검색 각도 분해 -> 웹검색 -> 소스 fetch -> claim 추출 -> 3-vote 검증 -> 보고서
```

Codex DeepResearch 확장 구조:

```text
질문
-> 텍스트/이미지 조사 계획 분해
-> 웹검색 + 이미지검색 + 페이지 스크린샷
-> 텍스트 claim 추출 + 시각 claim 추출
-> OCR/차트/객체/레이아웃/이미지 출처 검증
-> 텍스트 verifier + VLM verifier 교차투표
-> 증거 JSON + 인용 포함 보고서 + 이미지 appendix
```

목표 경험은 Claude Code식 deep-research의 high fan-out 조사를 Codex Plugin 방식으로 재현하는 것이다.

```text
질문
-> Planner가 20-100개 bounded research task로 분해
-> 다수 Codex subagent가 병렬로 검색, 페이지 읽기, 이미지 확인, visual observation을 수행
-> 각 subagent가 evidence shard를 생성
-> runner가 shard merge, dedupe, guardrail, verifier matrix를 적용
-> SynthesisAgent가 supported evidence만 사용해 최종 보고서를 작성
```

MVP는 이 목표 경험의 vertical slice이고, Private Alpha/Public Beta에서 실제 병렬 subagent orchestration을 제품 기능으로 완성한다.

## 사용자

- 개발자: CLI/Skill로 기술 조사, 라이브러리 비교, 장애 원인 분석, 코드/문서/스크린샷 기반 조사.
- 비개발자: 웹 UI 또는 간단한 명령으로 시장조사, 제품 비교, 이미지 품질 조사, 경쟁 서비스 분석.
- 에이전트: 저장된 evidence bundle을 재사용해 후속 작업, PRD, 구현 계획, QA 체크리스트로 승격.

## 입력 경로

최종 제품 방향:

- Codex DeepResearch의 최종 배포 단위는 Codex Plugin이다.
- Codex 자체의 built-in slash command를 수정하는 제품이 아니라, 설치형 Codex Plugin + Skill + local runner로 "내장 명령처럼" 호출되는 사용자 경험을 제공한다.
- CLI는 독립 제품이 아니라 plugin 내부 runner이자 개발/테스트/자동화용 보조 진입점이다.
- 1차 목표 호출 방식은 플러그인 설치 후 Codex에서 `$deep-research` Skill invocation을 사용하는 것이다.
- 2차 목표 호출 방식은 Codex의 `/skills` 선택기에서 `deep-research`를 고르는 방식이다.
- 3차 목표 호출 방식은 개발/디버깅용 `codex-deepresearch` CLI이다.
- `/deep-research`와 완전히 같은 slash command 이름을 Codex core에 추가하는 것은 비목표다. 단, 플러그인/스킬 설명과 CLI alias를 통해 사용자는 사실상 내장 워크플로우처럼 사용할 수 있어야 한다.

Primary Codex Plugin flow:

```text
Codex에 codex-deepresearch plugin 설치
-> $deep-research: <질문>
-> Codex 세션의 search/context/VLM capability와 plugin runner를 조합
-> report.md + evidence.json + run_status.json + synthesized-run report_status.json 생성
```

Codex Skill:

```text
$deep-research: <질문>
```

Local runner / developer CLI:

```bash
codex-deepresearch "AI 사진 서비스 품질관리 조사" --visual --max-sources 30
```

Plugin packaging:

```text
codex-deepresearch-plugin/
  .codex-plugin/plugin.json
  skills/deep-research/SKILL.md
  scripts/codex-deepresearch
  src/deepresearch/
```

Fixed plugin paths for this repository:

```text
Repository root:
  /home/user/Projects/codex-deepresearch/

Plugin root:
  plugins/codex-deepresearch/

Required plugin layout:
  plugins/codex-deepresearch/.codex-plugin/plugin.json
  plugins/codex-deepresearch/skills/deep-research/SKILL.md
  plugins/codex-deepresearch/scripts/codex-deepresearch
  plugins/codex-deepresearch/src/deepresearch/
  plugins/codex-deepresearch/tests/smoke/

Repo-local marketplace metadata:
  .agents/plugins/marketplace.json
```

Install/update smoke command target:

```bash
plugins/codex-deepresearch/scripts/codex-deepresearch smoke --install --invoke '$deep-research: Codex DeepResearch smoke test'
```

Execution modes:

| Mode | Primary user | Search path | VLM path | Purpose |
| --- | --- | --- | --- | --- |
| `codex-plugin` | Codex 사용자 | Codex-native search | Codex interactive VLM 또는 API adapter | 최종 제품 기본 모드 |
| `automated-cli` | 개발자/자동화 | OpenAI hosted search 또는 외부 provider | OpenAI Responses Vision API | 재현 가능한 batch run |
| `manual-sources` | 저비용/검증용 | 사용자가 URL/PDF/image 제공 | API, Codex interactive, 또는 manual | Phase 0와 fallback |

MVP의 제품 판단은 `codex-plugin` mode를 기준으로 한다. `automated-cli`는 같은 engine을 검증하고 자동화하는 보조 표면이다.

## Terminology and Product Contract

Codex DeepResearch는 다음 실행 단위를 구분한다.

- `Codex subagent`: Codex 세션 안에서 독립 작업 컨텍스트로 생성되어 검색, 파일 확인, 이미지 확인, 판단을 수행하는 Codex-side 조사 작업 단위다. 하나의 DeepResearch run은 여러 Codex subagent를 병렬로 실행할 수 있다.
- `runner agent`: plugin 내부 runner가 실행하는 파이프라인 단계다. 예: planner, router, fetcher, evidence writer, report writer.
- `model call`: OpenAI Responses API 또는 기타 model API에 대한 단일 호출이다.
- `verifier invocation`: 하나의 claim에 대해 하나의 verifier가 support/refute/uncertain vote를 산출하는 검증 작업이다. verifier invocation은 Codex subagent, runner agent, model call 중 하나 이상을 사용할 수 있지만 동일 개념이 아니다.
- `research task`: planner가 만든 병렬 조사 작업 단위다. 각 task는 angle, route, query, source/image cap, budget, policy constraints, expected artifact를 가진다.
- `evidence shard`: 하나의 Codex subagent 또는 runner stage가 만든 부분 evidence bundle이다. shard는 최종 `evidence.json`에 병합되기 전까지 독립적으로 validate되어야 한다.

제품 계약:

- 최종 제품은 Codex Plugin이다.
- `$deep-research` Skill invocation이 primary UX다.
- `/skills` 선택기를 통한 `deep-research` 선택은 secondary UX다.
- `codex-deepresearch` CLI는 plugin runner를 실행하는 개발/디버깅/자동화용 wrapper이며 독립 제품이 아니다.
- Codex core의 built-in slash command를 수정하지 않는다.
- MVP acceptance는 plugin install, manifest validation, marketplace metadata, `$deep-research` invocation smoke test를 기준으로 판정한다.
- canonical plugin root는 `plugins/codex-deepresearch/`다.
- canonical marketplace file은 `.agents/plugins/marketplace.json`이고, entry path는 `./plugins/codex-deepresearch`다.
- canonical local smoke command는 `python3 scripts/validate_repo.py`와 plugin validator 실행 후 `$deep-research: smoke test`를 수행하는 것이다.

## Codex Plugin Search/VLM Handoff Contract

`codex-plugin` mode는 Codex-native search와 Codex interactive visual analysis를 공개 library API처럼 직접 호출한다고 가정하지 않는다. 대신 Skill과 runner 사이에 명시적 handoff artifact를 둔다.

Search handoff:

- Precondition: 사용자가 Codex 세션에서 `$deep-research: <question>`을 호출한다.
- Runner responsibility: runner는 `search_tasks.json`을 생성한다. 각 task는 angle, query, freshness requirement, modality, max_results, source policy를 포함한다.
- Codex-side responsibility: Codex agent는 현재 세션의 search capability를 사용해 search task를 수행하고, 결과를 `search_results.jsonl`에 `SearchResult` schema로 기록한다.
- Runner ingestion: runner는 `search_results.jsonl`을 validate한다. invalid result는 `retrieval_status=failed` 또는 `policy_blocked`로 evidence에 남긴다.
- Fallback: `codex-plugin` mode에서 `codex-native` search가 불가능하면 `manual-sources` fallback 또는 `blocked_missing_search_handoff` 상태로 종료한다.

VLM handoff:

- Runner responsibility: runner는 visual-required 또는 visual-optional route에서 분석 대상 이미지를 `visual_tasks.json`과 artifact directory에 기록한다.
- Codex-side responsibility: Codex agent는 세션의 interactive image-reading capability로 이미지를 확인하고, 관찰 결과를 `visual_observations.jsonl`에 `VisualEvidence` schema로 기록한다.
- Runner ingestion: runner는 `visual_observations.jsonl`을 validate한다. visual-required claim에 valid visual evidence가 없으면 `verification_status=needs_visual_evidence`로 남기고 high-confidence claim으로 쓰지 않는다.
- Automated alternative: `automated-cli` mode는 `openai-responses-vision` adapter를 사용해 같은 `VisualEvidence` schema를 생성한다.
- Manual fallback: `manual-visual-review`는 사람이 같은 schema를 작성하는 fallback이다.

Handoff command protocol:

```text
codex-deepresearch prepare "<question>"
-> creates run directory, search_tasks.json, visual_tasks.json placeholder

Codex fills handoff artifacts
-> search_results.jsonl from Codex-native search
-> visual_observations.jsonl from Codex interactive VLM when needed

codex-deepresearch ingest --run <run_id>
-> validates SearchResult and VisualEvidence records
-> fetches allowed sources
-> normalizes evidence.json

codex-deepresearch verify --run <run_id>
-> extracts claims
-> applies route-specific verifier matrix
-> writes verifier_votes.jsonl and updates claim state

codex-deepresearch synthesize --run <run_id>
-> writes report.md and image appendix from supported claims only
```

The Skill must guide the user and Codex-side agent through this protocol. The runner must never assume it can call Codex-native search or `codex-interactive` VLM as a hidden API.

### Skill Invocation Full-Runner UX Contract

Phase 3 must close the gap between the user's installed Codex Skill experience and the automated runner experience. A fresh-session `$deep-research: <question>` invocation must not silently turn into a normal conversational answer when the user expects DeepResearch. The product default is a full-runner run that creates durable artifacts, exposes shard status, and returns the final report location.

Problem statement:

- Current skill invocation can be interpreted by Codex as guidance for the active assistant. The assistant may search the web and answer directly without completing `orchestrate-parallel`, verifier, synthesis, or report artifact stages.
- This creates a user-visible mismatch: developer-runner tests show `accepted_shards`, `parallel_orchestration_status.json`, `evidence.json`, and `report.md`, while a normal `$deep-research` session may only show a chat answer.
- A chat-only answer is allowed only when the user explicitly asks for quick mode or when the full-runner path is blocked and the blocked state is reported.

Default `$deep-research` flow:

```text
$deep-research: <question>
-> skill invocation router selects `full-runner` by default
-> runner prepare creates run directory, research_tasks.json, search_tasks.json, and budget_estimate.json
-> parallel orchestrator runs ready tasks through Codex subagents or equivalent worker contexts
-> status summary reports active, queued, failed, accepted, merged, and retried shard counts
-> guardrails, verifier matrix, and synthesis run on merged evidence only
-> final response gives report.md, evidence.json, run_status.json, report_status.json when synthesized, and visual/parallel status artifacts when applicable
```

`codex-plugin` full-runner state machine:

| State | Entry action | Required artifacts or diagnostics | Allowed next states |
| --- | --- | --- | --- |
| `preflight_diagnostics` | Validate plugin install, runner executable, trusted project root, run directory write access, search handoff availability, visual provider needs, auth/sandbox/approval constraints, and cost caps. | If a run directory can be created, write `run_status.json` with the exact terminal status, `ok=false`, `terminal=true`, and `diagnostics.actionable_cause` on failure. Visual-required runs with no configured real visual acquisition provider use `blocked_missing_visual_provider`, not generic preflight failure. If no run directory can be created, final response must show the same diagnostics and no success claim. | `prepared`, `blocked_preflight`, `blocked_missing_visual_provider` |
| `prepared` | Create run directory and canonical task placeholders. | `run_status.json`, `research_tasks.json`, `search_tasks.json`, `visual_tasks.json` when needed, `budget_estimate.json`. | `awaiting_search_handoff`, `parallel_orchestrating`, `serial_fallback_pending`, `blocked_missing_search_handoff`, `blocked_missing_visual_provider` |
| `awaiting_search_handoff` | Pause runner-owned ingestion while the Codex-side agent fills search results. | `search_tasks.json` plus `search_results.jsonl` records using `SearchResult`. Missing or invalid records remain explicit diagnostics. | `awaiting_visual_handoff`, `ingesting_handoff`, `blocked_missing_search_handoff`, `serial_fallback_pending` |
| `awaiting_visual_handoff` | For visual routes, expose local image/screenshot/PDF artifacts to the Codex-side agent or configured VLM adapter. | `visual_tasks.json`, selected image artifacts, `visual_observations.jsonl` records using `VisualEvidence`. If no real acquisition provider can produce required artifacts, write `blocked_missing_visual_provider`. | `ingesting_handoff`, `blocked_missing_visual_provider`, `blocked_missing_vlm_provider`, `policy_blocked_visual`, `budget_pruned_visual` |
| `ingesting_handoff` | Validate handoff artifacts, fetch allowed sources/images, normalize evidence. | Validated `SearchResult`/`VisualEvidence`, `evidence.json`, policy/fetch diagnostics. | `parallel_orchestrating`, `verifying`, `serial_fallback_pending`, `failed_validation` |
| `parallel_orchestrating` | Run ready `ResearchTask` items through Codex subagents or equivalent worker contexts. | `parallel_orchestration_status.json`, `subagent_assignments.jsonl`, `run_trace.jsonl`, evidence shards. | `verifying`, `serial_fallback_pending`, `failed_parallel_no_accepted_shards`, `blocked_parallel_execution` |
| `serial_fallback_pending` | Record why intended parallel or handoff execution cannot continue and whether fallback is allowed. | `run_status.json` with blocker, `needs_serial_handoff=true`, and fallback decision. `parallel_degraded` stays `false` until fallback actually runs. | `serial_fallback_running`, terminal blocked/failed status |
| `serial_fallback_running` | Execute the same task queue serially only when user/runner policy permits fallback. | `run_status.json` with `parallel_degraded=true`, serial task diagnostics, merged evidence artifacts. | `verifying`, `completed_serial_handoff`, `failed_validation` |
| `verifying` | Run guardrails and verifier matrix against merged evidence only. | `verifier_votes.jsonl`, updated `evidence.json`, guardrail diagnostics. | `synthesizing`, `policy_blocked_visual`, `budget_pruned_visual`, `failed_validation` |
| `synthesizing` | Generate final report from supported evidence only. | `report.md`, `report_status.json`, final `run_status.json`. | recognized terminal status, `failed_synthesis` |

Recognized terminal statuses for full-runner UX are: `completed_parallel`, `completed_partial_parallel`, `completed_serial_handoff`, `completed_auto_visual`, `partial_auto_visual`, `completed_fixture`, `blocked_preflight`, `blocked_missing_search_handoff`, `blocked_parallel_execution`, `blocked_missing_visual_provider`, `blocked_missing_vlm_provider`, `policy_blocked_visual`, `budget_pruned_visual`, `failed_parallel_no_accepted_shards`, `failed_validation`, and `failed_synthesis`. "Recognized" means the UX must report the state honestly; it does not mean the run passed a release gate. `serial_fallback_pending` is a recognized non-terminal handoff state and must not be reported as a successful terminal completion. `completed_fixture` is accepted only for development validation and never for real-use or release readiness gates.

The state machine reconciles the Skill UX with the Search/VLM handoff contract: `codex-plugin` mode may ask the active Codex agent to fill handoff artifacts, but the runner must not call Codex-native search, Codex interactive VLM, subagent spawning, or hidden Codex APIs as if they were stable library functions. Every Codex-side action must be represented by an artifact, a status transition, and diagnostics when blocked.

Allowed invocation modes:

| Invocation mode | Trigger | Required behavior | Artifact requirement |
| --- | --- | --- | --- |
| `full-runner` | default `$deep-research: <question>` | Execute the plugin runner through synthesis or an explicit blocked state. | Must create a run directory plus `run_status.json`; synthesized runs must also create `report.md`, `evidence.json`, and `report_status.json`; visual/parallel runs include their status artifacts. |
| `quick-chat` | explicit user request such as "quick answer" or "do not run full pipeline" | The assistant may answer conversationally using available tools. | Must state that no DeepResearch evidence bundle was produced. |
| `manual-handoff` | user provides URLs, PDFs, image URLs, or local files and asks to use them | Use manual source ingestion and then continue runner stages when possible. | Must create or update a run directory and show artifact paths. |
| `blocked` | missing search, Codex execution, VLM, auth, sandbox, or policy capability | Stop with an actionable blocked status instead of pretending full research completed. | Must write status diagnostics when the runner was started. |

User-visible status requirements:

- During execution, the user can see at least: run id/path, current stage, active task count, queued task count, failed task count, accepted shard count, merged shard count, retry count, and whether fallback/degradation occurred.
- The final assistant response must include exact status artifacts, not an unspecified equivalent. Successful synthesized runs must include `report.md`, `evidence.json`, `run_status.json`, and `report_status.json`; parallel runs also include `parallel_orchestration_status.json`; visual-attempted runs also include `visual_provider_status.json`; `run_trace.jsonl` is included when present.
- Blocked pre-synthesis runs must include `run_status.json` with `ok=false`, `terminal=true`, final `status`, and actionable diagnostics. They do not need `report_status.json` unless synthesis actually ran.
- If the run uses fixture, manual, serial fallback, or quick-chat mode, the final response must label that provenance clearly and must not imply a real parallel DeepResearch run occurred.
- If `--no-degrade` or the product equivalent is active, a blocked or failed parallel run must not synthesize a report in the same command unless the user explicitly starts a fallback run.
- A normal `$deep-research` completion cannot be considered successful unless the final report is generated from supported evidence after guardrails and verifier stages.

Fresh-session acceptance:

- In a new Codex session with the plugin installed, `$deep-research: <text-heavy question>` reaches `completed_parallel`, `completed_partial_parallel` with enough accepted evidence for synthesis, or an explicit blocked status; it must not end as chat-only without a run directory.
- The same fresh-session E2E records `accepted_shards > 0` for real `codex-exec` or equivalent subagent execution when the Codex runtime is available.
- The final assistant message exposes generated `report.md`, `evidence.json`, `run_status.json`, synthesized-run `report_status.json`, applicable visual/parallel status artifact paths, and a compact shard/status summary.
- A regression test or scripted smoke captures the transcript and fails if the final response lacks a run artifact path or if the required final status artifact set is missing. For any successful synthesized run, missing `report_status.json` is a failure.

### Automatic Web Visual Research Contract

Phase 3 Public Beta must support complete automatic web image research for visual-required tasks. "Complete automatic web image research" means a user can ask a question without providing image files, and DeepResearch can discover web images, screenshots, charts, or paper figures, analyze them through an allowed VLM path, connect the observations to claims, and use the image-backed claims in the final report.

Automatic web visual research flow:

```text
visual-required or visual-optional research task
-> visual_search_plan.json records query, target evidence type, provider, caps, and policy constraints
-> web/image provider returns candidate image URLs and source pages
-> page image extractor collects Open Graph images, body images, captions, alt text, surrounding text, srcset/lazy-loaded candidates
-> screenshot collector captures allowed first-viewport, full-page, scroll, or interaction screenshots when configured
-> PDF/academic collector rasterizes allowed PDF pages or figures when configured
-> image fetcher stores allowed images/screenshots as local artifacts with MIME, size, hash, page context, license/robots/policy metadata
-> image ranker deduplicates and selects VLM candidates under max image/model-call/cost caps
-> VisionExtractAgent writes visual_observations.jsonl through codex-interactive, openai-responses-vision, or manual-visual-review
-> VisualVerifierAgent links observations to claim visual_supports and verifier votes
-> synthesis cites image evidence IDs and includes an image appendix
```

Post-#97 integration requirement:

- P3-AV10 / #97 connects already-fetched local image, screenshot, or PDF-render artifacts to a Codex-native VLM worker through explicit `codex exec --json --image <artifact>` handoff. This is necessary but not sufficient for complete automatic web visual research.
- Complete automatic web visual research requires the default full-runner path to compose automatic discovery, fetch/capture/rasterization, Codex VLM analysis, verifier linkage, and report citation in one run without the user manually supplying image files.
- The follow-up integration issue P3-AV11 / #99 must bind `prepare -> orchestrate-parallel/search handoff -> visual acquisition -> codex-interactive VLM worker -> ingest-vision -> verify-claims -> synthesize` so a fresh `$deep-research` visual-required prompt can reach `completed_auto_visual` from web evidence alone.
- P3-AV11 must not add a hidden Codex VLM API assumption. Every Codex-native search or VLM action must be represented by handoff artifacts, status files, and explicit blocked diagnostics when the Codex surface cannot perform the action.

Post-#104 timeout and Apollo real-use E2E findings:

- PR #104 merged timeout hardening for Codex child execution. The runner must preserve raw child diagnostics and expose configurable `--codex-exec-timeout-seconds`; this reduces the previous blind spot where an `evidence_shard` timeout could hide the child's last actionable message.
- Follow-up real-use visual E2E with Apollo public images and `--codex-exec-timeout-seconds 900` produced three observed outcomes: one `completed_auto_visual`, one `partial_auto_visual` because only 2 Codex-interactive analyzed images were available versus the required 3, and one `failed_parallel_no_accepted_shards` where the child last message was `Selected model is at capacity. Please try a different model.` with `timeout=false`.
- These findings do not change product direction: the default path remains Codex Plugin plus Codex-native handoff artifacts; no hidden Codex API or mandatory external visual/search provider is introduced.

Required Phase 3 automatic visual artifacts:

- `visual_search_plan.json`: planned image search/page extraction/screenshot/PDF figure work per visual task.
- `visual_candidates.jsonl`: every discovered candidate with provider, origin, page URL, image URL, ranking score, rejection reason if pruned, and policy state.
- `image_fetch_status.jsonl`: fetch/download/screenshot/rasterization result, byte size, MIME, content hash, perceptual hash, local artifact path, and failure code.
- `visual_observations.jsonl`: OCR, chart/table reading, object/layout description, image-claim alignment, caveats, and provider metadata.
- `visual_provider_status.json`: configured visual providers, availability, real-vs-fixture provenance, VLM invocation counts, skipped work, and cost.

Minimal Phase 3 visual artifact schemas:

- `visual_search_plan.json` contains `schema_version`, `run_id`, `created_at`, and `tasks[]`. Each task has `plan_id`, `task_id`, `angle_id`, `route`, `target_evidence_type=web_image|page_image|screenshot|pdf_figure|chart_image`, `query`, `providers[]`, optional `source_search_result_ids[]`, `caps` (`max_candidates`, `max_fetches`, `max_vlm_images`, `max_cost_usd`), `policy_constraints`, `estimated_cost_usd`, and `state=planned|running|completed|blocked|skipped`.
- `visual_candidates.jsonl` has one record per candidate: `candidate_id`, `plan_id`, `task_id`, `angle_id`, optional `source_search_result_id`, `provider`, `provider_kind=web_image_search|page_extractor|screenshot|pdf_rasterizer|manual|fixture`, `provider_mode=real|fixture|manual|user_provided`, `provider_run_id`, `origin=image_search|page_image|open_graph|srcset|lazy_loaded|screenshot|pdf_figure|pdf_page`, `page_url`, `image_url`, `rank`, `score`, `policy_decision=allowed|blocked|manual_review|budget_pruned`, `policy_flags[]`, `candidate_status=discovered|ranked|selected|rejected|policy_blocked|budget_pruned|fetch_failed|fetched|analyzed`, `rejection_reason`, `estimated_cost_usd`, and `actual_cost_usd`.
- `image_fetch_status.jsonl` has one record per fetch/capture/rasterization attempt: `fetch_id`, `candidate_id`, `task_id`, `angle_id`, optional `source_search_result_id`, `fetch_status=fetched|failed|skipped|policy_blocked|budget_pruned|unsupported_mime|too_large|deduped`, `http_status`, `mime_type`, `byte_size`, `width`, `height`, `hash`, `phash`, `local_artifact_path`, `evidence_image_id`, `policy_decision`, `policy_flags[]`, `failure_code`, `estimated_cost_usd`, and `actual_cost_usd`.
- `visual_observations.jsonl` has one record per VLM/manual observation: `observation_id`, `evidence_image_id`, `task_id`, `angle_id`, `candidate_id`, `fetch_id`, `provider`, `model_or_tool`, `provider_mode=real|fixture|manual|user_provided`, `provider_provenance`, `observation_status=analyzed|failed|skipped|needs_manual_review|policy_blocked`, `observations[]`, `inferences[]`, optional `ocr_text`, `confidence`, `policy_decision=allowed|blocked|manual_review|budget_pruned`, `policy_flags[]`, `caveats[]`, `verifier_links[]` (`claim_id`, optional `visual_support_ref`, optional `verifier_vote_id`), `report_links[]` (`claim_id`, optional `report_section_id`, optional `citation_id`), `estimated_cost_usd`, `actual_cost_usd`, and `created_at`.
- `visual_provider_status.json` contains `schema_version`, `run_id`, `status`, `ok`, `terminal`, `metric_classification`, `minimums`, and `providers[]`. `minimums` has integer counts `required_vlm_images`, `candidate_count`, `selected_candidates`, `fetched_artifacts`, `vlm_images_analyzed`, `report_cited_images`; boolean `satisfied`; and `shortfall_reason=none|insufficient_candidates|fetch_failures|vlm_failures|policy_blocked|budget_pruned|report_linkage_missing`. Counts in `minimums` count only non-fixture, non-manual, non-user-provided records eligible for the selected release gate. Each provider record has `provider`, `provider_kind`, `provider_mode=real|fixture|manual|user_provided`, `configured`, `available`, `blocked_reason`, `invocations`, `candidates_discovered`, `artifacts_fetched`, `vlm_images_analyzed`, `estimated_cost_usd`, `actual_cost_usd`, and `last_error`.

Visual artifact validation rules:

- `task_id` must reference `research_tasks.json[].id`; `angle_id` must match that task's angle and route.
- `source_search_result_id`, when present, must reference `SearchResult.id`; page extraction, screenshot, and PDF rasterization candidates must preserve the source search result or explicit manual source that produced them.
- `candidate_id` is globally unique within a run. Every `image_fetch_status.jsonl.candidate_id` must reference a candidate, and every fetched/analyzed candidate must have exactly one fetch record or a documented dedupe target.
- `provider_mode=fixture|manual|user_provided` records are valid for mechanics but are excluded from real automatic visual release numerator counts.
- `policy_decision=blocked|manual_review|budget_pruned` candidates cannot create supported claims unless a later human review changes the policy state and records that review.
- If `fetch_status=fetched` and the artifact is selected for analysis, `evidence_image_id` must equal an `evidence.json.images[].id`; that image must preserve the same `task_id`, `angle_id`, `candidate_id`, `fetch_id`, `local_artifact_path`, `hash`, provider provenance, policy decision, and cost metadata.
- `visual_observations.jsonl` records must reference the same `evidence_image_id`, `candidate_id`, and `fetch_id` lineage as the fetched artifact. `observations[]` are directly seen/OCR/chart-read facts; `inferences[]` are model interpretation and must not be used as direct evidence without caveats. Supported visual/mixed claims must reference the image through `supporting_images[]`, `visual_supports[]`, verifier links, and report links.
- `run_status.json` and `visual_provider_status.json` must expose the terminal automatic visual state. `report_status.json.used_images` counts only cited image IDs from supported claims, and report citations must connect report section -> claim ID -> image ID -> candidate/fetch provenance.

Provider requirements:

- `codex-plugin` mode uses Codex-native search and `codex-interactive` VLM through explicit handoff artifacts. Public Beta automatic visual completion is judged on those Codex-native run artifacts by default.
- `automated-cli` mode should support at least one real web/image search provider and the `openai-responses-vision` VLM adapter for reproducible diagnostics, but those provider/API gates are not mandatory for Codex-native Public Beta completion.
- Fixture, local test, manual, or user-provided image evidence may validate mechanics, but cannot satisfy the Public Beta automatic web visual E2E gate.
- Text-only routes must still perform zero image search, screenshot capture, image fetch, or VLM analysis.

Sanitized-real-artifact acceptance:

- A `sanitized-real-artifact` validation run replays public web visual artifacts that were previously acquired by a real visual acquisition path, with private content removed. It may skip live web fetch for repeatability, but it must not replace the real acquisition lineage with fixtures.
- Each replayed artifact must preserve original source URL or page URL, retrieval time, provider name, `provider_kind`, `provider_mode=real`, `origin`, `candidate_id`, `fetch_id`, MIME type, byte size, hash, local artifact path, and robots/license/policy metadata.
- The validation run must rerun Codex-interactive VLM handoff against those local artifacts and write fresh non-fixture `visual_observations.jsonl` records. Prewritten fixture observations cannot satisfy this gate.
- `sanitized-real-artifact` can satisfy P3-AV12 positive acceptance only when candidate, fetch, observation, claim, verifier, and report citation lineage all remain non-fixture, non-manual, and non-user-provided.

Public Beta provider and scenario gates:

- Public Beta cannot claim complete automatic web visual research from fixture/manual/user-provided-only evidence. It must pass Codex-native real-use gates for search handoff, visual candidate/fetch artifacts, `codex-interactive` observations, visual verifier linkage, and report citation.
- The `codex-plugin` interactive visual E2E gate proves product UX: a fresh `$deep-research` visual-required prompt uses Codex-native search/VLM handoff artifacts and exposes `report.md`, `evidence.json`, `run_status.json`, `report_status.json`, `visual_provider_status.json`, and shard/status summary without hidden Codex APIs. A diagnostic run may end in an explicit blocked terminal status, but the release gate passes only with `completed_auto_visual`.
- The `automated-cli` real provider E2E gate proves reproducible automation: a no-user-image visual-required run uses at least one real web/image provider plus `openai-responses-vision`, records non-fixture provider provenance and cost, and reaches `completed_auto_visual`. This gate is a separate diagnostic and is required only as an external gate result artifact when the release is intentionally evaluated in external-gated mode; it is not a prompt-manifest metric.
- Scenario gates must include at least: product/image-centric web image discovery, UI/webpage screenshot comparison, public chart or market/report visual extraction, and public PDF/paper figure extraction. Each scenario needs at least one supported visual or mixed claim cited in `report.md`, except when it ends in an explicit blocked policy/capability state.
- A provider blocked by auth, policy, terms, rate limit, or missing configuration may produce a correct blocked run, but that blocked run does not satisfy the Public Beta release gate for the provider/scenario it was meant to cover.

Automatic visual completion states:

- `completed_auto_visual`: at least one non-fixture Codex-native visual candidate/fetch path ran, the configured visual minimum was satisfied, at least one supported visual/mixed claim was cited in `report.md`, and `visual_provider_status.json.minimums.satisfied=true`. For visual-required Public Beta gates, the configured minimum is at least 3 non-fixture `codex-interactive` analyzed images. Visual-optional runs may use a lower explicit scenario minimum only when the scenario is excluded from the visual-required release gate before launch.
- `partial_auto_visual`: providers ran and candidates were found, but the visual-required completion gate was not met because no image-backed claim reached supported status, required visual minimums were missed, or usable visual evidence was pruned by policy, quality, capacity, or cost limits.
- `blocked_missing_visual_provider`: the selected mode requires automatic visual research but no real image/search/screenshot provider is configured.
- `blocked_missing_vlm_provider`: visual artifacts exist but no allowed VLM path is executable.
- `policy_blocked_visual`: usable visual artifacts were blocked by robots, copyright, PII, sensitive image, or high-risk policy.
- `budget_pruned_visual`: visual work was skipped or truncated because configured image/model-call/cost caps were reached.

Automatic visual terminal status precedence:

- Missing required capability wins first: no configured/available real visual acquisition path is `blocked_missing_visual_provider`; fetched visual artifacts with no allowed VLM path is `blocked_missing_vlm_provider`.
- Parallel child execution failure wins before visual-minimum classification: if Codex child capacity or another child failure leaves `accepted_shards=0`, the terminal status is `failed_parallel_no_accepted_shards`; `partial_auto_visual` applies only after some evidence/visual path was accepted but the configured visual minimum or report linkage gate was missed.
- Policy rejection wins over partial completion when every discovered candidate is blocked by robots, copyright, paywall, login, CAPTCHA, PII, sensitive-image, or high-risk policy; the status is `policy_blocked_visual`.
- `budget_pruned_visual` is reserved for an explicitly low-budget/excluded diagnostic preset, or for a cap that prevents any eligible visual work from starting before candidate/fetch/VLM attempts can run.
- In the default visual-required Public Beta gate, if providers ran and candidates or artifacts existed but the configured minimum is missed because of fetch failures, VLM failures, capacity, cost, or image/model-call caps, the terminal status is `partial_auto_visual` with `diagnostics.failure_code=visual_minimum_shortfall` and `shortfall_reason=budget_pruned` or the more specific dominant shortfall reason.

Visual minimum shortfall handling:

- Public Beta visual-required release gates require `min_vlm_analyzed_images=3` for non-fixture Codex-interactive visual analysis unless the scenario is explicitly marked excluded before launch.
- If fewer than 3 non-fixture images/screenshots/figures are analyzed, the run must set terminal status `partial_auto_visual`, `ok=false`, and `diagnostics.failure_code=visual_minimum_shortfall`.
- `visual_provider_status.json.minimums` must record `required_vlm_images`, `candidate_count`, `selected_candidates`, `fetched_artifacts`, `vlm_images_analyzed`, `report_cited_images`, `satisfied`, and `shortfall_reason`.
- `minimums.satisfied` is valid only when `vlm_images_analyzed >= required_vlm_images`, `fetched_artifacts >= required_vlm_images`, and `report_cited_images >= 1` for visual-required gates.
- If `minimums.satisfied=false`, diagnostics must include `failure_code=visual_minimum_shortfall` for candidate/fetch/VLM count shortfalls or `failure_code=visual_report_linkage_missing` when enough images were analyzed but no visual/mixed claim was cited.
- Invalid states: `status=completed_auto_visual` with `minimums.satisfied=false`; `status=completed_auto_visual` with `vlm_images_analyzed < required_vlm_images`; `status=completed_auto_visual` with `report_cited_images < 1`; `status=partial_auto_visual` without a non-`none` `shortfall_reason`.
- A run with `visual_minimum_shortfall` may produce a caveated report only when text-only partial delivery was explicitly allowed; it cannot pass automatic visual E2E or Public Beta release gates.

Zero-candidate and provider-unavailable handling:

- If no real visual acquisition provider is configured or available before visual work starts, the terminal status is `blocked_missing_visual_provider`, not `partial_auto_visual`.
- If a configured provider is invoked successfully but returns zero candidates, the terminal status is `partial_auto_visual` with `shortfall_reason=insufficient_candidates`.
- If candidates exist but all are rejected by robots, copyright, paywall, login, CAPTCHA, PII, sensitive-image, or high-risk policy, the terminal status is `policy_blocked_visual`.
- If candidates exist but all eligible work is pruned by an explicitly low-budget/excluded diagnostic preset or by a cap that prevents any visual attempt from starting, the terminal status is `budget_pruned_visual`. In the default visual-required gate, pruning after providers/candidates/artifacts exist is `partial_auto_visual` with `visual_minimum_shortfall`.
- If fetched visual artifacts exist but no allowed VLM path can execute, the terminal status is `blocked_missing_vlm_provider`.

Visual candidate top-up and replacement rules for P3-AV12:

- For visual-required image-centric runs, the runner starts with `required_vlm_images=3` and selects at least `min(max_images, max(10, required_vlm_images * 4))` eligible candidates when available.
- Candidate selection prioritizes allowed policy state, source quality, direct image URL availability, MIME support, size bounds, dedupe uniqueness, and source/page relevance.
- Fetch failures, unsupported MIME, oversized artifacts, dedupe collisions, and policy blocks must advance to the next eligible candidate until either `fetched_artifacts >= required_vlm_images` or all eligible candidates are exhausted.
- VLM analysis must continue across fetched artifacts until `vlm_images_analyzed >= required_vlm_images`, VLM capability becomes blocked, budget/cost cap is reached, or no fetched artifacts remain.
- If top-up exhausts candidates before the minimum is met, the run ends `partial_auto_visual` with `shortfall_reason=insufficient_candidates|fetch_failures|policy_blocked|budget_pruned|vlm_failures` matching the dominant failure category.

Automatic visual status envelope:

| Status | `ok` | `terminal` | Metric classification | Notes |
| --- | ---: | ---: | --- | --- |
| `completed_auto_visual` | `true` | `true` | success, included in numerator and denominator | Requires report-cited supported visual/mixed claim and non-fixture provenance. |
| `partial_auto_visual` | `false` for visual-required, `true` only for explicitly allowed text-only partial delivery | `true` | failure, included in denominator but not numerator | May produce a caveated report, but cannot pass automatic visual E2E. |
| `blocked_missing_visual_provider` | `false` | `true` | excluded from phase metric denominator, release-gate failure when the provider is required | Pre-run diagnostic must name missing provider/config. |
| `blocked_missing_vlm_provider` | `false` | `true` | excluded from phase metric denominator, release-gate failure when visual analysis is required | Must preserve fetched artifacts for later review when policy allows. |
| `policy_blocked_visual` | `true` when policy is enforced correctly, otherwise `false` | `true` | excluded from phase metric denominator | No blocked image can support a high-confidence claim. |
| `budget_pruned_visual` | `true` only when pruning follows the selected preset; `false` when required evidence is pruned below gate minimums | `true` | included as failure for automatic visual E2E unless the run is explicitly low-budget/excluded | Cost fields must show which cap triggered pruning. |

Public Beta automatic visual acceptance:

- A visual-required run with no user-provided images can complete through Codex-native search plus real web image/page/screenshot/PDF visual acquisition artifacts.
- At least 10 web visual candidates are collected for image-centric questions unless policy or provider failure blocks the run with an explicit state.
- At least 3 non-fixture images/screenshots/figures are analyzed through Codex interactive handoff in the default codex-plugin release gate. `openai-responses-vision` analysis remains the equivalent automated-cli diagnostic requirement. Explicit blocked terminal states are valid diagnostics but do not count as release-gate passes.
- At least one supported visual or mixed claim has `supporting_images[]`, valid `visual_supports[]`, a visual verifier vote, and is cited in `report.md`.
- The report clearly distinguishes observed image facts from text-source facts and records image caveats.
- The evidence browser can show the source page, image artifact, VLM observation, visual verifier vote, linked claim, and report citation together.
- Manual or user-provided image fallback is allowed only as supplemental evidence; it cannot hide failure of the automatic path.

### Parallel Codex Subagent Orchestration Contract

DeepResearch의 목표 조사 모델은 단일 agent가 모든 조사를 순차 처리하는 것이 아니라, planner가 만든 bounded research task를 여러 Codex subagent에 나눠 병렬 처리하는 구조다.

Parallel orchestration flow:

```text
$deep-research: <question>
-> runner prepare creates research_tasks.json
-> orchestrator assigns ready tasks to Codex subagents up to max_concurrent_codex_subagents
-> each subagent performs search/read/image inspection for one task
-> each subagent writes evidence_shards/<task_id>/evidence_shard.json and handoff JSONL files
-> runner validates every shard independently
-> shard merger deduplicates sources/images/claims and writes merge_status.json
-> verifier matrix and synthesis run on merged evidence only
```

Required parallel artifacts:

- `research_tasks.json`: canonical task queue for one run.
- `subagent_assignments.jsonl`: append-only assignment log.
- `evidence_shards/<task_id>/evidence_shard.json`: per-task evidence output.
- `evidence_shards/<task_id>/search_results.jsonl`: per-task search result handoff.
- `evidence_shards/<task_id>/visual_observations.jsonl`: per-task visual handoff when needed.
- `merge_status.json`: source/image/claim dedupe decisions, conflicts, rejected shards, and merged artifact paths.
- `run_trace.jsonl`: task assignment, subagent start/finish/failure, merge, retry, and synthesis events.

`ResearchTask` minimum fields:

```json
{
  "id": "task_research_001",
  "angle_id": "angle_001",
  "route": "text_only|visual_required|visual_optional",
  "query": "official release notes for ...",
  "state": "queued|assigned|running|completed|failed|blocked|retryable|merged|discarded",
  "assigned_subagent_id": null,
  "attempt": 0,
  "max_attempts": 2,
  "max_sources": 8,
  "max_images": 0,
  "source_policy": {"decision": "allowed", "flags": []},
  "output_shard_path": "evidence_shards/task_research_001/evidence_shard.json",
  "trace_event_ids": []
}
```

Task state rules:

- `queued` tasks may become `assigned`.
- `assigned` tasks must record `assigned_subagent_id` before becoming `running`.
- `running` tasks may become `completed`, `failed`, `blocked`, or `retryable`.
- `completed` tasks may become `merged` only after shard validation passes.
- `failed` tasks may become `retryable` only when `attempt < max_attempts` and the failure category is retry-safe.
- `blocked` tasks must keep explicit policy, missing-capability, or missing-input reason and must not be silently retried.
- `discarded` tasks must record dedupe, budget, or policy reason in `merge_status.json`.

Parallel mode requirements:

- `codex-plugin` mode uses Codex subagents when the current Codex surface can spawn or delegate subagent work. If subagents are unavailable and fallback is allowed, the runner must degrade to serial handoff execution and record `parallel_degraded=true`; `--no-degrade` fails fast with diagnostics.
- `automated-cli` mode may use OpenAI Agents SDK or an equivalent local worker pool for reproducible parallel task execution.
- `manual-sources` mode does not spawn subagents by default; it can still merge manually supplied evidence shards.
- Every subagent output must be treated as untrusted until evidence schema validation, guardrails, and verifier matrix pass.
- No final report claim may cite a shard that failed validation, was policy-blocked, or was discarded by dedupe.
- The default `standard` run uses conservative parallelism. The Claude Code-style high fan-out mode is `exhaustive` and requires explicit user confirmation, cost cap, and hard `max_concurrent_codex_subagents <= 100`.

M18 implementation decision:

- The first production implementation of parallel research orchestration uses an automated runner adapter, not an undocumented plugin-internal subagent API.
- The automated runner adapter invokes Codex through `codex exec --json -c agents.max_threads=N` or through the Codex SDK/MCP server with equivalent config.
- The adapter must parse JSON events for `spawn_agent`, `wait`, `close_agent`, thread IDs, child status, child messages, and failures.
- Each child subagent prompt must require a schema-valid evidence shard file path or a strict JSON result. Natural-language summaries alone are insufficient for merge.
- The adapter must persist the raw Codex event stream or a normalized projection in `run_trace.jsonl`.
- If `codex exec`, Codex SDK, authentication, sandbox, approval policy, or subagent spawning is unavailable and serial fallback is allowed, the run records `parallel_degraded=true` and executes the same `research_tasks.json` queue serially. With `--no-degrade`, the run fails fast with diagnostics instead of falling back.

2026-06-24 real-use E2E finding:

- Fixture adapter success is not sufficient evidence that real Codex subagent research works. Fixture runs prove task planning, shard validation, and merge mechanics only.
- Real-use E2E with `adapter=codex-exec` produced `research_tasks.json`, `subagent_assignments.jsonl`, and `parallel_orchestration_status.json`, but produced `accepted_shards=0`.
- The concrete observed child execution blocker was `Not inside a trusted directory and --skip-git-repo-check was not specified.` Child `codex exec` runs were launched from a temporary run directory rather than a trusted project context.
- The adapter must run child Codex sessions from a trusted project root with `-C <project-root>` while writing outputs to the target run directory. A repo-check bypass is allowed only for a user-approved diagnostic run where the output path is still under the selected run directory, the command records `repo_check_bypass_used=true`, and the run cannot be counted as passing real-use E2E acceptance.
- If all child tasks fail or no shards are accepted, status must not imply successful parallel execution. `parallel_degraded`, `status`, `needs_serial_handoff`, failure counts, and diagnostics must agree.
- Real-use visual E2E showed that image candidates and observations can be ingested, but final synthesis used `used_images=0`; therefore visual evidence ingestion is not complete until image observations can support claim `supporting_images` and appear in `report.md`.
- Real-use report E2E showed mechanically valid `report.md` output, but not user-quality synthesis. The final report must follow the user's requested shape, language, comparison table, gap list, and direct-answer format.
- A completed visual/text run must not remain `in_progress` because a later stage stale-reset an earlier visual stage.

Real-use Phase 2 hardening gate:

- A text-heavy real-use E2E using `adapter=codex-exec --no-degrade` must produce `accepted_shards > 0`, or fail fast with an explicit actionable diagnostic and no serial fallback.
- A visual-required real-use E2E must produce image evidence that is linked to at least one supported claim and reported with `report_status.used_images > 0`.
- A Korean user prompt must produce a Korean final report unless the user requests another language.
- Fixture adapter runs must be labeled as fixture-only and cannot satisfy real `codex-exec` E2E acceptance.

Parallel status matrix:

| Scenario | Required `status` | `parallel_degraded` | `needs_serial_handoff` | Serial fallback allowed | Exit behavior |
| --- | --- | ---: | ---: | ---: | --- |
| Fixture adapter succeeds | `completed_fixture` | `false` | `false` | `false` | success, but fixture-only |
| Real `codex-exec` partial or full success with `accepted_shards > 0` | `completed_parallel` or `completed_partial_parallel` | `false` | `false` when enough evidence remains, otherwise `true` | explicit user/runner policy only | success when acceptance threshold passes |
| Adapter unavailable before launch, fallback not yet run | `serial_fallback_pending` | `false` | `true` | `true` unless `--no-degrade` | pending/blocking handoff; fail fast with `--no-degrade` |
| All child tasks fail or `accepted_shards=0`, fallback not yet run | `failed_parallel_no_accepted_shards` | `false` | `true` | `true` unless `--no-degrade` | fail fast with `--no-degrade`; otherwise continue only as explicit serial handoff |
| Trust/sandbox/auth/approval blocker | `blocked_parallel_execution` | `false` unless fallback actually ran | `true` | `true` only after recording blocker and fallback decision | fail fast with `--no-degrade` |
| Explicit serial fallback completes after a parallel blocker | `completed_serial_handoff` | `true` | `false` | already used | success only if serial evidence/report gates pass |

`parallel_degraded=true` means a fallback path actually replaced intended parallel execution. A blocked or failed real parallel run that has not executed serial fallback must not set `parallel_degraded=true` merely to hide the failure.

`--no-degrade` failure contract:

- CLI commands exit non-zero.
- JSON result envelopes set `ok=false`, `status` to the matrix failure or blocked status, `parallel_degraded=false`, `needs_serial_handoff=true`, and include `diagnostics.actionable_cause`.
- No serial source fetching, report synthesis, or fallback evidence generation may run after the failure in the same command.

Codex child timeout and capacity handling:

- Child execution diagnostics must distinguish `timeout=true` from model-capacity or other retryable child failures. Raw child stderr/stdout, JSON event fragments, last child message, timeout setting, elapsed seconds, and attempt count must be preserved in `run_trace.jsonl` or a referenced raw diagnostic artifact.
- `--codex-exec-timeout-seconds` controls the child execution timeout and must be recorded in `run_status.json` and `parallel_orchestration_status.json`.
- If the child last message is `Selected model is at capacity. Please try a different model.` or an equivalent capacity response, classify the failure as `child_failure_code=codex_child_model_capacity` with `timeout=false`.
- `codex_child_model_capacity` is retry-safe while `attempt < max_attempts`, budget remains, and the run has not exceeded its timeout envelope. Default retry policy is `max_attempts=3`, `initial_delay_seconds=5`, `backoff_multiplier=2`, `max_delay_seconds=30`, `jitter_ratio=0.2`, and `max_retry_elapsed_seconds=min(120, codex_exec_timeout_seconds / 2)`. Test mode may set delay to `0` while preserving the computed policy fields.
- Retry delay for attempt `n` is `min(max_delay_seconds, initial_delay_seconds * backoff_multiplier^(n-1))` with jitter applied within `+/- jitter_ratio`; implementations must record both computed and actual sleep seconds.
- Each child attempt appends an attempt record with `attempt`, `max_attempts`, `child_thread_id`, `child_failure_code`, `timeout`, `returncode`, `last_message_text_preview`, `raw_child_event_artifacts`, `computed_backoff_seconds`, `actual_sleep_seconds`, and `retry_decision=retry|do_not_retry|retry_exhausted`.
- Quota exhaustion, billing-disabled states, invalid auth, persistent permission denial, policy blocks, sandbox/approval blockers, and deterministic schema validation failures are not model-capacity failures and must not be retried as capacity.
- If capacity retries are exhausted and `accepted_shards=0`, the terminal status is `failed_parallel_no_accepted_shards`, not a timeout and not `completed_partial_parallel`.

P3-RUN1 acceptance fixtures:

- Capacity recovery fixture: first `codex-exec` attempt exits with return code 1 and last child message `Selected model is at capacity. Please try a different model.`; second attempt writes a valid shard; final status accepts/merges the shard and preserves the first attempt diagnostics plus retry decision.
- Non-retry fixture: auth, sandbox, quota/billing, policy, or schema-validation failure does not retry as model capacity; final diagnostics identify the non-retryable category.
- Exhaustion fixture: all capacity attempts fail until `max_attempts` is reached; final status is `failed_parallel_no_accepted_shards`, `timeout=false`, and diagnostics show `retry_exhausted`.

Report quality gate:

- Representative real-use reports are scored out of 10 before Phase 2 exit.
- Passing threshold is `>=9/10`.
- Score dimensions: language match 1.0, direct answer 1.5, requested structure/table/gap list 1.5, cited evidence and evidence IDs 2.0, conflict/caveat handling 1.0, source-boilerplate suppression 1.0, visual evidence integration when applicable 1.0, concise user-facing synthesis 1.0.
- A report cannot score above 8 if it ignores the user's requested language or requested output shape.
- A visual-required report cannot score above 8 if `report_status.used_images=0` while usable image evidence exists.

Non-developer input path:

- MVP에서는 별도 web UI를 제공하지 않는다.
- Phase 1에서 허용하는 최소 비개발자 경로는 Codex 안의 `$deep-research` Skill prompt와 optional TUI/form prompt다.
- TUI/form이 구현되는 경우 입력 필드는 `question`, `depth preset`, `visual inclusion`, `source URLs`, `image files`로 제한한다.
- full web dashboard는 Public Beta 범위다.

## 주요 요구사항

1. 질문을 5-8개 조사 angle로 분해한다.
2. `ModalityRouter`가 angle마다 `text_only`, `visual_required`, `visual_optional` 중 하나로 분류한다.
3. 텍스트 소스는 본문, 날짜, 저자, 도메인, quote를 추출한다.
4. 이미지 소스는 URL, 원본 페이지, alt text, OCR, 시각 설명, perceptual hash, 캡션, 주변 문맥을 저장한다.
5. VLM 에이전트는 이미지를 다음 관점으로 분석한다.
   - OCR/text-in-image
   - 객체/장면/제품/인물/인터페이스 식별
   - 차트/표/그래프 해석
   - 이미지와 본문 claim의 일치 여부
   - 조작/스톡/마케팅 이미지 가능성
   - 같은 이미지의 중복/파생본 탐지
6. claim마다 route별 verifier matrix를 실행한다.
   - text-only claim은 text verifier 2개와 freshness/policy verifier 1개를 실행한다.
   - visual-required claim은 text verifier 2개, visual verifier 1개 이상, policy verifier 1개를 실행한다.
   - visual-optional claim은 budget에 따라 visual verifier를 생략할 수 있지만, visual vote 없는 시각 claim은 high-confidence가 될 수 없다.
7. 반박 2표 이상이면 claim을 폐기한다.
8. 최종 보고서는 모든 주요 주장에 source URL, quote, image evidence ID를 붙인다.
9. 실행 전 agent budget을 산정하고 사용자가 선택한 조사 깊이의 hard cap을 넘지 않는다.
10. VLM이 필요 없는 angle에는 이미지 수집과 VLM 호출을 하지 않는다.
11. Phase 2부터 planner는 angle을 bounded `ResearchTask`로 분해하고, parallel mode에서는 task를 여러 Codex subagent에 배정한다.
12. 각 Codex subagent는 자기 task 범위 안에서 검색, 페이지 읽기, 이미지 확인, visual observation 기록, evidence shard 생성을 담당한다.
13. runner는 subagent가 만든 evidence shard를 schema validation, guardrail, dedupe, merge 과정을 거쳐 최종 `evidence.json`으로 병합한다.
14. 병렬 실행은 `max_concurrent_codex_subagents`, `max_concurrent_runner_agents`, source/image/model-call cap을 모두 준수해야 한다.
15. subagent 실패는 전체 run 실패로 즉시 승격하지 않고, retry-safe failure는 task 단위로 재시도하며, blocked/policy failure는 명시 상태로 보존한다.

## 에이전트 구성

- `PlannerAgent`: 질문 분해, 텍스트/이미지 조사 필요성 판단.
- `OrchestratorAgent`: planner task를 Codex subagent 또는 runner worker에 배정하고 task state, retry, budget cap을 관리.
- `ModalityRouterAgent`: angle과 claim 후보를 `text_only`, `visual_required`, `visual_optional`로 분류하고 VLM 호출 여부를 결정.
- `SearchAgent`: 웹 검색 결과 수집.
- `ImageScoutAgent`: 이미지 검색, 페이지 대표 이미지, 스크린샷 후보 수집.
- `FetchAgent`: HTML/PDF/문서 fetch 및 본문 추출.
- `VisionExtractAgent`: 이미지 OCR, 시각 claim 추출, 차트/스크린샷 해석.
- `ClaimExtractorAgent`: 텍스트 claim 구조화.
- `VerifierAgent`: claim 반박 검색.
- `VisualVerifierAgent`: 이미지가 claim을 실제로 뒷받침하는지 검증.
- `ShardMergeAgent`: subagent evidence shard를 validate, dedupe, conflict-marking한 뒤 canonical `evidence.json`으로 병합.
- `SynthesisAgent`: 살아남은 claim만 병합해 보고서 작성.

## ModalityRouter 분류 규칙

`text_only`:

- 공식 문서, API 스펙, 릴리즈 노트, 법/정책 문서, 논문 본문처럼 텍스트 근거만으로 검증 가능한 질문.
- 이미지가 있어도 본문 claim의 장식 요소에 불과한 경우.
- 예: "Claude Code dynamic workflow 구조 조사", "Next.js 최신 라우팅 변경점 비교".

`visual_required`:

- 질문의 핵심 증거가 이미지, UI, 스크린샷, 사진, 차트, 다이어그램, 제품 외형, 영상 프레임, OCR 텍스트에 있는 경우.
- 텍스트 설명과 실제 이미지가 불일치할 수 있는 경우.
- 이미지 품질, 얼굴/손/치아/눈 왜곡, 디자인/레이아웃, 그래프 수치, 전후 비교, 지도/위성/현장 사진을 다루는 경우.
- 예: "AI 프로필 사진 서비스의 손/치아 아티팩트 검출 방식 조사", "경쟁 앱 온보딩 UI 비교".

`visual_optional`:

- 텍스트만으로 1차 답은 가능하지만 이미지가 claim confidence를 높이거나 반박할 수 있는 경우.
- 제품 비교, 시장조사, 뉴스 기사, 벤치마크 글처럼 캡처/차트/제품 이미지가 보조 근거인 경우.
- 예산이 낮거나 빠른 모드면 `text_only`로 degrade할 수 있다.

라우팅 출력 스키마:

```json
{
  "angle": "artifact detection",
  "modality": "visual_required",
  "reason": "The claim depends on visible hands, teeth, eyes, and image artifacts.",
  "visual_tasks": ["ocr", "artifact_detection", "image_claim_alignment"],
  "max_images": 12
}
```

## Visual Input and Acquisition Modes

VLM invocation paths:

| VLM path | 사용 위치 | 동작 | MVP 역할 |
| --- | --- | --- | --- |
| `codex-interactive` | Codex plugin/skill 세션 | Codex가 현재 세션의 이미지/스크린샷을 읽고 분석한다. Plugin은 분석 요청, evidence 구조화, 검증 단계를 안내한다. | 개인용 plugin mode의 우선 경로 |
| `openai-responses-vision` | 자동 CLI, batch, plugin runner의 자동 분석 | 수집 이미지/스크린샷을 OpenAI Responses API vision input으로 보내 분석 결과를 JSON으로 받는다. | 자동 실행이 필요한 경우의 명확한 API 경로 |
| `manual-visual-review` | API 사용 불가, 비용 제한, 민감 이미지 | 사람이 이미지를 확인하고 observation을 evidence에 입력한다. | fallback 및 high-risk review |

Codex interactive VLM capability:

- Codex는 사용자가 첨부한 이미지 파일과 스크린샷을 읽을 수 있다.
- 개인용 plugin MVP에서는 이 능력을 우선 사용한다.
- 단, local runner가 Codex interactive VLM을 안정적인 library API처럼 직접 호출할 수 있다고 가정하지 않는다.
- DeepResearch는 자동 수집한 이미지와 스크린샷을 로컬 run artifact로 저장한 뒤, 선택된 VLM path를 통해 `VisionExtractAgent`와 `VisualVerifierAgent`에 투입한다.
- 재현 가능한 자동 실행이 필요하면 `openai-responses-vision` adapter를 사용한다.
- 사용자가 제공한 이미지 파일, 붙여넣은 스크린샷, image URL은 자동 수집 결과를 보강하는 supplemental evidence로 취급한다.

Visual acquisition layers:

1. MVP basic visual acquisition
   - MVP 기본값.
   - 텍스트 검색 결과의 웹페이지에서 대표 이미지, Open Graph image, 본문 image 후보를 수집한다.
   - visual-required angle에서는 이미지 검색 provider가 가능하면 image search를 수행한다.
   - 각 high-relevance source page의 first viewport screenshot을 캡처한다.
   - 수집 이미지는 MIME, 크기, 중복 URL, basic hash로 필터링한다.
   - `standard` preset 기준 source당 최대 2개 이미지, visual angle당 최대 12개 이미지만 Codex VLM에 보낸다.

2. User-provided visual evidence
   - 사용자가 local image file, pasted screenshot, image URL, PDF page image를 추가로 제공할 수 있다.
   - 자동 수집이 놓친 이미지나 특정 사용자가 중요하게 보는 시각 자료를 보강한다.
   - visual-required 태스크에서 자동 수집 결과가 부족하면 사용자 제공 evidence를 우선 분석한다.

3. Advanced visual acquisition
   - Private Alpha 이후 범위.
   - 대량 이미지 검색, 스크롤/상호작용 screenshot, PDF page rasterization, 영상 frame sampling, perceptual hash clustering, resume-aware visual cache를 포함한다.
   - MVP의 basic collector를 더 넓고 안정적인 crawler/collector로 확장한다.

Decision:

- MVP는 plugin-first이며, VLM 기본값은 `codex-plugin` mode에서 `codex-interactive`, `automated-cli` mode에서 `openai-responses-vision`이다.
- user-provided visual evidence는 supplemental input이다.
- 자동 이미지 검색과 스크린샷 수집은 MVP에 포함한다.
- Private Alpha는 더 큰 규모, 더 정교한 중복 제거, resume/cache, 대량 캡처 안정화를 담당한다.
- `visual_required` 태스크는 자동 수집 이미지 또는 사용자 제공 이미지가 없거나 VLM path가 실행 불가능하면 `needs_visual_evidence` 상태로 남기고 high-confidence 결론을 내지 않는다.

## Invocation Budget

기본 preset:

| Preset | 최대 Codex-side handoff task | 최대 동시 Codex subagent | 최대 동시 runner agent | verifier invocation | model/API call hard cap | 최대 source | 최대 image | 용도 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `quick` | 16 | 4 | 4 | 24 | 32 | 8 | 4 | 빠른 사실 확인 |
| `standard` | 48 | 8 | 8 | 80 | 96 | 20 | 12 | MVP 기본 딥리서치 |
| `deep` | 96 | 24 | 12 | 180 | 220 | 40 | 30 | 고신뢰 보고서 |
| `exhaustive` | 256 | 100 | 16 | 500 | 600 | 100 | 80 | 비용 확인 후 실행하는 대형 조사 |

MVP 기본값은 `standard`다.

Budget terms:

- 48은 Codex-side handoff task 한도다. Codex-native search/VLM을 수행하는 agent-mediated work item 수를 제한한다.
- 8은 `standard` preset의 최대 동시 Codex subagent 수다. high fan-out 조사는 `deep` 또는 `exhaustive`에서만 허용한다.
- 80은 verifier invocation 한도다. 하나의 claim에 대해 하나의 verifier가 vote를 만드는 작업을 제한한다.
- 96은 paid or metered model/API call hard cap이다.
- 동시 runner agent 기본값은 8이고 MVP hard cap은 12다.
- `exhaustive`는 v2에서 제공하며 실행 전 예상 비용, 시간, `max_concurrent_codex_subagents` 값을 사용자에게 확인받는다.

Agent 산정식:

```text
1 Planner
+ 1 ModalityRouter
+ N SearchAgent
+ V ImageScoutAgent
+ S FetchAgent
+ I VisionExtractAgent
+ min(C, max_verify_claims) * verifier_count
+ 1 SynthesisAgent
```

여기서 `N`은 angle 수, `V`는 visual angle 수, `S`는 fetch source 수, `I`는 분석 이미지 수, `C`는 claim 수다. Budget을 넘으면 source quality, relevance, visual necessity 순으로 pruning한다.

## Evidence Schema v0

```json
{
  "schema_version": "0.1.0",
  "run_id": "dr_20260616_001",
  "created_at": "2026-06-16T10:00:00Z",
  "question": "...",
  "mode": "codex-plugin|automated-cli|manual-sources",
  "search_provider": "codex-native|openai|brave|tavily|serpapi|manual",
  "vlm_provider": "codex-interactive|openai-responses-vision|manual-visual-review",
  "sources": [
    {
      "id": "src_001",
      "type": "web|pdf|image|screenshot",
      "url": "...",
      "title": "...",
      "published_at": "...",
      "accessed_at": "2026-06-16T10:03:00Z",
      "quality": "primary|secondary|blog|forum|unknown",
      "retrieval_status": "fetched|failed|partial|manual",
      "local_artifact_path": "sources/src_001.html",
      "license_policy": "unknown|allowed|restricted|manual_review",
      "robots_policy": "unknown|allowed|disallowed|manual_review"
    }
  ],
  "routing": [
    {
      "angle": "artifact detection",
      "modality": "visual_required",
      "reason": "The claim depends on visible hands, teeth, eyes, and image artifacts.",
      "visual_tasks": ["ocr", "artifact_detection", "image_claim_alignment"],
      "max_images": 12
    }
  ],
  "budget": {
    "preset": "standard",
    "max_codex_handoff_tasks": 48,
    "max_concurrent_runner_agents": 8,
    "max_verifier_invocations": 80,
    "max_model_api_calls": 96,
    "verifier_invocations_used": 37
  },
  "images": [
    {
      "id": "img_001",
      "task_id": "task_research_001",
      "angle_id": "angle_001",
      "candidate_id": "vc_001",
      "fetch_id": "fetch_001",
      "source_id": "src_001",
      "origin": "screenshot",
      "image_url": "...",
      "page_url": "...",
      "local_artifact_path": "images/img_001.png",
      "mime_type": "image/png",
      "width": 1280,
      "height": 720,
      "hash": "sha256:...",
      "phash": "...",
      "ocr_text": "...",
      "observations": ["visible UI contains a pricing table"],
      "inferences": ["the screenshot likely came from the pricing page"],
      "visual_tasks": ["ocr", "image_claim_alignment"],
      "analysis_provider": "codex-interactive",
      "analysis_status": "analyzed|failed|skipped|needs_manual_review|policy_blocked",
      "provider_provenance": {
        "provider": "local-page",
        "provider_kind": "screenshot",
        "provider_mode": "real|fixture|manual|user_provided"
      },
      "policy_decision": "allowed|blocked|manual_review|budget_pruned",
      "policy_flags": [],
      "cost_usd": 0.0,
      "caveats": []
    }
  ],
  "claims": [
    {
      "id": "claim_001",
      "text": "...",
      "claim_type": "text|visual|mixed",
      "supporting_sources": ["src_001"],
      "supporting_images": ["img_001"],
      "visual_supports": [
        {
          "image_id": "img_001",
          "observation_ref": "images.img_001.observations[0]",
          "observation_index": 0,
          "observation_text": "visible UI contains a pricing table",
          "relation_type": "ocr_support|visual_match|chart_support|screenshot_support|context_support",
          "confidence": 0.74,
          "provider": "codex-interactive",
          "rationale": "The screenshot text matches the pricing claim."
        }
      ],
      "quote_spans": [
        {
          "source_id": "src_001",
          "quote": "...",
          "location": "paragraph 4"
        }
      ],
      "votes": [
        {
          "id": "vote_001",
          "claim_id": "claim_001",
          "verifier_type": "text",
          "agent_name": "text_verifier_1",
          "method": "model-call",
          "model_or_tool": "gpt-5.1",
          "vote": "support",
          "confidence": 0.72,
          "evidence_refs": ["src_001"],
          "rationale": "...",
          "created_at": "2026-06-22T00:00:00Z"
        },
        {
          "id": "vote_002",
          "claim_id": "claim_001",
          "verifier_type": "visual",
          "agent_name": "visual_verifier_1",
          "method": "codex-subagent",
          "model_or_tool": "codex-interactive",
          "vote": "refute",
          "confidence": 0.64,
          "evidence_refs": ["img_001"],
          "rationale": "...",
          "created_at": "2026-06-22T00:00:00Z"
        }
      ],
      "verification_status": "supported|refuted|disputed|insufficient_evidence|needs_visual_evidence|budget_pruned|policy_blocked|unverified",
      "review_status": "not_reviewed|auto_reviewed|human_accepted|human_rejected|needs_more_evidence",
      "promotion_status": "not_eligible|eligible|promoted_memory|promoted_playbook|promoted_skill|promoted_prd|promotion_rejected",
      "confidence": "high|medium|low",
      "caveats": ["small text OCR may be unreliable"]
    }
  ]
}
```

Schema v0 implementation requirements:

- `schema_version`, `run_id`, `created_at`, `mode`, `search_provider`, `vlm_provider`는 필수다.
- `vlm_provider`는 사용 가능한 시각 분석 경로를 기록한다. `text_only`처럼 VLM이 필요 없는 작업은 provider 값을 `none`으로 바꾸지 않고 route, `visual_tasks`, VLM invocation count, 또는 verifier state로 VLM 미사용을 표현한다.
- 모든 source는 `accessed_at`, `retrieval_status`, `quality`, `local_artifact_path`를 가진다.
- 모든 image/screenshot은 `VisualEvidence` schema를 따른다.
- 모든 image/screenshot은 `page_url` 또는 `image_url` 중 하나 이상과 `local_artifact_path`를 가진다.
- Phase 3 automatic visual image records must include `task_id`, `angle_id`, `candidate_id`, `fetch_id`, `provider_provenance`, `policy_decision`, and `cost_usd` when they came from automatic acquisition.
- VLM output은 `observations`와 `inferences`를 분리한다.
- 모든 high-confidence text claim은 하나 이상의 `quote_spans`를 가진다.
- 모든 high-confidence visual/mixed claim은 하나 이상의 `supporting_images`를 가진다.
- `supporting_images`는 빠른 필터링용 image ID 목록이다. visual/mixed claim이 `supporting_images`를 가지면 `visual_supports[]`도 반드시 가져야 한다.
- 모든 `visual_supports[].image_id`는 `images[].id`에 존재해야 한다.
- 모든 `visual_supports[]`는 해당 image의 관찰 근거를 deterministic하게 가리켜야 한다. 현재 schema v0에서는 `observation_index`가 `images[].observations[]`의 유효한 index이고, `observation_ref`는 `images.<image_id>.observations[<index>]` 형식이어야 한다. 이후 `visual_observations.jsonl` records가 stable record identifier를 갖게 되면 `visual_observations.jsonl:<record-id>`도 허용할 수 있다.
- `visual_supports[].observation_text`를 기록할 때는 참조한 observation 문자열과 일치해야 한다.
- `visual_supports[].relation_type`은 `ocr_support`, `visual_match`, `chart_support`, `screenshot_support`, `context_support` 중 하나다.
- `visual_supports[].confidence`는 0.0부터 1.0까지의 숫자이며, 별도 verifier vote 없이 claim을 high-confidence로 승격할 수 없다.
- `claims[].votes[]`는 `VerifierVote` schema를 그대로 embed하거나 `verifier_votes.jsonl`의 `id`를 참조한다. MVP는 embed를 기본으로 한다.
- verifier vote는 `id`, `claim_id`, `verifier_type`, `agent_name`, `method`, `model_or_tool`, `vote`, `confidence`, `evidence_refs`, `rationale`, `created_at`을 가진다.
- budget 때문에 검증하지 못한 claim은 `budget_pruned`로 남기고 최종 보고서의 확정 주장으로 쓰지 않는다.

### Evidence State Model v0

Claim 상태는 하나의 `status` 필드로 합치지 않는다. 검증, 사람 리뷰, 승격 상태를 분리한다.

`verification_status` enum:

- `unverified`: claim이 추출됐지만 verifier가 아직 실행되지 않았다.
- `supported`: verifier matrix를 통과했고 반박 임계값을 넘지 않았다.
- `refuted`: 반박 vote가 폐기 기준을 넘었다.
- `disputed`: support/refute가 충돌해 결론을 낼 수 없다.
- `insufficient_evidence`: quote, source, image evidence가 부족하다.
- `needs_visual_evidence`: visual-required claim인데 usable visual evidence가 없다.
- `budget_pruned`: budget cap 때문에 검증하지 못했다.
- `policy_blocked`: robots, paywall, copyright, PII, high-risk policy에 의해 사용 제한됐다.

`review_status` enum:

- `not_reviewed`: 사람이 검토하지 않았다.
- `auto_reviewed`: 자동 규칙으로 보고서 포함 가능 판정을 받았다.
- `human_accepted`: 사람이 승인했다.
- `human_rejected`: 사람이 거절했다.
- `needs_more_evidence`: 사람이 추가 증거 필요로 표시했다.

`promotion_status` enum:

- `not_eligible`: 승격 조건을 만족하지 않는다.
- `eligible`: 승격 가능하지만 아직 승격되지 않았다.
- `promoted_memory`: memory로 승격됐다.
- `promoted_playbook`: playbook으로 승격됐다.
- `promoted_skill`: skill로 승격됐다.
- `promoted_prd`: PRD로 승격됐다.
- `promotion_rejected`: 승격이 명시적으로 거절됐다.

Report inclusion rule:

- 최종 보고서의 확정 claim은 `verification_status=supported`이고 `review_status`가 `auto_reviewed` 또는 `human_accepted`여야 한다.
- 후속 Codex 작업에 재사용 가능한 claim은 `verification_status=supported`이고 `promotion_status=eligible|promoted_*` 중 하나여야 한다.

### Adapter Interface Schemas v0

All adapters must emit these canonical records before evidence ingestion.

`SearchResult`:

```json
{
  "id": "sr_001",
  "task_id": "task_search_001",
  "angle_id": "angle_001",
  "route": "text_only|visual_required|visual_optional",
  "provider": "codex-native|openai|brave|tavily|serpapi|manual",
  "query": "...",
  "url": "...",
  "title": "...",
  "snippet": "...",
  "result_type": "web|pdf|image|news|academic|manual",
  "rank": 1,
  "freshness_requirement": "latest|recent|historical|any",
  "published_at": null,
  "accessed_at": "2026-06-22T00:00:00Z",
  "language": "en",
  "region": "US",
  "policy_decision": "allowed|blocked|manual_review",
  "policy_flags": [],
  "raw_provider_metadata": {}
}
```

SearchResult validation rules:

- Every `SearchResult.task_id` must reference an existing search task.
- Every `SearchResult.angle_id` must reference an existing routed angle.
- `SearchResult.route` must match the referenced angle route.
- `policy_decision=blocked` result cannot create high-confidence claims.
- `provider=codex-native` is valid only in `codex-plugin` mode.

`VisualEvidence`:

```json
{
  "id": "img_001",
  "task_id": "task_research_001",
  "angle_id": "angle_001",
  "candidate_id": "vc_001",
  "fetch_id": "fetch_001",
  "source_id": "src_001",
  "origin": "page_image|image_search|screenshot|user_upload|manual",
  "image_url": null,
  "page_url": "...",
  "local_artifact_path": "images/img_001.png",
  "mime_type": "image/png",
  "width": 1280,
  "height": 720,
  "hash": "sha256:...",
  "phash": null,
  "ocr_text": null,
  "observations": ["Visible text says ..."],
  "inferences": ["This likely indicates ..."],
  "visual_tasks": ["ocr", "chart_reading", "image_claim_alignment"],
  "analysis_provider": "codex-interactive|openai-responses-vision|manual-visual-review",
  "analysis_status": "analyzed|failed|skipped|needs_manual_review|policy_blocked",
  "provider_provenance": {
    "provider": "example-provider",
    "provider_kind": "web_image_search|page_extractor|screenshot|pdf_rasterizer|manual|fixture",
    "provider_mode": "real|fixture|manual|user_provided"
  },
  "policy_decision": "allowed|blocked|manual_review|budget_pruned",
  "policy_flags": [],
  "cost_usd": 0.0,
  "caveats": []
}
```

`VerifierVote`:

```json
{
  "id": "vote_001",
  "claim_id": "claim_001",
  "verifier_type": "text|visual|policy|freshness",
  "agent_name": "text_verifier_1",
  "method": "codex-subagent|runner-agent|model-call|manual-review",
  "model_or_tool": "gpt-5.1|codex-interactive|manual",
  "vote": "support|refute|uncertain|blocked",
  "confidence": 0.72,
  "evidence_refs": ["src_001", "img_001"],
  "rationale": "...",
  "created_at": "2026-06-22T00:00:00Z"
}
```

## Verifier Matrix and Cost Caps

Verifier policy is route-specific.

| Route | Required verifier invocations | Visual verifier | Default max sources | Default max images | VLM cap | Report rule |
| --- | ---: | --- | ---: | ---: | --- | --- |
| `text_only` | 2 text + 1 freshness/policy | Forbidden | 12 | 0 | 0 | quote-backed claims only |
| `visual_required` | 2 text + 1 visual + 1 policy | Required | 20 | 12 | required, capped | image-backed claims required |
| `visual_optional` | 2 text + 1 policy, visual if budget allows | Optional | 16 | 6 | best-effort | visual claims cannot be high-confidence without visual vote |

Cost caps by preset:

| Preset | Search calls | Sources fetched | Images analyzed | Concurrent Codex subagents | Verifier invocations | Model calls | Notes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `quick` | 8 | 8 | 4 | 4 | 24 | 32 | no optional visual expansion |
| `standard` | 20 | 20 | 12 | 8 | 80 | 96 | MVP default |
| `deep` | 40 | 40 | 30 | 24 | 180 | 220 | requires confirmation |
| `exhaustive` | 100 | 100 | 80 | 100 | 500 | 600 | high fan-out, explicit confirmation only |

Shared preset caps in this table must remain consistent with the `Invocation Budget` table. If implementation changes subagent, source, image, verifier, or model-call caps in one table, it must update the other in the same PR.

Rules:

- `text_only` route must not perform image search, screenshot capture, or VLM analysis.
- `visual_required` route must not produce high-confidence conclusions without at least one valid `VisualEvidence` and one visual `VerifierVote`.
- `visual_optional` route may skip VLM under budget pressure, but any skipped visual evidence must be recorded as `budget_pruned`.

## 검증 규칙

- quote 없는 텍스트 claim은 `verification_status=insufficient_evidence`.
- 이미지 claim은 이미지 원본 URL 또는 캡처된 screenshot 없이는 `verification_status=needs_visual_evidence`.
- VLM이 본문과 다른 내용을 읽으면 visual contradiction으로 기록.
- 날짜가 중요한 claim은 publish date 또는 access date 필수.
- medical/legal/financial claim은 primary source 없으면 high confidence 금지.
- 이미지 내 작은 글씨, 회전, 비라틴 문자 OCR은 별도 caveat 필수.

## Review and Promotion Rules

- raw source/image는 `sources[]`와 `images[]`에만 존재하며 claim으로 승격되지 않는다.
- extracted claim은 `verification_status=unverified`로 시작한다.
- verifier matrix를 통과한 claim은 `verification_status=supported`가 될 수 있다.
- 자동 규칙을 통과한 claim은 `review_status=auto_reviewed`, 사람이 승인한 claim은 `review_status=human_accepted`가 된다.
- `verification_status=supported`이고 `review_status=auto_reviewed|human_accepted`인 evidence만 `promotion_status=eligible` 또는 `promoted_*` 상태가 될 수 있다.
- rejected, policy-blocked, budget-pruned evidence는 memory/playbook/skill/PRD로 승격할 수 없다.

## 에이전트 검색/재사용 경로

기본 저장소:

```text
~/.codex/deepresearch/runs/<run_id>/
  report.md
  evidence.json
  run_status.json
  report_status.json
  parallel_orchestration_status.json
  visual_provider_status.json
  images/
  screenshots/
  claims.jsonl
```

Codex는 후속 작업에서 `evidence.json`을 먼저 읽고, `verification_status=supported`이며 `promotion_status=eligible|promoted_*`인 claim만 재사용한다.

## MVP

1. 개인용 Codex Plugin으로 패키징해 Codex에서 설치 가능하게 만든다.
2. Codex Skill `$deep-research`를 plugin의 primary UX로 구현한다.
3. plugin 내부 runner를 만들고, CLI `codex-deepresearch`는 runner를 직접 실행하는 개발/디버깅용 진입점으로 제공한다.
4. `codex-plugin` mode는 Codex-native search와 `codex-interactive` VLM path를 기본으로 한다.
5. `automated-cli` mode는 provider abstraction을 통해 OpenAI hosted search 또는 외부 search provider와 `openai-responses-vision` VLM path를 사용한다.
6. `manual-sources` mode는 사용자가 제공한 URL/PDF/image만으로 Phase 0와 fallback run을 지원한다.
7. `ModalityRouterAgent`로 text-only/visual-required/visual-optional 분류를 구현한다.
8. visual-required angle에서 이미지 검색, 대표 이미지 추출, first viewport screenshot 캡처를 수행한다.
9. `standard` preset 기준 verifier invocation 80개, 동시 runner agent 8개를 기본값으로 둔다.
10. 보고서 `report.md`, schema v0 `evidence.json`, `run_status.json`, 그리고 synthesis가 성공한 run의 `report_status.json` 저장.
11. route-specific verifier matrix 구현.

MVP plugin acceptance:

- `.codex-plugin/plugin.json`이 validation을 통과한다.
- personal marketplace metadata가 존재하고 install/update/remove 절차가 문서화되어 있다.
- `$deep-research: smoke test`가 Codex 세션에서 run directory, `report.md`, `evidence.json`, `run_status.json`, 그리고 synthesis가 성공한 run의 `report_status.json`을 생성한다.
- `codex-plugin` mode의 search handoff와 VLM handoff artifact가 schema validation을 통과한다.
- `verification_status`, `review_status`, `promotion_status`가 모든 claim에 존재한다.
- `SearchResult`, `VisualEvidence`, `VerifierVote` adapter records가 schema v0에 맞게 validate된다.
- MVP guardrail 위반 evidence는 `review_status=human_accepted` 또는 `promotion_status=promoted_*` 상태가 될 수 없다.

MVP security and policy guardrails:

- robots/paywall/copyright/PII policy flags must be recorded on every source and visual artifact when detected.
- CAPTCHA, login-gated, or access-controlled content must not be bypassed.
- private user-provided images are stored locally only by default and are marked `sensitive_possible` unless the user opts into export.
- medical/legal/financial claims require primary-source evidence or must be downgraded from high confidence.
- image evidence from unknown or restricted sources cannot be promoted without human review.
- all generated reports must preserve source URLs and evidence IDs, but must not copy large copyrighted passages.

## Product Roadmap

### Phase 0: Prototype

목표: 최종 제품을 Codex Plugin으로 만들 수 있도록 plugin-first 구조와 evidence schema v0를 검증한다.

범위:

- 로컬 Python/TypeScript 패키지 스캐폴드.
- Codex Plugin skeleton과 `$deep-research` Skill skeleton.
- plugin 내부 runner skeleton.
- 개발/테스트용 단일 명령 `codex-deepresearch "<question>"`.
- 수동 source URL 입력 지원.
- 수동 image file, screenshot, image URL 입력 지원.
- `PlannerAgent`, `ModalityRouterAgent`, `SynthesisAgent` 최소 구현.
- 출력: `report.md`, schema v0 `evidence.json`, `run_status.json`, 그리고 synthesis가 성공한 run의 `report_status.json`.

Exit criteria:

- 텍스트-only 질문 3개, visual-required 질문 3개를 끝까지 처리한다.
- `ModalityRouterAgent`가 VLM 호출 필요 여부를 evidence에 기록한다.
- `evidence.json`이 schema v0 validation을 통과한다.
- 실패해도 부분 evidence를 저장한다.

### Phase 1: MVP

목표: 개인 사용자가 Codex에서 내장 워크플로우처럼 쓸 수 있게 한다.

범위:

- `$deep-research` Codex Skill.
- 개인용 Codex Plugin 패키지.
- plugin 내부 runner.
- 개발/디버깅용 CLI wrapper.
- `quick`, `standard`, `deep` preset.
- `codex-plugin` mode의 Codex-native search workflow.
- `automated-cli` mode의 search provider abstraction.
- fetch, claim extraction.
- basic automated image collection.
- high-relevance source page first viewport screenshot capture.
- `codex-interactive` 및 `openai-responses-vision` VLM path.
- user-provided image file, screenshot, image URL 보강 입력.
- route-specific verifier matrix.
- source quote와 image evidence ID가 포함된 Markdown 보고서.
- `~/.codex/deepresearch/runs/<run_id>/` 저장.

Exit criteria:

- `standard` preset에서 verifier invocation 80개 이하로 안정 실행하고, hard cap 96개를 넘지 않는다.
- unsupported claim이 최종 보고서에 들어가지 않는다.
- text-only 태스크에서 VLM 호출이 발생하지 않는다.
- visual-required 태스크에서 자동 이미지 수집 또는 스크린샷 캡처가 실행된다.
- visual-required 태스크에서 최소 1개 visual verifier가 실행된다.
- 재실행 없이 결과 파일을 Codex가 후속 작업에 재사용할 수 있다.

### Phase 2: Private Alpha

목표: MVP의 basic visual acquisition을 더 안정적이고 대규모로 확장하고, 긴 리서치를 재개 가능하게 만들며, 실제 병렬 Codex subagent 조사 구조를 도입한다.

범위:

- parallel Codex subagent orchestration: planner task fan-out, task assignment, evidence shard, merge/dedupe.
- 이미지 검색 API provider 확대와 품질 튜닝.
- full-page, scroll, interaction screenshot 캡처.
- 대표 이미지, Open Graph image, 본문 이미지 추출의 정확도 개선.
- perceptual hash 기반 이미지 중복 제거.
- OCR 결과와 VLM 설명 분리 저장.
- resumable run: 중단된 run을 `run_id`로 재개.
- cost estimator: 실행 전 예상 source/image/agent/token 수 표시.
- run trace: agent별 prompt, tool call, output summary 저장.

Exit criteria:

- `standard` preset에서 최소 8개 Codex subagent 또는 equivalent worker가 병렬 research task를 처리하고 evidence shard를 생성한다.
- `standard` preset의 real-use `codex-exec` E2E에서 trusted project context로 child Codex 실행이 성공하고 `accepted_shards > 0`을 기록한다.
- `exhaustive` preset은 explicit confirmation과 cost cap이 있을 때 최대 100개 Codex subagent fan-out을 계획할 수 있다.
- subagent evidence shard가 schema validation, guardrail, dedupe, merge를 거쳐 canonical `evidence.json`에 반영된다.
- 이미지가 핵심인 조사에서 자동으로 최소 10개 후보 이미지를 수집한다.
- visual-required real-use E2E에서 이미지 evidence가 claim `supporting_images`에 연결되고 최종 `report.md`의 visual finding에 사용된다.
- 중복 이미지 제거율과 제거 근거가 evidence에 남는다.
- 중단 후 재개해도 이미 처리한 source/image를 다시 분석하지 않는다.
- 사용자가 실행 전 비용 상한을 설정할 수 있다.
- 최종 report는 사용자가 요청한 형식, 언어, 비교표, gap list를 반영한다.
- 전체 visual/text pipeline이 끝나면 `run_status.json`과 applicable visual/parallel status artifacts가 completed 상태를 보고한다.

### Phase 3: Public Beta

목표: 반복 사용 가능한 제품 경험과 검토 UX를 제공하고, 병렬 subagent 조사를 사용자가 관찰하고 제어할 수 있게 한다. 이 단계에서 visual-required 질문은 사용자가 이미지를 직접 제공하지 않아도 웹 이미지, 페이지 스크린샷, 차트, 논문 figure를 자동 수집하고 VLM으로 판독해 보고서에 반영할 수 있어야 한다.

범위:

- Codex Plugin marketplace 등록 또는 개인 marketplace 등록 자동화.
- `/skills`에서 선택 가능한 안정 skill metadata.
- `$deep-research` fresh-session invocation이 기본적으로 full-runner run을 시작하고, chat-only 답변으로 새지 않게 하는 invocation router.
- final response artifact handoff: run directory, `report.md`, `evidence.json`, `run_status.json`, synthesized-run `report_status.json`, applicable visual/parallel status artifacts, shard summary를 사용자에게 노출.
- TUI 또는 lightweight web dashboard.
- run list, progress, pause/resume/cancel.
- parallel subagent monitor: active/queued/failed/merged task count, subagent count, shard merge status.
- evidence browser: source, image, claim, vote를 탐색.
- automatic web visual research: web/image search provider, page image extraction, screenshot capture, optional PDF figure rasterization, image fetch/cache, VLM analysis, visual verifier, report citation까지 end-to-end 자동화.
- `openai-responses-vision` automated adapter를 Public Beta automatic visual E2E의 명시적 VLM path로 지원.
- visual provider diagnostics: real provider, fixture, manual fallback, user-provided supplemental evidence를 구분해서 표시.
- human review: claim을 `accepted`, `rejected`, `needs_more_evidence`로 수동 판정.
- report templates: technical report, market report, competitor analysis, incident report.
- export: Markdown, JSON, CSV, HTML bundle.

Exit criteria:

- 새 Codex 세션에서 `$deep-research: <text-heavy question>`을 실행하면 full-runner run directory가 생성되고, 최종 응답에 `report.md`, `evidence.json`, `run_status.json`, synthesized-run `report_status.json`, applicable visual/parallel status artifacts, shard summary가 표시된다.
- `$deep-research`가 quick-chat mode로 답할 수 있는 경우는 사용자가 명시적으로 빠른 답변을 요청했을 때뿐이며, 이 경우 evidence bundle이 생성되지 않았음을 표시한다.
- 사용자가 코드 파일을 직접 열지 않고 run 상태와 evidence를 확인할 수 있다.
- 사용자가 active subagent count, queued task count, failed task count, merged shard count를 볼 수 있다.
- 사용자가 `deep` 또는 `exhaustive` 실행 전 `max_concurrent_codex_subagents`와 cost cap을 확인하고 승인할 수 있다.
- visual-required 실사용 E2E에서 사용자 제공 이미지 없이 `completed_auto_visual` 상태가 나온다.
- accepted real-use visual E2E에서 최소 10개 web visual candidate, 최소 3개 non-fixture VLM 분석 이미지, 최소 1개 report-cited visual/mixed claim을 기록한다.
- visual-required real-use E2E cannot pass if Codex-interactive analyzed image count is below the configured minimum. The run must expose `visual_minimum_shortfall` diagnostics and end as `partial_auto_visual`.
- Codex child model-capacity failures are retried with bounded backoff, classified separately from timeouts, and preserve the raw child last message. If retries exhaust with no accepted shards, the run ends as `failed_parallel_no_accepted_shards`.
- evidence browser가 source page, image artifact, VLM observation, visual verifier vote, linked claim, report citation을 한 흐름으로 보여준다.
- fixture/local/manual/user-provided visual evidence만으로는 Public Beta automatic visual gate를 통과하지 않는다.
- human review 결과가 다음 run과 후속 Codex 작업에 반영된다.
- plugin 설치/업데이트/제거 절차가 문서화된다.
- 20개 이상 실제 리서치 태스크에서 실패 유형이 분류되어 있고, 그중 8개 이상은 자동 웹 이미지 조사가 필요한 visual-required 또는 visual-optional 태스크다.

### Phase 4: Product v1

목표: 개인과 소규모 팀이 신뢰할 수 있는 리서치 제품으로 사용한다.

범위:

- 안정된 plugin 배포.
- versioned evidence schema.
- migration for old run artifacts.
- team/shared config: budget, source policy, visual policy, high-risk domain policy.
- policy guardrails: robots, paywall, copyright, PII, medical/legal/financial caveats.
- evaluation suite: known-answer benchmark, visual QA benchmark, citation correctness benchmark.
- observability: cost, latency, failure rate, verifier disagreement, VLM usage ratio.
- cache: fetched pages, image analysis, OCR, embeddings/hash.
- promotion workflow: supported + auto/human accepted evidence -> memory/playbook/skill/PRD.
- documentation: install, quickstart, config, troubleshooting, examples.

Exit criteria:

- `quick`, `standard`, `deep` preset이 문서화된 비용/시간 범위 안에서 동작한다.
- claim citation correctness benchmark가 설정된 기준을 통과한다.
- visual-required benchmark에서 visual verifier가 누락되지 않는다.
- schema migration이 이전 run artifact를 깨뜨리지 않는다.
- 사용자가 `verification_status=supported`이고 `review_status=auto_reviewed|human_accepted`인 evidence만 후속 Codex 작업에 주입할 수 있다.

### Phase 5: Team/Cloud Extension

목표: 팀 단위 반복 리서치와 장기 지식 축적을 지원한다.

범위:

- shared evidence repository.
- scheduled research runs.
- connector integration: GitHub, Linear, Notion, Google Drive, Slack.
- team approval workflow.
- role-based access for sensitive evidence.
- hosted run workers or remote execution backend.
- API server for submitting research jobs.
- dashboard-level analytics across runs.

Exit criteria:

- 팀원이 동일 evidence bundle을 조회하고 승인 상태를 공유한다.
- 반복 조사에서 기존 supported + auto/human accepted evidence를 재검증하거나 stale 처리한다.
- source policy와 budget policy가 팀 설정으로 강제된다.
- 민감 이미지와 private source가 export에 포함될지 제어할 수 있다.

## Phase Work Breakdown

### Phase 0 WBS: Prototype

Epics:

- Plugin-first scaffold
- Core runner
- Minimal agent loop
- Evidence schema v0
- Smoke evaluation

Tasks:

1. `plugins/codex-deepresearch` plugin scaffold를 최종 제품 루트로 확정한다.
2. `$deep-research` Skill skeleton과 plugin 내부 runner skeleton을 만든다.
3. `codex-deepresearch "<question>"` 개발용 CLI를 runner wrapper로 연결한다.
4. 실행하면 run directory를 생성한다.
5. schema v0 JSON Schema와 example fixture를 만든다.
6. `PlannerAgent`가 질문을 3-5개 angle로 분해하게 한다.
7. `ModalityRouterAgent`가 각 angle을 `text_only`, `visual_required`, `visual_optional`로 분류하게 한다.
8. 수동 source URL과 image URL을 입력받는 옵션을 만든다.
9. 텍스트 source에서 title, body excerpt, quote 후보를 추출한다.
10. image URL을 선택된 VLM path로 분석해 OCR, visual observation, visual claim을 저장한다.
11. `SynthesisAgent`가 최소 보고서를 생성한다.
12. 실패 시 partial `evidence.json`을 저장한다.
13. 텍스트-only 3개, visual-required 3개 smoke test fixture를 만든다.

Deliverables:

- Codex Plugin skeleton
- `$deep-research` Skill skeleton
- CLI prototype
- schema v0 JSON Schema
- `report.md`
- `evidence.json`
- smoke test fixture

### Phase 1 WBS: MVP

Epics:

- Codex Skill integration
- Personal plugin packaging
- Search/fetch/extract pipeline
- Basic visual acquisition
- VLM adapter paths
- Verification pipeline
- Budget presets

Tasks:

1. `$deep-research` Skill의 `SKILL.md`를 작성한다.
2. 개인용 Codex Plugin manifest `.codex-plugin/plugin.json`을 작성한다.
3. plugin 내부 runner를 Skill에서 호출 가능한 script로 감싼다.
4. CLI는 runner를 직접 실행하는 개발/디버깅용 wrapper로 둔다.
5. `quick`, `standard`, `deep` preset config를 구현한다.
6. `codex-plugin`, `automated-cli`, `manual-sources` 실행 모드를 구현한다.
7. Skill mode는 Codex-native search workflow를 사용하고, CLI mode는 provider abstraction을 사용하게 분리한다.
8. HTML/PDF fetcher와 본문 추출기를 구현한다.
9. high-relevance source page에서 Open Graph image와 본문 image 후보를 추출한다.
10. visual-required angle에서 image search provider가 가능하면 image search를 수행한다.
11. high-relevance source page의 first viewport screenshot을 캡처한다.
12. 수집 이미지와 스크린샷을 `images/`, `screenshots/` run artifact로 저장한다.
13. 수집 이미지를 MIME, 크기, URL 중복, basic hash로 필터링한다.
14. `VisionExtractAgent`가 선택된 VLM path로 이미지와 스크린샷을 분석하게 한다.
15. `ClaimExtractorAgent`가 quote 포함 claim을 구조화하게 한다.
16. route-specific verifier matrix에 따라 text, visual, policy/freshness verifier를 실행한다.
17. 반박 2표 이상이면 `verification_status=refuted` 처리한다.
18. Budget 초과 시 source quality, relevance, visual necessity 기준으로 pruning한다.
19. text-only route에서 이미지 검색, screenshot, VLM 호출이 발생하지 않는 테스트를 만든다.
20. visual-required route에서 자동 이미지 수집 또는 screenshot 캡처가 실행되는 테스트를 만든다.
21. visual-required route에서 visual verifier가 반드시 실행되는 테스트를 만든다.
22. Markdown report와 schema v0 evidence JSON을 Codex 후속 작업에서 읽기 쉬운 형태로 저장한다.

Deliverables:

- Codex Skill
- personal Codex Plugin
- plugin runner
- developer CLI wrapper
- basic image collector
- first viewport screenshot collector
- VLM adapter paths
- budget preset config
- route-specific verification engine
- MVP test suite

### Phase 1 MVP Vertical Slice Tickets

These tickets are the canonical MVP issue backlog. Each ticket must be independently testable and must produce or validate concrete artifacts.

#### Ticket M1: Plugin Scaffold and Manifest

Input:

- plugin root path
- plugin name `codex-deepresearch`
- Skill name `deep-research`

Output:

- `.codex-plugin/plugin.json`
- `skills/deep-research/SKILL.md`
- runner script entrypoint

Acceptance tests:

- plugin manifest validates.
- plugin appears in local/personal marketplace metadata.
- install/update/remove docs exist.
- `$deep-research: smoke test` starts a run directory.
- plugin root exists at `plugins/codex-deepresearch/`.
- repo-local marketplace metadata exists at `.agents/plugins/marketplace.json`.
- install/update smoke command exits 0.

#### Ticket M2: Execution Mode Resolver

Input:

- mode: `codex-plugin|automated-cli|manual-sources`
- provider flags
- budget preset

Output:

- normalized run config
- rejected invalid combinations

Acceptance tests:

- `codex-plugin + codex-native` is valid.
- `automated-cli + codex-native` is rejected.
- `manual-sources + external search provider` is rejected.
- invalid VLM provider for mode returns a clear error.

#### Ticket M3: Evidence Schema Validator

Input:

- `evidence.json`
- `search_results.jsonl`
- `visual_observations.jsonl`
- `verifier_votes.jsonl`

Output:

- validation pass/fail
- machine-readable validation errors

Acceptance tests:

- valid fixture passes.
- missing required state fields fails.
- dangling source/image references fail.
- high-confidence visual claim without image evidence fails.

#### Ticket M4: Codex Search Handoff Slice

Input:

- question
- search tasks
- Codex-native search results recorded as `SearchResult`

Output:

- normalized sources in evidence
- fetch queue

Acceptance tests:

- `search_results.jsonl` ingests into `sources`.
- invalid URLs are rejected with status.
- source policy flags are preserved.

#### Ticket M5: Manual Sources Slice

Input:

- user-provided URL/PDF/image URL/local image

Output:

- source records
- visual evidence records where applicable

Acceptance tests:

- manual URL creates source.
- manual image creates `VisualEvidence`.
- no external search call is made.

#### Ticket M6: Modality Router Slice

Input:

- question
- planner angles

Output:

- route per angle: `text_only|visual_required|visual_optional`
- visual task list and caps

Acceptance tests:

- API-doc question routes text-only.
- UI comparison routes visual-required.
- market report routes visual-optional.
- route is recorded in evidence.

#### Ticket M7: Fetch and Claim Extraction Slice

Input:

- normalized sources
- fetch queue
- source policy flags

Output:

- fetched source artifacts
- quote candidates
- extracted claims
- source-to-claim links

Acceptance tests:

- allowed HTML source produces title, body excerpt, quote candidates, and local artifact path.
- allowed PDF source produces text excerpt or `retrieval_status=partial` with caveat.
- blocked or failed source is preserved with `retrieval_status=failed|policy_blocked` and does not create high-confidence claims.
- every extracted claim references an existing source id.
- dangling source references fail schema validation.

#### Ticket M8: VLM Handoff and Vision Adapter Slice

Input:

- visual tasks
- local image artifacts
- selected VLM path

Output:

- `VisualEvidence` records

Acceptance tests:

- `codex-interactive` path accepts handoff observations.
- `openai-responses-vision` path emits same schema.
- `manual-visual-review` path emits same schema.
- visual-required with no visual result becomes `needs_visual_evidence`.

#### Ticket M9: Verification Matrix Slice

Input:

- extracted claims
- route
- sources/images

Output:

- `VerifierVote` records
- updated `verification_status`

Acceptance tests:

- text-only claim gets 2 text votes and 1 policy/freshness vote.
- visual-required claim gets at least 1 visual vote.
- 2 refute votes set `verification_status=refuted`.
- budget-pruned claim is excluded from final report.

#### Ticket M10: Report Generation Slice

Input:

- evidence bundle
- supported claims

Output:

- `report.md`
- image appendix
- citation/evidence mapping

Acceptance tests:

- every high-confidence text claim has quote/source.
- every high-confidence visual claim has image evidence ID.
- unsupported/refuted/policy-blocked claims are excluded or caveated.

#### Ticket M11: Guardrail Enforcement Slice

Input:

- source and visual fixtures with robots, paywall, copyright, PII, private image, and high-risk domain cases

Output:

- policy flags
- blocked evidence states
- report redaction/caveat behavior

Acceptance tests:

- login-gated or CAPTCHA-protected content is not bypassed.
- robots/paywall/copyright flags are preserved on source records.
- private user-provided images are marked `sensitive_possible` by default.
- high-risk medical/legal/financial claim without primary source cannot become high confidence.
- policy-blocked evidence cannot become `review_status=human_accepted` or `promotion_status=promoted_*`.
- generated report does not copy large copyrighted passages.
- unknown-license image cannot become `promotion_status=eligible` without `review_status=human_accepted`.

#### Ticket M12: MVP Smoke Suite

Input:

- 3 text-only fixtures
- 3 visual-required fixtures
- 2 visual-optional fixtures

Output:

- automated smoke results

Acceptance tests:

- `$deep-research` invocation completes one text-only run.
- plugin install/update smoke passes.
- text-only run performs zero VLM calls.
- visual-required run performs visual handoff and visual verifier.
- evidence validates against schema v0.
- guardrail fixture suite passes.

### Phase 2 WBS: Private Alpha

Epics:

- Advanced visual acquisition
- Parallel subagent orchestration
- Resume/retry
- Cost estimation
- Traceability

Tasks:

1. 이미지 검색 provider abstraction을 여러 provider로 확장한다.
2. 웹페이지 이미지 추출 정확도를 개선하고 favicon/thumbnail noise를 줄인다.
3. Playwright 기반 full-page, scroll, interaction screenshot capture를 구현한다.
4. 이미지 다운로드, MIME validation, size limit, content hash를 강화한다.
5. perceptual hash 기반 중복 제거와 near-duplicate clustering을 구현한다.
6. OCR 결과와 VLM visual summary를 별도 필드로 저장한다.
7. run step state machine을 도입해 중단된 run을 재개할 수 있게 한다.
8. 이미 처리한 source/image/claim은 재실행하지 않도록 cache key를 만든다.
9. 실행 전 예상 agent/source/image/token 수를 계산한다.
10. 사용자 budget cap을 넘으면 실행 전 축소안을 제시한다.
11. agent별 prompt, tool call summary, output preview를 trace로 저장한다.
12. 실패 유형을 `fetch_failed`, `vision_failed`, `verification_disagreement`, `budget_pruned` 등으로 분류한다.
13. `research_tasks.json` task queue와 task state transition validation을 구현한다.
14. Codex subagent assignment log와 `max_concurrent_codex_subagents` scheduler를 구현한다.
15. subagent별 `evidence_shard.json`, per-task search/visual JSONL, trace event linkage를 구현한다.
16. shard validation, source/image/claim dedupe, conflict marking, merge status를 구현한다.
17. failed/retryable/blocked/discarded task를 전체 run 상태와 report caveat에 반영한다.
18. subagent orchestration이 불가능한 Codex surface에서는 fallback 허용 시 serial handoff로 degrade하고 `parallel_degraded=true`를 기록한다. `--no-degrade`에서는 diagnostics와 함께 fail fast 한다.

Deliverables:

- automated image collection
- parallel research task queue
- Codex subagent scheduler
- evidence shard merger
- screenshot collector
- resumable run engine
- cost estimator
- run trace JSONL

### Phase 2 Private Alpha Tickets

These tickets extend the completed Phase 1 MVP backlog and are the canonical Private Alpha implementation backlog.

#### Ticket M13: Run Trace JSONL

Status: implemented.

Output:

- `run_trace.jsonl`
- trace writer/validator
- stage status links to trace artifacts

Acceptance tests:

- runner stages append public-safe trace records.
- failed stages record failure category.
- MVP smoke includes trace artifact paths.

#### Ticket M14: Run Step State Machine

Output:

- run step state artifact
- valid stage transition rules
- resume-safe command behavior

Acceptance tests:

- major runner stages record pending/running/completed/failed/skipped state.
- invalid transitions fail with machine-readable errors.
- interrupted runs expose the next safe stage by `run_id`.

#### Ticket M15: Cache Keys and Idempotent Resume

Output:

- source/image/claim cache keys
- idempotent resume behavior

Acceptance tests:

- completed source/image/claim units are not reprocessed unless inputs change.
- changed URL, content hash, source policy, visual evidence, or claim text invalidates the relevant cache key.

#### Ticket M16: Cost Estimator and Budget Cap Suggestions

Output:

- pre-run cost/work estimate artifact
- budget cap reduction suggestions

Acceptance tests:

- estimates include source count, image count, verifier invocation count, Codex subagent count, runner stage count, model-call placeholders, and high-water cost bounds.
- `deep` and `exhaustive` presets require explicit confirmation.

#### Ticket M17: Advanced Visual Acquisition v2

Output:

- expanded image candidate collection
- screenshot capture interface
- MIME/size/hash/near-duplicate validation

Acceptance tests:

- visual-required runs collect at least 10 candidate image records from configured local/test providers.
- text-only routes still perform zero image search, screenshot, or VLM work.

#### Ticket M18: Parallel Codex Subagent Orchestration

Input:

- question, planner angles, route decisions, budget preset
- `research_tasks.json`
- `codex exec --json` availability or Codex SDK/MCP server availability
- Codex auth, sandbox, approval policy, and `agents.max_threads` config

Output:

- bounded research task fan-out
- `subagent_assignments.jsonl`
- `evidence_shards/<task_id>/evidence_shard.json`
- per-task search/visual handoff JSONL
- normalized Codex subagent event trace from `spawn_agent`, `wait`, and `close_agent`
- `merge_status.json`
- merged canonical `evidence.json`

Acceptance tests:

- planner output can be expanded into at least 20 bounded `ResearchTask` records for a broad research question.
- automated runner can invoke Codex with `codex exec --json -c agents.max_threads=N` or Codex SDK/MCP server equivalent.
- real accepted `codex-exec` child runs execute from a trusted project context and still write outputs to the intended run directory; diagnostic repo-check bypass runs are recorded separately and do not satisfy real-use E2E acceptance.
- raw or normalized JSON events record `spawn_agent`, child thread IDs, `wait` completion states, child messages, failures, and `close_agent`.
- `standard` preset schedules up to 8 concurrent Codex subagents or equivalent worker contexts.
- `exhaustive` preset can plan up to 100 Codex subagents only after explicit confirmation and cost cap.
- every completed subagent task writes a schema-valid evidence shard before merge.
- shard merge deduplicates repeated source URLs, image hashes, and equivalent claim text.
- failed retry-safe tasks can be retried without re-running completed tasks.
- blocked or discarded tasks are preserved in `merge_status.json` and excluded from final confident claims.
- if Codex subagents, `codex exec`, SDK/MCP server, auth, sandbox, approvals, or all child shards fail, the run records an internally consistent degraded or failed state and executes serial handoff only when that fallback is explicit.

M19-M24 sequencing:

- M24 documentation/validation distinction can land at any time and should land early enough that future E2E reports label fixture vs real runs correctly.
- M19 trusted `codex-exec` context and M20 status semantics are the implementation blockers for reliable real parallel E2E.
- M21 visual evidence linkage depends on stable evidence/image IDs and should be validated before M22 claims visual report quality.
- M22 report-quality synthesis depends on verified evidence/report status from M20 and, for visual prompts, M21.
- M23 run-step state stability can be developed independently, but Phase 2 exit requires it before reporting completed visual/text E2E.

#### Ticket M19: Real Codex-Exec Trusted Context

Depends on: M18 adapter surface.

Unblocks: M20 real failure semantics, M22 real parallel report E2E, Phase 2 exit gate.

Input:

- `research_tasks.json`
- run directory under `/tmp`, repo-local `research-runs`, or user-selected `--runs-dir`
- project root path
- Codex CLI auth and trust policy

Output:

- child `codex exec` commands that run from trusted project context
- evidence shards written to the intended run directory
- actionable diagnostics when child execution is blocked

Acceptance tests:

- real-use `orchestrate-parallel --adapter codex-exec --no-degrade` produces `accepted_shards > 0` when Codex auth/runtime is available.
- trusted-directory failures include the exact command context and remediation.
- regression coverage fails if child execution uses an untrusted run directory as the Codex working root.
- repo-check bypass is allowed only for explicit diagnostic runs, records `repo_check_bypass_used=true`, constrains output to the selected run directory, and cannot satisfy real-use E2E acceptance.

#### Ticket M20: Parallel Status and Diagnostics Semantics

Depends on: M19 for real `codex-exec` failure reproduction.

Unblocks: honest user-facing run status, M22 report status, Phase 2 exit gate.

Input:

- `research_tasks.json`
- `subagent_assignments.jsonl`
- child stdout/stderr or normalized event stream
- `merge_status.json`

Output:

- internally consistent `parallel_orchestration_status.json`
- failure counts by category
- preserved diagnostics sufficient to debug child failures

Acceptance tests:

- all-child-failed runs do not report successful parallel execution.
- `parallel_degraded`, `status`, and `needs_serial_handoff` agree.
- implementation matches the PRD parallel status matrix for fixture success, real partial/full success, adapter unavailable, all-child-failed, trust/sandbox/auth blocker, and completed serial handoff.
- `run-status` reports the same state as `parallel_orchestration_status.json`.
- tests cover all-child-failed, partial-shard, adapter-unavailable, and fixture-success cases.

#### Ticket M21: Visual Evidence to Claim and Report Linkage

Depends on: stable evidence schema image IDs and deterministic visual observation references.

Unblocks: M22 visual-required report quality and visual Phase 2 exit gate.

Input:

- `visual_candidates.jsonl`
- `visual_observations.jsonl`
- `evidence.json.images`
- visual or mixed claims

Output:

- claim `supporting_images`
- visual verifier eligibility
- `report.md` visual findings
- `report_status.used_images`

Linkage contract:

- A claim may include an image in `supporting_images` only when a `VisualEvidence` record references the image ID and includes an observation or OCR span relevant to the claim text.
- The claim must store the image ID, deterministic observation reference/index, relation type (`ocr_support`, `visual_match`, `chart_support`, `screenshot_support`, or `context_support`), provider, rationale, and visual confidence.
- Visual verifier eligibility requires at least one linked observation that is not policy-blocked and whose provider is recorded as real VLM, Codex interactive handoff, manual visual review, local screenshot fixture, local image fixture, or fixture.
- `report.md` may count an image as used only when a report claim cites that image-backed claim or its evidence ID.

Acceptance tests:

- visual observations can support claim `supporting_images`.
- visual-required claims with usable image evidence can become report-eligible after verification.
- visual-required real-use E2E writes `report_status.used_images > 0`.
- evidence records whether image analysis came from real VLM, Codex interactive handoff, manual visual review, or fixture provider.

#### Ticket M22: User-Shaped Final Report Synthesis

Depends on: M20 status semantics for report status. Visual-required report quality also depends on M21.

Unblocks: user-facing Phase 2 real-use acceptance.

Input:

- verified evidence
- user question and requested output shape
- report template hints

Output:

- user-facing `report.md`
- structured report status explaining included and excluded evidence

Acceptance tests:

- Korean prompts produce Korean reports unless the user asks otherwise.
- comparison prompts produce comparison tables.
- recommendation prompts include direct answer, evidence, caveats, and gaps.
- boilerplate headings and navigation text do not dominate final synthesis.
- real-use technical adoption and competitor-comparison E2E reports pass the PRD report quality gate with score `>=9/10`.

#### Ticket M23: Run-Step State Stability After Visual/Text Reruns

Depends on: existing run-step state machine.

Unblocks: completed-state Phase 2 real-use visual/text E2E.

Input:

- completed `ingest_vision`
- later `fetch-claims`, guardrail, verification, and synthesis stages
- `run_steps.json`

Output:

- completed run status after full visual/text pipeline
- idempotent rerun history without corrupting primary stage status

Acceptance tests:

- sequence `prepare -> acquire-visual -> ingest-vision -> fetch-claims -> enforce-guardrails -> verify-claims -> synthesize -> run-status` ends completed.
- later stages do not stale-reset earlier completed stages unless the upstream input hash changed.
- reruns record skipped/rerun decisions in history without hiding the completed primary state.

#### Ticket M24: Fixture vs Real E2E Validation Distinction

Depends on: existing validation docs and runner status output.

Unblocks: reliable interpretation of M19-M23 E2E results.

Input:

- fixture adapter validation runs
- real `codex-exec` E2E runs
- validation/reporting docs

Output:

- docs and validation output that identify adapter type and evidence source
- real E2E checklist distinct from fixture merge checks
- `parallel_orchestration_status.json` and `merge_status.json` include an `evidence_source` object with `type`, `adapter`, `accepted_shards`, `fixture_only`, `manual_handoff`, `attempted_real_child_execution`, `real_child_execution`, and `real_use_e2e_eligible`.
- Zero accepted `codex-exec` shards are labeled `evidence_source.type=failed_real_child_execution`, not `real_child_execution`.
- Failed and retryable child task diagnostics are preserved in `merge_status.json.failed_tasks`, `failure_counts`, and `diagnostics`.
- `manual_ingest_status.json` includes `evidence_source.type=manual_handoff`.

Real-use E2E checklist:

- Run `orchestrate-parallel --adapter codex-exec --no-degrade` against a prepared real-use run when Codex auth/runtime is available.
- Require `adapter=codex-exec`, `evidence_source.type=real_child_execution`, and `accepted_shards > 0`.
- Require `--no-degrade` zero-shard real child runs to return `ok=false` and `status=failed_parallel_no_accepted_shards`.
- Treat `adapter=fixture`, `evidence_source.fixture_only=true`, or deterministic `example.com` fixture evidence as fixture-only validation, never as real-use E2E success.
- If the run degrades or fails, report `status`, `ok`, `parallel_degraded`, `needs_serial_handoff`, `degraded_reason`, `evidence_source`, `failure_counts`, `diagnostics`, and failed/rejected/blocked task diagnostics from `parallel_orchestration_status.json` and `merge_status.json`.
- Interpret the result against the PRD Parallel status matrix and Report quality gate before claiming Phase 2 readiness.

Acceptance tests:

- fixture tests remain documented as no-network merge mechanics tests.
- real-use E2E requires `adapter=codex-exec` and `accepted_shards > 0` unless the run explicitly degrades or fails with actionable diagnostics.
- validation output highlights whether evidence came from fixture, manual handoff, or real child execution.
- fake-available `codex-exec --no-degrade` child failures with `accepted_shards=0` fail unambiguously and preserve task-level diagnostics.
- fixture success cannot satisfy Phase 2 real-use `codex-exec` acceptance.

### Phase 3 WBS: Public Beta

Epics:

- Product UX
- Parallel research UX
- Automatic web visual research
- Evidence review
- Report templates
- Install/update flow

Tasks:

1. `$deep-research` skill invocation router를 만든다. 기본 mode는 `full-runner`이고, `quick-chat`은 명시 요청이 있을 때만 허용한다.
2. fresh-session skill invocation이 `prepare -> orchestrate-parallel -> guardrails -> verify -> synthesize`를 끝까지 실행하거나 explicit blocked status로 종료하게 한다.
3. 최종 assistant response가 run directory, `report.md`, `evidence.json`, `run_status.json`, synthesized-run `report_status.json`, applicable visual/parallel status artifacts, shard summary, fallback/degradation 여부를 항상 표시하게 한다.
4. chat-only, fixture-only, manual-only, serial fallback, real parallel provenance를 최종 응답과 `run_status.json` 및 applicable status artifacts에서 구분한다.
5. `$deep-research` fresh-session E2E transcript gate를 추가한다. 성공처럼 보이는 응답에 run artifact path, `run_status.json`, 또는 synthesis 이후 `report_status.json`이 없으면 실패한다.
6. run list, run detail, claim detail을 볼 수 있는 TUI 또는 lightweight web dashboard를 만든다.
7. 진행 중 run의 phase, active/queued/failed/merged task count, Codex subagent count, source count, image count를 표시한다.
8. pause/resume/cancel control을 구현한다.
9. `max_concurrent_codex_subagents`, `max-cost-usd`, preset confirmation UI를 구현한다.
10. source/image/claim/vote를 연결해서 탐색하는 evidence browser를 만든다.
11. claim review 상태 `accepted`, `rejected`, `needs_more_evidence`를 저장한다.
12. review 결과가 후속 synthesis와 Codex reuse에 반영되게 한다.
13. technical report, market report, competitor analysis, incident report template을 만든다.
14. Markdown, JSON, CSV, HTML bundle export를 구현한다.
15. plugin 설치, 업데이트, 제거 절차를 문서화한다.
16. 실제 리서치 태스크 20개 이상을 실행하고 실패 유형을 수집한다.
17. onboarding quickstart와 example gallery를 만든다.
18. `visual_search_plan.json`, `visual_candidates.jsonl`, `image_fetch_status.jsonl`, `visual_provider_status.json` artifact를 구현한다.
19. real web/image search provider adapter를 구현하고, provider별 결과를 `visual_candidates.jsonl`로 정규화한다.
20. 웹페이지 Open Graph image, 본문 image, `srcset`, lazy-loaded image, caption, alt text, 주변 문맥 추출기를 구현한다.
21. 원격 이미지 fetch/cache layer를 구현하고 MIME, size, hash, perceptual hash, local artifact path, policy metadata를 기록한다.
22. Playwright 또는 equivalent browser automation 기반 first-viewport/full-page/scroll screenshot collector를 구현한다.
23. 논문/보고서 PDF page 또는 figure rasterization provider를 구현한다. CAPTCHA, 로그인, paywall 우회는 비목표로 유지한다.
24. `openai-responses-vision` automated adapter가 image URL, local artifact, screenshot, PDF page image를 분석해 `visual_observations.jsonl`을 생성하게 한다.
25. VisualVerifierAgent가 VLM observation을 claim `visual_supports[]`, visual verifier vote, report citation으로 연결하게 한다.
26. `completed_auto_visual`, `partial_auto_visual`, `blocked_missing_visual_provider`, `blocked_missing_vlm_provider`, `policy_blocked_visual`, `budget_pruned_visual` 상태를 `run_status.json`, `visual_provider_status.json`, dashboard에 표시한다.
27. fixture/manual/user-provided-only visual runs와 real automatic web visual runs를 validation에서 구분한다.
28. codex-plugin interactive visual E2E suite를 만든다. fresh-session `$deep-research`가 Codex-native search/VLM handoff artifacts를 채우고 hidden API 없이 `completed_auto_visual`에 도달하는지 검증한다. explicit blocked terminal status는 진단으로 기록하지만 release-gate pass로 계산하지 않는다.
29. automated-cli real provider visual E2E suite를 만든다. no-user-image run이 real provider acquisition과 `openai-responses-vision`을 사용해 제품 이미지 비교, UI screenshot 비교, 뉴스/시장 차트 판독, 논문 figure 판독을 통과해야 한다.
30. automatic visual E2E 실패 유형을 provider failure, fetch failure, policy block, VLM failure, visual contradiction, report linkage failure로 분류한다.
31. Codex-native local visual worker path를 full-runner에 연결한다. 이미 확보된 image/screenshot/PDF-render artifact를 `codex exec --json --image <artifact>`로 분석하고, observation lineage를 `evidence.json`, `visual_provider_status.json`, `run_trace.jsonl`, report citation까지 보존한다. 이 항목은 P3-AV10 / #97로 추적한다.
32. Full-runner automatic web visual integration을 구현한다. 사용자 제공 이미지 없이 Codex-native search handoff와 page image extraction, image fetch/cache, screenshot capture, PDF figure/page rasterization 중 가능한 acquisition path를 실행하고, 확보한 local artifact를 P3-AV10 Codex VLM worker에 넘겨 `completed_auto_visual` 또는 명확한 blocked terminal status에 도달하게 한다. 이 항목은 P3-AV11 / #99로 추적한다.

Deliverables:

- skill invocation router
- fresh-session full-runner E2E gate
- final response artifact handoff
- codex-plugin interactive visual E2E gate
- automated-cli real provider visual E2E gate
- dashboard/TUI
- parallel subagent progress monitor
- automatic web visual research pipeline
- full-runner automatic web visual integration from web discovery through Codex VLM report citation
- real visual provider diagnostics
- evidence browser
- human review workflow
- report templates
- export bundle
- beta documentation

Phase 3 product UX issue candidates and ordering:

| Issue candidate | Scope | Depends on | Can run in parallel with |
| --- | --- | --- | --- |
| P3-UX1 Skill full-runner invocation router | Update the Skill/runner handoff so `$deep-research` defaults to `full-runner`, allows `quick-chat` only by explicit request, and records mode/provenance in `run_status.json` plus applicable visual/parallel status artifacts. | Phase 2 real `codex-exec` orchestration and run-step state | P3-AV1 |
| P3-UX2 Final response artifact handoff | Ensure successful or blocked skill runs always return run directory, `report.md`, `evidence.json`, `run_status.json`, `report_status.json` when synthesized, shard counts, fallback/degradation state, and key diagnostics. | P3-UX1 | P3-AV1, P3-AV2 |
| P3-UX3 Fresh-session skill E2E gate | Add a scripted/transcript E2E that invokes the installed skill in a fresh Codex session and fails if a successful-looking response lacks runner artifacts, `run_status.json`, or `report_status.json` after synthesis. | P3-UX2 | P3-AV2, P3-AV3, P3-AV4, P3-AV5 |
| P3-UX4 Progress and shard monitor shell | Show active, queued, failed, accepted, merged, retried shard counts plus stage and run id in TUI or lightweight dashboard. | P3-UX2 | P3-AV2, P3-AV3, P3-AV4, P3-AV5 |
| P3-UX5 Pause/resume/cancel controls | Add user controls that operate on the same run-state model used by full-runner skill invocations. | P3-UX4 | Evidence browser UX |

Phase 3 automatic visual issue candidates and ordering:

| Issue candidate | Scope | Depends on | Can run in parallel with |
| --- | --- | --- | --- |
| P3-AV1 Visual artifact schema and status states | Implement `visual_search_plan.json`, `visual_candidates.jsonl`, `image_fetch_status.jsonl`, `visual_provider_status.json`, automatic visual run states, and validation fixtures. | Phase 2 stable evidence schema and run-step state | Product UX shell |
| P3-AV2 Real image search provider adapter | Add one real web/image search provider, candidate normalization, provider diagnostics, cost counters, and no-fixture provenance checks. | P3-AV1 | P3-AV3, P3-AV4, P3-AV5 |
| P3-AV3 Page image extraction and fetch/cache | Extract Open Graph/body/srcset/lazy/captioned images, fetch allowed images, record MIME/size/hash/policy/local path, and dedupe. | P3-AV1 | P3-AV2, P3-AV4, P3-AV5 |
| P3-AV4 Browser screenshot collector | Capture allowed first-viewport/full-page/scroll screenshots with viewport/capture metadata and policy state. | P3-AV1 | P3-AV2, P3-AV3, P3-AV5 |
| P3-AV5 PDF figure/page rasterizer | Rasterize allowed PDF pages/figures and record page/figure provenance. | P3-AV1 | P3-AV2, P3-AV3, P3-AV4 |
| P3-AV6 Automated VLM adapter | Implement `openai-responses-vision` analysis for image URL/local image/screenshot/PDF page image and emit validated `visual_observations.jsonl`. | P3-AV2 or P3-AV3 or P3-AV4 or P3-AV5 | Evidence browser UX |
| P3-AV7 Visual verifier and report linkage | Convert VLM observations into `visual_supports[]`, visual verifier votes, image appendix entries, report citations, and evidence browser links. | P3-AV6 | Report templates |
| P3-AV8 Codex-plugin interactive visual E2E gate | Add fresh-session `$deep-research` visual prompts that prove Codex-native search/VLM handoff artifacts are filled, ingested, and reported without hidden API assumptions. | P3-UX3, P3-AV7 | Install/update docs |
| P3-AV9 Automated-cli real provider visual E2E gate | Add no-user-image automated CLI prompts using real provider acquisition plus `openai-responses-vision`; enforce provider scenario gates, `completed_auto_visual`, 10 candidates for image-centric prompts, 3 VLM analyzed images, and 1 report-cited visual/mixed claim. | P3-AV7 | Install/update docs |
| P3-AV10 Codex-native VLM child runner | Add the Codex-native image worker path for already-fetched local visual artifacts, using explicit `codex exec --json --image <artifact>` handoff and preserving visual observations, provider status, run trace, and report citation lineage. This is tracked by #97 and is necessary but not sufficient for complete automatic web visual research. | P3-AV7, P3-AV8 | P3-AV11 planning and docs |
| P3-AV11 Full-runner automatic web visual integration | Connect Codex-native search/page/PDF/screenshot visual acquisition to P3-AV10 so a fresh visual-required `$deep-research` run can automatically discover web images/charts/figures, fetch or capture allowed local artifacts, run Codex VLM analysis, verify visual claims, and cite image evidence in `report.md` without user-provided images. This is tracked by #99. | P3-AV2, P3-AV3, P3-AV4, P3-AV5, P3-AV7, P3-AV8, P3-AV10 | Follow-up docs and release validation |
| P3-AV12 Visual fetch/VLM minimum stabilization | Stabilize visual candidate selection, fetch replacement, and Codex-interactive VLM handoff so visual-required runs satisfy the 3 analyzed-image minimum or end with explicit `visual_minimum_shortfall` diagnostics in `partial_auto_visual`. This is tracked by #105. | P3-AV11, PR #104 diagnostics | P3-RUN1 |

Phase 3 runner hardening issue candidates and ordering:

| Issue candidate | Scope | Depends on | Can run in parallel with |
| --- | --- | --- | --- |
| P3-RUN1 Codex-exec child capacity retry/backoff | Detect Codex child model-capacity responses from raw diagnostics, classify them with `timeout=false`, retry with bounded exponential backoff and jitter, and fail as `failed_parallel_no_accepted_shards` when retries exhaust with no accepted shards. This is tracked by #106. | PR #104 timeout diagnostics and real `codex-exec` orchestration | P3-AV12 |

Wave 7 implementation acceptance:

- P3-AV12 passes when deterministic visual acquisition tests show top-up from failed candidates to at least 3 fetched artifacts, deterministic Codex-interactive worker tests analyze at least 3 artifacts while preserving non-fixture provider provenance in the output records, text-only routes still perform zero visual work, and a sanitized real-use or sanitized-real-artifact visual-required run reaches `completed_auto_visual` with `minimums.satisfied=true`. Fixture-only runs may validate mechanics but cannot satisfy this positive acceptance gate.
- P3-AV12 negative acceptance: if only 1-2 non-fixture Codex-interactive images are analyzed, the run must remain `partial_auto_visual`, `ok=false`, with `diagnostics.failure_code=visual_minimum_shortfall` and `visual_provider_status.json.minimums.shortfall_reason` set.
- P3-AV12 negative acceptance also covers report linkage: if at least 3 non-fixture Codex-interactive images are analyzed but `report_cited_images=0`, the run must remain `partial_auto_visual`, `ok=false`, with `diagnostics.failure_code=visual_report_linkage_missing` and `shortfall_reason=report_linkage_missing`.
- P3-RUN1 passes when deterministic runner tests cover capacity recovery, non-retry auth/sandbox/quota/policy/schema failures, retry exhaustion, bounded zero-delay test backoff, per-attempt raw diagnostic preservation, and final `merge_status.json` / `parallel_orchestration_status.json` retry summaries.
- Wave 7 real-use validation should rerun an Apollo-style public image prompt after both issues land. If Codex model capacity does not occur during validation, deterministic capacity fixtures remain the acceptance source; do not wait for a flaky external capacity event to prove P3-RUN1.

Safe development waves:

- Wave 1: P3-UX1 and P3-AV1. P3-UX1 closes the product invocation gap; P3-AV1 defines the automatic visual state/schema contract.
- Wave 2: P3-UX2, P3-UX3, P3-AV2, P3-AV3, P3-AV4, and P3-AV5 can proceed after their Wave 1 dependencies because UX artifact handoff and visual acquisition providers write separate normalized artifacts.
- Wave 3: P3-UX4 and P3-AV6. P3-UX4 consumes `run_status.json` plus applicable visual/parallel status artifacts; P3-AV6 depends on at least one real image artifact path from Wave 2.
- Wave 4: P3-UX5 and P3-AV7. P3-UX5 extends run controls; P3-AV7 depends on validated VLM observations from P3-AV6.
- Wave 5: P3-AV8 and P3-AV9 are release gates. P3-AV8 cannot pass until P3-UX3 and P3-AV7 are complete. P3-AV9 cannot pass until P3-AV2, P3-AV3, P3-AV4, P3-AV5, P3-AV6, and P3-AV7 are complete.
- Wave 6: P3-AV10 and P3-AV11 are post-gate hardening issues discovered by real installed-plugin testing. P3-AV10 / #97 closes the Codex-native local artifact VLM worker gap. P3-AV11 / #99 is the next safe implementation wave and cannot be accepted until it proves the complete no-user-image web discovery -> local artifact acquisition -> Codex VLM -> visual verifier -> report citation path in the default full-runner UX.
- Wave 7: P3-RUN1 / #106 and P3-AV12 / #105 are post-P3-AV11 real-use hardening issues. They may proceed in parallel because P3-RUN1 owns child execution retry/backoff while P3-AV12 owns visual fetch/VLM minimum stability. Final visual-minimum acceptance requires both model-capacity and visual-minimum failure classifications to be stable.

### Phase 4 WBS: Product v1

Epics:

- Stable schema and migration
- Policy guardrails
- Evaluation and observability
- Promotion workflow
- Documentation hardening

Tasks:

1. `evidence.json` schema에 version을 부여한다.
2. 이전 artifact를 새 schema로 변환하는 migration을 구현한다.
3. source policy, visual policy, high-risk domain policy config를 만든다.
4. robots, paywall, copyright, PII 처리 규칙을 policy layer로 분리한다.
5. medical/legal/financial caveat enforcement를 구현한다.
6. known-answer benchmark를 만든다.
7. visual QA benchmark를 만든다.
8. citation correctness benchmark를 만든다.
9. cost, latency, failure rate, verifier disagreement, VLM usage ratio metric을 수집한다.
10. fetched pages, OCR, image analysis, embeddings/hash cache를 구현한다.
11. supported + auto/human accepted evidence를 memory/playbook/skill/PRD로 승격하는 workflow를 만든다.
12. install, quickstart, config, troubleshooting, examples 문서를 완성한다.
13. plugin release checklist와 changelog 프로세스를 만든다.

Deliverables:

- stable evidence schema
- migration tool
- policy config
- evaluation suite
- observability dashboard
- promotion workflow
- v1 docs

### Phase 5 WBS: Team/Cloud Extension

Epics:

- Shared repository
- Scheduled runs
- Team approval
- Connector integration
- Remote execution

Tasks:

1. shared evidence repository schema를 설계한다.
2. run owner, reviewer, approver 역할을 정의한다.
3. team budget/source/visual policy를 설정으로 강제한다.
4. scheduled research run을 등록/수정/중지할 수 있게 한다.
5. stale evidence detection과 재검증 workflow를 만든다.
6. GitHub, Linear, Notion, Google Drive, Slack connector ingestion을 구현한다.
7. sensitive evidence에 대한 access control을 구현한다.
8. export 시 private source와 민감 이미지 포함 여부를 제어한다.
9. hosted worker 또는 remote execution backend를 설계한다.
10. research job submit/status/result API를 구현한다.
11. org-level analytics dashboard를 만든다.
12. team onboarding과 admin guide를 작성한다.

Deliverables:

- shared evidence backend
- scheduled run service
- approval workflow
- connector ingestion
- remote worker/API
- org analytics
- admin documentation

## Release Capability Matrix

| Capability | Prototype | MVP | Private Alpha | Public Beta | Product v1 | Team/Cloud |
| --- | --- | --- | --- | --- | --- | --- |
| Codex Plugin | Skeleton | Personal | Personal | Marketplace-ready | Stable | Team-managed |
| Codex Skill | Skeleton | Yes | Yes | Yes | Yes | Yes |
| CLI 실행 | Dev wrapper | Dev wrapper | Yes | Yes | Yes | Yes |
| 텍스트 검색/fetch | Basic | Yes | Yes | Yes | Yes | Yes |
| 이미지 URL 분석 | Basic | Yes | Yes | Yes | Yes | Yes |
| 자동 이미지 검색 | No | Basic | Provider beta | Yes | Yes | Yes |
| 스크린샷 수집 | No | Basic | Provider beta | Yes | Yes | Yes |
| 자동 웹 이미지 조사 E2E | No | No | Partial | Yes | Stable | Team-managed |
| PDF/논문 figure 자동 판독 | No | No | Prototype | Basic | Yes | Yes |
| ModalityRouter | Basic | Yes | Yes | Yes | Yes | Policy-aware |
| Agent budget | Manual | Presets | Cost estimator | User controls | Policy controls | Team controls |
| Evidence 저장 | Schema v0 JSON/MD | Schema v0 JSON/MD | Versioned draft | Browsable | Versioned stable | Shared |
| Human review | No | File edit | Basic | Review UI | Promotion workflow | Team approval |
| 재개/resume | No | No | Yes | Yes | Yes | Yes |
| Observability | Logs | Run stats | Trace | Dashboard | Metrics suite | Org analytics |

## Product-Level Acceptance Criteria

- 최종 제품은 Codex Plugin으로 설치되고, 사용자는 Codex 안에서 plugin을 통해 DeepResearch를 시작한다.
- 사용자는 Codex 안에서 `$deep-research`를 호출해 별도 설명 없이 리서치를 시작할 수 있다.
- CLI는 같은 엔진을 실행할 수 있지만, 독립 제품이 아니라 plugin runner의 개발/자동화용 보조 표면이다.
- 모든 run은 schema v0 이상을 따르는 재사용 가능한 `evidence.json`을 남긴다.
- plugin manifest `.codex-plugin/plugin.json`이 validation을 통과한다.
- personal marketplace metadata가 존재하고 install/update/remove 절차가 문서화되어 있다.
- `$deep-research: smoke test`가 Codex 세션에서 run directory, `report.md`, `evidence.json`, `run_status.json`, 그리고 synthesis가 성공한 run의 `report_status.json`을 생성한다.
- `codex-plugin` mode의 search handoff와 VLM handoff artifact가 schema validation을 통과한다.
- `verification_status`, `review_status`, `promotion_status`가 모든 claim에 존재한다.
- `SearchResult`, `VisualEvidence`, `VerifierVote` adapter records가 schema v0에 맞게 validate된다.
- text-only 작업은 VLM 비용을 쓰지 않는다.
- visual-required 작업은 VLM 분석과 visual verifier를 생략하지 않는다.
- Public Beta visual-required 작업은 사용자 제공 이미지 없이 Codex-native search handoff와 `codex-interactive` VLM handoff artifacts를 통해 `completed_auto_visual` 상태에 도달해야 한다. automated-cli real provider E2E와 `openai-responses-vision`은 별도 재현성/diagnostic gate로 유지하되 Codex-native completion의 필수 조건은 아니다.
- Public Beta automatic visual gate는 fixture, local test provider, manual review, user-provided-only image evidence만으로 통과할 수 없다.
- 최종 보고서의 모든 high-confidence claim은 quote 또는 image evidence를 가진다.
- high-risk domain claim은 primary source 또는 caveat 없이는 high confidence가 될 수 없다.
- 사용자는 `verification_status=supported`이고 `review_status=auto_reviewed|human_accepted`인 evidence만 memory/playbook/skill/PRD로 승격할 수 있다.
- MVP guardrail 위반 evidence는 `review_status=human_accepted` 또는 `promotion_status=promoted_*` 상태가 될 수 없다.
- 제품은 중단, 실패, 재시도, 부분 결과 저장을 정상적인 상태로 다룬다.

## 비목표

- Claude Code 워크플로우 코드 복사.
- Codex core built-in slash command 수정.
- 완전 자동 사실판정 보장.
- CAPTCHA/로그인 뒤 콘텐츠 우회.
- 의료 영상, 법적 판단, 금융 조언의 단독 자동 결론.

## 기술 선택

- Product surface: Codex Plugin + Codex Skill. CLI는 plugin runner의 개발/테스트/자동화용 wrapper다.
- Orchestration: 제품 UX는 Codex Plugin + Skill이다. Phase 2 M18의 첫 병렬 구현은 automated runner adapter를 사용한다. 이 adapter는 `codex exec --json -c agents.max_threads=N` 또는 Codex SDK/MCP server equivalent로 Codex subagent workflow를 실행하고 JSON event stream을 추적한다. 나중에 Codex가 plugin-internal subagent API를 제공하면 별도 adapter로 추가한다.
- Model/API: OpenAI Responses API. `automated-cli` 또는 자동 VLM adapter가 필요할 때 사용한다. 공식 vision 문서는 이미지 입력을 URL, Base64 data URL, file ID로 받을 수 있고 여러 이미지를 한 요청에 넣을 수 있다고 설명한다.
- VLM adapters: `codex-interactive`, `openai-responses-vision`, `manual-visual-review`.
- Storage: JSONL + Markdown, 나중에 SQLite로 확장.
- Image processing: perceptual hash, EXIF 추출, screenshot 캡처, OCR/VLM 결과 병합.
- Web visual acquisition: Codex-native search handoff, page image extractor, browser screenshot collector, PDF figure/page rasterizer, image fetch/cache, visual candidate ranker를 분리된 adapter로 구현한다. Public Beta automatic visual E2E의 기본 completion path는 Codex-native search artifacts와 `codex-interactive` VLM handoff records이며, external provider/API path는 automated-cli diagnostic으로 별도 검증한다.

## Search Provider Modes and Cost Model

Codex Plugin의 search workflow와 자동 CLI의 search provider는 분리한다.

Product-first decision:

- 최종 제품은 Codex Plugin이다.
- MVP의 기본 실행 모드는 `codex-plugin`이고, 기본 검색 경로는 Codex 세션 안의 Codex-native search workflow다.
- `codex-plugin` mode에서는 Codex agent가 현재 세션의 web search 기능을 사용해 텍스트 검색과 출처 확인을 수행하고, DeepResearch runner는 evidence schema, claim verification, VLM routing, 보고서 구조화를 담당한다.
- `automated-cli` mode에서 재현 가능한 자동 검색이 필요할 때만 OpenAI hosted web search 또는 외부 search API provider를 사용한다.
- `manual-sources` mode는 Phase 0, 저비용 검증, 민감 source 검토의 fallback이다.

Codex product behavior:

- Codex CLI는 `web_search = "cached"`를 기본값으로 사용한다.
- cached mode는 OpenAI가 유지하는 web search cache/index에서 결과를 가져오며 live page를 직접 fetch하지 않는다.
- `web_search = "live"` 또는 `--search`는 최신 웹 데이터를 가져오는 live browsing 성격이다.
- 이 Codex 내장 search tool은 Codex 제품 표면이며, Codex DeepResearch 구현이 안정적으로 의존할 공개 library API로 취급하지 않는다.

Mode-specific provider policy:

| Execution mode | Default search provider | Allowed alternatives | Notes |
| --- | --- | --- | --- |
| `codex-plugin` | `codex-native` | `manual` | Plugin MVP 기본값. Codex 세션 품질과 사용자의 승인 흐름을 우선한다. |
| `automated-cli` | `openai` 또는 `manual` | `brave`, `tavily`, `serpapi` | 재현 가능한 batch run, CI smoke, provider 비교에 사용한다. |
| `manual-sources` | `manual` | 없음 | 사용자가 제공한 URL/PDF/image만 처리한다. |

Visual provider policy:

| Visual acquisition path | Public Beta role | Must record |
| --- | --- | --- |
| web image search provider | visual-required/optional automatic image candidate discovery | query, provider, result rank, image URL, source page URL, usage policy |
| page image extractor | representative, Open Graph, body, captioned, and lazy-loaded image discovery | page URL, DOM/source context, alt/caption/surrounding text, rejection reason |
| browser screenshot collector | first-viewport/full-page/scroll/interaction screenshots when allowed | page URL, viewport, capture mode, local path, policy state |
| PDF figure/page rasterizer | academic paper/report figure and page image candidates | PDF URL/path, page number, figure hint, rasterized artifact path |
| image fetch/cache | local artifact creation and dedupe | MIME, byte size, hash, perceptual hash, cache key, policy flags |
| VLM adapter | OCR, chart reading, object/layout interpretation, image-claim alignment | provider, model or handoff path, observation, inference, caveats, cost metadata |

Provider options:

0. Codex-native search mode
   - `codex-plugin` MVP의 기본값.
   - Codex 세션 안에서 Codex의 web search 기능을 사용한다.
   - 비용: 별도 외부 검색 API 비용은 없다. 단, Codex 사용량/모델 토큰/VLM 분석 비용은 사용 환경의 과금 정책을 따른다.
   - 장점: 설정이 거의 없고, Codex의 현재 검색 품질을 그대로 활용할 수 있다.
   - 단점: `automated-cli`나 백그라운드 worker에서 같은 검색 결과를 API처럼 재현하기 어렵다.
   - 적용: text-only 태스크, 빠른 개인 조사, Codex 대화 안에서 바로 실행하는 리서치.

1. OpenAI hosted web search via Responses API
   - `automated-cli`의 기본 provider 후보.
   - 장점: Responses API/Agents SDK tool orchestration과 잘 맞고 별도 검색 provider 계정이 필요 없다.
   - 비용: OpenAI API pricing 기준 web search는 별도 tool call 과금 대상이다. 구체 가격은 구현 시점의 provider pricing config에서 계산한다.
   - 별도 과금: 검색 결과를 읽고 판단하는 model input/output token, VLM image input token, verifier/synthesis token은 여전히 과금된다.

2. External search API provider
   - Brave Search, Tavily, SerpAPI, Bing/Google CSE 등으로 교체 가능한 adapter를 둔다.
   - 비용: provider별 query 과금 + 결과 처리용 model token 비용.
   - 장점: 이미지 검색, 특정 지역/언어/뉴스/쇼핑 검색 등 provider별 기능을 선택할 수 있다.
   - 단점: provider별 rate limit, 약관, 결과 품질 차이가 크다.

3. User-provided sources only
   - 사용자가 URL, PDF, image URL을 직접 제공하는 low-cost mode.
   - 비용: 검색 API 비용은 0에 가깝지만 fetch, parsing, model/VLM 분석 비용은 발생한다.
   - Prototype과 MVP의 fallback mode로 유지한다.

Cost accounting:

```text
total_cost =
  search_call_cost
+ fetch/compute/runtime_cost
+ codex_subagent_runtime_cost
+ codex_cli_or_sdk_orchestration_overhead
+ text_model_input_output_cost
+ image_input_token_cost
+ verifier_agent_cost
+ synthesis_agent_cost
```

Budget controls:

- `--max-search-calls`
- `--max-sources`
- `--max-visual-candidates`
- `--max-images`
- `--max-screenshots`
- `--max-pdf-pages`
- `--max-subagents`
- `--max-agents`
- `--max-cost-usd`
- `--codex-runner codex-exec|codex-sdk|serial`
- `--provider codex-native|openai|brave|tavily|serpapi|manual`

MVP policy:

- MVP 기본 제품 모드는 `codex-plugin`이고 기본 검색 경로는 `codex-native`다.
- CLI나 재현 가능한 batch run에서는 provider abstraction을 통해 `openai`, `brave`, `tavily`, `serpapi`, `manual` 중 선택한다.
- 비용이 가장 예측 가능한 자동 실행 기본값은 OpenAI hosted web search 또는 user-provided sources mode다.
- 실행 전 예상 search calls, model calls, image analyses, upper-bound cost를 표시한다.
- `text_only` route는 이미지 검색과 VLM 비용을 쓰지 않는다.
- `visual_optional` route는 budget이 부족하면 이미지 검색을 생략한다.

Public Beta automatic visual policy:

- visual-required route는 configured real visual provider가 없으면 `blocked_missing_visual_provider`로 종료한다.
- visual-required route에서 visual artifacts가 있지만 VLM path가 없으면 `blocked_missing_vlm_provider`로 종료한다.
- provider가 fixture/local/manual/user-provided-only이면 `completed_auto_visual`이 될 수 없다.
- copyright, robots, paywall, PII, sensitive image, high-risk domain policy가 image artifact 단위로 기록되지 않으면 해당 image는 supported claim에 연결할 수 없다.
- 비용 상한을 초과한 visual candidate, screenshot, PDF page, VLM call은 작업 단위 pruning reason으로 기록한다. Run-level terminal status는 앞의 automatic visual terminal status precedence를 따른다.

## Success Metrics and Phase Thresholds

Metric denominators:

- Product-Level Acceptance Criteria는 release gate다. gate 항목은 해당 release에서 100% 통과해야 한다.
- 아래 phase metric은 `policy_blocked`, `needs_manual_review`, `blocked_missing_search_handoff`, `blocked_missing_visual_provider`, `blocked_missing_vlm_provider`, `policy_blocked_visual`로 정상 차단된 run을 제외한 completed non-blocked runs 기준이다.
- blocked run은 실패가 아니라 별도 `blocked_*` 상태로 집계하지만, Public Beta에서 필수 provider/scenario gate를 검증하는 run이 `blocked_*`로 끝나면 해당 release gate는 미충족이다.
- `partial_auto_visual`과 non-exempt `budget_pruned_visual`은 automatic visual E2E denominator에 포함하고 numerator에서는 제외한다.
- `completed_fixture`는 fixture-only metric에는 사용할 수 있지만 real-use, codex-plugin interactive, automated-cli real provider, Public Beta release denominator와 numerator에서 제외한다.

Metric status classification:

| Status family | `ok`/terminal rule | Phase metric classification |
| --- | --- | --- |
| `completed_parallel`, `completed_partial_parallel`, `completed_serial_handoff`, `completed_auto_visual` | `ok=true`, `terminal=true` | success when the metric-specific artifact gates pass |
| `partial_auto_visual` | `terminal=true`; `ok=false` for visual-required unless explicit text-only partial delivery was allowed | included failure for automatic visual E2E |
| `budget_pruned_visual` | `terminal=true`; `ok` depends on selected preset | included failure for automatic visual E2E unless the run is explicitly marked low-budget/excluded before launch |
| `blocked_*`, `policy_blocked_visual` | `ok=false` except policy blocks that are correctly enforced with no unsafe claim leakage; always `terminal=true` | excluded from phase-rate denominators, but not accepted for required release gates |
| `failed_*`, `failed_validation`, `failed_synthesis` | `ok=false`, `terminal=true` | included failure when the run reached the metric scope |

| Metric | Prototype | MVP | Private Alpha | Public Beta | Product v1 |
| --- | ---: | ---: | ---: | ---: | ---: |
| completed non-blocked evidence schema validation pass rate | 80% | 95% | 98% | 99% | 99% |
| unsupported high-confidence claim leakage | 0 | 0 | 0 | 0 | 0 |
| text-only VLM call count | 0 | 0 | 0 | 0 | 0 |
| completed non-blocked visual-required visual verifier coverage | 70% | 95% | 98% | 99% | 99% |
| completed non-blocked report citation/evidence ID coverage | 80% | 95% | 98% | 99% | 99% |
| plugin install + invocation smoke pass | skeleton | 100% | 100% | 100% | 100% |
| parallel task shard merge success rate | n/a | n/a | 95% | 98% | 99% |
| real codex-exec E2E accepted shard success rate | n/a | n/a | 90% | 95% | 98% |
| visual evidence used in visual-required report | n/a | 95% | 98% | 99% | 99% |
| automatic web visual E2E pass rate | n/a | n/a | 70% | 90% | 95% |
| codex-plugin interactive visual E2E pass rate | n/a | n/a | 70% | 90% | 95% |
| automated-cli real provider visual E2E pass rate | n/a | n/a | 70% | 90% | 95% |
| visual minimum satisfaction rate | n/a | n/a | 90% | 95% | 98% |
| codex child capacity diagnostic classification coverage | n/a | n/a | 100% | 100% | 100% |
| codex child capacity retry policy compliance | n/a | n/a | 95% | 99% | 99% |
| real visual provider provenance coverage | n/a | n/a | 95% | 99% | 99% |
| user-requested report shape adherence | n/a | 90% | 95% | 98% | 99% |
| duplicate source/image/claim merge leakage | n/a | n/a | < 2% | < 1% | < 0.5% |
| median standard run completion | n/a | < 20 min | < 15 min | < 12 min | < 10 min |
| policy-blocked evidence leakage into accepted claims | 0 | 0 | 0 | 0 | 0 |
| fresh-session skill full-runner artifact handoff pass rate | n/a | n/a | 90% | 98% | 99% |

Metric definitions:

- `real codex-exec E2E accepted shard success rate`: percentage of non-blocked real `adapter=codex-exec` E2E runs where `accepted_shards > 0`; fixture adapter runs are excluded from numerator and denominator.
- `visual evidence used in visual-required report`: percentage of non-blocked visual-required runs where at least one supported claim has `supporting_images`, `visual_supports[]`, and `report_status.used_images > 0`.
- `automatic web visual E2E pass rate`: aggregate percentage of non-blocked visual-required real-use runs with no user-provided images where `run_status.json.status` reaches `completed_auto_visual`, Codex-native visual candidate/fetch artifacts exist, at least 3 non-fixture `codex-interactive` VLM handoff observations exist, and at least one visual/mixed claim is cited in `report.md`.
- `codex-plugin interactive visual E2E pass rate`: percentage of fresh-session `$deep-research` visual E2E runs where Codex-native search/VLM handoff artifacts are filled, ingested, reported, and surfaced in the final response without hidden Codex API assumptions. Explicit blocked terminal statuses are tracked separately and do not count as passes.
- `automated-cli real provider visual E2E pass rate`: percentage of no-user-image automated CLI visual E2E diagnostic runs where real provider acquisition plus `openai-responses-vision` reaches `completed_auto_visual` with required provider provenance, cost fields, and report citation linkage. This is separate from the default Codex-native Public Beta completion path.
- `visual minimum satisfaction rate`: percentage of non-blocked visual-required real-use E2E runs where `visual_provider_status.json.minimums.satisfied=true`, at least 3 non-fixture Codex-interactive analyzed images exist, and at least one visual/mixed claim is cited in `report.md`.
- `codex child capacity diagnostic classification coverage`: percentage of Codex child model-capacity failures that preserve raw child diagnostics, set `child_failure_code=codex_child_model_capacity`, and record `timeout=false`.
- `codex child capacity retry policy compliance`: percentage of retry-eligible capacity failures that follow configured bounded backoff, attempt limits, timeout limits, and terminal-status rules.
- `real visual provider provenance coverage`: percentage of image/screenshot/PDF visual artifacts with provider, origin, source page, local artifact, hash, policy state, VLM path, and real-vs-fixture provenance recorded.
- `user-requested report shape adherence`: percentage of sampled real-use reports scoring `>=9/10` on the report quality gate.
- `fresh-session skill full-runner artifact handoff pass rate`: percentage of fresh Codex session `$deep-research` E2E runs where the final response includes a run artifact path, `report.md`, `evidence.json`, `run_status.json`, `report_status.json` after synthesis, status/shard summary, and the backing files exist after the response.

## 구현 순서

1. Codex Plugin 구조와 `$deep-research` Skill UX를 확정하고, 기본 invocation mode를 `full-runner`로 고정한다.
2. schema v0 JSON Schema, fixture, validation command를 만든다.
3. plugin 내부 runner와 개발용 CLI wrapper를 만든다.
4. `Planner -> ModalityRouter -> Search -> Fetch -> Extract -> Verify -> Synthesize` 파이프라인을 runner에 구현한다.
5. `codex-plugin`, `automated-cli`, `manual-sources` 실행 모드를 분리한다.
6. search provider mode를 skill용 Codex-native workflow와 CLI용 provider abstraction으로 분리한다.
7. VLM path를 `codex-interactive`, `openai-responses-vision`, `manual-visual-review`로 분리한다.
8. Agent budget preset과 pruning을 구현한다.
9. run trace, run step state machine, cache key를 구현한다.
10. Automated runner adapter를 통해 `codex exec --json` 또는 Codex SDK/MCP server 기반 Codex subagent 병렬 orchestration, evidence shard, merge/dedupe를 구현한다.
11. `$deep-research` fresh-session E2E가 full-runner artifact handoff를 통과하게 한다.
12. Phase 3 automatic web visual research artifacts와 provider diagnostics를 구현한다.
13. real web/image search provider, page image extractor, screenshot collector, PDF figure rasterizer, image fetch/cache를 구현한다.
14. `openai-responses-vision` automated adapter와 visual verifier/report linkage를 구현한다.
15. 시각 evidence appendix를 생성한다.
16. 개인 marketplace 등록과 plugin install/update 절차를 문서화한다.
17. 웹 UI/워크플로우 대시보드에서 subagent 진행 상태, automatic visual status, visual provider provenance, cost cap을 제어한다.

## 참고 근거

- OpenAI Images and Vision: 이미지 분석, URL/Base64/file ID 입력, detail/cost/한계.  
  https://developers.openai.com/api/docs/guides/images-vision
- OpenAI Agents SDK: agents, tools, handoffs, guardrails, tracing, sandbox execution.  
  https://developers.openai.com/api/docs/libraries#use-the-agents-sdk
- Codex Subagents: Codex CLI/App subagent workflow, `agents.max_threads`, `spawn_agent` orchestration behavior.
  https://developers.openai.com/codex/subagents
- Codex SDK and MCP server: programmatic Codex thread execution and MCP `codex`/`codex-reply` tools.
  https://developers.openai.com/codex/guides/agents-sdk

## 자체 검토

문제점: 처음 초안은 딥리서치 파이프라인만 있고, 비개발자 입력 경로와 지식 승격 경로가 약했다. 또한 Codex에서 내장 명령처럼 쓰는 배포 표면, subagent 상한, VLM 필요 여부 분류가 명시되어 있지 않았다. 이후 검토에서 MVP가 user-provided image에 의존하면 딥리서치라고 보기 어렵다는 문제가 추가로 확인됐다. 추가 리뷰에서는 문서가 "최종 제품은 Codex Plugin"이라는 방향보다 "독립 CLI 프로그램을 만든 뒤 plugin으로 포장"하는 것처럼 읽히고, Codex interactive VLM/search와 자동 CLI API 호출이 섞여 있다는 문제가 확인됐다. 2026-06-23 PRD 진화 검토에서는 Claude Code deep-research식 병렬 subagent 조사 경험이 예산표에 암시되어 있을 뿐, planner fan-out, subagent assignment, evidence shard, merge/dedupe, 100-agent high fan-out cap이 구현 계약으로 명시되어 있지 않다는 문제가 확인됐다. 2026-06-24 실사용 E2E에서는 Phase 2 artifact는 생성되지만, real `codex-exec` shard가 accepted 되지 않고, visual evidence가 final report에 쓰이지 않으며, report synthesis가 사용자 질문 형식을 따르지 않는 문제가 확인됐다. 2026-06-24 추가 PRD 리뷰에서는 Phase 3가 dashboard/evidence review 중심으로 정의되어 있어, 사용자가 원하는 "웹 조사 중 이미지/차트/논문 figure를 자동 발견하고 VLM으로 판독하는" end-to-end 자동 웹 이미지 조사가 구현 범위와 gate에 충분히 명시되어 있지 않다는 문제가 확인됐다. 2026-06-24 skill UX 리뷰에서는 새 Codex 세션의 `$deep-research` invocation이 full runner를 끝까지 실행하지 않고 일반 대화형 답변으로 새어 나갈 수 있어, 개발 검증용 runner 결과와 실제 사용자 체감이 달라지는 문제가 확인됐다. 2026-06-29 PR #104 이후 Apollo public-image 실사용 E2E에서는 timeout hardening 자체는 동작했지만, 한 run은 2개 이미지만 Codex-interactive로 분석되어 3개 visual minimum을 충족하지 못했고, 다른 run은 `Selected model is at capacity. Please try a different model.` 메시지와 함께 child가 실패했음이 확인됐다.

수정: 최종 배포 단위를 Codex Plugin으로 명시하고, CLI는 plugin 내부 runner의 개발/테스트/자동화용 wrapper로 재정의했다. 실행 모드를 `codex-plugin`, `automated-cli`, `manual-sources`로 분리했고, VLM path를 `codex-interactive`, `openai-responses-vision`, `manual-visual-review`로 분리했다. Search provider도 plugin용 Codex-native workflow와 CLI용 provider abstraction으로 나누었다. 또한 `schema_version`, source retrieval metadata, image artifact path, VLM observation/inference 분리, quote span, verifier vote metadata를 포함하는 Evidence Schema v0를 PRD의 핵심 계약으로 확정했다. 이번 진화에서는 Parallel Codex Subagent Orchestration Contract를 추가해 `research_tasks.json`, `subagent_assignments.jsonl`, evidence shard, `merge_status.json`, task state, degradation behavior, `max_concurrent_codex_subagents`, `exhaustive` 100-subagent confirmation rule을 구현 가능한 요구사항으로 명시했다. 2026-06-23 추가 검증에서는 Codex CLI가 `spawn_agent`, `wait`, `close_agent` JSON events로 2개 subagent를 생성하고 결과를 회수하는 smoke test를 통과했으므로, M18의 첫 구현 방식을 automated runner adapter로 확정했다. 2026-06-24 수정에서는 real-use E2E finding을 PRD에 추가하고, M19-M24 hardening tickets로 trusted `codex-exec` context, parallel status semantics, visual evidence linkage, user-shaped report synthesis, run-step stability, fixture-vs-real E2E distinction을 공식 Phase 2 후속 범위로 편입했다. 이번 Phase 3 진화에서는 Automatic Web Visual Research Contract를 추가하고, `visual_search_plan.json`, `visual_candidates.jsonl`, `image_fetch_status.jsonl`, `visual_provider_status.json`, real provider provenance, `completed_auto_visual` 상태, `openai-responses-vision` automated adapter, browser screenshot, PDF figure rasterization, visual E2E gate를 Phase 3 범위와 WBS에 명시했다. 또한 Skill Invocation Full-Runner UX Contract를 추가해 `$deep-research` 기본 mode를 `full-runner`로 고정하고, `quick-chat`은 명시 요청 때만 허용하며, 최종 응답이 run directory, `report.md`, `evidence.json`, `run_status.json`, synthesized-run `report_status.json`, applicable visual/parallel status artifacts, shard summary를 노출해야 한다는 fresh-session E2E gate를 Phase 3 P3-UX 이슈로 분리했다. PR #56 후속 검토에서는 codex-plugin full-runner state machine, final status artifact set(`run_status.json`, synthesized-run `report_status.json`, visual/parallel status files), Phase 3 visual artifact field rules, codex-plugin interactive visual E2E와 automated-cli real provider E2E 분리, Public Beta provider/scenario gates, automatic visual ok/terminal/metric classification을 추가해 implementation contract를 더 좁혔다. 추가 후속 검토에서는 `blocked_missing_visual_provider` 전이를 preflight/prepared/visual handoff에서 명시하고, `visual_observations.jsonl` record schema를 추가했다. 2026-06-26 후속 정리에서는 #97 / P3-AV10이 이미 확보된 local visual artifact를 Codex-native VLM worker로 읽는 필요조건일 뿐 complete automatic web visual research가 아님을 명시하고, #99 / P3-AV11을 다음 safe wave로 추가해 full-runner가 web discovery -> local artifact acquisition -> Codex VLM -> visual verifier -> report citation을 한 run에서 완성해야 한다는 acceptance를 추가했다. 2026-06-29 후속 정리에서는 #105 / P3-AV12와 #106 / P3-RUN1을 Wave 7으로 추가해 visual minimum shortfall은 `partial_auto_visual`과 `visual_minimum_shortfall` diagnostics로, Codex child model capacity는 `codex_child_model_capacity`와 bounded retry/backoff로 처리하도록 계약을 좁혔다.

남은 리스크: Codex Plugin 안에서 `codex-interactive` VLM과 Codex-native search를 어느 정도까지 자동화할 수 있는지는 구현 중 검증이 필요하다. 병렬 subagent 실행 자체는 Codex CLI smoke test로 확인됐지만, automated runner adapter는 Codex auth, sandbox, approval policy, nested `codex exec`, SDK/MCP server availability, JSON event compatibility, child thread cleanup을 안정적으로 처리해야 한다. Codex subagent를 사용할 수 없는 surface에서는 serial handoff 또는 `automated-cli` worker fallback으로 degrade해야 한다. 100-subagent high fan-out은 비용, rate limit, workspace policy, trace volume을 크게 키우므로 `exhaustive` preset에서만 explicit confirmation과 cost cap으로 제한한다. 자동 실행을 위해 `openai-responses-vision` 또는 hosted search를 사용할 경우 비용과 API 정책이 별도로 적용된다. 이미지 검색 API, 저작권/robots 정책, VLM hallucination, 비용 폭증은 구현 단계에서 별도 guardrail과 rate limit이 필요하다. Phase 3 automatic visual E2E는 real provider, remote image fetch, browser automation, PDF rasterization, VLM API 비용, provider 약관과 rate limit에 의존하므로 fixture 통과와 별도 gate로 운영해야 한다. Fresh-session skill E2E는 Codex plugin installation, skill selection, session transcript capture, and nested runner availability에 의존하므로 CLI-only validation과 별도 gate로 운영해야 한다. Real-use E2E가 fixture validation과 다른 실패를 드러냈으므로, 앞으로 Phase 2 acceptance는 fixture-only smoke가 아니라 실제 `codex-exec` child run, visual-required run, 사용자 형식의 report synthesis를 별도 gate로 검증해야 한다. Product v1 이후의 cloud/team 범위는 인증, 저장소, 결제, 조직 정책에 따라 별도 아키텍처 PRD가 필요할 수 있다.
