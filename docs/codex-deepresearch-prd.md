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

## 사용자

- 개발자: CLI/Skill로 기술 조사, 라이브러리 비교, 장애 원인 분석, 코드/문서/스크린샷 기반 조사.
- 비개발자: 웹 UI 또는 간단한 명령으로 시장조사, 제품 비교, 이미지 품질 조사, 경쟁 서비스 분석.
- 에이전트: 저장된 evidence bundle을 재사용해 후속 작업, PRD, 구현 계획, QA 체크리스트로 승격.

## 입력 경로

Codex 내장 명령형 사용 목표:

- Codex 자체의 built-in slash command를 수정하는 제품이 아니라, 설치형 Codex Plugin + Skill + CLI wrapper로 "내장 명령처럼" 호출되는 사용자 경험을 제공한다.
- 1차 목표 호출 방식은 `$deep-research` Skill invocation이다.
- 2차 목표 호출 방식은 `codex-deepresearch` CLI이다.
- 3차 목표 호출 방식은 플러그인 설치 후 Codex의 `/skills` 선택기에서 `deep-research`를 고르는 방식이다.
- `/deep-research`와 완전히 같은 slash command 이름을 Codex core에 추가하는 것은 비목표다. 단, 플러그인/스킬 설명과 CLI alias를 통해 사용자는 사실상 내장 워크플로우처럼 사용할 수 있어야 한다.

개발자 CLI:

```bash
codex-deepresearch "AI 사진 서비스 품질관리 조사" --visual --max-sources 30
```

Codex Skill:

```text
$deep-research: <질문>
```

Plugin packaging:

```text
codex-deepresearch-plugin/
  .codex-plugin/plugin.json
  skills/deep-research/SKILL.md
  scripts/codex-deepresearch
  src/deepresearch/
```

비개발자 UI:

- `question`
- 조사 깊이
- 이미지 포함 여부
- 신뢰도 기준
- 출력 형식
- 이미지 업로드 또는 URL 목록

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
6. claim마다 최소 3개 verifier를 실행한다.
   - 텍스트 verifier 2개
   - visual verifier 1개 이상
   - 이미지 근거가 있는 claim이면 visual verifier 필수
7. 반박 2표 이상이면 claim을 폐기한다.
8. 최종 보고서는 모든 주요 주장에 source URL, quote, image evidence ID를 붙인다.
9. 실행 전 agent budget을 산정하고 사용자가 선택한 조사 깊이의 hard cap을 넘지 않는다.
10. VLM이 필요 없는 angle에는 이미지 수집과 VLM 호출을 하지 않는다.

## 에이전트 구성

- `PlannerAgent`: 질문 분해, 텍스트/이미지 조사 필요성 판단.
- `ModalityRouterAgent`: angle과 claim 후보를 `text_only`, `visual_required`, `visual_optional`로 분류하고 VLM 호출 여부를 결정.
- `SearchAgent`: 웹 검색 결과 수집.
- `ImageScoutAgent`: 이미지 검색, 페이지 대표 이미지, 스크린샷 후보 수집.
- `FetchAgent`: HTML/PDF/문서 fetch 및 본문 추출.
- `VisionExtractAgent`: 이미지 OCR, 시각 claim 추출, 차트/스크린샷 해석.
- `ClaimExtractorAgent`: 텍스트 claim 구조화.
- `VerifierAgent`: claim 반박 검색.
- `VisualVerifierAgent`: 이미지가 claim을 실제로 뒷받침하는지 검증.
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

Codex-native VLM capability:

- Codex는 사용자가 첨부한 이미지 파일과 스크린샷을 읽을 수 있다.
- 개인용 MVP에서는 이 능력을 우선 사용한다.
- DeepResearch는 자동 수집한 이미지와 스크린샷을 로컬 run artifact로 저장한 뒤, Codex-native VLM 분석 경로로 `VisionExtractAgent`와 `VisualVerifierAgent`에 투입한다.
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

