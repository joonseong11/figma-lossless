# 손실 없는 Figma Design Compiler와 검증 게이트

## 결론

103개 화면이 하나의 기능 구현 단위라면 기능 범위를 줄이는 대신 에이전트의 작업 컨텍스트만 화면·상태 단위로 분할해야 합니다. 정확성을 만드는 방법은 에이전트가 103개 화면을 모두 기억하기를 기대하는 것이 아니라, 하나라도 처리되지 않으면 시스템이 완료 상태가 될 수 없게 만드는 것입니다.

```text
Figma MCP raw + PRD
  → deterministic Design IR compiler
  → Coverage Ledger + Flow Contract + Component/Asset Map
  → 화면별 구현 작업
  → Structure/Copy/Asset/Visual/Flow Gate
  → Defect Manifest
  → 국소 수정
  → 모든 hard gate 통과 시에만 완료
```

## Design IR

Markdown은 사람이 읽는 파생 문서이고 JSON Design IR이 검증의 source of truth입니다. 각 화면에는 Figma node ID, viewport, reference screenshot/code, exact copy, geometry, 추출 가능한 typography/style, asset identity, raw evidence hash와 단계별 상태가 들어갑니다.

Figma나 PRD에 없는 값을 추론해야 한다면 다음처럼 추론임을 명시해야 합니다.

```json
{
  "source": "inferred",
  "confidence": 0.6,
  "requiresConfirmation": true
}
```

## Coverage Ledger

전체 frame을 먼저 인벤토리화하고 아래 상태를 node별로 관리합니다.

```text
discovered → contextFetched → specCompiled → implemented
           → structurePassed → copyPassed → assetPassed
           → visualPassed → flowPassed
```

에이전트는 작은 묶음만 처리해도 되지만 전체 완료 여부는 Ledger가 판정합니다. 상세 context, 구현, 검증 또는 flow transition 결과가 하나라도 없으면 완료가 아닙니다.

## 손실을 막는 전달 방식

에이전트 사이에는 디자인의 자연어 요약을 전달하지 않습니다. `screens/<node-id>.json`, reference PNG/TSX, asset manifest, flow contract, component map과 defect ID만 공유합니다. 수정 에이전트에도 전체 기능이 아니라 해당 defect와 관련 IR·코드만 전달합니다.

## Asset와 component 계약

Figma 원격 asset URL은 만료되기 전에 로컬 파일로 동결하고 SHA-256을 기록합니다. 비슷한 아이콘으로의 묵시적 대체는 허용하지 않습니다. Code Connect를 사용할 수 없다면 component map에서 모든 차이를 `exact-match`, `must-fix`, `approved-deviation` 중 하나로 결정합니다.

## PRD flow 계약

정적 Figma 화면과 PRD 행동을 다음 상태 전이로 컴파일합니다.

```json
{
  "id": "signin-invalid-credentials",
  "from": "signin.initial",
  "action": "submit rejected credentials",
  "to": "signin.auth-failure",
  "figmaNodeId": "21734:37324",
  "requiresConfirmation": false
}
```

모든 transition에는 실행 테스트 결과가 있어야 합니다. 정보가 없으면 추측하지 않고 `requiresConfirmation`으로 남깁니다.

## 완료 기준

- screen/state coverage: 100%
- exact copy: 100%
- asset identity: 100%
- flow transition coverage: 100%
- geometry: 기본 ±1px
- color와 추출 가능한 typography property: exact
- raster similarity: 고정 환경에서 사전에 정한 threshold 이상
- 의도적인 차이: 명시적 승인 필수

목표는 AI가 실수하지 않기를 기대하는 것이 아니라, 실수한 결과가 완료 상태로 통과하지 못하게 만드는 것입니다.
