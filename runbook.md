# QA 설치 가이드 (비개발자용)

hub 를 서버에 설치·삭제·업데이트하는 가장 짧은 길만 담았다.
더 깊은 내용이 필요하면 [README](../README.md), [RUNBOOK-hubctl](RUNBOOK-hubctl.md) 을 본다.

모든 명령은 **내 노트북**에서, **이 저장소 폴더 안**에서 실행한다:

```bash
cd hub-provisioning
```

---

## 1. 설정 — 처음 한 번만

### 1-1. SSH 키 등록 (가장 먼저!)

설치 도구는 서버에 SSH 로 접속한다. 먼저 **내 노트북에 키가 있는지** 확인하고
(처음 쓰는 노트북엔 없다), 서버마다 **딱 한 번** 키를 등록해 둔다:

```bash
ssh-keygen -t ed25519                   # 키 생성 (한 번만) — 물어보면 전부 그냥 엔터
ssh-copy-id connecteve@100.96.35.62     # 서버 계정@서버IP — 비밀번호 1회 입력
ssh connecteve@100.96.35.62 true        # 아무것도 안 뜨고 끝나면 성공
```

> 키가 이미 있으면 `ssh-keygen` 이 덮어쓸지 물어본다 — `n` 으로 빠져나가면 된다.
> `ssh-copy-id` 가 `ERROR: No identities found` 라고 하면 키 생성을 건너뛴 것이다.

> 이걸 건너뛰면 설치가 `Permission denied (publickey,password)` 로 실패한다.
> `-K` 프롬프트에 비밀번호를 넣어도 소용없다 — 그건 SSH 용이 아니라 sudo 용이다(§4 참고).

### 1-2. AWS 설정 (이미지 저장소 접근)

발급받은 액세스 키를 등록한다:

```bash
aws configure
# AWS Access Key ID     : (받은 키 입력)
# AWS Secret Access Key : (받은 시크릿 입력)
# Default region name   : ap-northeast-2
# Default output format : (그냥 엔터)

aws sts get-caller-identity   # 계정 정보가 나오면 성공
```

### 1-3. 접속 정보 영구 등록 (Vault 주소 · Bitbucket 토큰 · Grafana 비밀번호)

터미널을 새로 열어도 유지되도록 셸 설정 파일에 **한 번만** 등록한다.

```bash
nano ~/.zshrc        # 맥 기본. 리눅스/bash 를 쓰면: nano ~/.bashrc
```

파일 **맨 아래**에 다음을 붙여넣고 `<>` 부분을 실제 값으로 바꾼 뒤
저장한다 (nano: `Ctrl+O` → 엔터 → `Ctrl+X`):

```bash
# --- hub 설치 자격 ---
export VAULT_ADDR=https://vault.connecteve.com
export HUB_DEPLOY_GIT_TOKEN=<발급받은 Atlassian API 토큰>
export HUB_DEPLOY_GIT_USER=x-bitbucket-api-token-auth
# (onprem 설치용) Grafana 관리자 초기 비밀번호 — 미설정이면 랜덤 생성
export GRAFANA_ADMIN_PASSWORD='<원하는 비밀번호>'
```

저장했으면 지금 터미널에 바로 반영한다 (이후로는 자동):

```bash
source ~/.zshrc      # bashrc 에 넣었다면: source ~/.bashrc
```

> - `GRAFANA_ADMIN_PASSWORD` 는 **설치 시점에만** 적용된다 — 이미 설치된 서버의
>   비밀번호는 바뀌지 않는다(재설치 때 적용).
> - 이 파일에는 토큰이 평문으로 저장된다 — 노트북 화면잠금을 켜 두고, 공용 계정에는 넣지 않는다.

### 1-4. Vault 로그인

```bash
vault login -method=oidc      # 브라우저가 열리면 회사 계정으로 로그인
```

> 로그인 토큰은 며칠 지나면 만료된다 — 만료되면 **이 명령만** 다시 실행하면 된다
> (만료 여부는 §1-6 preflight 가 알려준다).

### 1-5. 서버 등록 — 여러 대도 여기에

설치할 서버는 전부 [inventory/sites.yml](../inventory/sites.yml) 에 적는다.
서버가 여러 대면 **블록을 복사해서 아래에 추가**하면 된다:

```yaml
sites:
  hosts:
    bumin-node1:                      # 서버를 부르는 이름 (자유, -l 에 쓰는 이름)
      ansible_host: 100.96.35.62      # 서버 IP
      ansible_user: connecteve        # SSH 계정
      site_name: bumin                # 사이트 이름 (temporal 네임스페이스 등에 쓰임)
      profile: onprem                 # onprem | hybrid-with-ai | hybrid-without-ai
      inference: [koa, spin]          # 이 사이트가 띄울 AI 추론 (ai 프로파일만 의미)
    gangnam-node1:                    # ← 두 번째 서버는 이렇게 추가
      ansible_host: 10.0.0.21
      ansible_user: ubuntu
      site_name: gangnam
      profile: hybrid-with-ai
      inference: [koa]
```

