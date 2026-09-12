# 검증 모드 — 구현이 시안대로 됐는지 기계로 판정하기

이 문서는 **검증 모드**의 전부입니다: 게이트 12개, 실행 절차, 설정, 승인 편차, 강제 훅, 그리고 왜 이렇게 설계했는지. 추출만 쓴다면 읽을 필요가 없습니다 — [README](../README.md)의 추출 절차로 충분합니다.

검증 모드는 기본으로 **잠겨** 있습니다. 구현물·dev 서버·Playwright·계약 파일이 있고, 세션이 검증을 끝까지 돌리도록 강제되는 것에 동의할 때 풉니다.

```bash
figma-lossless mode --set verify      # 이 디렉터리 트리에서 잠금 해제 (.figma-lossless/mode.json)
FIGMA_LOSSLESS_MODE=verify figma-lossless validate …   # 또는 이 프로세스에서만 (유효한 값이면 파일보다 우선)
figma-lossless mode --clear           # 파일 삭제 → 다시 잠금 (환경변수가 켜져 있으면 그 프로세스는 여전히 verify)
```

모드 파일은 현재 디렉터리에서 위로 올라가며 가장 가까운 것을 씁니다(git 이 저장소를 찾는 방식과 같습니다). 잘못된 값은 무시하고 다음 출처(환경변수 → 파일 → 기본 `extract`)로 넘어갑니다. 잠금 상태에서 `validate`·`propose-*`·`vendor-assets`·캡처 어댑터를 실행하면 리포트·스크린샷 같은 산출물을 만들기 전에 안내 문구와 함께 `exit 1`로 거부됩니다(플러그인 런처의 첫 실행 부트스트랩 캐시는 그 전에 만들어질 수 있습니다). **강제 훅을 켜는 것은 파일입니다** — 훅은 Claude Code 를 띄운 환경만 보므로 명령 앞에 붙인 환경변수로는 켜지지 않습니다([상태와 강제 훅](#상태와-강제-훅)).

## 파이프라인

```text
Figma REST API ── collect: 버전 고정, 노드별 1:1 원본 + SHA-256,
        │         expected==collected 증명, --include-images 렌더 PNG
        ▼
Design IR compiler ── 속성 회계(compiled | preservedOpaque | unsupported)
        │             ▲ MCP 보완재: get_variable_defs(토큰 정의), Code Connect
        ▼
표현 컴포넌트 + 닫힌 검사 페이지 + 실제 제품 페이지
        │
        ▼
Playwright 캡처 어댑터 ── DOM rect/computed style/var() 토큰 참조/스크린샷
        │
        ▼
12 verification gates ── defect manifest ── HTML report / CI exit code
```

### 이 설계가 나온 실패

어떤 파일럿은 화면 61개를 시안과 픽셀 단위로 똑같이 만들어 "통과" 판정을 받았는데, 열어 보니 **입력창이 눌리지 않고 사용자 이름 자리에 예시 값이 박혀 있었습니다.** 당시 검사 규칙이 "시안 글자와 한 글자도 다르면 불합격"이라 **진짜 사용자 이름을 넣으면 오히려 떨어졌기 때문**입니다. 지금은 "이 자리가 고정 문구인가, 데이터가 들어올 자리인가"를 나눠 판정하고, 데이터 자리에 시안 예시 값이 그대로 있으면 **불합격**입니다.

### 답하는 질문 / 답하지 않는 질문

| 이 도구가 답합니다 | 답하지 않습니다 |
|---|---|
| 이 화면이 시안과 같은 값으로 그려졌는가 | 이 화면이 **좋은 디자인인가** |
| 문구가 로케일마다 정확한가 | 시안에 **없는 상태**(예: 꺼진 토글)를 어떻게 그릴지 |
| 데이터 자리에 예시 값이 남아 있지 않은가 | 그 데이터가 **진짜 API에서 왔는가**(예시 값만 아니면 통과) |
| 무엇을 **검사하지 않았는지** | 클릭·입력이 동작하는가 (사람이 쓴 E2E 결과를 받아 확인만 함) |

### 계약을 만드는 사람의 부담을 줄이는 규칙

이 도구는 **선언한 것만 검사할 수 있습니다.** 검사 항목을 늘릴 때마다 사람이 쓸 계약이 늘어나고, 그게 지나치면 아무도 안 쓰게 됩니다(어떤 파일럿은 승인 항목을 461건 써야 했습니다). 그래서 **검사 항목을 추가할 때는 그 계약의 초안을 뽑아 주는 명령을 같이 만듭니다.**

| 계약 | 초안을 뽑아 주는 명령 |
|---|---|
| 데이터 자리 (`data-contract.json`) | `propose-slots` |
| 재사용할 기존 코드 | `propose-reuse` |
| 로케일 문구 대조 | `export-copy` |

모든 제안은 **사람 확인이 필수**입니다(`requiresConfirmation: true`). 자동으로 적용되는 것은 없습니다.

## 게이트

| Gate | 하드 실패 조건 |
|---|---|
| `coverage` | 발견된 화면의 상세 context 또는 구현 snapshot 누락, `specCompiled=false` |
| `accounting` | 컴파일러가 분류하지 못한 canonical 속성(미지의 key), 수집 불완전(`incomplete-collection`) |
| `structure` | 표시되어야 하는 `data-node-id` 요소 누락, 숨김 요소가 실제로 렌더링됨 |
| `copy` | Figma 원문과 텍스트가 한 글자라도 다름 (placeholder·aria-label 등 속성 카피 포함) |
| `product-route` | 데이터 슬롯이 있는 화면을 실제 제품 라우트에서 캡처했는지 확인. 기본은 누락 warning, `requireProductRouteCheck:true`이면 hard |
| `geometry` | x/y/width/height가 허용 오차 초과 |
| `style` | 글꼴·색(알파 포함)·행간·자간·padding·gap·border·radius·그림자·그라데이션·textAlign 등 계약 불일치 |
| `token` | Figma 변수에 바인딩된 속성을 구현이 `var(--…)` 참조 없이 하드코딩 (정책: off/warn/hard, 기본 warn) |
| `asset` | 임시 Figma URL을 로컬 파일+SHA-256으로 동결하지 않음 또는 구현 asset 불일치 |
| `visual` | reference/actual 스크린샷 부재, viewport 불일치, pixel mismatch 비율 초과 |
| `component` | 디자인 시스템 매핑/차이 결정이 없거나 구현에서 관찰되지 않음 |
| `flow` | 모든 상태 전이에 대응하는 실행 테스트가 없거나 실패 |

### 아무것도 재지 않은 게이트

`checked == 0` 인 게이트는 통과가 아니라 `NOT_EVALUATED` 이고, **번들의 증거가 그 게이트에 잴 것을 줬는데도 0건**이면 결함입니다. 이 판정은 `require*` 플래그가 아니라 증거에서, 그리고 **배제를 적용하기 전에** 유도합니다 — 배제가 게이트를 비우는 바로 그 수단이라, 배제 후에 읽으면 "원래 잴 게 없었다"고 동의해 버리기 때문입니다.

feature 단위로 비는 경우:

| 게이트 | 결함 | 조건 |
|---|---|---|
| `coverage` | `gate-not-evaluated` (hard) | manifest 가 화면을 선언했는데 `coverage.json` 이 비었음 |
| `accounting` | `gate-not-evaluated` (hard) | 정본(`figma-rest`) 번들이거나 manifest 가 선언했는데 `property-accounting.json` 이 없음 |
| `accounting` | `accounting-artifact-unusable` (hard) | 아티팩트는 있으나 `counts.nodes` 가 0이거나 `restCollection` 이 없음 |
| `token` · `component` · `flow` | `gate-not-evaluated` (hard) | 정책이 `hard` 이거나 계약이 존재하는데 0건 |

**화면 단위**로 비는 경우는 따로 봅니다. feature 합계만 세면 화면 A 를 통째로 비워도 화면 B 의 1건이 총계를 양수로 만들어 PASS 가 나오고, A 는 어디에도 나타나지 않습니다.

| 결함 | 심각도 | 조건 |
|---|---|---|
| `screen-absent-from-coverage` | hard | 컴파일된 화면이 `coverage.json` 에 없음 |
| `screen-absent-from-manifest` | hard | `coverage.json` 에는 있는데 manifest 가 그 화면의 계약을 컴파일하지 않음 |
| `copy-fully-excluded` · `geometry-fully-excluded` · `style-fully-excluded` | warning | 그 화면에 이 게이트가 비교할 계약이 하나도 남지 않음 |
| `reference-screenshot-absent` | warning | `requireReferenceScreenshots:false` 상태에서 그 화면에 레퍼런스가 없음 |

세 `*-fully-excluded` 가 실패가 아니라 경고인 것은 판단입니다. 배제는 원래 무언가를 없애라고 있는 기능이고 — mock 상태바 시계를 구현할 사람은 없습니다 — 텍스트나 스타일이 device chrome 뿐이던 화면은 배제 후 **이 게이트가 읽을 게 없는 정상 화면**이 됩니다. 배제가 전혀 없어도 같은 모양이 나옵니다: MCP 경로는 클래스 없는 요소에 `style: {}` 를, canonical 경로는 `absoluteBoundingBox` 없는 노드에 값이 전부 `null` 인 rect 를 씁니다. 여기서 죽이면 배제가 문서화된 일을 한 것을 벌하게 됩니다.

대신 **침묵하지 않습니다.** 침묵하면 "이 화면엔 원래 잴 게 없었다"와 "이 화면이 검증에서 빠졌다"가 초록 리포트 안에서 똑같이 보이기 때문입니다.

`NOT_EVALUATED` 게이트는 화면을 보증하지 않습니다. coverage 표의 `copyPassed`·`assetPassed` 같은 플래그는 게이트 상태가 `PASS` 일 때만 참입니다 — 예전에는 "아무것도 안 잰 게이트"의 `passed: true` 를 읽어 61화면 전부에 `assetPassed: true` 를 찍었습니다.

빠져나가는 길은 승인이 아니라 설정입니다(이 결함들은 전부 승인 불가). 배제 범위를 좁히거나(`excludedSubtreeNames`·`excludedInteriorNames`), 번들을 다시 컴파일하거나, 해당 요구를 `false` 로 선언하십시오.

`visual` 게이트 자체에는 `gate-not-evaluated` 가 없습니다. `visualPolicy` 는 픽셀 차이의 **심각도** 다이얼이고, 비교를 요구하는 키는 `requireReferenceScreenshots` 이며 그건 이미 화면별 하드 실패로 발화하기 때문입니다.

## 모드 (스코프)

| 스코프 | 필요한 모드 | 단계 | 터미널 상태 |
|---|---|---|---|
| **디자인 사양** (구현자에게 넘김) | 추출(기본) | `collect` → `compile` → `export-design` | 사양 문서 |
| **카피 감사** (다국어 문구 체크) | 추출(기본) | `collect`(이미지 생략) → `compile` → `export-copy` → 카탈로그 diff | diff 보고서 |
| **전체 검증** | **검증(잠금 해제)** | 전체 파이프라인 | fresh `validate` exit 0 (또는 사용자 결정만 남은 결함 보고) |

### 디자인 사양 모드

번들은 게이트용 자료구조입니다 — manifest 와 화면별 JSON, node id 로 주소를 매긴 평평한 요소 목록. 사람이 읽고 구현할 형태가 아닙니다. `export-design` 은 같은 증거를 **하나의 읽을 수 있는 문서**로 펴서, 에이전트나 사람에게 "이대로 만들어 주세요" 하고 건넬 수 있게 합니다.

```bash
figma-lossless export-design --bundle <bundle> --output spec.md \
  --exclude "Status Bar,Home Indicator"          # 디바이스 크롬 제외
figma-lossless export-design --bundle <bundle> --output spec.json --format json
```

- 요소는 Figma 의 부모/자식 구조 그대로 들여쓰기되고, 각 문구는 그것을 보여주는 노드에 붙습니다.
- 시안에서 숨긴 레이어는 **"must not render"** 로 명시합니다. 그냥 빼면 요구사항이 부재로 바뀝니다.
- 가독성을 위해 값이 0인 spacing·border 와 빈 효과 목록은 생략하고, **생략했다는 사실을 문서 머리에 적습니다.** `--all-properties` 로 끄거나 `--format json` 을 쓰면 전량이 나옵니다.
- `--screens` 로 화면을 고를 수 있고, 번들에 없는 id 를 주면 빈 파일 대신 오류가 납니다.
- **단방향 내보내기입니다.** 이 파일을 쓴다고 나중에 검증을 돌릴 의무가 생기지 않습니다. node id 를 같이 싣는 것은 하네스로 돌아올 **수 있게** 하려는 것이지 돌아오라는 뜻이 아닙니다.

폴백: REST 접근이 불가하면 저장된 Figma MCP `CallToolResult` JSON을 `compile --input`으로 컴파일하는 기존 경로가 동작합니다. 단, MCP 경로는 스타일 계약이 좁고 수집 완전성 증명이 없습니다. 두 입력을 함께 주면 canonical REST가 속성의 우선 소스입니다.

## 작동법

Python 3.9+와 Pillow가 필요합니다. 저장소에서 직접 실행할 때는 `PYTHONPATH=src`를 붙입니다. 종료 코드 규약: `0` 성공/통과, `2` 게이트·완전성 실패, `1` 입력 오류.

**0. 토큰 준비** — `~/.figma-token`에 personal access token을 넣습니다. bare 토큰 또는 `FIGMA_TOKEN=`/`FIGMA_*_TOKEN=` 형식(`export` 접두 허용)만 인정하며, 다른 이름의 시크릿이 섞인 파일은 오전송 방지를 위해 거부됩니다. REST 파일/노드/이미지 엔드포인트는 플랜 무관, Variables API만 Enterprise 전용이라 토큰 정의는 MCP `get_variable_defs`로 보완합니다.

**1. 수집** — 파일 버전을 고정하고 노드별 원본과 렌더 PNG를 동결합니다.

```bash
PYTHONPATH=src python3 -m figma_lossless collect \
  --file-key <URL의 file key> \
  --node-ids "22137:49538,22137:49564" \
  --output evidence --include-images
```

URL의 `node-id=22137-49538`은 `22137:49538`(콜론)으로 변환합니다. exit 0이 `expected == collected` 증명입니다. resume 시 저장 바이트를 재검증하고, 파일 버전이 바뀌면 전체 재수집합니다(버전 혼합 금지).

**2. 컴파일** — canonical JSON을 IR로 매핑하고 모든 속성을 회계 처리합니다.

```bash
PYTHONPATH=src python3 -m figma_lossless compile \
  --rest-input evidence --output bundle --feature-id <기능 ID> \
  [--variable-defs variable-defs.json] [--flow-contract …] [--component-map …] \
  [--data-contract data-contract.json]
```

`missingContexts`와 `accountingViolations`가 0이 되기 전에는 구현을 시작하지 않습니다. 회계 위반은 "Figma가 준 속성을 분류하지 못했다"는 뜻이며 조용한 해소는 허용되지 않습니다.

**2.5 계약 초안 받기(선택, 권장)** — 사람이 빈 종이에서 계약을 쓰지 않게 하는 단계입니다. 두 명령 모두 **제안만 하고 아무것도 적용하지 않습니다**(`requiresConfirmation: true`).

```bash
PYTHONPATH=src python3 -m figma_lossless propose-slots \
  --bundle bundle --output slots.json [--format md]
```

`propose-slots`는 **어느 텍스트가 데이터 자리인지** 후보를 뽑습니다. 원리는 단순합니다 — 같은 화면의 여러 벌(언어 4개 × 상태 3개 같은 것)을 겹쳐 놓고 **끝까지 안 바뀌는 글자**를 찾습니다. 라벨은 번역되지만 사람 이름과 이메일은 번역되지 않기 때문입니다.

- 화면을 묶는 기준은 **프레임 이름이 아니라 구조의 닮은 정도**입니다(요소 이름 구성의 Jaccard ≥ `--group-similarity`, 기본 0.8). 이름으로 묶으면 이름이 같은 다른 화면이 한 그룹이 되어 판정이 무의미해집니다.
- 변형끼리 너무 비슷하면(불변 비율 > `--max-invariance-ratio`, 기본 0.5) 신호가 없다고 보고 **제안 대신 경고**를 냅니다. 한 로케일만 있는 그룹이 대표적입니다.
- **못 찾는 것**: 로케일마다 형식이 바뀌는 데이터(영문 `1`, 국문 `1개`)는 글자가 다르므로 걸리지 않습니다. 이건 사람이 직접 계약에 넣어야 합니다.

```bash
PYTHONPATH=src python3 -m figma_lossless propose-reuse \
  --bundle bundle --repo-index repo-index.json --output reuse.json [--min-overlap 2]
```

`propose-reuse`는 **이미 레포에 있는 코드**를 후보로 제시합니다. 시안의 문구가 이미 메시지 카탈로그에 있으면 그 문구를 쓰는 파일이 곧 재사용 후보이기 때문입니다.

하네스는 **어떤 언어도 파싱하지 않습니다.** 인덱스는 호출자가 만들어 넘깁니다. 최소 형태는 이렇습니다.

```json
{
  "schemaVersion": 1,
  "strings": [
    {"value": "동행 수", "source": "src/app/fare.constant.ts:314", "symbol": "companionCountHeader"}
  ],
  "components": [{"name": "FareGuideSection", "source": "src/components/FareGuideSection.tsx"}]
}
```

`확인`·`취소` 같은 흔한 문구가 후보를 부풀리지 않도록, **너무 많은 파일에 나오는 값은 후보 산정에서 뺍니다**
(`--max-source-frequency`, 기본 20개 파일).

**3. asset 동결** — `vendor-assets --bundle bundle`. canonical Figma asset URL만 허용하고 redirect·크기·형식·active SVG를 검사합니다.

**4. 구현은 세 층으로 나눕니다.** 검사 코드가 제품 코드에 달라붙지 않게 하는 경계입니다. 자동차 충돌 시험을 떠올리면 쉽습니다. 실제 차체는 함께 쓰되, 센서와 더미 승객은 시험장에만 둡니다.

1. **껍데기 컴포넌트(표현 컴포넌트)** — 화면을 그리는 컴포넌트입니다. 표시할 데이터, 클릭 콜백, 선택형 `ids?`를 모두 props로 받습니다. 제품 훅이나 API를 직접 호출하지 않습니다. `data-node-id={ids?.email}`처럼 `ids`가 있을 때만 Figma 마커를 붙입니다. 반면 `data-slot="user.email"`은 “여기가 사용자 이메일 자리다”라는 제품 의미가 있으므로 해당 값의 요소에 계속 둡니다. `data-testid`처럼 검사기가 찾을 수 있는 의미 있는 이름표입니다. padding·gap은 자식 위치로 재므로 화면 루트뿐 아니라 계약된 자손에도 같은 방식을 씁니다.
2. **검사 전용 페이지(fixture)** — fixture는 테스트를 위해 입력을 고정한 페이지입니다. Figma의 예시 값과 `ids`를 껍데기 컴포넌트에 주입합니다. 이 라우트는 배포 빌드에서 제외하거나, 명시적인 검사 환경이 아니면 404를 반환해 사용자가 열 수 없게 닫습니다. `data-component`, `data-figma-asset`, `data-asset-sha256` 같은 검사 메타데이터도 이 층에서만 주입합니다.
3. **제품 페이지** — 실제 훅/API에서 받은 데이터와 실제 콜백을 같은 껍데기 컴포넌트에 전달합니다. `ids`는 넘기지 않습니다. 슬롯 요소에는 계약의 `binding`과 같은 `data-slot`을 둡니다. 예를 들어 계약의 `binding`이 `user.email`이면 DOM도 `data-slot="user.email"`입니다. Figma 예시 값을 기본값이라는 이름으로 제품 소스에 복사하지 않습니다.

```tsx
// ProfileShell 안의 이메일 요소에는 data-slot="user.email"이 있다.
const auditPage = <ProfileShell email={figma.email} onSave={noop} ids={{email: "21734:37250"}} />;
const productPage = <ProfileShell email={user.email} onSave={saveProfile} />;
```

검사 전용 페이지만 캡처하면 충분하지 않습니다. 그 캡처는 껍데기가 시안처럼 그려진다는 것만 증명합니다. 제품 페이지가 여전히 `Fei Lin` 같은 예시 사용자를 하드코딩하거나 실제 데이터 훅을 우회해도 보지 못합니다. 따라서 **데이터 슬롯이 선언된 화면은 제품 라우트도 한 번 `routeKind: "product"`로 캡처해야 합니다.** 제품 캡처가 하나도 없으면 `product-route-unverified`가 남습니다. 기본은 warning이고 `requireProductRouteCheck:true`이면 hard이며, 검증 부재이므로 승인 편차로 없앨 수 없습니다.

**5. 캡처** — 구현 리포에서 dev 서버를 띄우고 실행합니다. localhost same-origin만 허용, 폰트/이미지 readiness·애니메이션·locale·timezone·DPR 고정. 화면별 `routeKind`는 `"fixture"` 또는 `"product"`이며, 생략하면 기존 계획서 호환을 위해 fixture로 동작합니다.

- fixture 캡처는 지금까지와 같습니다. 화면 루트와 계약된 자손의 `data-node-id`를 찾아 카피·지오메트리·스타일·구조·에셋·토큰·픽셀 증거를 모읍니다.
- product 캡처는 `[data-node-id]` 루트를 찾지 않습니다. 문서 전체의 `[data-slot]`만 모으고, 계약의 `binding`으로 값을 찾습니다. 이 화면에는 카피·지오메트리·스타일·구조 등 디자인 게이트를 적용하지 않습니다. 제품 캡처가 초록이어도 “화면 전체가 검증됐다”는 뜻이 아니라 **“실제 데이터 자리가 비지 않았고 시안 예시값에 묶이지 않았다”**는 뜻뿐입니다.

```json
{
  "screens": [
    {"nodeId": "21734:37250", "name": "Profile", "route": "/profile", "routeKind": "product"}
  ]
}
```

같은 Figma 화면의 fixture와 product 라우트는 캡처 계획을 두 개로 나눠 각각 실행합니다. 검증할 때 fixture 산출물을 `--actual`, product 산출물을 `--actual-b`로 전달하면 한 보고서가 두 증거를 함께 읽습니다. `--actual-b`를 서로 다른 fixture 데이터 캡처에 쓰는 차등 검사와 product 캡처가 모두 필요하면 별도 검증 실행으로 각각 증거를 남깁니다.

```bash
node adapters/playwright-capture.mjs --plan capture-plan.json --output actual.json
```

**6. 검증** — 실패 시 exit 2이므로 CI merge gate로 바로 사용합니다.

```bash
PYTHONPATH=src python3 -m figma_lossless validate \
  --bundle bundle --actual actual-a.json [--actual-b actual-b.json] \
  --output report [--config gate-config.json]
```

리포트는 `report/report.html`, 기계 판독은 `gate-results.json`, 결함 목록은 `defects.json`. 완료 판정의 근거는 항상 이 산출물이지 채팅 보고가 아닙니다.

`data-contract.json`은 사람이 확정한 데이터 슬롯과 고정 chrome을 선언합니다. 슬롯은
`screenNodeId`, `nodeId`, 자유 문자열 `binding`, 필수 `designExemplar`와 선택 `shape`
(`email`, `time`, `numericMask`, `singleGrapheme`, `text`)를 가집니다. 슬롯은 exact copy
대신 비어 있음·시안 exemplar 재사용·shape 위반을 검사하고, chrome은 copy 비교에서만 빠진
사유가 배제 장부에 남습니다. `--actual-b`를 주면 같은 슬롯이 서로 다른 데이터에서도 바뀌지
않는 경우를 `slot-not-bound`로 검출합니다. 생략하면 검사를 하지 않았다는 warning이 남습니다.

**7. 카피 감사(선택)** — 로케일별 문구를 구현 없이 추출해 카탈로그와 대조합니다.

```bash
PYTHONPATH=src python3 -m figma_lossless export-copy \
  --bundle bundle --output copy.json [--format csv]
```

### 게이트 설정 (gate-config.json)

| 키 | 기본값 | 의미 |
|---|---|---|
| `geometryTolerancePx` | 1 | rect 허용 오차(px) |
| `styleNumericTolerance` | 0.1 | px 단위 스타일 값 허용 오차 |
| `styleRatioTolerance` | 0.01 | lineHeight(비율) 허용 오차 |
| `styleOpacityTolerance` | 0.01 | opacity 허용 오차 |
| `visualChannelTolerance` / `visualMaxMismatchRatio` | 8 / 0.005 | 픽셀 비교 채널 허용치 / 불일치 비율 상한 |
| `tokenUsagePolicy` | `"warn"` | token 게이트 정책 (`off`/`warn`/`hard`) |
| `tokenMap` | `{}` | Figma 변수명 → CSS 커스텀 프로퍼티명 매핑 |
| `requireFlowContract` | `true` | flow 계약이 없으면 hard 실패 |
| `requireComponentContract` | `true` | component 계약이 없으면 hard 실패 |
| `requireDataContract` | `false` | data 계약이 없으면 hard 실패(기본은 기존 번들 호환 warning) |
| `requireProductRouteCheck` | `false` | 슬롯 화면의 제품 라우트 캡처가 없으면 hard 실패(기본은 warning) |

`tokenMap`은 빈 상태로 첫 validate를 돌리면 `token-unmapped` 경고가 채워야 할 목록을 나열해 줍니다. **허용치를 키워 결함을 침묵시키는 것은 금지된 경로입니다** — 의도적 차이는 `approvedDeviations`로 명시 승인합니다. 훅이 이 파일의 수정에 사용자 승인을 요구하는 이유입니다.

#### 계약을 요구하는 두 키

`requireFlowContract` / `requireComponentContract`는 **기본값이 `true`** 입니다. 계약을 주지 않으면 해당 게이트가 `flow-contract-missing` / `component-contract-missing`으로 hard 실패합니다. 검증되지 않은 동작과 컴포넌트 정체성이 조용히 통과하던 경로를 막기 위한 기본값입니다.

플로우가 실제로 없는 기능이나 공용 컴포넌트를 쓰지 않는 화면이라면, 그 사실을 **설정으로 선언**하십시오:

```json
{ "requireFlowContract": false, "requireComponentContract": false }
```

이건 결함을 숨기는 것이 아니라 "이 기능에는 검증할 플로우가 없다"는 판단을 리뷰 가능한 형태로 남기는 것입니다. 계약 없이 조용히 통과하는 것과의 차이가 여기에 있습니다.

#### 승인 편차(`approvedDeviations`)의 경계

엔트리는 **사유(`reason`)와 선택자를 모두** 가져야 합니다. 선택자는 `gate`, `type`, `screenNodeId`, `elementNodeId`, `nodeId`, `property` 중 최소 하나입니다. 사유만 적은 엔트리는 런의 모든 hard 결함에 매치되므로 `approved-deviation-unscoped`로 거부됩니다.

그리고 **승인할 수 있는 결함 종류가 한정돼 있습니다.** 승인은 "이 차이는 받아들일 만하다"는 판단이므로, 검사가 실제로 돌아 **두 값을 비교한 결과**인 결함만 대상이 됩니다:

`copy-mismatch`, `missing-copy`, `slot-empty`, `slot-not-bound`, `slot-shape-mismatch`, `geometry-mismatch`, `style-mismatch`, `gradient-missing`, `visual-mismatch`, `missing-element`, `hidden-element-rendered`, `rendered-asset-mismatch`, `token-not-used`, `component-not-observed`, `flow-test-failed`

**나머지는 전부 승인할 수 없습니다.** `flow-test-missing`(E2E가 없음), `flow-assumption-unresolved`(전이를 확인 안 함), `component-decision-open`(매핑 결정 안 함), `gate-not-evaluated`(게이트가 아무것도 안 잼), `product-route-unverified`(제품 라우트 캡처가 없음), `slot-marker-missing`(제품 DOM에서 binding 이름표를 찾지 못함), `missing-actual-*`(캡처에 데이터가 없음) 같은 것들은 **검증이 일어나지 않았다**는 사실이지 판단할 차이가 아닙니다. 승인을 시도하면 매치되지 않고 `unusedEntries`에 나타납니다.

이건 의도적으로 allowlist입니다. 처음에는 "승인 못 하는 목록"으로 만들었다가, 목록에 없는 모든 종류가 기본 승인 가능이라는 걸 발견했습니다 — 파일럿의 blocker 29건(전부 "아직 검증 안 함")이 config 3줄로 통과됐습니다. 새 결함 종류를 추가하는 비용은 `UNAPPROVABLE_DEFECT_TYPES`에 한 줄이고, 반대 방향의 비용은 조용한 통과입니다.

## 상태와 강제 훅

`skills/verify-design/SKILL.md`의 실행 정책(완주 원칙·스코프·역질문 규칙) 중 기계적으로 판정 가능한 부분은 플러그인 훅(`hooks/hooks.json` + `hooks/harness_hook.py`)이 강제합니다. 플러그인 설치 시 자동 등록되고 제거 시 함께 사라집니다. **검증 모드로 잠금을 푼 디렉터리에서만 동작하고**, 추출 모드에서는 어떤 이벤트에도 반응하지 않습니다(상태 파일도 만들지 않습니다).

상태 파일 `<cwd>/.figma-lossless/state.json`의 FSM:

```text
            collect/compile 실행 관찰, 또는 /verify-design 호출 프롬프트
 inactive ────────────────────────────────────────▶ active(scope)
    ▲                                          │        │
    │ 24h 무활동 자동 만료 / reset               │        │ pause --reason "<사유>"
    └──────────────────────────────────────────┤        ▼
                                               │     paused ──사용자 답변──▶ active
                                               │ 카피 감사: export-copy 성공
                                               │ 전체 검증: gate-results.json passed=true
                                               ▼
                                            terminal (Stop 허용)
```

- **Stop**: 활성·비터미널 워크플로가 있으면 세션 종료를 차단하고 "계속하거나 pause를 선언하라"를 주입합니다. 터미널 판정은 훅이 `gate-results.json`을 직접 읽어 내립니다. 3회 연속 차단 후에는 통과시켜 세션을 가두지 않습니다.
- **오발동 방지**: 훅은 전역 등록이지만 활성화는 실제 하네스 명령 관찰 또는 명시적 `/verify-design` 호출 프롬프트에서만 일어납니다(단순 언급은 무시). 24시간 무활동 상태는 자가 치유(비활성화)되고, `PreToolUse` 승인 요구는 `gate-config*.json`(키 1개 이상) 또는 그 외 `.json`의 서로 다른 게이트 키 2개 이상에만 적용됩니다. 소스 파일은 검사하지 않습니다.
- **탈출구**: 요청 결함으로 멈출 때는 `python3 "$PLUGIN_ROOT/hooks/harness_hook.py" pause --reason "<사유>"`(사유 필수, 감사 가능). 방치 상태 즉시 정리는 같은 스크립트의 `reset`(사유 불필요). 훅 내부 오류는 fail-open이라 무관한 작업을 깨지 않습니다.

### 멈춤은 두 종류다

강제 훅이 막으려는 것은 **검증을 끝내지 않은 채 조용히 나가는 것**이다. 사용자에게 결정을 물으려고
멈추는 것은 그것과 반대다 — 사용자가 질문을 보고 있고, 답하거나 "검증부터 끝내라" 고 말할 수 있다.
그때 Stop 을 막으면 에이전트는 자기 것이 아닌 결정을 **추측**하게 되는데, 그게 이 하네스가 막으려는
바로 그 실패다.

그래서 훅은 **에이전트가 멈추면서 무슨 말을 했는지**로 판정한다:

| 신호 | 판정 |
|---|---|
| 마지막 메시지 맺음부(비인용 5줄)가 질문 | 자동 일시정지 |
| 그 외의 종료 | 기존대로 차단 (최대 3회 후 fail-open) |

메시지는 Claude Code 가 Stop payload 로 넘겨주는 `last_assistant_message` 를 쓴다. 트랜스크립트 파일은
Stop 시점에 아직 flush 되지 않을 수 있어 이전 턴 텍스트를 읽는다 — 실측으로 확인했고, 파일 파싱은
그 필드가 없는 호스트를 위한 폴백으로만 남겼다.

`AskUserQuestion` 은 **신호로 쓰지 않는다.** PostToolUse 는 도구가 *반환할 때* 발생하고 그 도구는
사용자가 이미 준 답을 반환한다. 거기서 일시정지를 걸면 사람이 기다림을 끝낸 순간에 "사람을 기다리는
중"으로 표시하는 셈이라, 아무거나 묻고 답을 받은 뒤 미완료 검증을 버리고 나가도 차단이 풀린다.
독립 반증 리뷰가 이 구멍을 찾아냈다.

`pause --reason` 은 그대로 남아 있다 — 질문 없이 멈춰야 할 때를 위한 것이다. 탈출구는 원래부터
에이전트가 스스로 여는 것이었고, 이 변경은 **정직한 경우에서 절차 비용만 없앤다.**

일시정지는 다음 사용자 입력에서 해제되지만, **`lastActivityAt` 을 갱신하지는 않는다.** 갱신하던 때는
일시정지를 반복하는 것만으로 24시간 자가치유가 영원히 발동하지 않아, 중간에 버려진 워크플로가 그
디렉터리의 모든 후속 세션을 인질로 잡았다.

## 설계 — 왜 이렇게 만들었나

이 하네스에는 중앙 컨트롤러(FSM)가 없습니다. 순서와 상태는 성격이 다른 네 층이 나눠 담당합니다.

1. **단계 순서 = 데이터 의존성 (Make 방식 DAG).** `collect → compile → 구현 → capture → validate`의 순서는 전이 테이블이 아니라 "각 단계의 입력이 이전 단계의 산출물 파일"이라는 사실로 강제됩니다. 순서를 건너뛰면 다음 단계가 입력 부재로 실패합니다.
2. **진행 상태 = 디스크의 산출물.** "어디까지 왔나"는 상태 변수가 아니라 evidence/bundle/report 산출물의 존재와 신선도로 복원합니다. 각 산출물이 입력의 SHA-256과 파일 버전을 품고 있어 상태가 현실과 어긋날 수 없습니다. 세션이 끊겨도 손실이 없습니다.
3. **세션 정지 판정 = 소형 FSM (강제 훅).** `<cwd>/.figma-lossless/state.json`이 "이 세션이 지금 멈춰도 되는가" 하나만 판정합니다. 아래 [상태와 강제 훅](#상태와-강제-훅) 참조.
4. **노드별 완료 장부 = Coverage Ledger.** IR 안의 노드별 상태(`discovered → contextFetched → specCompiled → implemented → …passed`)는 전이를 실행하는 컨트롤러가 아니라 **증명된 것의 기록**이며, 게이트가 읽고 판정합니다.

검증의 근거는 항상 관찰된 사실입니다: 수집 완전성은 `expected == collected` manifest로, 스타일은 canonical JSON 수치 대 computed style로, 완료는 훅이 직접 읽는 `gate-results.json`으로 — 에이전트의 주장은 어디에서도 신뢰 근거가 아닙니다.

## 알려진 한계

숨기지 않고 적습니다. 도구를 과신하는 것이 가장 위험합니다.

### "진짜 API에 연결됐다"는 증명이 아닙니다

데이터 자리 검사가 잡는 것은 **"시안 예시 값을 그대로 베끼지 않았다"**까지입니다. 예시 값이 아닌 *또 다른* 고정값을 넣으면 통과합니다. `validate --actual-b`로 서로 다른 데이터를 두 번 캡처하면 구멍이 좁아지지만 없어지지는 않습니다.

### 로케일마다 형식이 바뀌는 데이터는 못 찾습니다

같은 값이 영문 `1`, 국문 `1개`로 나오면 글자가 다르므로 "안 바뀜"에 걸리지 않고 고정 문구로 판정됩니다. 이런 자리는 `data-contract.json`에 사람이 직접 넣어야 합니다. **못 찾는 것은 조용히 출하되므로 잘못 찾는 것보다 위험합니다.**

### 동작은 사람이 만든 테스트가 검증합니다

`flow` 게이트는 브라우저를 몰지 않습니다. 사람이 쓴 E2E 결과 파일을 받아 **계약과 같은 것을 검사했는지(서명 대조)** 만 확인합니다. 클릭·입력이 실제로 동작하는지는 여전히 사람이 테스트를 써야 합니다.

### 시안에 없는 것은 추출할 수 없습니다

행동 UX, 안 그린 상태, 반응형 의도, 로케일 규칙은 Figma 파일에 존재하지 않습니다. 어떤 도구로도 뽑을 수 없고 `flow-contract`·PRD로 사람이 계약화해야 합니다.

### 화면 전체가 데이터인 경우

`propose-slots`는 변형끼리 너무 비슷한 그룹(불변 비율 > 0.5)을 신호 없음으로 보고 제안하지 않습니다. 화면 대부분이 사용자 데이터인 경우가 여기 걸릴 수 있습니다 — 슬롯 판별이 가장 필요한 화면이 빠지는 셈입니다.

### 렌더링 오차

글리프 배치·안티앨리어싱은 렌더러마다 다르므로 픽셀 비교는 허용치 기반입니다. 구조화된 값 비교가 주력이고 래스터는 보조입니다.

### 그 외

- **`token` 게이트의 성격**: CSS 우선순위 승자를 가리지 않고 **참조가 존재하는지**만 봅니다. CSS 변수를 쓰지 않는 프로젝트에서는 `warn`으로 두고 참고 신호로만 쓰십시오.
- **그림자 비교**: 허용치가 연쇄되면서 근소하게 다른 그림자 쌍이 같다고 판정될 수 있습니다(수용된 한계).
- **동적 값**: 시각·서버 데이터처럼 의도적으로 달라야 하는 값은 무시하지 말고 계약으로 모델링하십시오. 기본은 정확 일치입니다.
- **자체 시험용 스냅샷**은 구현 증거로 쓸 수 없습니다. 일반 `validate`에서 하드 실패하고 훅도 테스트 디렉터리 밖 사용을 막습니다.
- 생성된 evidence·bundle·report는 기본적으로 Git에 커밋하지 않습니다.
