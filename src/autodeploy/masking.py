"""로그 마스킹. DB·Slack·웹 응답에 시크릿 평문이 실리지 않게 한다.

`mask_url_secrets` 는 원래 workflow.py 에 있던 것을 옮겨왔다 (workflow 가 재수출하므로
기존 import 경로는 그대로 동작한다). 웹 콘솔 쪽 모듈이 asyncssh/slack 을 끌고 오는
workflow 를 import 하지 않게 하려는 목적.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

# URL embedded credential pattern: scheme://user:TOKEN@host → scheme://user:***@host
# git clone 명령 + stderr가 토큰 포함 URL을 echo할 때 DB/Slack 평문 노출 방지 (QA D-3).
_URL_SECRET_PATTERN = re.compile(r"(https?://[^/\s:@]+:)[^@\s]+(@)")

REDACTED = "***"


def mask_url_secrets(text: str) -> str:
    return _URL_SECRET_PATTERN.sub(rf"\1{REDACTED}\2", text)


class SecretMasker:
    """알려진 시크릿 값들을 평문 치환.

    hubctl 은 VAULT_TOKEN·HUB_DEPLOY_GIT_TOKEN 을 환경에서 읽어 하위 명령에 넘기고,
    ansible 은 실패한 태스크의 인자를 통째로 되뱉는다. 값을 미리 등록해두고
    **DB 적재 전에** 지운다 (dev-spec-web-console §F4 마스킹).

    짧은 값은 등록하지 않는다 — 예컨대 become 비밀번호가 "1234" 라면 로그의 모든
    숫자열이 ***로 변해 진단이 불가능해진다. 그 경우는 마스킹을 포기하는 대신
    비밀번호를 길게 쓰는 것이 맞다.
    """

    MIN_LEN = 6

    __slots__ = ("_values",)

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        seen: set[str] = set()
        for raw in secrets:
            if not raw:
                continue
            value = raw.strip()
            if len(value) >= self.MIN_LEN:
                seen.add(value)
        # 긴 값부터 지운다. 짧은 값이 긴 값의 부분문자열일 때 긴 쪽이 먼저 사라져야
        # "***XYZ" 같은 잔여물이 남지 않는다.
        self._values: tuple[str, ...] = tuple(sorted(seen, key=len, reverse=True))

    @property
    def values(self) -> tuple[str, ...]:
        return self._values

    def __call__(self, text: str) -> str:
        out = mask_url_secrets(text)
        for value in self._values:
            if value in out:
                out = out.replace(value, REDACTED)
        return out
