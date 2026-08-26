"""웹에서 시작한 작업의 Slack 게시 (dev-spec-web-console §F7).

기존 `SlackNotifier` 를 쓰지 않는다. 그쪽은 v1 `Job` 데이터클래스와 `Step` enum,
`messages.parent_message()` 의 병원코드·타겟IP 형식에 묶여 있어서, hubctl 작업을
끼워넣으려면 가짜 v1 Job 을 만들어야 하고 메시지도 엉뚱해진다. D1(Slack 코드는
건드리지 않는다)을 지키려면 여기서 따로 만드는 편이 옳다.

게시는 **시작 1회 + 종료 1회**만 한다. 로그를 통째로 흘리면 채널이 잠기고,
어차피 웹이 실시간 로그를 보여준다. 스레드 링크는 `chat.getPermalink` 로 받아온다 —
워크스페이스 도메인을 모르는 채로 URL 을 조립하면 안 되기 때문이다.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

_KIND_LABEL = {
    "install": "설치",
    "configure": "configure",
    "patch": "패치",
    "rollback": "롤백",
    "verify": "검증",
    "clean": "초기화",
}
_STATUS_LABEL = {
    "succeeded": ("✅", "성공"),
    "failed": ("❌", "실패"),
    "cancelled": ("⚠️", "취소됨"),
    "awaiting": ("⏸️", "승인 대기"),
}


def _hosts_text(hosts: tuple[str, ...] | list[str]) -> str:
    if not hosts:
        return "컨트롤러"
    return ", ".join(hosts)


class WebJobNotifier:
    """slack-sdk AsyncWebClient 를 주입받아 게시한다.

    Slack 이 죽었다고 설치가 실패하면 안 되므로 모든 실패를 삼키고 로그만 남긴다.
    """

    def __init__(self, client: Any, channel_id: str) -> None:
        self._client = client
        self._channel = channel_id

    async def job_started(
        self,
        job_id: int,
        *,
        kind: str,
        hosts: tuple[str, ...],
        env: str | None,
        ref: str | None,
        started_by: str,
        command: str,
    ) -> tuple[str | None, str | None]:
        """(thread_ts, permalink). 실패하면 (None, None)."""
        label = _KIND_LABEL.get(kind, kind)
        lines = [
            f"*{label} 시작* · 작업 #{job_id}",
            f"• 대상: `{_hosts_text(hosts)}`",
        ]
        if env:
            lines.append(f"• 환경: `{env}`")
        if ref:
            lines.append(f"• ref: `{ref}`")
        lines.append(f"• 실행: {started_by}")
        lines.append(f"```{command}```")

        try:
            resp = await self._client.chat_postMessage(
                channel=self._channel, text="\n".join(lines)
            )
            ts = resp["ts"]
        except Exception:
            log.exception("Slack 시작 알림 실패 (job=%d)", job_id)
            return None, None

        permalink = None
        try:
            link = await self._client.chat_getPermalink(
                channel=self._channel, message_ts=ts
            )
            permalink = link.get("permalink")
        except Exception:
            # 링크가 없어도 스레드 게시는 계속된다. 화면에서 버튼만 안 보인다.
            log.warning("Slack permalink 조회 실패 (job=%d)", job_id)
        return ts, permalink

    async def job_finished(
        self,
        job_id: int,
        *,
        thread_ts: str | None,
        status: str,
        exit_code: int | None,
        hosts: list[dict],
        duration: str,
        console_url: str | None = None,
    ) -> None:
        if not thread_ts:
            return
        glyph, label = _STATUS_LABEL.get(status, ("•", status))
        lines = [f"{glyph} *{label}* · 작업 #{job_id} · {duration}"]
        if exit_code is not None:
            lines.append(f"• 종료 코드: `{exit_code}`")
        for host in hosts:
            mark = "✅" if host["status"] == "succeeded" else "❌" if host["status"] == "failed" else "⚠️"
            recap = host.get("recap")
            detail = (
                f" (ok={recap['ok']} changed={recap['changed']}"
                f" failed={recap['failed']} unreachable={recap['unreachable']})"
                if recap else ""
            )
            lines.append(f"{mark} `{host['host']}`{detail}")
        if console_url:
            lines.append(f"<{console_url}|웹 콘솔에서 로그 보기>")

        try:
            await self._client.chat_postMessage(
                channel=self._channel, thread_ts=thread_ts, text="\n".join(lines)
            )
        except Exception:
            log.exception("Slack 종료 알림 실패 (job=%d)", job_id)
