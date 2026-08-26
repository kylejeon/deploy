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
    parser, lines = real
    by_text = {p.text.split(" ")[0] + p.text[:30]: p.step for p in lines}
    del by_text
    ok_alpha_first = next(p for p in lines if p.text == "ok: [alpha]")
    assert ok_alpha_first.step == "bootstrap"
    second_play = next(p for p in lines if p.text.startswith("TASK [second play task]"))
    assert second_play.step == "configure"
    assert parser.step == "configure"


def test_step_started_only_on_boundary(real):
    _, lines = real
    started = [p.step for p in lines if p.step_started]
    assert started == ["bootstrap", "configure"]


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
