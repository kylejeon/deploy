"""ansible_log 파서 테스트.

REAL_OUTPUT 은 2026-08-26 에 ansible-core 2.21.1 로 실제 실행해 받은 출력을 그대로
붙여넣은 것이다(경로만 축약). 손으로 지어낸 샘플로는 `[ERROR]` 블록 + 소스 발췌 +
들여쓴 YAML 본문이 뒤섞이는 실제 모양을 재현할 수 없어서, 회귀 기준으로 박아둔다.
"""
from __future__ import annotations

import pytest

from autodeploy.ansible_log import (
    AnsibleLogParser,
    HostRecap,
    LineKind,
    host_status,
    is_step,
)

REAL_OUTPUT = """
PLAY [Bootstrap (host -> empty k0s)] *******************************************

TASK [ok task] *****************************************************************
ok: [alpha]
ok: [beta]
[ERROR]: Task failed: Failed to connect to the host via ssh: ssh: connect to host 192.0.2.1 port 22: Operation timed out
Origin: /repo/play.yml:6:7

4   gather_facts: false
5   tasks:
6     - name: ok task
        ^ column 7

fatal: [gamma]: UNREACHABLE! =>
    changed: false
    msg: 'Task failed: Failed to connect to the host via ssh: ssh: connect to host 192.0.2.1
        port 22: Operation timed out'
    unreachable: true

TASK [changed task] ************************************************************
changed: [beta]
changed: [alpha]

TASK [skipped task] ************************************************************
skipping: [alpha]
skipping: [beta]

TASK [delegated task] **********************************************************
ok: [beta -> localhost]
ok: [alpha -> localhost]

TASK [loop task] ***************************************************************
ok: [beta] => (item=one)
ok: [alpha] => (item=one)

TASK [fail only on beta] *******************************************************
skipping: [alpha]
[ERROR]: Task failed: Module failed: The command exited with a non-zero return code.
Origin: /repo/play.yml:23:7

21       changed_when: false
22       loop: [one, two]
23     - name: fail only on beta
         ^ column 7

fatal: [beta]: FAILED! =>
    changed: true
    cmd:
    - /usr/bin/false
    msg: The command exited with a non-zero return code.
    rc: 1

PLAY [Configuration (empty k0s -> platform)] ***********************************

TASK [second play task] ********************************************************
ok: [alpha]

PLAY RECAP *********************************************************************
alpha                      : ok=5    changed=1    unreachable=0    failed=0    skipped=2    rescued=0    ignored=0
beta                       : ok=4    changed=1    unreachable=0    failed=1    skipped=1    rescued=0    ignored=0
gamma                      : ok=0    changed=0    unreachable=1    failed=0    skipped=0    rescued=0    ignored=0
"""


def parse_all(text: str) -> tuple[AnsibleLogParser, list]:
    parser = AnsibleLogParser()
    return parser, [parser.feed(line) for line in text.splitlines()]


@pytest.fixture
def real():
    return parse_all(REAL_OUTPUT)


# ── 호스트 귀속 ─────────────────────────────────────────────────────


def test_error_block_is_not_attributed_to_a_host(real):
    """`[ERROR]` 블록과 소스 발췌가 직전 호스트에 잘못 붙으면 안 된다.

    붙으면 호스트 필터(AC-6)에서 남의 서버 로그에 엉뚱한 에러가 나타난다.
    """
    _, lines = real
    for parsed in lines:
        if parsed.text.startswith(("[ERROR]", "Origin:")) or "column 7" in parsed.text:
            assert parsed.host is None, f"호스트가 붙었다: {parsed.text!r} -> {parsed.host}"


def test_source_excerpt_lines_have_no_host(real):
    _, lines = real
    excerpts = [p for p in lines if p.text.startswith(("4   ", "5   ", "6     ", "21  ", "22  ", "23  "))]
    assert excerpts, "발췌 줄을 못 찾았다 — 픽스처가 바뀌었는지 확인"
    assert all(p.host is None for p in excerpts)