- MVP는 Codex-native VLM + basic automated visual acquisition을 기본으로 한다.
- user-provided visual evidence는 supplemental input이다.
- 자동 이미지 검색과 스크린샷 수집은 MVP에 포함한다.
- Private Alpha는 더 큰 규모, 더 정교한 중복 제거, resume/cache, 대량 캡처 안정화를 담당한다.
- `visual_required` 태스크는 자동 수집 이미지 또는 사용자 제공 이미지가 없으면 `needs_visual_evidence` 상태로 남기고 high-confidence 결론을 내지 않는다.

## Agent Budget

기본 preset:

| Preset | 최대 동시 subagent | 최대 총 subagent | 최대 source | 최대 image | 용도 |
| --- | ---: | ---: | ---: | ---: | --- |
| `quick` | 4 | 16 | 8 | 4 | 빠른 사실 확인 |
| `standard` | 8 | 48 | 20 | 12 | 기본 딥리서치 |
| `deep` | 12 | 96 | 40 | 30 | 고신뢰 보고서 |
| `exhaustive` | 16 | 256 | 100 | 80 | 비용 확인 후 실행하는 대형 조사 |

MVP 기본값은 `standard`다. MVP hard cap은 총 subagent 96개, 동시 subagent 12개다. `exhaustive`는 v2에서 제공하며 실행 전 예상 비용과 시간을 사용자에게 확인받는다.

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

## 저장 스키마

```json
{
  "run_id": "dr_20260616_001",
  "question": "...",
  "sources": [
    {
      "id": "src_001",
      "type": "web|pdf|image|screenshot",
      "url": "...",
      "title": "...",
      "published_at": "...",
      "quality": "primary|secondary|blog|forum|unknown"
    }
  ],
  "routing": [
    {
      "angle": "artifact detection",
      "modality": "visual_required",
      "visual_tasks": ["ocr", "artifact_detection", "image_claim_alignment"],
      "max_images": 12
    }
  ],
  "budget": {
    "preset": "standard",
    "max_concurrent_agents": 8,
    "max_total_agents": 48,
    "agents_used": 37
  },
  "images": [
    {
      "id": "img_001",
      "source_id": "src_001",
      "image_url": "...",
      "ocr_text": "...",
      "vlm_summary": "...",
      "visual_claims": ["..."],
      "phash": "..."
    }
  ],
  "claims": [
    {
      "id": "claim_001",
      "text": "...",
      "supporting_sources": ["src_001"],
      "supporting_images": ["img_001"],
      "votes": [
        {
          "agent": "text_v1",
          "refuted": false,
          "evidence": "..."
        },
        {
          "agent": "visual_v1",
          "refuted": true,
          "evidence": "..."
        }
      ],
      "status": "confirmed|refuted|uncertain"
    }
  ]
}
```

## 검증 규칙

- quote 없는 텍스트 claim은 `uncertain`.
- 이미지 claim은 이미지 원본 URL 또는 캡처된 screenshot 없이는 `uncertain`.
- VLM이 본문과 다른 내용을 읽으면 visual contradiction으로 기록.
- 날짜가 중요한 claim은 publish date 또는 access date 필수.
- medical/legal/financial claim은 primary source 없으면 high confidence 금지.
- 이미지 내 작은 글씨, 회전, 비라틴 문자 OCR은 별도 caveat 필수.

## 승인 레벨

- `raw`: 수집만 된 source/image.
- `extracted`: claim 추출 완료.
- `verified`: 3-vote 검증 통과.
- `accepted`: 사람이 승인하거나 정책상 충분한 근거 충족.
- `promoted`: playbook, memory, skill, PRD, implementation plan으로 승격.

## 에이전트 검색/재사용 경로

기본 저장소:

```text
~/.codex/deepresearch/runs/<run_id>/
  report.md
  evidence.json
  images/
  screenshots/
  claims.jsonl
```

Codex는 후속 작업에서 `evidence.json`을 먼저 읽고, claim status가 `verified` 또는 `accepted`인 항목만 재사용한다.

## MVP

