# Semantic Regression 30 실패 요약

이 문서는 30개 semantic regression 실사용 실행에서 실패한 7개 케이스를 정리한 것입니다.

실행 산출물 위치:

`/tmp/codex-dr-semantic-regression-30-20260714T002345Z`

검증 결과 파일:

`/tmp/codex-dr-semantic-regression-30-20260714T002345Z/logs/semantic-release-validation.stdout.json`

## 전체 결과

- 실행 driver 기준: 30개 중 24개 성공, 6개 실패.
- release validator 기준: 30개 중 23개 통과, 7개 실패.
- 최종 semantic regression gate: 실패.
- 차이가 나는 이유: `sem-reg-027`은 실행 자체는 성공했지만, release validator에서 `semantic_materialization_diff.valid`가 `true`가 아니어서 실패로 계산됨.

## 실패 케이스

### 1. `sem-reg-004`

질문:

건축 `architecture model` 산출물을 공공 설계 기준과 입찰 문서 기준으로 비교해줘.

관찰된 실패:

- 최종 상태: `blocked_semantic_review_failed`
- reviewer 점수: 8.4
- blocker: `GLOBAL_SOURCE_BUDGET_CONTRADICTION`
- 직접 근거: reviewer가 "전체 source 제한은 20개라고 선언했지만, 세부 task의 `max_sources` 합은 최대 55개까지 허용한다"고 판단함.

쉽게 설명:

planner가 "이번 조사는 전체 출처를 20개 안에서 하겠다"고 말해놓고, 실제 세부 조사 task들은 합치면 최대 55개 출처까지 쓸 수 있게 만들었습니다.

중요한 점은 실제로 55개를 썼다는 뜻이 아닙니다. 문제는 계획 자체가 20개 제한을 지킬 수 없게 설계됐다는 것입니다.

왜 문제인가:

- runner가 전체 제한 20개를 따라야 하는지, task별 제한 합계 55개를 따라야 하는지 모호해짐.
- 실행 시간, 비용, 토큰 사용량을 예측하기 어려워짐.
- 조사 범위가 통제되지 않으면 최종 보고서가 산만해질 수 있음.
- release validator는 "통제 가능한 조사 계획"을 요구하므로, 이런 숫자 모순은 실패로 봄.

### 2. `sem-reg-013`

질문:

Compare browser extension permission implementation guidance from official browser vendor docs.

관찰된 실패:

- 최종 상태: `blocked_semantic_review_failed`
- reviewer 점수: 8.4
- blocker: `NON_EXECUTABLE_MULTI_VENDOR_SOURCE_CAP`
- 직접 근거: `task_003`, `task_007`, `task_011`, `task_015`가 Microsoft Edge와 Apple Safari를 둘 다 비교해야 하는데, source는 1개만 허용함.

쉽게 설명:

Edge와 Safari를 비교하려면 최소한 Edge 공식 문서 1개와 Safari 공식 문서 1개가 필요합니다. 그런데 일부 task는 source를 1개만 허용했습니다.

즉, 비교를 하라고 해놓고 비교에 필요한 최소 근거 수를 허용하지 않은 것입니다.

왜 문제인가:

- 한 벤더의 공식 문서 하나로 두 벤더를 모두 대표할 수 없음.
- task가 실행 불가능한 형태가 됨.
- planner는 벤더별로 task를 나누거나, 비교 대상별 최소 source를 허용해야 함.

### 3. `sem-reg-018`

질문:

Compare public recycling poster images and official municipal recycling rules.

관찰된 실패:

- 최종 상태: `blocked_semantic_review_failed`
- reviewer 점수: 8.2
- blockers:
  - `UNBOUND_JURISDICTION_PLACEHOLDERS`
  - `MISSING_SELECTION_WORKFLOW`
  - `NON_NEGOTIABLE_TASK_COVERAGE_INVALID`
- 직접 근거: reviewer가 `municipality 1`, `municipality 2` 같은 placeholder를 발견했고, 실제 지자체를 어떻게 고를지에 대한 실행 가능한 절차가 없다고 판단함.

쉽게 설명:

planner가 실제 지자체 이름을 정하거나, 어떤 기준으로 지자체를 고를지 정하지 않았습니다. 대신 "지자체 1", "지자체 2" 같은 빈칸 표현을 썼습니다.

이건 실제 조사 계획이 아니라 자리표시자에 가깝습니다.

왜 문제인가:

- 조사 에이전트가 어떤 지자체를 조사해야 하는지 알 수 없음.
- 재활용 포스터와 재활용 규칙이 서로 다른 지자체에서 나온 자료일 수 있음.
- 그러면 최종 보고서가 잘못된 비교를 할 위험이 있음.

### 4. `sem-reg-019`

질문:

Research testing requirements for drinking-water lead sampling, distinguishing lab testing from school IT systems.

관찰된 실패:

- 최종 상태: `blocked_semantic_review_failed`
- reviewer 점수: 8.7
- blocker: `SOURCE_CAP_CONTRADICTION`
- 직접 근거: locked constraint는 task당 source 1개를 요구했지만, 대부분 task는 `max_sources=2`, 일부는 `max_sources=3`으로 설정됨.

쉽게 설명:

계획에서는 "각 task는 source 1개로 제한한다"고 했는데, 실제 task 설정은 2개 또는 3개 source를 허용했습니다.

즉, 계획의 규칙과 실행 설정이 서로 달랐습니다.

왜 문제인가:

- source 예산이 실제 task에서 강제되지 않음.
- 실행 범위가 의도보다 커질 수 있음.
- validator는 이런 계획을 release-ready로 인정할 수 없음.

### 5. `sem-reg-024`

질문:

Research architecture review in hospital facility planning, not software architecture, using official planning guidance.

관찰된 실패:

- 최종 상태: `blocked_semantic_review_failed`
- reviewer 점수: 8.9
- blocker: `substitute_implementation_check_failed`
- 직접 근거: reviewer가 "계획이 내부 구현 작업이나 일반 템플릿 작업으로 대체된 것처럼 보인다"고 판단함.

쉽게 설명:

사용자 질문은 병원 시설 계획에서의 `architecture review`입니다. 소프트웨어 architecture가 아닙니다.

그런데 planner가 만든 계획이 병원 시설 계획이라는 도메인에 충분히 붙어 있지 않고, 일반적인 구현/템플릿식 계획처럼 보였기 때문에 막혔습니다.

왜 문제인가:

- 사용자의 질문 의미를 충분히 이해하지 못한 케이스에 가까움.
- `architecture`라는 단어가 소프트웨어가 아니라 시설/건축 맥락임을 끝까지 유지해야 함.
- semantic planner의 핵심 요구사항은 사용자의 실제 의도를 보존하는 것임.

### 6. `sem-reg-027`

질문:

Research public software supply-chain attestation implementation guidance from official standards and vendor docs.

관찰된 실패:

- 최종 상태: `completed_parallel`
- 실행 driver 기준: 성공
- release validator 기준: 실패
- 실패 이유: `semantic_materialization_diff.valid`가 `true`가 아님.
- 직접 근거:
  - `semantic_materialization_difference`
  - `semantic_materialization_lineage_failure`, count 3
  - `semantic_materialization_artifact_check_failed` for `search_results`

쉽게 설명:

이 케이스는 조사가 실행되긴 했습니다. 보고서와 산출물도 만들어졌습니다.

하지만 release validator가 "처음 semantic plan에서 만든 task와 실제 search 결과/산출물이 정확히 연결됐다는 증거가 부족하다"고 판단했습니다.

즉, 실행은 됐지만 계획과 결과물 사이의 연결고리, 즉 lineage가 깨진 것입니다.

왜 문제인가:

- release 기준에서는 "조사를 했다"만으로는 부족함.
- 어떤 최종 claim과 evidence가 어떤 semantic task에서 왔는지 추적 가능해야 함.
- lineage가 깨지면 planner가 만든 의미론적 분해가 실제 조사 결과로 이어졌다는 증명이 안 됨.

### 7. `sem-reg-029`

질문:

Research model risk management in banking regulation, separating statistical model governance from ML software implementation.

관찰된 실패:

- 최종 상태: `blocked_semantic_review_failed`
- reviewer 점수: 8.7
- blocker: `SOURCE_BUDGET_NOT_ENFORCED`
- 직접 근거: 계획은 최대 20개 source와 task당 decisive source 1개를 말했지만, 20개 task 중 19개가 `max_sources > 1`을 허용함.

쉽게 설명:

planner가 "각 task는 핵심 source 1개만 쓰는 방식으로 통제하겠다"고 했지만, 실제 task 대부분은 source를 2개 이상 쓸 수 있게 되어 있었습니다.