def test_fatal_yaml_body_inherits_host(real):
    """`fatal:` 뒤 들여쓴 YAML 본문은 같은 호스트로 묶여야 에러 상세가 안 사라진다."""
    _, lines = real
    body = [p for p in lines if p.text.strip() in ("rc: 1", "- /usr/bin/false", "changed: true")]
    assert len(body) == 3
    assert all(p.host == "beta" for p in body)
    assert all(p.kind is LineKind.ERROR for p in body)


def test_unreachable_yaml_body_inherits_host(real):
    _, lines = real
    body = [p for p in lines if p.text.strip() == "unreachable: true"]
    assert len(body) == 1
    assert body[0].host == "gamma"


def test_blank_line_closes_continuation():
    parser = AnsibleLogParser()
    parser.feed("fatal: [beta]: FAILED! => ")
    assert parser.feed("    rc: 1").host == "beta"
    parser.feed("")
    assert parser.feed("    stray indented line").host is None


def test_result_line_without_arrow_does_not_open_continuation():
    """`ok: [alpha]` 뒤에는 본문이 없다. 이어짐을 열면 다음 들여쓴 줄을 삼킨다."""
    parser = AnsibleLogParser()
    assert parser.feed("ok: [alpha]").host == "alpha"
    assert parser.feed("    unrelated indented").host is None


def test_delegated_host_is_the_real_target(real):
    _, lines = real
    delegated = [p for p in lines if p.text.startswith("ok: [beta -> localhost]")]
    assert delegated[0].host == "beta"


def test_loop_item_line_keeps_host(real):
    _, lines = real
    looped = [p for p in lines if "(item=one)" in p.text]
    assert {p.host for p in looped} == {"alpha", "beta"}


# ── 분류 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "line,expected",
    [
        ("PLAY [anything] ***", LineKind.TASK),
        ("TASK [role : step] ***", LineKind.TASK),
        ("RUNNING HANDLER [restart] ***", LineKind.TASK),
        ("ok: [h]", LineKind.OK),
        ("changed: [h]", LineKind.CHANGED),
        ("skipping: [h]", LineKind.SKIP),
        ("fatal: [h]: FAILED! =>", LineKind.ERROR),
        ("fatal: [h]: UNREACHABLE! =>", LineKind.ERROR),
        ("[ERROR]: something", LineKind.ERROR),
        ("[WARNING]: something", LineKind.WARN),
        ("[DEPRECATION WARNING]: something", LineKind.WARN),
        ("ignoring: [h]", LineKind.WARN),
        ("just some text", LineKind.OUT),
    ],
)
def test_line_classification(line, expected):
    assert AnsibleLogParser().feed(line).kind is expected


@pytest.mark.parametrize(
    "line,expected",
    [
        ("━━ Preflight — 컨트롤러 자격 점검 ━━", LineKind.TASK),
        ("✔ Vault: VAULT_ADDR + 토큰 OK", LineKind.OK),
        ("✘ VAULT_ADDR 미설정", LineKind.ERROR),
        ("! preflight 건너뜀", LineKind.WARN),
        ("• playbook : site.yml", LineKind.OUT),
    ],
)
def test_hubctl_helper_prefixes(line, expected):
    """색이 꺼진 hubctl 출력(bin/hubctl 의 info/note/ok/warn/err)."""
    assert AnsibleLogParser().feed(line).kind is expected


# ── 단계 ────────────────────────────────────────────────────────────


def test_steps_advance_by_play_name(real):
    """install 의 두 PLAY 는 단계 이름을 갖지 않는다 — 그 아래 롤이 정한다.

    PLAY 는 Bootstrap·Configuration 둘뿐이라 50분짜리 설치가 큰 덩어리 셋으로만
    보였다. PLAY 는 그 구간의 **첫 단계**로만 밀어두고, 실제 진행은 롤이 끈다.
    """
    parser, lines = real
    ok_alpha_first = next(p for p in lines if p.text == "ok: [alpha]")
    assert ok_alpha_first.step == "preflight"
    second_play = next(p for p in lines if p.text.startswith("TASK [second play task]"))
    assert second_play.step == "cluster"
    assert parser.step == "cluster"


def test_step_started_only_on_boundary(real):
    _, lines = real
    started = [p.step for p in lines if p.step_started]
    assert started == ["preflight", "cluster"]