1. CLI `codex-deepresearch` 구현.
2. Codex Skill `$deep-research` 구현.
3. 개인용 Codex Plugin으로 패키징해 `/skills`에서 선택 가능하게 만든다.
4. 텍스트 검색 + basic automated visual acquisition + Codex-native VLM 분석을 지원한다.
5. `ModalityRouterAgent`로 text-only/visual-required/visual-optional 분류를 구현한다.
6. visual-required angle에서 이미지 검색, 대표 이미지 추출, first viewport screenshot 캡처를 수행한다.
7. `standard` preset 기준 총 subagent 48개, 동시 8개를 기본값으로 둔다.
8. 보고서 `report.md`와 `evidence.json` 저장.
9. claim 3-vote 검증 구현.

## Product Roadmap

### Phase 0: Prototype

목표: Claude Code deep-research 구조를 clean-room으로 재현할 수 있는지 검증한다.

범위:

- 로컬 Python/TypeScript 패키지 스캐폴드.
- 단일 명령 `codex-deepresearch "<question>"`.
- 수동 source URL 입력 지원.
- 수동 image file, screenshot, image URL 입력 지원.
- `PlannerAgent`, `ModalityRouterAgent`, `SynthesisAgent` 최소 구현.
- 출력: `report.md`, `evidence.json`.

Exit criteria:

- 텍스트-only 질문 3개, visual-required 질문 3개를 끝까지 처리한다.
- `ModalityRouterAgent`가 VLM 호출 필요 여부를 evidence에 기록한다.
- 실패해도 부분 evidence를 저장한다.

### Phase 1: MVP

목표: 개인 사용자가 Codex에서 내장 워크플로우처럼 쓸 수 있게 한다.

범위:

- `$deep-research` Codex Skill.
- 개인용 Codex Plugin 패키지.
- `quick`, `standard`, `deep` preset.
- 텍스트 웹검색, fetch, claim extraction.
- basic automated image collection.
- high-relevance source page first viewport screenshot capture.
- Codex-native VLM 기반 image/screenshot 분석.
- user-provided image file, screenshot, image URL 보강 입력.
- claim당 3-vote 검증.
- source quote와 image evidence ID가 포함된 Markdown 보고서.
- `~/.codex/deepresearch/runs/<run_id>/` 저장.

Exit criteria:

- `standard` preset에서 총 subagent 48개 이하로 안정 실행.
- unsupported claim이 최종 보고서에 들어가지 않는다.
- text-only 태스크에서 VLM 호출이 발생하지 않는다.
- visual-required 태스크에서 자동 이미지 수집 또는 스크린샷 캡처가 실행된다.
- visual-required 태스크에서 최소 1개 visual verifier가 실행된다.
- 재실행 없이 결과 파일을 Codex가 후속 작업에 재사용할 수 있다.

### Phase 2: Private Alpha

목표: MVP의 basic visual acquisition을 더 안정적이고 대규모로 확장하고, 긴 리서치를 재개 가능하게 만든다.

범위:

- 이미지 검색 API provider 확대와 품질 튜닝.
- full-page, scroll, interaction screenshot 캡처.
- 대표 이미지, Open Graph image, 본문 이미지 추출의 정확도 개선.
- perceptual hash 기반 이미지 중복 제거.
- OCR 결과와 VLM 설명 분리 저장.
- resumable run: 중단된 run을 `run_id`로 재개.
- cost estimator: 실행 전 예상 source/image/agent/token 수 표시.
- run trace: agent별 prompt, tool call, output summary 저장.

Exit criteria:

- 이미지가 핵심인 조사에서 자동으로 최소 10개 후보 이미지를 수집한다.
- 중복 이미지 제거율과 제거 근거가 evidence에 남는다.
- 중단 후 재개해도 이미 처리한 source/image를 다시 분석하지 않는다.
- 사용자가 실행 전 비용 상한을 설정할 수 있다.

### Phase 3: Public Beta

목표: 반복 사용 가능한 제품 경험과 검토 UX를 제공한다.

범위:

- Codex Plugin marketplace 등록 또는 개인 marketplace 등록 자동화.
- `/skills`에서 선택 가능한 안정 skill metadata.
- TUI 또는 lightweight web dashboard.
- run list, progress, pause/resume/cancel.
- evidence browser: source, image, claim, vote를 탐색.
- human review: claim을 `accepted`, `rejected`, `needs_more_evidence`로 수동 판정.
- report templates: technical report, market report, competitor analysis, incident report.
- export: Markdown, JSON, CSV, HTML bundle.

Exit criteria:

- 사용자가 코드 파일을 직접 열지 않고 run 상태와 evidence를 확인할 수 있다.
- human review 결과가 다음 run과 후속 Codex 작업에 반영된다.
- plugin 설치/업데이트/제거 절차가 문서화된다.
- 20개 이상 실제 리서치 태스크에서 실패 유형이 분류되어 있다.

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
- promotion workflow: accepted evidence -> memory/playbook/skill/PRD.
- documentation: install, quickstart, config, troubleshooting, examples.

Exit criteria:

- `quick`, `standard`, `deep` preset이 문서화된 비용/시간 범위 안에서 동작한다.
- claim citation correctness benchmark가 설정된 기준을 통과한다.
- visual-required benchmark에서 visual verifier가 누락되지 않는다.
- schema migration이 이전 run artifact를 깨뜨리지 않는다.
- 사용자가 accepted evidence만 후속 Codex 작업에 주입할 수 있다.

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
- 반복 조사에서 기존 accepted evidence를 재검증하거나 stale 처리한다.
- source policy와 budget policy가 팀 설정으로 강제된다.
- 민감 이미지와 private source가 export에 포함될지 제어할 수 있다.

## Phase Work Breakdown

### Phase 0 WBS: Prototype

Epics:

- Core runner
- Minimal agent loop
- Evidence artifact
- Smoke evaluation

Tasks:

1. `deepresearch/` 패키지와 단일 entrypoint를 만든다.
2. `codex-deepresearch "<question>"` 명령을 실행하면 run directory를 생성한다.
3. `PlannerAgent`가 질문을 3-5개 angle로 분해하게 한다.
4. `ModalityRouterAgent`가 각 angle을 `text_only`, `visual_required`, `visual_optional`로 분류하게 한다.
5. 수동 source URL과 image URL을 입력받는 옵션을 만든다.
6. 텍스트 source에서 title, body excerpt, quote 후보를 추출한다.
7. image URL을 VLM으로 분석해 OCR, visual summary, visual claims를 저장한다.
8. `SynthesisAgent`가 최소 보고서를 생성한다.
9. 실패 시 partial `evidence.json`을 저장한다.
10. 텍스트-only 3개, visual-required 3개 smoke test fixture를 만든다.

Deliverables:

- CLI prototype
- `report.md`
- `evidence.json`
- smoke test fixture

### Phase 1 WBS: MVP

Epics:

- Codex Skill integration
- Personal plugin packaging
- Search/fetch/extract pipeline
- Basic visual acquisition
- Codex-native VLM analysis
- Verification pipeline
- Budget presets

Tasks:

1. `$deep-research` Skill의 `SKILL.md`를 작성한다.
2. 개인용 Codex Plugin manifest `.codex-plugin/plugin.json`을 작성한다.
3. CLI runner를 Skill에서 호출 가능한 script로 감싼다.
4. `quick`, `standard`, `deep` preset config를 구현한다.
5. 웹 검색 provider abstraction을 만든다.
6. HTML/PDF fetcher와 본문 추출기를 구현한다.
7. high-relevance source page에서 Open Graph image와 본문 image 후보를 추출한다.
8. visual-required angle에서 image search provider가 가능하면 image search를 수행한다.
9. high-relevance source page의 first viewport screenshot을 캡처한다.
10. 수집 이미지와 스크린샷을 `images/`, `screenshots/` run artifact로 저장한다.
11. 수집 이미지를 MIME, 크기, URL 중복, basic hash로 필터링한다.
12. `VisionExtractAgent`가 Codex-native VLM으로 이미지와 스크린샷을 분석하게 한다.
13. `ClaimExtractorAgent`가 quote 포함 claim을 구조화하게 한다.
14. `VerifierAgent` 2개와 `VisualVerifierAgent` 1개를 claim마다 실행한다.
15. 반박 2표 이상이면 claim을 `refuted` 처리한다.
16. Budget 초과 시 source quality, relevance, visual necessity 기준으로 pruning한다.
17. text-only route에서 이미지 검색, screenshot, VLM 호출이 발생하지 않는 테스트를 만든다.
18. visual-required route에서 자동 이미지 수집 또는 screenshot 캡처가 실행되는 테스트를 만든다.
19. visual-required route에서 visual verifier가 반드시 실행되는 테스트를 만든다.
20. Markdown report와 evidence JSON을 Codex 후속 작업에서 읽기 쉬운 형태로 저장한다.

