"""터미널 서브커맨드 — 웹 콘솔 계정 관리 (dev-spec-web-console §F1).

    python -m autodeploy adduser <아이디>
    python -m autodeploy passwd  <아이디>
    python -m autodeploy deluser <아이디>
    python -m autodeploy users
    python -m autodeploy fixlogs [작업번호]

비밀번호는 항상 프롬프트로만 받는다 — 인자로 받으면 셸 히스토리에 남는다.
"""
from __future__ import annotations

import asyncio
import getpass
import sys
from collections.abc import Callable
from pathlib import Path

from autodeploy.accounts import (
    AccountError,
    create_user,
    delete_user,
    list_users,
    set_password,
)
from autodeploy.db import connect, init_db
from autodeploy.repository import reclassify_script_logs
from autodeploy.settings import resolve_db_path

COMMANDS = ("adduser", "passwd", "deluser", "users", "fixlogs")

USAGE = """사용법:
  autodeploy adduser <아이디>   웹 콘솔 계정 생성
  autodeploy passwd  <아이디>   비밀번호 변경 (기존 세션 전부 로그아웃)
  autodeploy deluser <아이디>   계정 삭제
  autodeploy users              계정 목록
  autodeploy fixlogs [번호]     저장된 로그의 오류 분류를 현재 파서로 다시 매김
                                (번호 생략 시 전체. 표시용 값만 고치며 로그 원문은 그대로)

인자 없이 실행하면 데몬(Slack 봇 + 웹 콘솔)이 뜬다."""

PasswordPrompt = Callable[[str], str]


def _ask_twice(prompt: PasswordPrompt, who: str) -> str:
    first = prompt(f"{who} 비밀번호: ")
    second = prompt("한 번 더: ")
    if first != second:
        raise AccountError("두 번 입력한 비밀번호가 다릅니다")
    return first


async def _run(argv: list[str], db_path: Path, prompt: PasswordPrompt) -> int:
    cmd = argv[0]
    await init_db(db_path)

    async with connect(db_path) as db:
        if cmd == "users":
            users = await list_users(db)
            if not users:
                print("등록된 계정이 없습니다. 'autodeploy adduser <아이디>' 로 만드세요.")
                return 0
            print(f"{'아이디':<20} {'생성':<21} {'마지막 로그인':<21} 상태")
            for u in users:
                state = "비활성" if u.disabled else "정상"
                print(f"{u.username:<20} {u.created_at or '-':<21} {u.last_login_at or '-':<21} {state}")
            return 0

        if cmd == "fixlogs":
            # 파서를 고치기 전에 쌓인 줄은 옛 분류를 그대로 들고 있다. 새 작업만
            # 정상으로 두면 과거 작업 화면이 계속 틀린 채로 남는다.
            target = None
            if len(argv) >= 2:
                try:
                    target = int(argv[1])
                except ValueError:
                    print(f"[오류] 작업번호는 정수여야 합니다: {argv[1]!r}", file=sys.stderr)
                    return 2
            changed = await reclassify_script_logs(db, job_id=target)
            if not changed:
                print("바뀐 줄이 없습니다 (이미 최신 분류).")
                return 0
            for job_id, n in sorted(changed.items()):
                print(f"작업 #{job_id}: {n}줄 재분류")
            print(f"총 {sum(changed.values())}줄")
            return 0

        if len(argv) < 2:
            print(f"[오류] {cmd} 에는 아이디가 필요합니다\n\n{USAGE}", file=sys.stderr)
            return 2
        username = argv[1]

        if cmd == "adduser":
            await create_user(db, username, _ask_twice(prompt, username))
            print(f"계정을 만들었습니다: {username}")
        elif cmd == "passwd":
            await set_password(db, username, _ask_twice(prompt, username))
            print(f"비밀번호를 변경했습니다: {username} (기존 세션은 모두 로그아웃됩니다)")
        elif cmd == "deluser":
            await delete_user(db, username)
            print(f"계정을 삭제했습니다: {username}")
        return 0


def main(argv: list[str], *, prompt: PasswordPrompt | None = None) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if argv[0] not in COMMANDS:
        print(f"[오류] 알 수 없는 명령: {argv[0]}\n\n{USAGE}", file=sys.stderr)
        return 2

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    try:
        return asyncio.run(_run(argv, resolve_db_path(), prompt or getpass.getpass))
    except AccountError as exc:
        print(f"[오류] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print()
        return 130