# ── 롤 단위 단계 (실제 설치 로그 기준) ──────────────────────────────

INSTALL_ROLES = (
    "preflight", "os_base", "tools_aqua", "network", "time", "storage", "k0s",
    "cluster_storage", "k0s_airgap_export", "zarf_init", "gitops_publish",
    "app_charts_fetch", "flux_install", "platform_secrets", "shared_storage",
    "platform_component", "otel_instrumentation", "consul_seed", "secrets_fetch",
    "temporal_register", "flux_wire", "verify",
)


def test_install_roles_walk_the_nine_steps_in_order():
    """실제 설치가 내는 롤 순서를 그대로 흘리면 9단계가 순서대로 나와야 한다.

    롤 이름과 순서는 실서버 설치 로그(job #33, 1627줄)에서 확인한 것이다.
    묶음이 순서를 건너뛰면 화면의 단계가 뒤로 돌아간다.
    """
    parser = AnsibleLogParser()
    seen = []
    for role in INSTALL_ROLES:
        step = parser.feed(f"TASK [{role} : 무언가] ***").step
        if not seen or seen[-1] != step:
            seen.append(step)
    assert seen == ["preflight", "os", "k0s", "cluster", "gitops", "flux",
                    "platform", "wire", "verify"]


def test_a_role_out_of_order_never_walks_the_step_back():
    """같은 롤이 뒤에서 또 돌아도 진행 표시가 되돌아가면 안 된다.

    되돌아가면 사람이 "처음부터 다시 하나" 로 읽는다.
    """
    parser = AnsibleLogParser()
    parser.feed("TASK [platform_component : 설치] ***")
    assert parser.step == "platform"
    parser.feed("TASK [cluster_storage : 뒷정리] ***")
    assert parser.step == "platform"


def test_task_without_a_known_role_keeps_the_step():
    """설명문 TASK(`TASK [한글 설명]`)는 단계를 흔들지 않는다."""
    parser = AnsibleLogParser()
    parser.feed("TASK [gitops_publish : push] ***")
    parsed = parser.feed("TASK [앱 차트 fetch-once — 로컬 반입] ***")
    assert parsed.step == "gitops"
    assert parsed.step_started is False


def test_role_name_without_a_task_suffix_is_matched():
    parser = AnsibleLogParser()
    assert parser.feed("TASK [flux_install] ***").step == "flux"


def test_a_job_kind_is_not_a_step():
    """`install` 은 종류지 단계가 아니다.

    이걸 current_step 에 적어두는 바람에 화면이 단계 목록에서 못 찾아
    진행 표시가 통째로 멈춰 있었다.
    """
    assert is_step("install") is False
    assert is_step("bootstrap") is False   # 옛 키 — 표시용으로만 남겼다
    assert is_step(None) is False
    assert is_step("gitops") is True


def test_hubctl_preflight_header_sets_step():
    parser = AnsibleLogParser()
    parsed = parser.feed("━━ Preflight — 컨트롤러 자격 점검 (Vault / AWS / Bitbucket) ━━")
    assert parsed.step == "preflight"
    assert parsed.step_started is True


@pytest.mark.parametrize(
    "play,step",
    [
        ("PLAY [hubctl verify] ****", "verify"),
        ("PLAY [패치 번들 생성 (patch_create)] ****", "create"),
        ("PLAY [패치 번들 적용 (patch_apply)] ****", "apply"),
        ("PLAY [패치 롤백 (patch_apply/rollback)] ****", "rollback"),
        ("PLAY [Clean (초기화 — reset|uninstall)] ****", "clean"),
    ],
)
def test_every_step_marker_matches_a_real_play_name(play, step):
    assert AnsibleLogParser().feed(play).step == step


# ── RECAP ───────────────────────────────────────────────────────────


def test_recap_parsed_for_every_host(real):
    parser, _ = real
    assert set(parser.recaps) == {"alpha", "beta", "gamma"}
    assert parser.recaps["alpha"] == HostRecap("alpha", ok=5, changed=1, unreachable=0, failed=0, skipped=2)
    assert parser.recaps["beta"].failed == 1
    assert parser.recaps["gamma"].unreachable == 1