Deliverables:

- Codex Skill
- personal Codex Plugin
- CLI runner
- basic image collector
- first viewport screenshot collector
- Codex-native VLM analysis path
- budget preset config
- 3-vote verification engine
- MVP test suite

### Phase 2 WBS: Private Alpha

Epics:

- Advanced visual acquisition
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

Deliverables:

- automated image collection
- screenshot collector
- resumable run engine
- cost estimator
- run trace JSONL

### Phase 3 WBS: Public Beta

Epics:

- Product UX
- Evidence review
- Report templates
- Install/update flow

Tasks:

1. run list, run detail, claim detail을 볼 수 있는 TUI 또는 lightweight web dashboard를 만든다.
2. 진행 중 run의 phase, agent count, source count, image count를 표시한다.
3. pause/resume/cancel control을 구현한다.
4. source/image/claim/vote를 연결해서 탐색하는 evidence browser를 만든다.
5. claim review 상태 `accepted`, `rejected`, `needs_more_evidence`를 저장한다.
6. review 결과가 후속 synthesis와 Codex reuse에 반영되게 한다.
7. technical report, market report, competitor analysis, incident report template을 만든다.
8. Markdown, JSON, CSV, HTML bundle export를 구현한다.
9. plugin 설치, 업데이트, 제거 절차를 문서화한다.
10. 실제 리서치 태스크 20개 이상을 실행하고 실패 유형을 수집한다.
11. onboarding quickstart와 example gallery를 만든다.

Deliverables:

- dashboard/TUI
- evidence browser
- human review workflow
- report templates
- export bundle
- beta documentation

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
11. accepted evidence를 memory/playbook/skill/PRD로 승격하는 workflow를 만든다.
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
| CLI 실행 | Yes | Yes | Yes | Yes | Yes | Yes |
| Codex Skill | No | Yes | Yes | Yes | Yes | Yes |
| Codex Plugin | No | Personal | Personal | Marketplace-ready | Stable | Team-managed |
| 텍스트 검색/fetch | Basic | Yes | Yes | Yes | Yes | Yes |
| 이미지 URL 분석 | Basic | Yes | Yes | Yes | Yes | Yes |
| 자동 이미지 검색 | No | Basic | Yes | Yes | Yes | Yes |
| 스크린샷 수집 | No | Basic | Yes | Yes | Yes | Yes |
| ModalityRouter | Basic | Yes | Yes | Yes | Yes | Policy-aware |
| Agent budget | Manual | Presets | Cost estimator | User controls | Policy controls | Team controls |
| Evidence 저장 | JSON/MD | JSON/MD | Versioned draft | Browsable | Versioned stable | Shared |
| Human review | No | File edit | Basic | Review UI | Promotion workflow | Team approval |
| 재개/resume | No | No | Yes | Yes | Yes | Yes |
| Observability | Logs | Run stats | Trace | Dashboard | Metrics suite | Org analytics |

## Product-Level Acceptance Criteria

- 사용자는 Codex 안에서 `$deep-research`를 호출해 별도 설명 없이 리서치를 시작할 수 있다.
- 사용자는 CLI에서도 같은 엔진을 실행할 수 있다.
- 모든 run은 재사용 가능한 `evidence.json`을 남긴다.
- text-only 작업은 VLM 비용을 쓰지 않는다.
- visual-required 작업은 VLM 분석과 visual verifier를 생략하지 않는다.
- 최종 보고서의 모든 high-confidence claim은 quote 또는 image evidence를 가진다.
- high-risk domain claim은 primary source 또는 caveat 없이는 high confidence가 될 수 없다.
- 사용자는 accepted evidence만 memory/playbook/skill/PRD로 승격할 수 있다.
- 제품은 중단, 실패, 재시도, 부분 결과 저장을 정상적인 상태로 다룬다.

