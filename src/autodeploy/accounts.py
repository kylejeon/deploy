"""웹 콘솔 계정 — scrypt 해시 + users 테이블 CRUD.

dev-spec-web-console §F1. 세션 발급/검증은 web/auth.py 담당이고,
여기는 비밀번호와 사용자 레코드만 다룬다 (CLI 와 웹이 함께 쓴다).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
from dataclasses import dataclass

import aiosqlite

# scrypt 파라미터 — 저장된 해시와 짝이므로 바꾸면 기존 비밀번호를 못 푼다.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
DKLEN = 32
SALT_BYTES = 16

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
MIN_PASSWORD_LEN = 8


class AccountError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class User:
    id: int
    username: str
    created_at: str | None = None
    last_login_at: str | None = None
    disabled_at: str | None = None

    @property
    def disabled(self) -> bool:
        return self.disabled_at is not None


def _derive(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=DKLEN
    )


def hash_password(password: str) -> tuple[bytes, bytes]:
    """(salt, hash) 반환. 평문은 어디에도 저장하지 않는다."""
    validate_password(password)
    salt = os.urandom(SALT_BYTES)
    return salt, _derive(password, salt)


def verify_password(password: str, salt: bytes, expected: bytes) -> bool:
    return hmac.compare_digest(_derive(password, salt), expected)


def validate_username(username: str) -> None:
    if not USERNAME_RE.match(username):
        raise AccountError(
            f"아이디가 올바르지 않습니다: {username!r} — 소문자/숫자로 시작하는 2~32자 (. _ - 사용 가능)"
        )


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise AccountError(f"비밀번호는 최소 {MIN_PASSWORD_LEN}자입니다")


def _row_to_user(row: aiosqlite.Row) -> User:
    return User(
        id=row["id"],
        username=row["username"],
        created_at=row["created_at"],
        last_login_at=row["last_login_at"],
        disabled_at=row["disabled_at"],
    )


async def create_user(db: aiosqlite.Connection, username: str, password: str) -> int:
    validate_username(username)
    salt, digest = hash_password(password)
    if await get_user(db, username) is not None:
        raise AccountError(f"이미 있는 아이디입니다: {username}")
    cur = await db.execute(
        "INSERT INTO users (username, pw_hash, pw_salt) VALUES (?, ?, ?)",
        (username, digest, salt),
    )
    await db.commit()
    return int(cur.lastrowid)


async def set_password(db: aiosqlite.Connection, username: str, password: str) -> None:
    if await get_user(db, username) is None:
        raise AccountError(f"없는 아이디입니다: {username}")
    salt, digest = hash_password(password)
    await db.execute(
        "UPDATE users SET pw_hash=?, pw_salt=? WHERE username=?", (digest, salt, username)
    )
    await db.commit()
    # 비밀번호가 바뀌면 기존 세션은 전부 무효화한다
    await db.execute(
        "DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username=?)",
        (username,),
    )
    await db.commit()


async def delete_user(db: aiosqlite.Connection, username: str) -> None:
    if await get_user(db, username) is None:
        raise AccountError(f"없는 아이디입니다: {username}")
    await db.execute("DELETE FROM users WHERE username=?", (username,))
    await db.commit()


async def get_user(db: aiosqlite.Connection, username: str) -> User | None:
    async with db.execute("SELECT * FROM users WHERE username=?", (username,)) as cur:
        row = await cur.fetchone()
    return _row_to_user(row) if row else None


async def list_users(db: aiosqlite.Connection) -> list[User]:
    async with db.execute("SELECT * FROM users ORDER BY username") as cur:
        rows = await cur.fetchall()
    return [_row_to_user(r) for r in rows]


async def authenticate(
    db: aiosqlite.Connection, username: str, password: str
) -> User | None:
    """성공 시 User, 실패 시 None. 아이디 존재 여부를 응답으로 구분할 수 없게 한다.

    없는 아이디여도 더미 salt 로 scrypt 를 한 번 돌려 응답 시간을 맞춘다.
    """
    async with db.execute(
        "SELECT * FROM users WHERE username=?", (username,)
    ) as cur:
        row = await cur.fetchone()

    if row is None:
        _derive(password, b"\x00" * SALT_BYTES)  # 타이밍 평탄화
        return None
    if row["disabled_at"] is not None:
        _derive(password, b"\x00" * SALT_BYTES)
        return None
    if not verify_password(password, row["pw_salt"], row["pw_hash"]):
        return None

    # CURRENT_TIMESTAMP(UTC) 로 통일한다. 여기만 datetime.now()(로컬)를 쓰면
    # created_at·key_installed_at 과 형식·기준시가 달라져 화면에서 9시간 어긋난다.
    await db.execute(
        "UPDATE users SET last_login_at=CURRENT_TIMESTAMP WHERE id=?", (row["id"],)
    )
    await db.commit()
    return _row_to_user(row)
