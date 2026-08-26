"""세션 발급·검증 + 로그인 제한 + CSRF (dev-spec-web-console §F1).

## 세션 토큰

랜덤 32바이트를 발급해 **쿠키에는 원본, DB 에는 sha256 만** 넣는다. DB 가 유출돼도
그 값으로는 로그인할 수 없다 (비밀번호를 해시로 저장하는 것과 같은 이유).

만료는 SQLite 의 `datetime('now', ...)` 로 계산한다. 파이썬에서 계산해 넣으면
`datetime.now()`(로컬)와 SQLite `CURRENT_TIMESTAMP`(UTC) 가 어긋나 만료가
9시간씩 밀리거나 당겨진다.

## CSRF

세션 원본 토큰에서 결정적으로 파생한다 (`sha256("csrf:" + token)`).
`sessions` 에 컬럼을 늘리지 않아도 되고, 검증이 상수 시간 비교 하나로 끝난다.

안전한 이유: 원본 토큰은 `HttpOnly` 쿠키라 스크립트가 못 읽고, 파생값은
같은 오리진에서 `/api/me` 로만 받을 수 있다. 다른 오리진의 스크립트는 쿠키도
응답도 못 읽으므로 헤더에 넣을 값을 만들 수 없다. 쿠키는 자동으로 실려가지만
헤더는 그렇지 않다는 점이 이 방식의 핵심이다.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import aiosqlite

from autodeploy.accounts import User, authenticate

log = logging.getLogger(__name__)

SESSION_COOKIE = "ad_session"
CSRF_HEADER = "X-CSRF-Token"
SESSION_TTL_DAYS = 14
TOKEN_BYTES = 32

# 잠금 정책 (§F1): (IP, username) 조합 5회 실패 시 60초.
MAX_FAILURES = 5
LOCK_SECONDS = 60.0

# 실패 응답은 항상 이 문구 하나 — 아이디 존재 여부가 새지 않게 한다.
LOGIN_FAILED_MESSAGE = "아이디 또는 비밀번호가 올바르지 않습니다"


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def csrf_token_for(raw_session_token: str) -> str:
    return hashlib.sha256(f"csrf:{raw_session_token}".encode()).hexdigest()


def csrf_matches(raw_session_token: str, presented: str | None) -> bool:
    if not presented:
        return False
    return hmac.compare_digest(csrf_token_for(raw_session_token), presented)


@dataclass(frozen=True, slots=True)
class Session:
    user: User
    raw_token: str

    @property
    def csrf_token(self) -> str:
        return csrf_token_for(self.raw_token)


# ── 로그인 제한 ─────────────────────────────────────────────────────


@dataclass(slots=True)
class _Attempts:
    failures: int = 0
    locked_until: float = 0.0


class LoginThrottle:
    """(IP, username) 별 실패 카운터. 프로세스 메모리에만 둔다.

    데몬을 재시작하면 초기화되지만, 재시작에는 맥미니 접근 권한이 필요하므로
    공격자가 이를 우회 수단으로 쓸 수는 없다. 영속화할 이유가 없다.
    """

    __slots__ = ("_max_failures", "_lock_seconds", "_now", "_state")

    def __init__(
        self,
        *,
        max_failures: int = MAX_FAILURES,
        lock_seconds: float = LOCK_SECONDS,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_failures = max_failures
        self._lock_seconds = lock_seconds
        self._now = now
        self._state: dict[tuple[str, str], _Attempts] = {}

    def retry_after(self, ip: str, username: str) -> float:
        """남은 잠금 시간(초). 0 이면 시도해도 된다."""
        entry = self._state.get((ip, username))
        if entry is None:
            return 0.0
        remaining = entry.locked_until - self._now()
        if remaining <= 0:
            # 잠금이 풀렸으면 카운터도 같이 비운다 — 잠금 직후 1회 실패로
            # 곧바로 다시 잠기면 사실상 영구 잠금이 된다.
            if entry.locked_until:
                self._state.pop((ip, username), None)
            return 0.0
        return remaining

    def record_failure(self, ip: str, username: str) -> float:
        entry = self._state.setdefault((ip, username), _Attempts())
        entry.failures += 1
        if entry.failures >= self._max_failures:
            entry.locked_until = self._now() + self._lock_seconds
            entry.failures = 0
            log.warning("로그인 %d회 실패 — %.0f초 잠금 (ip=%s user=%s)",
                        self._max_failures, self._lock_seconds, ip, username)
            return self._lock_seconds
        return 0.0

    def reset(self, ip: str, username: str) -> None:
        self._state.pop((ip, username), None)

    def clear(self) -> None:
        self._state.clear()


# ── 세션 CRUD ───────────────────────────────────────────────────────


async def create_session(
    db: aiosqlite.Connection,
    user_id: int,
    *,
    client_ip: str | None = None,
    ttl_days: int = SESSION_TTL_DAYS,
) -> str:
    """원본 토큰을 돌려준다. 저장되는 것은 해시뿐이다."""
    raw = secrets.token_urlsafe(TOKEN_BYTES)
    await db.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at, client_ip)"
        " VALUES (?, ?, datetime('now', ?), ?)",
        (hash_token(raw), user_id, f"+{int(ttl_days)} days", client_ip),
    )
    await db.commit()
    return raw


async def resolve_session(db: aiosqlite.Connection, raw_token: str | None) -> Session | None:
    """쿠키 값 → 세션. 만료·삭제·비활성 계정이면 None."""
    if not raw_token:
        return None
    async with db.execute(
        """SELECT u.* FROM sessions s
             JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ?
              AND s.expires_at > datetime('now')
              AND u.disabled_at IS NULL""",
        (hash_token(raw_token),),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return Session(user=_user_from_row(row), raw_token=raw_token)


async def destroy_session(db: aiosqlite.Connection, raw_token: str | None) -> None:
    if not raw_token:
        return
    await db.execute("DELETE FROM sessions WHERE token_hash=?", (hash_token(raw_token),))
    await db.commit()


async def purge_expired_sessions(db: aiosqlite.Connection) -> int:
    cur = await db.execute("DELETE FROM sessions WHERE expires_at <= datetime('now')")
    await db.commit()
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


# ── 로그인 ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class LoginResult:
    session: Session | None = None
    retry_after: float = 0.0
    message: str = field(default=LOGIN_FAILED_MESSAGE)

    @property
    def ok(self) -> bool:
        return self.session is not None


async def login(
    db: aiosqlite.Connection,
    *,
    username: str,
    password: str,
    client_ip: str,
    throttle: LoginThrottle,
    ttl_days: int = SESSION_TTL_DAYS,
) -> LoginResult:
    username = (username or "").strip().lower()
    remaining = throttle.retry_after(client_ip, username)
    if remaining > 0:
        return LoginResult(
            retry_after=remaining,
            message=f"로그인 시도가 너무 많습니다. {int(remaining) + 1}초 후 다시 시도하세요",
        )

    user = await authenticate(db, username, password)
    if user is None:
        # 잠금을 유발한 시도 자체도 '자격 실패'로 답한다. 여기서 429 를 주면
        # 마지막 실패만 응답이 달라져, 그 차이로 잠금 임계값을 역산할 수 있다.
        # 잠금은 다음 시도부터 retry_after 로 드러난다.
        throttle.record_failure(client_ip, username)
        return LoginResult()

    throttle.reset(client_ip, username)
    raw = await create_session(db, user.id, client_ip=client_ip, ttl_days=ttl_days)
    return LoginResult(session=Session(user=user, raw_token=raw))


def _user_from_row(row: aiosqlite.Row) -> User:
    return User(
        id=row["id"],
        username=row["username"],
        created_at=row["created_at"],
        last_login_at=row["last_login_at"],
        disabled_at=row["disabled_at"],
    )