profile 세 가지:

| profile | 뜻 |
|---|---|
| `onprem` | 전부 사이트 안에서 동작 (폐쇄망 운영) |
| `hybrid-with-ai` | 사이트에 앱+AI, 중앙(클라우드)과 연동 |
| `hybrid-without-ai` | 사이트에 앱만, 중앙과 연동 |

> hybrid 는 클라우드 쪽 사전 준비가 필요하다 — 관리자에게 확인 ([README §9](../README.md)).

### 1-6. 준비 끝났는지 점검

```bash
./bin/hubctl preflight        # Vault / AWS / Bitbucket 세 가지를 한 번에 점검
```

전부 OK 가 나오면 설정 끝. 실패한 항목은 §1 의 해당 단계를 다시 한다.

### 설정 트러블슈팅

| 증상 | 원인 → 조치 |
|---|---|
| `ssh-copy-id` 가 비밀번호를 거부 | 서버 계정 비밀번호가 틀림 — 계정 정보 재확인 |
| `aws sts get-caller-identity` 실패 | 키 오타 또는 만료 — `aws configure` 다시 |
| `vault login` 이 403/거부 | `VAULT_ADDR` 오타 확인 후 재로그인. 예전 토큰이 남아 방해하면 `unset VAULT_TOKEN` 후 다시 |
| preflight 에서 Bitbucket 실패 | 토큰 만료 또는 `HUB_DEPLOY_GIT_USER` 누락 — §1-4 다시 |

---

## 2. 설치

```bash
./bin/hubctl install -e dev -l bumin-node1 -K
```

`-K` 를 주면 시작할 때 **BECOME password** 를 물어본다 → **서버 계정의 sudo 비밀번호**를 입력한다.
30분~1시간쯤 걸리고, 마지막에 `failed=0` 이면 성공이다.

### 인자 설명

| 인자 | 뜻 | 예 |
|---|---|---|
| `-e` | 환경 — 어느 Vault 금고(dev/stage/prod)에서 설정값을 가져올지. **QA 는 보통 `dev`** | `-e dev` |
| `-l` | 어느 서버에 설치할지 — sites.yml 에 적은 이름. **생략하면 등록된 전체 서버**에 실행되니 주의 | `-l bumin-node1` |
| `-K` | 서버 sudo 비밀번호를 물어봐 달라 — 비밀번호 없이 sudo 되는 서버가 아니면 항상 필요 | `-K` |
| `-- -e 이름=값` | `--` 뒤는 고급 옵션 전달용 (아래 브랜치/태그 지정에 사용) | `-- -e hub_deploy_ref=v1.2.0` |

### 특정 브랜치/태그로 설치하기

설치되는 앱 버전 목록은 **hub-deploy 저장소**가 정한다. 기본은 **`main` 브랜치**다.
다른 브랜치나 태그로 설치하려면:

```bash
# 브랜치로
./bin/hubctl install -e dev -l bumin-node1 -K -- -e hub_deploy_ref=feature/obs-stack

# 태그로 (태그일 땐 ref_type 도 함께)
./bin/hubctl install -e dev -l bumin-node1 -K -- -e hub_deploy_ref=v1.2.0 -e hub_deploy_ref_type=tag
```

### 설치 확인

```bash
./bin/hubctl verify -l bumin-node1 -K     # 검증 항목만 다시 실행 — failed=0 이면 정상
```

> `hubctl status` 라는 요약 명령도 있지만 노트북에 클러스터 접속 설정(KUBECONFIG)이
> 있어야 동작한다 — QA 는 verify 만으로 충분하다.

### 이미 설치된 서버에 다시 반영하기 — configure

`install` 은 사실 두 단계다: **bootstrap**(서버 OS 준비 + 빈 클러스터) + **configure**(플랫폼·앱 설치).
서버에 이미 설치돼 있고 **내용물만 새로 반영**하고 싶을 땐 뒷단계인 configure 만 돌리면 된다
(bootstrap 을 건너뛰어 더 빠르고, 인자는 install 과 동일):

```bash
./bin/hubctl configure -e dev -l bumin-node1 -K -- -e hub_deploy_ref=<브랜치|태그>
```

어떤 명령을 쓸지 고르는 법:

| 상황 | 명령 |
|---|---|
| 새 서버 / `clean --uninstall` 로 완전삭제한 서버 | `install` |
| `clean`(reset) 직후 재설치 | `install` |
| 설치된 서버에 앱 **버전만** 올리기 | `patch` (§3-1 — 바뀐 앱만, 가장 빠름) |
| 설치된 서버에 구조가 바뀐 hub-deploy 를 통째로 반영 (patch 가 "configure 재실행" 안내로 중단된 경우 포함) | `configure` |

