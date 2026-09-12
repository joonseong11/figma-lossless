# Figma Lossless

<p align="center">
  <strong>Figma 시안을 값 하나 잃지 않고 꺼내, 사람과 에이전트가 그대로 구현할 수 있는 문서로 만듭니다.</strong>
</p>

<p align="center">
  <img alt="version" src="https://img.shields.io/badge/version-0.7.0-blue">
  <img alt="license" src="https://img.shields.io/badge/License-MIT-yellow.svg">
  <img alt="python" src="https://img.shields.io/badge/python-3.9%2B-brightgreen">
  <img alt="tests" src="https://img.shields.io/badge/tests-495%20run%20%C2%B7%201%20skipped-success">
</p>

**지원:** Claude Code · Codex (플러그인 하나) · 터미널 CLI

## 무엇을 하나

에이전트에게 Figma 링크를 주고 "이대로 만들어 줘"라고 하면, 시안은 자연어로 요약되는 순간 뭉개집니다. `#F7F8F9`는 "연한 회색"이 되고, 8px 간격은 "약간의 여백"이 됩니다.

이 도구는 Figma REST 원본을 **버전을 고정해 통째로** 받고, 속성을 하나하나 회계 처리해(분류 못 한 값은 `accountingViolations` 로 드러납니다) 사람이 읽을 수 있는 **사양 문서**로 펴냅니다. 여기까지가 **추출 모드**이고, 기본으로 열려 있습니다. Figma 토큰 하나면 됩니다.

구현이 그 사양대로 됐는지 기계로 판정하는 **검증 모드**(게이트 12개 + 세션을 붙잡는 강제 훅)도 있지만, **기본은 잠겨 있습니다.** 구현물과 dev 서버가 있고 판정을 원할 때 직접 풉니다.

| | 추출 모드 (기본) | 검증 모드 (잠금) |
|---|---|---|
| 필요한 것 | Figma 토큰 | + 구현물, dev 서버, Playwright, 계약 파일 |
| 명령 | `collect` · `compile` · `export-design` · `export-copy` | + `propose-*` · `vendor-assets` · 캡처 · `validate` |
| 결과 | `evidence/` · `bundle/` · `spec.md` · `copy.json` | `gate-results.json` · `report.html` |
| 강제 훅 | 없음 | 검증을 끝내기 전 세션 종료 차단 |

**시안을 대충 참고만 하면 되는 작업이라면 이 도구는 과합니다.**

## 5분 시작

### 1. 설치

```bash
git clone https://github.com/joonseong11/figma-lossless.git ~/figma-lossless

# Claude Code
claude plugin marketplace add ~/figma-lossless
claude plugin install figma-lossless@figma-lossless-dev

# Codex
codex plugin marketplace add ~/figma-lossless
codex plugin add figma-lossless@figma-lossless-dev
```

```bash
claude plugin list | grep figma          # 목록에 보이면 등록 완료
cd ~/figma-lossless && git pull && claude plugin update figma-lossless    # 갱신
claude plugin uninstall figma-lossless                                    # 제거
```

Python 3.9+ 가 필요합니다. Pillow 는 처음 실행할 때 격리된 가상환경(`~/.cache/figma-lossless/`, `FIGMA_LOSSLESS_CACHE_DIR` 로 변경, `FIGMA_LOSSLESS_NO_BOOTSTRAP=1` 로 자동 생성 금지)에 자동 설치됩니다. Node.js 와 Playwright(검증할 **대상 저장소**에 설치)는 검증 모드의 캡처 단계에만 필요합니다.

### 2. Figma 토큰

`~/.figma-token` 파일에 personal access token 을 넣습니다. 아래 세 형식 다 됩니다.

```text
figd_xxxxxxxxxxxx
FIGMA_TOKEN=figd_xxxxxxxxxxxx
export FIGMA_TOKEN=figd_xxxxxxxxxxxx
```

다른 서비스의 시크릿이 섞인 파일(예: `.env` 전체)은 오전송을 막기 위해 거부합니다. 파일·노드·이미지 조회는 Figma 플랜과 무관합니다.

### 3. 추출

에이전트에게 한 줄이면 됩니다.

```text
/verify-design 이 Figma 화면의 구현 사양을 뽑아줘: <Figma URL>
```

직접 치려면 세 명령입니다. URL 의 `node-id=22137-49538` 은 `22137:49538` 로 바꿉니다.

```bash
export PYTHONPATH=src            # 저장소에서 직접 실행할 때만
H="python3 -m figma_lossless"

$H collect --file-key <file key> --node-ids "22137:49538,22137:49564" \
   --output evidence --include-images        # exit 0 = 빠짐없이 받았다는 증명
$H compile --rest-input evidence --output bundle --feature-id my-feature
                                             # 출력의 accountingViolations 가 0 이어야 값이 안 뭉개진 것.
                                             # 0 이 아니면 exit 0 이라도 멈추고 원인을 봅니다
$H export-design --bundle bundle --output spec.md \
   --exclude "Status Bar,Home Indicator"     # 기기 UI 제외
```

`spec.md` 하나에 모든 화면의 구조·문구·치수·색·폰트가 Figma 의 부모/자식 순서 그대로 들어 있습니다. 시안에서 숨긴 레이어는 "must not render" 로 명시됩니다. 이 문서를 에이전트나 사람에게 주고 "이대로 만들어 주세요" 하면 됩니다. **나중에 검증을 돌릴 의무는 생기지 않습니다.**