즉, 말로는 제한한다고 했지만 실행 계획에서는 제한이 지켜지지 않았습니다.

왜 문제인가:

- 전체 source budget이 실제로 강제되지 않음.
- 조사 범위가 의도보다 커질 수 있음.
- `sem-reg-004`, `sem-reg-019`와 같은 유형의 budget-control 실패임.

## 공통 패턴

사실:

- 6개 실패는 semantic reviewer가 실행 전 또는 release-ready 단계에서 막은 케이스입니다:
  - `sem-reg-004`
  - `sem-reg-013`
  - `sem-reg-018`
  - `sem-reg-019`
  - `sem-reg-024`
  - `sem-reg-029`
- 1개 실패는 실행은 끝났지만 release validator에서 실패한 케이스입니다:
  - `sem-reg-027`
- 가장 많이 나온 문제는 source budget과 bounded plan의 불일치입니다.

## 수정 및 재검증 결과

적용한 수정:

- source budget 문구와 task별 `max_sources`가 충돌하면 stale constraint를 제거하고, runner-level budget은 계속 유효하다는 문구로 정규화합니다.
- Edge/Safari처럼 여러 공식 vendor가 들어간 task는 `expected_source_types`가 일반적인 "official vendor docs" 하나여도 최소 source 수를 높입니다.
- `municipality 1` 같은 자리표시자 관할은 selection/binding workflow 없이는 통과하지 못하게 하고, 누락 시 workflow를 materialize합니다.
- visual task가 `max_images`보다 많은 이미지 확보를 요구하면 task 문장을 cap과 맞게 정규화합니다.
- 병원 시설·건축 `architecture` 문맥은 software/internal architecture 오탐으로 막지 않되, 실제 software architecture drift는 계속 차단합니다.
- release search result merge는 child가 보낸 `semantic_plan_hash`를 그대로 믿지 않고 부모 task의 canonical lineage로 정규화합니다.

targeted prepare 재검증:

- `sem-reg-004`: 통과, score 9.7, blocker 없음.
- `sem-reg-013`: 통과, score 9.7, blocker 없음.
- `sem-reg-018`: 통과, score 9.7, blocker 없음.
- `sem-reg-019`: 통과, score 9.8, blocker 없음.
- `sem-reg-024`: 통과, score 9.6, blocker 없음.
- `sem-reg-027`: prepare 통과, score 9.8, blocker 없음. 별도 lineage 회귀 테스트도 통과.
- `sem-reg-029`: 통과, score 9.8, blocker 없음.

targeted prepare 산출물:

- `/tmp/codex-dr-sem-reg7-prepare-20260714/sem_reg7_prepare_summary.json`
- `/tmp/codex-dr-sem-reg2-prepare-20260714/sem_reg2_prepare_summary.json`
- `/tmp/codex-dr-sem-reg024b-prepare-20260714/sem_reg024_prepare_summary.json`

검증 한계:

- 위 재검증은 원래 6개 planner/reviewer blocker와 `sem-reg-027` lineage 회귀를 확인하기 위한 targeted 검증입니다.
- #133 전체 signoff에는 여전히 30개 regression 전체 실행, 20개 Public Beta semantic, 12개 holdout, 5개 manual trace audit 실행이 필요합니다.

쉽게 말하면:

planner가 질문을 쪼개긴 했지만, 일부 케이스에서는 "실제로 실행 가능한 조사 계획"으로 충분히 단단하게 만들지 못했습니다.

특히 다음 문제가 반복됐습니다:

- 전체 source 제한과 task별 source 제한이 서로 맞지 않음.
- 비교 대상이 여러 개인데 source를 너무 적게 허용함.
- 실제 조사 대상을 정하지 않고 placeholder를 남김.
- 질문의 도메인을 충분히 보존하지 못하고 일반 템플릿처럼 흐름.
- semantic plan과 실제 산출물 사이의 lineage가 일부 깨짐.

## 다음 수정 방향

우선순위 높은 수정:

- 전체 source budget을 task 생성 단계에서 강제로 맞추기.
- 비교 질문에서는 비교 대상별 최소 source 수를 자동으로 확보하기.
- `municipality 1` 같은 placeholder가 나오면 release-ineligible로 막기.
- 병원 시설 계획처럼 단어가 애매한 질문에서 도메인 drift를 더 강하게 검출하기.
- `semantic_materialization_diff`에서 search result lineage가 깨지지 않게 보강하기.
