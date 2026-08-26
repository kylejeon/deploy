"""세션·CSRF·로그인 제한 단위 테스트 (dev-spec-web-console §F1)."""
from __future__ import annotations

import pytest

from autodeploy.accounts import create_user, delete_user
from autodeploy.db import connect
from autodeploy.web.auth import (
    LOGIN_FAILED_MESSAGE,
    LoginThrottle,
    create_session,
    csrf_matches,
    csrf_token_for,
    destroy_session,
    hash_token,
    login,
    purge_expired_sessions,
    resolve_session,
)

PASSWORD = "correct-horse-battery"


@pytest.fixture
async def db(temp_db):
    async with connect(temp_db) as conn:
        await create_user(conn, "yonghyuk", PASSWORD)
        yield conn


# ── 세션 토큰 ───────────────────────────────────────────────────────


async def test_raw_token_is_never_stored(db):
    """DB 가 유출돼도 그 값으로 로그인할 수 없어야 한다."""
    raw = await create_session(db, 1)
    async with db.execute("SELECT token_hash FROM sessions") as cur:
        stored = (await cur.fetchone())["token_hash"]
    assert stored != raw
    assert stored == hash_token(raw)


async def test_resolve_returns_the_user(db):
    raw = await create_session(db, 1)
    session = await resolve_session(db, raw)
    assert session is not None
    assert session.user.username == "yonghyuk"


@pytest.mark.parametrize("token", [None, "", "bogus-token"])
async def test_resolve_rejects_bad_tokens(db, token):
    assert await resolve_session(db, token) is None


async def test_expired_session_is_rejected(db):
    """만료는 SQLite 시계로 계산한다 — 파이썬 로컬시각과 섞으면 9시간 어긋난다."""
    raw = await create_session(db, 1, ttl_days=14)
    await db.execute(
        "UPDATE sessions SET expires_at = datetime('now', '-1 second') WHERE token_hash=?",
        (hash_token(raw),),
    )
    await db.commit()
    assert await resolve_session(db, raw) is None


async def test_session_not_yet_expired_is_accepted(db):
    raw = await create_session(db, 1, ttl_days=1)
    assert await resolve_session(db, raw) is not None


async def test_disabled_user_session_stops_working(db):
    raw = await create_session(db, 1)
    await db.execute("UPDATE users SET disabled_at=CURRENT_TIMESTAMP WHERE id=1")
    await db.commit()
    assert await resolve_session(db, raw) is None


async def test_destroy_session(db):
    raw = await create_session(db, 1)
    await destroy_session(db, raw)
    assert await resolve_session(db, raw) is None


async def test_deleting_a_user_cascades_to_sessions(db):
    raw = await create_session(db, 1)
    await delete_user(db, "yonghyuk")
    assert await resolve_session(db, raw) is None


async def test_purge_expired_removes_only_expired(db):
    live = await create_session(db, 1, ttl_days=7)
    dead = await create_session(db, 1, ttl_days=7)
    await db.execute(
        "UPDATE sessions SET expires_at = datetime('now', '-1 day') WHERE token_hash=?",
        (hash_token(dead),),
    )
    await db.commit()
    assert await purge_expired_sessions(db) == 1
    assert await resolve_session(db, live) is not None


# ── CSRF ────────────────────────────────────────────────────────────


def test_csrf_token_is_derived_from_the_session_token():
    assert csrf_token_for("abc") == csrf_token_for("abc")
    assert csrf_token_for("abc") != csrf_token_for("abd")


def test_csrf_token_is_not_the_session_token():
    """헤더로 나가는 값이 쿠키 값과 같으면 파생하는 의미가 없다."""
    assert csrf_token_for("session-token") != "session-token"


def test_csrf_matches():
    token = "session-token"
    assert csrf_matches(token, csrf_token_for(token)) is True


@pytest.mark.parametrize("presented", [None, "", "wrong", "SESSION-TOKEN"])
def test_csrf_rejects_wrong_values(presented):
    assert csrf_matches("session-token", presented) is False


# ── 로그인 제한 ─────────────────────────────────────────────────────


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_throttle_locks_after_max_failures():
    clock = FakeClock()
    throttle = LoginThrottle(max_failures=5, lock_seconds=60.0, now=clock)
    for _ in range(4):
        assert throttle.record_failure("10.0.0.1", "yonghyuk") == 0.0
        assert throttle.retry_after("10.0.0.1", "yonghyuk") == 0.0
    assert throttle.record_failure("10.0.0.1", "yonghyuk") == 60.0
    assert throttle.retry_after("10.0.0.1", "yonghyuk") == 60.0


def test_throttle_unlocks_after_the_window():
    clock = FakeClock()
    throttle = LoginThrottle(max_failures=2, lock_seconds=60.0, now=clock)
    throttle.record_failure("ip", "u")
    throttle.record_failure("ip", "u")
    assert throttle.retry_after("ip", "u") == 60.0
    clock.advance(61)
    assert throttle.retry_after("ip", "u") == 0.0