다국어 문구만 뽑아 카탈로그와 대조하려면 `$H export-copy --bundle bundle --output copy.json` 을 씁니다.

종료 코드: `0` 성공 · `2` 수집 불완전(`collect`)·게이트 실패(`validate`) · `1` 입력 오류. `compile` 은 회계 위반이 있어도 `0` 으로 끝나므로 출력의 `accountingViolations` 를 봐야 합니다. 생성된 `evidence/`·`bundle/` 은 로컬 경로와 임시 URL 을 담으므로 Git 에 커밋하지 않습니다(`.gitignore` 에 있습니다).

## 검증 모드 잠금 풀기

구현이 시안대로 됐는지 판정하고 싶을 때만 풉니다.

```bash
$H mode                       # 지금 모드와 출처 확인
$H mode --set verify          # 이 디렉터리 트리에서 잠금 해제 (.figma-lossless/mode.json 생성)
FIGMA_LOSSLESS_MODE=verify …  # 또는 이 프로세스에서만 (유효한 값이면 파일보다 우선)
$H mode --clear               # 파일 삭제 → 다시 잠금
```

`$H` 는 위 5분 시작의 `python3 -m figma_lossless` 입니다. 플러그인으로 설치했다면 `python3 <플러그인 경로>/skills/verify-design/scripts/run_harness.py mode …` 이고, 에이전트에게 "검증 모드 풀어줘" 라고 해도 됩니다. 파일은 현재 디렉터리에서 위로 올라가며 가장 가까운 것을 쓰므로 프로젝트 루트에서 한 번 풀면 하위 디렉터리에도 적용됩니다. 잘못된 값은 무시하고 다음 출처로 넘어가므로(환경변수 → 파일 → 기본 잠금) 오타만으로 검증이 풀리지는 않습니다.

잠긴 상태에서 `validate` 나 캡처 어댑터를 실행하면 리포트·스크린샷 같은 산출물을 만들기 전에 안내와 함께 거부됩니다. 풀면 두 가지가 바뀝니다.

- 캡처·게이트 명령이 동작합니다. 실제로 렌더된 DOM 을 계약과 대조해 **12개 항목**으로 판정하고, 실패하면 `exit 2` 라 CI 머지 게이트로 그대로 씁니다. 아무것도 재지 않은 항목은 통과가 아니라 `NOT_EVALUATED` 이고 그 자체가 결함입니다.
- Claude Code 강제 훅이 이 디렉터리에서 살아납니다. 검증을 끝내지 않고 세션을 나가는 것을 막고, 게이트 허용치를 느슨하게 하는 편집에는 사용자 승인을 요구합니다. 정당하게 멈춰야 할 때는 `harness_hook.py pause --reason "<사유>"` 로 선언합니다. **훅을 켜는 것은 파일입니다** — 명령 앞에 붙인 환경변수는 그 프로세스만 풀고, 훅은 Claude Code 를 띄운 환경만 봅니다. Codex 에는 훅이 없어 완주 여부를 사람이 봅니다.

에이전트에게는 이렇게 말합니다.

```text
/verify-design 이 Figma 화면을 우리 구현과 대조 검증해줘: <Figma URL>
구현: <리포/브랜치>, dev 서버: <실행 명령>, 화면별 경로: /account?locale=ko|en
```

절차·게이트 12개·설정·승인 편차·훅 동작·설계 근거는 **[검증 모드 문서](./docs/verification.md)** 에 있습니다.

## 알려진 한계

- **"진짜 API 에 연결됐다"는 증명이 아닙니다.** 데이터 자리 검사는 시안 예시 값을 베끼지 않았는지까지만 봅니다.
- **로케일마다 형식이 바뀌는 데이터**(`1` / `1개`)는 고정 문구로 잘못 판정될 수 있어 사람이 계약에 직접 넣어야 합니다.
- **동작은 사람이 쓴 E2E 가 검증합니다.** `flow` 게이트는 그 결과가 계약과 같은 것을 검사했는지만 확인합니다.
- **시안에 없는 것은 추출할 수 없습니다.** 안 그린 상태, 반응형 의도, 로케일 규칙은 PRD 로 사람이 계약화합니다.
- **픽셀 비교는 보조입니다.** 렌더러마다 글리프 배치가 달라 허용치 기반이고, 구조화된 값 비교가 주력입니다.

## 문서

| 문서 | 언제 읽나 |
|---|---|
| [검증 모드](./docs/verification.md) | 게이트·절차·설정·훅·설계 근거 전부 |
| [스킬 사용법](./skills/verify-design/SKILL.md) | 에이전트가 이 도구를 어떤 순서로 쓰는지 |
| [아키텍처](./docs/architecture.md) | 수집·컴파일·판정이 어떻게 나뉘어 있는지 |
| [게이트 설계 근거](./docs/gate-design-rationale.md) | 게이트 판정이 왜 이 모양인지 |
| [예제 계약](./examples/sign-in/) | 계약 파일 4종의 실제 모양 |
| [변경 이력](./CHANGELOG.md) | 릴리스마다 무엇이 바뀌었는지 |

## 테스트

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 라이선스

[MIT](./LICENSE)
