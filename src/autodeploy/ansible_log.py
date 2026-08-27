"""hubctl / ansible-playbook 출력 스트림 파서.

각 줄을 (분류·호스트·단계)로 태깅하고 `PLAY RECAP` 에서 호스트별 최종 집계를 뽑는다.
dev-spec-web-console-20260826 §F4·§F5.

## 실측 전제 (2026-08-26, ansible-core 2.21.1, 이 저장소 ansible.cfg)

- `stdout_callback=default` + `callback_result_format=yaml`
- ansible 은 `[ERROR]` 블록까지 **전부 stdout** 으로 쓴다. stderr 로 오는 것은
  hubctl 자체의 `warn()`/`err()` 뿐이다.
- 색은 넣지 않는다. hubctl 은 `[ -t 1 ]` 로 TTY 일 때만 색을 켜고 우리는 파이프로
  읽으므로 평문이 온다. `ANSIBLE_FORCE_COLOR` 도 설정하지 않는다.

실제 출력 예 (그대로 옮김) ::

    PLAY [Bootstrap (host -> empty k0s)] ****************************

    TASK [ok task] **************************************************
    ok: [alpha]
    ok: [beta -> localhost]
    ok: [beta] => (item=one)
    skipping: [alpha]
    [ERROR]: Task failed: Failed to connect to the host via ssh: ...
    Origin: /repo/play.yml:6:7

    4   gather_facts: false
    5   tasks:
    6     - name: ok task
            ^ column 7

    fatal: [gamma]: UNREACHABLE! =>
        changed: false
        msg: 'Task failed: ...'
        unreachable: true

    PLAY RECAP ******************************************************
    alpha  : ok=5 changed=1 unreachable=0 failed=0 skipped=2 rescued=0 ignored=0

여기서 두 가지가 파서 설계를 좌우한다.

1. `fatal:` 뒤에 **들여쓴 YAML 본문**이 따라온다 → 그 줄들도 같은 호스트로 묶여야
   호스트 필터(AC-6)에서 에러 상세가 사라지지 않는다.
2. `[ERROR]`/`Origin:`/소스 발췌도 들여쓰기가 섞여 있지만 **호스트가 없다** →
   무턱대고 "들여쓰면 직전 호스트" 로 묶으면 남의 호스트에 붙는다.

그래서 이어짐(continuation) 호스트는 **`=>` 로 끝나는 결과 줄** 에서만 열고,
빈 줄이나 들여쓰지 않은 줄에서 즉시 닫는다. 소스 발췌는 행번호가 0열에서 시작하므로
(`4   gather_facts: false`) 들여쓴 줄로 취급되지 않아 자연히 걸러진다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class LineKind(StrEnum):
    """로그 한 줄의 분류. 웹 콘솔이 색을 입히는 기준."""

    TASK = "task"     # PLAY / TASK / hubctl 헤더 — 단계 경계
    OK = "ok"
    CHANGED = "chg"
    ERROR = "err"
    WARN = "warn"
    SKIP = "skip"
    RECAP = "recap"
    OUT = "out"       # 그 외 (컨트롤러 공통 줄)


# 단계 판정 (§F5). PLAY 이름은 실제 playbook 의 `- name:` 값과 정확히 일치해야 한다.
# ansible 은 `PLAY [<name>] ****` 로 출력하므로 접두 일치로 잡는다.
STEP_MARKERS: tuple[tuple[str, str], ...] = (
    ("━━ Preflight", "preflight"),
    # Bootstrap·Configuration PLAY 는 단계를 바꾸지 않는다. 바로 뒤에 오는 롤이
    # 훨씬 정확하고, 여기서 한 번 바꾸면 롤이 잡히기 전까지 화면이 엉뚱한 단계를
    # 가리킨다. 대신 시작 표시(step_started)를 위해 목록에는 남겨둔다.
    ("PLAY [Bootstrap (host -> empty k0s)]", "preflight"),
    ("PLAY [Configuration (empty k0s -> platform)]", "cluster"),
    ("PLAY [hubctl verify]", "verify"),
    ("PLAY [패치 번들 생성 (patch_create)]", "create"),
    ("PLAY [패치 번들 적용 (patch_apply)]", "apply"),
    ("PLAY [패치 롤백 (patch_apply/rollback)]", "rollback"),
    ("PLAY [Clean (초기화 — reset|uninstall)]", "clean"),
)

# PLAY 는 install 에 둘(Bootstrap·Configuration)뿐이라 50분짜리 설치가 큰 덩어리
# 셋으로만 보였다. 그 아래 **롤**을 단계로 쓴다 — `TASK [<롤> : ...]` 로 나온다.
#
# 묶음은 **실제 실행 순서 그대로**여야 한다. 순서를 건너뛰어 묶으면 화면의 단계가
# 뒤로 돌아간다 (예: shared_storage 는 platform_secrets 뒤에 온다).
# 아래 순서는 실제 설치 로그(job #33)에서 확인한 것이다.
ROLE_STEPS: dict[str, str] = {
    # ── bootstrap.yml
    "preflight": "preflight",
    "os_base": "os", "tools_aqua": "os", "network": "os", "time": "os", "storage": "os",
    "k0s": "k0s",
    # ── cluster.yml
    "cluster_storage": "cluster", "k0s_airgap_export": "cluster",
    "zarf_init": "gitops", "gitops_publish": "gitops", "app_charts_fetch": "gitops",
    "flux_install": "flux",
    "platform_secrets": "platform", "shared_storage": "platform",
    "platform_component": "platform", "otel_instrumentation": "platform",
    "consul_seed": "wire", "secrets_fetch": "wire",
    "temporal_register": "wire", "flux_wire": "wire",
    "verify": "verify",
}

# `TASK [flux_install : Install Flux]` / `TASK [flux_install]` 에서 롤 이름만 뽑는다.
_TASK_ROLE_RE = re.compile(r"^TASK \[(?P<role>[^\]:]+?)\s*(?::[^\]]*)?\]")

# 단계는 **뒤로 가지 않는다.** 같은 롤이 두 곳에서 돌거나 묶음 순서가 어긋나도
# 화면의 진행 표시가 되돌아가면 사람이 "다시 하는 건가" 로 읽는다.
STEP_ORDER: tuple[str, ...] = (
    "preflight", "os", "k0s", "cluster", "gitops", "flux", "platform", "wire",
    "verify", "create", "apply", "rollback", "clean",
)
_STEP_RANK = {key: i for i, key in enumerate(STEP_ORDER)}


def is_step(key: str | None) -> bool:
    """로그에서 실제로 나오는 단계 키인가.

    작업 종류 이름(`install`)은 단계가 아니다. 그걸 current_step 에 적어두면
    화면이 단계 목록에서 못 찾아 진행 표시가 통째로 멈춘다 — 실제로 그랬다.
    """
    return key in _STEP_RANK

STEP_LABELS: dict[str, str] = {
    "preflight": "사전 점검",
    "os": "서버 기본 구성",
    "k0s": "k0s 클러스터",
    "cluster": "클러스터 스토리지",
    "gitops": "GitOps 적재",
    "flux": "Flux 설치",
    "platform": "플랫폼 컴포넌트",
    "wire": "연동 (Consul·Vault·Temporal)",
    "verify": "검증",
    "create": "번들 생성",
    "apply": "번들 적용",
    "rollback": "롤백",
    "clean": "초기화",
    # 옛 기록에 남아 있는 값. 지난 작업 화면이 키를 그대로 노출하지 않게 남겨둔다.
    "bootstrap": "부트스트랩",
    "configure": "플랫폼 구성",
    "install": "설치",
}

# `ok: [host]`, `ok: [host -> delegate]`, `fatal: [host]: FAILED! =>`
_RESULT_RE = re.compile(
    r"^(?P<verb>ok|changed|skipping|failed|fatal|unreachable|included|ignoring)"
    r":\s+\[(?P<host>[^\]\s]+)(?:\s*->\s*[^\]]+)?\]"
)

# PLAY RECAP 구간. 뒤쪽 필드는 ansible 버전에 따라 없을 수 있어 옵션으로 둔다.
_RECAP_RE = re.compile(
    r"^(?P<host>\S+)\s*:\s*ok=(?P<ok>\d+)\s+changed=(?P<changed>\d+)"
    r"\s+unreachable=(?P<unreachable>\d+)\s+failed=(?P<failed>\d+)"
    r"(?:\s+skipped=(?P<skipped>\d+))?"
    r"(?:\s+rescued=(?P<rescued>\d+))?"
    r"(?:\s+ignored=(?P<ignored>\d+))?"
)

_VERB_KIND: dict[str, LineKind] = {
    "ok": LineKind.OK,
    "changed": LineKind.CHANGED,
    "skipping": LineKind.SKIP,
    "failed": LineKind.ERROR,
    "fatal": LineKind.ERROR,
    "unreachable": LineKind.ERROR,
    "included": LineKind.OUT,
    "ignoring": LineKind.WARN,
}

# hubctl 출력 헬퍼 접두 (색이 꺼진 상태). bin/hubctl 의 info/note/ok/warn/err.
_HUBCTL_PREFIX: tuple[tuple[str, LineKind], ...] = (
    ("━━ ", LineKind.TASK),
    ("✔ ", LineKind.OK),
    ("✘ ", LineKind.ERROR),
    ("! ", LineKind.WARN),
    ("• ", LineKind.OUT),
)


@dataclass(frozen=True, slots=True)
class HostRecap:
    """`PLAY RECAP` 한 줄. 호스트별 최종 상태 판정의 근거 (§F5)."""

    host: str
    ok: int = 0
    changed: int = 0
    unreachable: int = 0
    failed: int = 0
    skipped: int = 0
    rescued: int = 0
    ignored: int = 0

    @property
    def succeeded(self) -> bool:
        return self.failed == 0 and self.unreachable == 0


@dataclass(frozen=True, slots=True)
class ParsedLine:
    text: str                 # 마스킹까지 끝난 표시용 본문 (개행 제거)
    kind: LineKind
    host: str | None          # None = 컨트롤러 공통 줄 (호스트 필터에서 항상 표시)
    step: str | None          # 이 줄 시점의 단계 키
    step_started: bool = False
    recap: HostRecap | None = None


class AnsibleLogParser:
    """줄을 순서대로 `feed` 하면 태깅해서 돌려준다. 한 작업(=한 프로세스)당 1개."""

    __slots__ = ("step", "in_recap", "recaps", "_cont_host", "_cont_kind")

    def __init__(self) -> None:
        self.step: str | None = None
        self.in_recap = False
        self.recaps: dict[str, HostRecap] = {}
        self._cont_host: str | None = None
        self._cont_kind: LineKind = LineKind.OUT

    def feed(self, text: str) -> ParsedLine:
        raw = text.rstrip("\r\n")
        line = raw.rstrip()

        if not line:
            self._close_continuation()
            return ParsedLine(raw, LineKind.OUT, None, self.step)

        indented = raw[:1].isspace()

        if line.startswith("PLAY RECAP"):
            self.in_recap = True
            self._close_continuation()
            return ParsedLine(raw, LineKind.TASK, None, self.step)

        if self.in_recap and not indented:
            recap = _parse_recap(line)
            if recap is not None:
                self.recaps[recap.host] = recap
                return ParsedLine(raw, LineKind.RECAP, recap.host, self.step, recap=recap)

        step_started = self._advance_step(line)

        if indented:
            # 직전 결과 줄의 YAML 본문. 호스트를 물려받아 필터에서 함께 보이게 한다.
            return ParsedLine(raw, self._continuation_kind(), self._cont_host, self.step)

        match = _RESULT_RE.match(line)
        if match is not None:
            host = match.group("host")
            kind = _VERB_KIND[match.group("verb")]
            # `=>` 로 끝나면 YAML 본문이 따라온다. 그때만 이어짐을 연다.
            if line.endswith("=>"):
                self._cont_host, self._cont_kind = host, kind
            else:
                self._close_continuation()
            return ParsedLine(raw, kind, host, self.step)

        self._close_continuation()
        return ParsedLine(raw, _classify_plain(line), None, self.step, step_started=step_started)

    def _advance_step(self, line: str) -> bool:
        for marker, key in STEP_MARKERS:
            if line.startswith(marker):
                return self._set_step(key)
        role = _TASK_ROLE_RE.match(line)
        if role is not None:
            key = ROLE_STEPS.get(role.group("role").strip())
            if key is not None:
                return self._set_step(key)
        return False

    def _set_step(self, key: str) -> bool:
        """앞으로만 간다. 뒤로 가는 전환은 무시한다 (STEP_ORDER)."""
        if self.step == key:
            return False
        here, there = _STEP_RANK.get(self.step), _STEP_RANK.get(key)
        if here is not None and there is not None and there < here:
            return False
        self.step = key
        return True

    def _close_continuation(self) -> None:
        self._cont_host = None
        self._cont_kind = LineKind.OUT

    def _continuation_kind(self) -> LineKind:
        """본문은 **여는 줄과 같은 종류**다.

        전에는 이어짐이 열려 있기만 하면 무조건 ERROR 로 칠했다. 그러면
        `ok: [h] =>` 뒤의 `msg: All assertions passed` 같은 성공 본문까지 빨갛게
        되고, 오류 요약 패널이 성공 메시지로 가득 찬다(실측: 23건 중 대부분).
        """
        return self._cont_kind


def _parse_recap(line: str) -> HostRecap | None:
    m = _RECAP_RE.match(line)
    if m is None:
        return None

    def num(name: str) -> int:
        value = m.group(name)
        return int(value) if value is not None else 0

    return HostRecap(
        host=m.group("host"),
        ok=num("ok"),
        changed=num("changed"),
        unreachable=num("unreachable"),
        failed=num("failed"),
        skipped=num("skipped"),
        rescued=num("rescued"),
        ignored=num("ignored"),
    )


def _classify_plain(line: str) -> LineKind:
    if line.startswith("PLAY [") or line.startswith("TASK [") or line.startswith("RUNNING HANDLER ["):
        return LineKind.TASK
    for prefix, kind in _HUBCTL_PREFIX:
        if line.startswith(prefix):
            return kind
    if line.startswith("[ERROR]"):
        return LineKind.ERROR
    if line.startswith("[WARNING]") or line.startswith("[DEPRECATION WARNING]"):
        return LineKind.WARN
    # `fatal: [` 로 시작하지 않는 변종 대비 (§F4 의 FAILED!/unreachable 판정).
    if "FAILED!" in line or "UNREACHABLE!" in line:
        return LineKind.ERROR
    return LineKind.OUT


def host_status(recaps: dict[str, HostRecap], host: str) -> str:
    """RECAP 기준 호스트별 최종 상태.

    RECAP 에 아예 없으면 앞선 play 에서 탈락한 것이므로 실패로 본다 (§F5).
    """
    recap = recaps.get(host)
    if recap is None:
        return "failed"
    return "succeeded" if recap.succeeded else "failed"