## 비목표

- Claude Code 워크플로우 코드 복사.
- Codex core built-in slash command 수정.
- 완전 자동 사실판정 보장.
- CAPTCHA/로그인 뒤 콘텐츠 우회.
- 의료 영상, 법적 판단, 금융 조언의 단독 자동 결론.

## 기술 선택

- Orchestration: OpenAI Agents SDK. 공식 문서는 agents, tools, handoffs, guardrails, tracing에 적합하다고 설명한다.
- Model/API: OpenAI Responses API. 공식 vision 문서는 이미지 입력을 URL, Base64 data URL, file ID로 받을 수 있고 여러 이미지를 한 요청에 넣을 수 있다고 설명한다.
- Storage: JSONL + Markdown, 나중에 SQLite로 확장.
- Image processing: perceptual hash, EXIF 추출, screenshot 캡처, OCR/VLM 결과 병합.

## Search Backend and Cost Model

Codex 자체의 web search와 Codex DeepResearch의 search backend는 분리한다.

Personal-first decision:

- 당장 목표가 판매용 제품이 아니라 개인이 Codex 안에서 고품질 리서치를 하는 것이라면, MVP의 기본 검색 경로는 Codex 내장 search를 사용하는 `codex-native` mode로 둔다.
- `codex-native` mode에서는 Codex agent가 현재 세션의 web search 기능을 사용해 텍스트 검색과 출처 확인을 수행하고, DeepResearch runner는 evidence schema, claim verification, VLM routing, 보고서 구조화를 담당한다.
- 독립 실행형 CLI에서 완전 자동으로 검색까지 수행해야 할 때만 OpenAI hosted web search 또는 외부 search API provider를 사용한다.
- 즉 개인용 MVP에서는 "검색 API 제품화"보다 "Codex가 찾은 근거를 구조화, 검증, 저장, 재사용하는 것"을 우선한다.

Codex product behavior:

- Codex CLI는 `web_search = "cached"`를 기본값으로 사용한다.
- cached mode는 OpenAI가 유지하는 web search cache/index에서 결과를 가져오며 live page를 직접 fetch하지 않는다.
- `web_search = "live"` 또는 `--search`는 최신 웹 데이터를 가져오는 live browsing 성격이다.
- 이 Codex 내장 search tool은 Codex 제품 표면이며, Codex DeepResearch 구현이 안정적으로 의존할 공개 library API로 취급하지 않는다.

Codex DeepResearch provider options:

0. Codex-native search mode
   - 개인용 MVP의 기본값.
   - Codex 세션 안에서 Codex의 web search 기능을 사용한다.
   - 비용: 별도 외부 검색 API 비용은 없다. 단, Codex 사용량/모델 토큰/VLM 분석 비용은 사용 환경의 과금 정책을 따른다.
   - 장점: 설정이 거의 없고, Codex의 현재 검색 품질을 그대로 활용할 수 있다.
   - 단점: 독립 실행형 CLI나 백그라운드 worker에서 같은 검색 결과를 API처럼 재현하기 어렵다.
   - 적용: text-only 태스크, 빠른 개인 조사, Codex 대화 안에서 바로 실행하는 리서치.

