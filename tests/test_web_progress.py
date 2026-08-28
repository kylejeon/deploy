"""오래 걸리는 요청의 진행 상태판 (web/progress.py)."""
from __future__ import annotations

from autodeploy.web.progress import DETAIL_MAX, ProgressBoard


def test_nothing_is_running_at_first():
    board = ProgressBoard()
    assert board.get("alpha") is None


def test_the_first_step_is_set_when_it_starts():
    """시작하자마자 물어봐도 빈칸이면 안 된다 — 화면이 그때 처음 그린다."""
    board = ProgressBoard()
    board.start("alpha", ("connect", "key", "verify"))

    item = board.get("alpha")
    assert item.step == "connect"
    assert item.as_dict() == {
        "running": True, "steps": ["connect", "key", "verify"],
        "step": "connect", "detail": "",
    }


def test_moving_to_the_next_step_clears_the_old_output():
    """앞 단계의 마지막 줄이 남으면 무슨 일이 도는지 잘못 읽힌다."""
    board = ProgressBoard()
    board.start("alpha", ("key", "prep"))
    board.detail("alpha", "authorized_keys 갱신")
    board.step("alpha", "prep")

    assert board.get("alpha").step == "prep"
    assert board.get("alpha").detail == ""


def test_a_long_line_is_cut():
    """apt 출력은 한 줄이 길다. 그대로 흘리면 모달이 늘어난다."""
    board = ProgressBoard()
    board.start("alpha", ("prep",))
    board.detail("alpha", "x" * (DETAIL_MAX + 50))
    assert len(board.get("alpha").detail) == DETAIL_MAX


def test_a_blank_line_does_not_erase_what_was_there():
    """빈 줄이 하나 지나갔다고 화면이 깜빡이면 안 된다."""
    board = ProgressBoard()
    board.start("alpha", ("prep",))
    board.detail("alpha", "AnyDesk 설치")
    board.detail("alpha", "   ")
    assert board.get("alpha").detail == "AnyDesk 설치"


def test_finishing_removes_it():
    """끝난 것과 시작 안 한 것을 구분할 이유가 없다 — 둘 다 '안 돌고 있다'."""
    board = ProgressBoard()
    board.start("alpha", ("connect",))
    board.finish("alpha")
    assert board.get("alpha") is None
    board.finish("alpha")  # 두 번 불러도 터지지 않는다


def test_updates_to_something_that_ended_are_ignored():
    """요청이 끝난 뒤 늦게 도착한 줄이 판을 되살리면 안 된다."""
    board = ProgressBoard()
    board.start("alpha", ("connect",))
    board.finish("alpha")

    board.step("alpha", "verify")
    board.detail("alpha", "늦게 온 줄")
    assert board.get("alpha") is None


def test_two_servers_do_not_mix():
    board = ProgressBoard()
    board.start("alpha", ("connect", "key"))
    board.start("beta", ("connect", "key"))
    board.step("beta", "key")

    assert board.get("alpha").step == "connect"
    assert board.get("beta").step == "key"