def test_recap_success_judged_by_failed_and_unreachable(real):
    parser, _ = real
    assert parser.recaps["alpha"].succeeded is True
    assert parser.recaps["beta"].succeeded is False    # failed=1
    assert parser.recaps["gamma"].succeeded is False   # unreachable=1


def test_recap_lines_are_tagged_with_their_host(real):
    _, lines = real
    recap_lines = [p for p in lines if p.kind is LineKind.RECAP]
    assert [p.host for p in recap_lines] == ["alpha", "beta", "gamma"]


def test_host_missing_from_recap_counts_as_failed():
    """앞선 play 에서 탈락하면 RECAP 에 아예 안 나온다 (§F5)."""
    parser, _ = parse_all(REAL_OUTPUT)
    assert host_status(parser.recaps, "alpha") == "succeeded"
    assert host_status(parser.recaps, "beta") == "failed"
    assert host_status(parser.recaps, "nonexistent") == "failed"


def test_recap_tolerates_missing_trailing_fields():
    """구버전/변형 출력에 skipped 이후 필드가 없어도 죽지 않아야 한다."""
    parser = AnsibleLogParser()
    parser.feed("PLAY RECAP ****")
    parsed = parser.feed("node1                      : ok=3    changed=0    unreachable=0    failed=0")
    assert parsed.kind is LineKind.RECAP
    assert parser.recaps["node1"] == HostRecap("node1", ok=3)


def test_text_before_recap_marker_is_not_treated_as_recap():
    parser = AnsibleLogParser()
    parsed = parser.feed("somehost : ok=1 changed=0 unreachable=0 failed=0")
    assert parsed.kind is not LineKind.RECAP
    assert parser.recaps == {}


def test_carriage_return_is_stripped():
    parsed = AnsibleLogParser().feed("ok: [alpha]\r\n")
    assert parsed.text == "ok: [alpha]"
    assert parsed.host == "alpha"


# ── 이어짐 본문의 종류 ──────────────────────────────────────────────


def _kinds(block: str) -> list[str]:
    parser = AnsibleLogParser()
    return [parser.feed(line).kind.value for line in block.splitlines()]


def test_continuation_body_inherits_the_opening_verb():
    """`ok: [h] =>` 뒤의 본문은 ok 다. 실패 본문만 err 여야 한다.

    전에는 이어짐이 열려 있기만 하면 무조건 ERROR 로 칠했다. 그 결과
    `msg: All assertions passed` 같은 성공 본문이 빨갛게 뜨고, 오류 요약
    패널이 성공 메시지로 가득 찼다 (실측 23건 중 대부분이 이것이었다).
    """
    assert _kinds(
        "ok: [testpc] =>\n"
        "  changed: false\n"
        "  msg: All assertions passed"
    ) == ["ok", "ok", "ok"]

    assert _kinds(
        "changed: [testpc] =>\n"
        "  msg: k0s 단일 노드 준비 완료"
    ) == ["chg", "chg"]

    assert _kinds(
        "fatal: [testpc]: FAILED! =>\n"
        "  msg: 실패 이유\n"
        "  rc: 1"
    ) == ["err", "err", "err"]


def test_continuation_closes_on_the_next_unindented_line():
    assert _kinds(
        "fatal: [testpc]: FAILED! =>\n"
        "  msg: 실패\n"
        "ok: [testpc]\n"
        "  이건 이어짐이 아니다"
    ) == ["err", "err", "ok", "out"]


def test_a_blank_line_ends_the_continuation():
    assert _kinds(
        "fatal: [testpc]: FAILED! =>\n"
        "  msg: 실패\n"
        "\n"
        "  더 이상 실패 본문이 아니다"
    ) == ["err", "err", "out", "out"]


def test_retry_lines_are_not_errors():
    """`FAILED - RETRYING` 은 정상 폴링이다. k0s 기동 대기만 해도 수십 줄 나온다."""
    assert _kinds(
        "FAILED - RETRYING: [testpc]: k0s | 노드 Ready 대기 (59 retries left)."
    ) == ["out"]