def test_counter_resets_when_the_lock_lifts():
    """잠금 해제 직후 1회 실패로 곧바로 다시 잠기면 사실상 영구 잠금이 된다."""
    clock = FakeClock()
    throttle = LoginThrottle(max_failures=2, lock_seconds=60.0, now=clock)
    throttle.record_failure("ip", "u")
    throttle.record_failure("ip", "u")
    clock.advance(61)
    assert throttle.retry_after("ip", "u") == 0.0
    assert throttle.record_failure("ip", "u") == 0.0, "카운터가 새로 시작돼야 한다"


def test_throttle_is_scoped_per_ip_and_username():
    throttle = LoginThrottle(max_failures=2, lock_seconds=60.0, now=FakeClock())
    throttle.record_failure("10.0.0.1", "yonghyuk")
    throttle.record_failure("10.0.0.1", "yonghyuk")
    assert throttle.retry_after("10.0.0.1", "yonghyuk") > 0
    assert throttle.retry_after("10.0.0.2", "yonghyuk") == 0.0
    assert throttle.retry_after("10.0.0.1", "other") == 0.0


def test_success_resets_the_counter():
    throttle = LoginThrottle(max_failures=3, lock_seconds=60.0, now=FakeClock())
    throttle.record_failure("ip", "u")
    throttle.record_failure("ip", "u")
    throttle.reset("ip", "u")
    assert throttle.record_failure("ip", "u") == 0.0


# ── login() ─────────────────────────────────────────────────────────


async def test_login_success_issues_a_session(db):
    throttle = LoginThrottle()
    result = await login(
        db, username="yonghyuk", password=PASSWORD, client_ip="ip", throttle=throttle
    )
    assert result.ok
    assert result.session.user.username == "yonghyuk"
    assert await resolve_session(db, result.session.raw_token) is not None


async def test_login_is_case_insensitive_on_username(db):
    result = await login(
        db, username="  YongHyuk  ", password=PASSWORD, client_ip="ip",
        throttle=LoginThrottle(),
    )
    assert result.ok


async def test_wrong_password_gives_the_generic_message(db):
    result = await login(
        db, username="yonghyuk", password="wrong-password", client_ip="ip",
        throttle=LoginThrottle(),
    )
    assert not result.ok
    assert result.message == LOGIN_FAILED_MESSAGE


async def test_unknown_user_gives_the_same_message(db):
    """아이디 존재 여부가 응답으로 새면 계정 열거가 가능해진다."""
    result = await login(
        db, username="nosuchuser", password=PASSWORD, client_ip="ip",
        throttle=LoginThrottle(),
    )
    assert not result.ok
    assert result.message == LOGIN_FAILED_MESSAGE


async def test_login_locks_out_after_repeated_failures(db):
    """AC-2: 잘못된 비밀번호 5회 후 60초 잠긴다."""
    throttle = LoginThrottle(max_failures=5, lock_seconds=60.0, now=FakeClock())
    for _ in range(5):
        result = await login(
            db, username="yonghyuk", password="nope", client_ip="ip", throttle=throttle
        )
        assert not result.ok

    blocked = await login(
        db, username="yonghyuk", password=PASSWORD, client_ip="ip", throttle=throttle
    )
    assert not blocked.ok, "잠금 중에는 올바른 비밀번호도 통과하면 안 된다"
    assert blocked.retry_after > 0
    assert "초 후" in blocked.message


async def test_lock_lifts_and_correct_password_works(db):
    clock = FakeClock()
    throttle = LoginThrottle(max_failures=2, lock_seconds=60.0, now=clock)
    for _ in range(2):
        await login(db, username="yonghyuk", password="nope", client_ip="ip", throttle=throttle)
    clock.advance(61)
    result = await login(
        db, username="yonghyuk", password=PASSWORD, client_ip="ip", throttle=throttle
    )
    assert result.ok


async def test_successful_login_clears_earlier_failures(db):
    throttle = LoginThrottle(max_failures=3, lock_seconds=60.0, now=FakeClock())
    await login(db, username="yonghyuk", password="nope", client_ip="ip", throttle=throttle)
    await login(db, username="yonghyuk", password="nope", client_ip="ip", throttle=throttle)
    assert (await login(
        db, username="yonghyuk", password=PASSWORD, client_ip="ip", throttle=throttle
    )).ok
    # 카운터가 남아있었다면 다음 1회 실패로 잠겼을 것이다.
    assert await throttle_failure_locks(throttle) is False


async def throttle_failure_locks(throttle: LoginThrottle) -> bool:
    return throttle.record_failure("ip", "yonghyuk") > 0


async def test_client_ip_is_recorded(db):
    result = await login(
        db, username="yonghyuk", password=PASSWORD, client_ip="192.168.0.9",
        throttle=LoginThrottle(),
    )
    async with db.execute("SELECT client_ip FROM sessions") as cur:
        assert (await cur.fetchone())["client_ip"] == "192.168.0.9"
    assert result.ok
