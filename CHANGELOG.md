# 변경 이력

형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/)를 따르고,
버전은 [유의적 버전](https://semver.org/lang/ko/)을 따릅니다.

## [0.6.0] — 2026-09-02

첫 공개 릴리스입니다. 동작은 0.5.0과 같고, 이름과 문서 동선만 바뀌었습니다.

### 변경 (호환성 깨짐)

- **이름을 `figma-lossless` 하나로 통일했습니다.** 이전에는 저장소·CLI·파이썬 모듈이 각각
  `figma-lossless-design-harness` / `figma-harness` / `lossless_figma_harness`로 갈라져 있었습니다.
  - CLI: `figma-harness` → **`figma-lossless`**
  - 모듈: `import lossless_figma_harness` → **`import figma_lossless`**
  - 상태 디렉터리: `<cwd>/.figma-harness/` → **`<cwd>/.figma-lossless/`**
  - 환경변수: `FIGMA_HARNESS_*` → **`FIGMA_LOSSLESS_*`**
    (`FIGMA_LOSSLESS_CACHE_DIR`, `FIGMA_LOSSLESS_NO_BOOTSTRAP`, `FIGMA_LOSSLESS_BOOTSTRAPPED`)
  - 캐시: `~/.cache/figma-lossless-design-harness/` → **`~/.cache/figma-lossless/`**
  - 플러그인: `figma-lossless@figma-lossless-dev`로 재설치해야 합니다.

### 문서

- README의 첫 동선을 **추출**(`collect` → `compile` → `export-design`)에서 끝나도록 바꿨습니다.
  Figma 토큰만 있으면 브라우저 캡처나 계약 작성 없이 사양 문서까지 도달합니다.
  `validate`와 게이트 12개는 "더 나아가려면"으로 옮겼습니다. 기능은 그대로입니다.
- 실제 제품에서 뽑았던 예제를 일반 로그인 화면 예제(`examples/sign-in/`)로 교체했습니다.
- **[게이트 설계 근거](./docs/gate-design-rationale.md)** 를 새로 실었습니다. 게이트 판정이 왜 이
  모양인지, 독립 반증 리뷰가 이미 커밋된 설계를 세 번 뒤집은 기록, 그리고 어떤 "개선"이 실제로는
  후퇴인지를 담았습니다.

### 테스트

- 실제 `collect` 번들로 회계를 검증하는 테스트가 하드코딩된 로컬 경로 대신
  `FIGMA_LOSSLESS_REAL_BUNDLE` 환경변수를 읽습니다. 변수가 없으면 건너뜁니다.

## [0.5.0] — 2026-09-02

시안 예시 값이 제품 코드에 남는 것을 검사가 **강요하던** 구조를 뒤집은 릴리스입니다.
이전에는 실제 사용자 데이터를 연결하면 `copy` 게이트에 걸려 떨어졌습니다.

### 추가

- **`data-contract.json`** — 사람이 확정한 "데이터 자리" 계약. 그 자리는 시안과의 정확 일치 대신
  연결 여부를 검사합니다. 캡처된 값이 시안 예시 값과 같으면 `slot-not-bound`(hard),
  비어 있으면 `slot-empty`, 선언한 형식과 다르면 `slot-shape-mismatch`.
- **`propose-slots`** — 어느 텍스트가 데이터 자리인지 후보를 제안합니다. 같은 화면의 여러 벌에서
  끝까지 바뀌지 않는 값을 찾는 방식입니다. 화면 묶기는 프레임 이름이 아니라 구조의 닮은 정도
  (요소 이름 구성의 Jaccard, 기본 0.8)로 합니다.
- **`propose-reuse`** — 레포에 이미 있는 코드를 재사용 후보로 제안합니다. 인덱스는 호출자가
  제공하며 하네스는 어떤 언어도 파싱하지 않습니다.
- **`product-route` 게이트** (12번째) — 데이터 자리가 선언된 화면을 실제 제품 라우트에서도
  캡처했는지 확인합니다. 캡처가 없으면 `product-route-unverified`이며 **승인할 수 없습니다.**
- **`validate --actual-b`** — 서로 다른 데이터로 두 번 캡처해, 값이 바뀌지 않는 자리를 찾습니다.
- 캡처 계획의 화면별 **`routeKind`** (`"fixture"` | `"product"`). 제품 라우트는 Figma 노드 id 대신
  `data-slot="<binding>"`만 수집합니다.
- 게이트 설정 키 `requireDataContract`(기본 `false`), `requireProductRouteCheck`(기본 `false`).
- `LICENSE`(MIT) 파일과 `pyproject.toml`의 `license` 항목. 이전에는 플러그인 매니페스트만
  MIT라고 선언하고 전문이 없었습니다.

### 변경

- **README를 문제 진술로 시작하도록 재구성했습니다.** 이전에는 정의 없는 요약 문장과 내부 구조
  설명이 먼저 나오고, 실행 가능한 명령이 111번째 줄에 있었습니다. 설계 근거는 한 글자도 지우지 않고
  아래로 옮겼습니다.
- 제품 라우트만 캡처된 화면은 **디자인 검증이 끝난 화면으로 세지 않습니다**(`product-verified-only`).
  화면별 통과 표시도 그 화면에서 실제로 측정한 결과만 반영합니다 — 이전에는 다른 화면의 게이트
  상태가 복사됐습니다.
- 데이터 계약은 컴파일 시 **컴파일된 화면과 대조**합니다. 없는 노드를 가리키거나 `designExemplar`가
  시안 원문과 다르면 hard 실패입니다.

### 수정

- 가시성 정보가 없는 과거 캡처를 "숨겨진 슬롯"으로 단정하던 문제. 이제 `slot-visibility-unknown`
  (승인 불가)으로 구분해 보고하고, 값 판정은 그와 무관하게 계속 실행합니다.
- 테스트 픽스처에 실제 이메일 주소가 들어가 있던 것을 합성 주소로 교체했습니다.

### 호환성

- **계약을 주지 않으면 동작이 바뀌지 않습니다.** 기존 번들로 검증하면 이전과 같은 결과가 나옵니다
  (실측: 파일럿 번들의 승인 편차 1,690건 동일). 다만 실행당 1회 `data-contract-absent` 경고가
  남습니다.
- 캡처 계획에 `routeKind`가 없으면 `"fixture"`로 동작합니다.
- 게이트가 11개에서 **12개**로 늘었습니다. 게이트 수를 세는 자동화가 있다면 갱신이 필요합니다.

### 검증

- 테스트 **471개 통과** (0.4.0 시점 369개).
- 독립 품질 감사 2회에서 나온 BLOCK 지적을 모두 반영했습니다.

## [0.4.0] — 2026-08-12

- 디자인 사양 모드(`export-design`)와 훅의 `design-spec` 스코프.
- 아무것도 측정하지 않은 게이트를 통과로 처리하던 구멍을 막았습니다(`must_evaluate`).
- `flow`·`component` 계약을 기본 필수로 전환했습니다.
- 승인 편차를 denylist에서 **allowlist**로 바꿨습니다. denylist였을 때는 "아직 검증하지 않음"
  종류가 설정 세 줄로 통과됐습니다.
- `copy`·`style` 게이트가 폼 컨트롤(`value`/`placeholder`/`::placeholder`)을 읽습니다.
- 캡처 성능 개선(61화면 10분+ → 46초).

## [0.3.0] — 2026-08-05

- 첫 공개 가능 형태. 수집·컴파일·검증 파이프라인과 강제 훅.
