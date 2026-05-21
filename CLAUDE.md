# AutoDeploy — Claude Code Instructions

# 절대 하지 말아야 할 것들
- 내 허락 없이 파일 삭제하지 마
- 모르면 추측하지 말고 물어봐
- 작업 중간에 임의로 다른 방향으로 바꾸지 마

# 절대로 지켜야 할 것들
- 95% 확신이 생기기 전에는 어떤 변경도 하지 말아줘. 그 확신에 도달할 때까지 추가 질문을 해줘.

# 작업 마칠 때마다 해야할 것들
- 새 세션이 시작되면 가장 먼저 "progress.txt" 파일을 읽고, 끝낼 때 "뭘 했고, 어디까지 됐고, 다음엔 뭘 해야 해"를 기록해

# Git 규칙
- 절대 main 브랜치에 push/merge 하지 마
- 커밋할 때, 어떤 내용을 작업했는지 작성해줘
- 모든 작업은 claude 브랜치에서만
- main 머지는 CEO가 수동으로 함

---

# 에이전트 라우팅 룰

## 파이프라인 (Phase 1 → 6)

```
① Research → ② Planning → ③ Design → ④ Dev → ⑤ QA → ⑥ Launch
@researcher    @planner     @designer   @developer  @qa    @marketer
```

## 언제 어떤 에이전트를 쓸 것인가

### @researcher — Phase 1: 리서치
- 경쟁사 분석 요청 시
- 시장/트렌드 리서치 요청 시
- 기능 기획 전 유저 니즈 조사 시
- 산출물 → `docs/research/`

### @planner — Phase 2: 기획
- 새 기능의 개발지시서(Dev Spec) 작성 시
- 기존 기능의 스펙 변경/추가 시
- DB 스키마 설계가 필요한 작업 시
- 산출물 → `docs/specs/dev-spec-*.md`

### @designer — Phase 3: 디자인
- UI/UX 디자인 명세 작성 시
- 기존 화면 리디자인 시
- 컴포넌트 스타일 결정이 필요한 작업 시
- 반드시 개발지시서가 먼저 있어야 함
- 산출물 → `docs/specs/design-spec-*.md`

### @developer — Phase 4: 개발
- 기능 구현 시 (개발지시서 + 디자인 명세 필요)
- 버그 수정 시
- 리팩토링 시
- 산출물 → 코드 (claude 브랜치)

### @qa — Phase 5: 검수
- 구현 완료 후 코드 검수 시
- 보안 리뷰가 필요한 시
- 수용 기준 대조 검증 시
- 산출물 → `docs/qa/qa-report-*.md`

### @marketer — Phase 6: 마케팅
- Product Hunt 런칭 콘텐츠 제작 시
- 블로그/체인지로그 작성 시
- 이메일 캠페인 작성 시
- 산출물 → `docs/marketing/`

## 라우팅 판단 기준

| 사용자가 이런 말을 하면 | 이 에이전트로 |
|----------------------|-------------|
| "~에 대해 조사해줘", "경쟁사 분석" | @researcher |
| "~기능 기획해줘", "스펙 작성", "개발지시서" | @planner |
| "~화면 디자인해줘", "UI 명세", "디자인 명세", "디자인 목업" | @designer |
| "~기능 구현해줘", "수정해줘", "개발해줘", "코드 작성" | @developer |
| "~검수해줘", "QA", "코드 리뷰", "보안 리뷰" | @qa |

## 에이전트 순차 호출 (풀 파이프라인)

하나의 기능을 처음부터 끝까지 만들 때:
1. `@researcher` — 리서치 → `docs/research/` 산출
2. `@planner` — 리서치 기반 개발지시서 → `docs/specs/dev-spec-*.md` 산출
3. `@designer` — 개발지시서 기반 디자인 명세 → `docs/specs/design-spec-*.md` 산출
4. `@developer` — 두 명세 기반 구현 → 코드 커밋
5. `@qa` — 구현 검수 → `docs/qa/qa-report-*.md` 산출

## 병렬 가능한 조합
- `@planner` + `@researcher` (서로 다른 주제)

## 병렬 불가 (충돌 위험)
- `@developer` 2개 동시 (공유 파일: router, locales, types)
- `@developer` + `@designer` 같은 기능 (명세 확정 전 구현 불가)

---

# 참조 문서
- PRD: `docs/prd.md`
- 아키텍처: `docs/ARCHITECTURE.md`
- UI 가이드: `docs/UI_GUIDE.md`
- 파이프라인 가이드: `_template/pipeline-guide.md`
- 개발지시서 템플릿: `_template/dev-spec-template.md`
- 디자인 명세 템플릿: `_template/design-spec-template.md`