---

## 3. 새 버전 적용 (fetch) / 초기화 (clean)

### 3-1. 새 버전 적용 — patch

설치가 끝난 서버에 새 앱 버전(hub-deploy 의 새 태그/브랜치)을 올린다. 재설치가 아니라 **바뀐 앱만** 갱신된다
(앱의 cpu/메모리 설정 변경도 같은 방법으로 반영된다):

```bash
./bin/hubctl patch -l bumin-node1 -K -- -e hub_deploy_ref=v1.2.1 -e hub_deploy_ref_type=tag
./bin/hubctl rollback -l bumin-node1 -K       # 방금 패치가 문제면 직전 상태로 되돌리기
```

- 비밀번호(BECOME password)를 **두 번** 묻는다 — 생성/적용 단계가 각각 한 번씩. 정상이다.
- 중간에 **"어떤 앱이 바뀌는지 요약"** 이 표시되고 `[y/N]` 을 물어본다 — 목록이 예상과
  다르면 `n` 으로 취소한다 (취소해도 서버에는 아무 변화가 없다).

### 3-2. 초기화 — clean

**"이 서버에 hub 를 다시 깔 것인가?"** 로 고른다:

```bash
./bin/hubctl clean -l bumin-node1 -K                 # reset: 다시 깔 서버 (기본)
./bin/hubctl clean -l bumin-node1 --keep-data -K     # reset + 앱 데이터(/data)는 보존
./bin/hubctl clean -l bumin-node1 --uninstall -K     # uninstall: 반납할 서버 (완전삭제)
```

| | reset (기본) | uninstall (완전삭제) |
|---|---|---|
| 언제 | 설치가 꼬였다 / 프로파일 바꿔 재설치 | 서버 반납, hub 철수 |
| 지우는 것 | 클러스터·앱·데이터 전부 | reset 이 지우는 것 + 도구·다운로드 파일·네트워크 설정까지 **hub 흔적 전부** |
| 남기는 것 | 설치 도구·큰 다운로드 파일 → **재설치가 빠름** | (OS 기본 구성만 남음) |

- 파괴적 작업이라 실행하면 **서버 이름을 직접 타이핑**해서 확인해야 한다.
- reset 후 재설치는 §2 의 install 한 번이면 된다 (재부팅 한 번 권장).

---

## 4. 실행 트러블슈팅

| 증상 (에러 메시지) | 원인 → 조치 |
|---|---|
| `Permission denied (publickey,password)` / `UNREACHABLE` | SSH 키 미등록 — **§1-1 `ssh-copy-id`** 를 그 서버에 실행. (`-K` 비밀번호는 sudo 용이라 이 에러를 못 막는다) |
| `you must install the sshpass program` | `-k`(SSH 비밀번호 모드)를 썼는데 노트북에 sshpass 가 없음 — `-k` 대신 **§1-1 키 등록**으로 해결 (권장) |
| `Missing sudo password` | `-K` 를 빼먹음 — 명령에 `-K` 추가 |
| `Invalid or missing path ['connevo-hub/...']` | `-e` 환경이 틀림 (예: dev 서버인데 `-e prod`) — **`-e dev`** 로 다시. 재설치·clean 불필요, 같은 명령 재실행이면 됨 |
| `VAULT_ADDR 미설정` 또는 Vault 인증 실패 | 로그인 토큰 만료 — **§1-4 로그인만 다시**. `VAULT_ADDR 미설정` 이 뜨면 §1-3 등록 여부 확인 |
| hub-deploy clone/토큰 에러 | `~/.zshrc` 의 `HUB_DEPLOY_GIT_TOKEN` 값 확인 — 만료면 재발급해서 §1-3 값 교체 후 `source ~/.zshrc` |
| `Failed to update apt cache` | 서버가 쓰는 우분투 미러 장애 — 관리자에게 알리고 [README §10](../README.md) 의 미러 교체 절차 |
| patch 가 "패치 스코프 밖 — configure 재실행이 필요한 변경" 으로 중단 | 앱 추가/제거 등 구조 변경이 감지됨 — §2 의 `configure` 로 반영 (정상 동작, 서버 무변경 상태로 중단된 것) |
| 설치가 중간에 실패 | **같은 명령을 그대로 재실행** — 이미 된 부분은 건너뛰므로 안전하다. 반복해서 같은 곳에서 실패하면 로그 첫 번째 빨간 줄을 관리자에게 전달 |
| 여러 번 재실행해도 계속 꼬임 | `clean`(reset) 후 재설치 — §3-2 |

> 표에 없는 에러는: 실패한 TASK 이름 + 빨간 에러 줄을 복사해서 관리자에게 전달한다.