1. OpenAI hosted web search via Responses API
   - 제품화 또는 독립 실행형 CLI의 기본 provider 후보.
   - 장점: Responses API/Agents SDK tool orchestration과 잘 맞고 별도 검색 provider 계정이 필요 없다.
   - 비용: OpenAI API pricing 기준 web search는 별도 tool call 과금 대상이다. 2026-06-16 확인 기준 가격은 `$10 / 1k calls`이고 search content tokens는 무료다.
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
+ text_model_input_output_cost
+ image_input_token_cost
+ verifier_agent_cost
+ synthesis_agent_cost
```

Budget controls:

- `--max-search-calls`
- `--max-sources`
- `--max-images`
- `--max-agents`
- `--max-cost-usd`
- `--provider codex-native|openai|brave|tavily|serpapi|manual`

MVP policy:

- 개인용 MVP 기본값은 `codex-native`다.
- 독립 실행형 CLI나 재현 가능한 batch run에서는 provider abstraction을 통해 `openai`, `brave`, `tavily`, `serpapi`, `manual` 중 선택한다.
- 비용이 가장 예측 가능한 제품화 기본값은 OpenAI hosted web search 또는 user-provided sources mode다.
- 실행 전 예상 search calls, model calls, image analyses, upper-bound cost를 표시한다.
- `text_only` route는 이미지 검색과 VLM 비용을 쓰지 않는다.
- `visual_optional` route는 budget이 부족하면 이미지 검색을 생략한다.

## 성공 지표

- claim당 평균 source 수
- image-backed claim 비율
- refuted claim 비율
- unsupported claim 최종 보고서 유입률 0%
- report 생성 비용/시간
- 사람이 검토 후 수정한 claim 비율

## 구현 순서

1. `deepresearch/` 패키지 스캐폴드.
2. `Planner -> ModalityRouter -> Search -> Fetch -> Extract -> Verify -> Synthesize` 파이프라인 구현.
3. Agent budget preset과 pruning 구현.
4. `VisionExtractAgent` 추가.
5. evidence schema 저장.
6. Codex Skill 래퍼 추가.
7. Codex Plugin 패키징과 개인 marketplace 등록 지원.
8. 시각 evidence appendix 생성.
9. 나중에 웹 UI/워크플로우 대시보드 추가.

## 참고 근거

- OpenAI Images and Vision: 이미지 분석, URL/Base64/file ID 입력, detail/cost/한계.  
  https://developers.openai.com/api/docs/guides/images-vision
- OpenAI Agents SDK: agents, tools, handoffs, guardrails, tracing, sandbox execution.  
  https://developers.openai.com/api/docs/libraries#use-the-agents-sdk

## 자체 검토

문제점: 처음 초안은 딥리서치 파이프라인만 있고, 비개발자 입력 경로와 지식 승격 경로가 약했다. 또한 Codex에서 내장 명령처럼 쓰는 배포 표면, subagent 상한, VLM 필요 여부 분류가 명시되어 있지 않았다. MVP 이후 제품화 단계도 없어 실제 제품 수준으로 언제 무엇을 갖춰야 하는지 판단하기 어려웠고, 각 phase를 바로 구현 티켓으로 전환할 작업분해도 부족했다. 이후 검토에서 MVP가 user-provided image에 의존하면 딥리서치라고 보기 어렵다는 문제가 추가로 확인됐다.

수정: CLI, Skill, Plugin 기반 호출 표면을 분리했고, `raw -> extracted -> verified -> accepted -> promoted` 승인 단계를 추가했다. 또한 이미지 evidence를 재사용 가능한 JSON 스키마로 고정했다. `ModalityRouterAgent`와 `Agent Budget` 섹션을 추가해 text-only/visual-required/visual-optional 라우팅과 subagent cap을 명시했다. `Product Roadmap`, `Phase Work Breakdown`, `Release Capability Matrix`, `Product-Level Acceptance Criteria`를 추가해 Prototype, MVP, Private Alpha, Public Beta, Product v1, Team/Cloud 단계를 구분하고 각 단계의 epics, tasks, deliverables를 명시했다. MVP 범위에 basic automated visual acquisition, first viewport screenshot capture, Codex-native VLM image/screenshot analysis를 포함하도록 수정했다.

남은 리스크: 이미지 검색 API, 저작권/robots 정책, VLM hallucination, 비용 폭증은 구현 단계에서 별도 guardrail과 rate limit이 필요하다. Product v1 이후의 cloud/team 범위는 인증, 저장소, 결제, 조직 정책에 따라 별도 아키텍처 PRD가 필요할 수 있다.
