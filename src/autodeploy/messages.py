"""Slack Block Kit 메시지 빌더. design-spec M-1 ~ M-13 구현.

각 함수는 ``{"text": "...", "blocks": [...]}`` 형태의 dict 반환.
text는 알림·접근성용 fallback, blocks는 실제 렌더링.
"""
from __future__ import annotations

from collections.abc import Sequence

from autodeploy.models import Job, JobStatus, Step

_STEP_ICONS: dict[Step, str] = {
    Step.SSH_CONNECT: "🔌",
    Step.GIT_PULL: "📥",
    Step.INFRA_INSTALL: "⚙️",
    Step.APP_INSTALL: "📦",
    Step.HEALTHCHECK: "🩺",
    Step.DONE: "✅",
}

_STEP_LABELS_KR: dict[Step, str] = {
    Step.SSH_CONNECT: "SSH 접속",
    Step.GIT_PULL: "스크립트 동기화",
    Step.INFRA_INSTALL: "인프라 설치",
    Step.APP_INSTALL: "어플리케이션 설치",
    Step.HEALTHCHECK: "헬스체크",
    Step.DONE: "완료",
}

_STEP_ORDER: tuple[Step, ...] = (
    Step.SSH_CONNECT,
    Step.GIT_PULL,
    Step.INFRA_INSTALL,
    Step.APP_INSTALL,
    Step.HEALTHCHECK,
)


# ---------- M-1: 부모 메시지 ----------

def parent_message(job: Job, *, total_duration_s: float | None = None) -> dict:
    icon, headline = _parent_headline(job, total_duration_s)
    hospital = _hospital_summary(job)
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"{icon}  {headline}", "emoji": True}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*대상*\n`{job.target_ip}`"},
                {"type": "mrkdwn", "text": f"*유형*\n`{job.deployment_type}`"},
                {"type": "mrkdwn", "text": f"*병원*\n{hospital}"},
                {"type": "mrkdwn", "text": f"*요청자*\n<@{job.started_by}>"},
            ],
        },
    ]
    if job.retry_of is not None:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"↻ 재시도 of #{job.retry_of}"}],
        })
    if job.status == JobStatus.RUNNING:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "진행 상황은 이 스레드에서 갱신됩니다 ↓"}],
        })
    return {"text": headline, "blocks": blocks}


def _parent_headline(job: Job, duration_s: float | None) -> tuple[str, str]:
    if job.status == JobStatus.SUCCEEDED:
        d = f" ({_fmt_duration(duration_s)})" if duration_s is not None else ""
        return "✅", f"AutoDeploy 작업 #{job.id} 완료{d}"
    if job.status == JobStatus.FAILED:
        step_part = f" — {job.current_step.value}" if job.current_step else ""
        return "❌", f"AutoDeploy 작업 #{job.id} 실패{step_part}"
    if job.status == JobStatus.CANCELLED:
        return "⚠️", f"AutoDeploy 작업 #{job.id} 취소됨"
    return "🔵", f"AutoDeploy 작업 #{job.id} 시작"


# ---------- M-2: 작업 시작 ack ----------

def ack_message(job_id: int) -> dict:
    text = f"⏳  접수했습니다. 작업 ID #{job_id}\n취소: `@autodeploy cancel {job_id}`"
    return {"text": text, "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]}


def cancel_ack(job_id: int) -> dict:
    """cancel 명령 즉시 응답 — 워크플로 정리는 백그라운드에서. 최종 결과는 스레드의 취소 요약."""
    text = f"🛑  작업 #{job_id} 취소 요청 받았습니다. 잠시 후 스레드에 결과가 갱신됩니다."
    return {"text": text, "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]}


# ---------- M-3: 단계 시작·완료 ----------

def step_started(step: Step) -> dict:
    icon = _STEP_ICONS.get(step, "▶️")
    label = _STEP_LABELS_KR.get(step, step.value)
    step_num, total = _step_position(step)
    text = f"⏳  [단계 {step_num}/{total}] {icon} {label} 시작"
    return {"text": text, "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]}


def step_finished(step: Step, *, success: bool, duration_s: float) -> dict:
    icon = "✓" if success else "✗"
    label = _STEP_LABELS_KR.get(step, step.value)
    step_num, total = _step_position(step)
    status = "완료" if success else "실패"
    text = f"{icon}  [단계 {step_num}/{total}] {label} {status} ({_fmt_duration(duration_s)})"
    return {"text": text, "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]}


def _step_position(step: Step) -> tuple[int, int]:
    try:
        idx = _STEP_ORDER.index(step) + 1
    except ValueError:
        idx = len(_STEP_ORDER)
    return idx, len(_STEP_ORDER)


# ---------- M-4: stdout 라이브 미리보기 ----------

def stdout_preview(step: Step, lines: Sequence[str], *, last_update_kst: str) -> dict:
    label = _STEP_LABELS_KR.get(step, step.value)
    tail = list(lines)[-10:]
    body = "\n".join(_truncate(l, 80) for l in tail) or "(아직 출력 없음)"
    text = f"실시간 로그 — {label}"
    return {
        "text": text,
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"📋  *{text}*"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"```\n{body}\n```"}},
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"마지막 업데이트 `{last_update_kst}` · 5초 간격"},
                ],
            },
        ],
    }


# ---------- M-5: 성공 요약 ----------

def success_summary(job: Job, *, total_duration_s: float) -> dict:
    text = f"작업 #{job.id} 완료"
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"✅  *{text}*"}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*총 소요시간*\n{_fmt_duration(total_duration_s)}"},
                {
                    "type": "mrkdwn",
                    "text": f"*커밋*\n`{job.script_commit_sha or '-'}`",
                },
            ],
        },
    ]
    if job.admin_web_url:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"▶ *Admin Web (Frontend) 접속*\n<{job.admin_web_url}|{job.admin_web_url}>"},
        })
    # admin 외 URL이 있으면 함께 노출 (예: Temporal Web, Web-PACS)
    extras = {k: v for k, v in (job.extra_urls or {}).items() if v != job.admin_web_url}
    if extras:
        lines = "\n".join(f"• *{label}*: <{url}|{url}>" for label, url in extras.items())
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"기타 URL:\n{lines}"},
        })
    hospital = _hospital_lines(job)
    if hospital:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"다음 단계: Admin Web에서 병원 등록\n{hospital}"}],
        })
    return {"text": text, "blocks": blocks, "attachment_color": "good"}


# ---------- M-6: 실패 요약 ----------

def failure_summary(job: Job, *, step: Step | None, stderr_tail: Sequence[str], duration_s: float) -> dict:
    step_label = _STEP_LABELS_KR.get(step, step.value if step else "?")
    text = f"작업 #{job.id} 실패 — {step.value if step else '?'}"
    body = "\n".join(_truncate(l, 120) for l in list(stderr_tail)[-20:])
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"❌  *{text}*"}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*실패 단계*\n{_STEP_ICONS.get(step, '')} {step_label}"},
                {"type": "mrkdwn", "text": f"*소요시간*\n{_fmt_duration(duration_s)} (실패 시점까지)"},
            ],
        },
    ]
    if body.strip():
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*stderr (마지막 20줄)*\n```\n{body}\n```"},
        })
    if job.error_message:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"에러: `{_truncate(job.error_message, 240)}`"}],
        })
    return {"text": text, "blocks": blocks, "attachment_color": "danger"}


# ---------- M-7: 취소 요약 ----------

def cancel_summary(job: Job, *, step_at_cancel: Step | None, duration_s: float) -> dict:
    text = f"작업 #{job.id} 취소됨"
    where = (
        f"{_STEP_LABELS_KR.get(step_at_cancel, step_at_cancel.value)} 직후"
        if step_at_cancel else "단계 경계"
    )
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"⚠️  *{text}*"}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*취소 시점*\n{where}"},
                {"type": "mrkdwn", "text": f"*소요시간*\n{_fmt_duration(duration_s)}"},
            ],
        },
        {
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": "타겟 서버 상태: 부분 설치된 상태로 남음. 정리하려면 SSH 접속하여 직접 제거.",
            }],
        },
    ]
    return {"text": text, "blocks": blocks, "attachment_color": "warning"}


# ---------- M-8: status ----------

def status_response(job: Job | None) -> dict:
    if job is None:
        text = "진행 중인 작업이 없습니다."
        return {
            "text": text,
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": f"ℹ️  {text}\n최근 작업: `@autodeploy list 5`"}},
            ],
        }
    icon = _status_icon(job.status)
    step_label = _STEP_LABELS_KR.get(job.current_step) if job.current_step else "?"
    text = f"작업 #{job.id} · {job.status.value}"
    return {
        "text": text,
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"{icon}  *{text}*"}},
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*대상*\n`{job.target_ip}`"},
                    {"type": "mrkdwn", "text": f"*유형*\n`{job.deployment_type}`"},
                    {"type": "mrkdwn", "text": f"*현재 단계*\n{step_label}"},
                    {"type": "mrkdwn", "text": f"*요청자*\n<@{job.started_by}>"},
                ],
            },
        ],
    }


# ---------- M-9: list ----------

def list_response(jobs: Sequence[Job], *, limit: int) -> dict:
    text = f"최근 작업 {len(jobs)}건"
    if not jobs:
        return {
            "text": "작업 이력이 없습니다.",
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "ℹ️  작업 이력이 없습니다."}}],
        }
    header = f"{'ID':>4}  {'상태':<6}  {'유형':<22}  {'대상':<16}"
    rows = [header, "-" * len(header)]
    for j in jobs:
        rows.append(
            f"{('#' + str(j.id)):>4}  {_status_icon(j.status):<6}  {j.deployment_type:<22}  {j.target_ip:<16}"
        )
    body = "\n".join(rows)
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"📋  *{text}*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"```\n{body}\n```"}},
    ]
    if len(jobs) >= limit:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"더 많이 보려면 `@autodeploy list {min(limit * 2, 50)}`"}],
        })
    return {"text": text, "blocks": blocks}


# ---------- M-10: help ----------

def help_response(valid_types: Sequence[str]) -> dict:
    types_lines = "\n".join(f"  • `{t}`" for t in valid_types)
    text = "AutoDeploy 명령어"
    body = (
        "*명령어*\n"
        "`@autodeploy install <IP> --type=<TYPE> --code=<병원코드> "
        "[--name=\"...\"] [--address=\"...\"]`\n"
        "  새 설치 시작.\n\n"
        "`@autodeploy status [job-id]`\n"
        "  진행 중 작업 상태. 생략 시 최신 1건.\n\n"
        "`@autodeploy list [N]`\n"
        "  최근 N건 (기본 10, 최대 50).\n\n"
        "`@autodeploy cancel <job-id>`\n"
        "  진행 중인 작업을 즉시 취소. 다음 await 지점에서 중단되며 스레드에 취소 요약이 게시됨.\n\n"
        "`@autodeploy retry [job-id]`\n"
        "  작업 재시도. 스레드 댓글로 실행하면 그 스레드의 원본 작업을 자동 인식.\n\n"
        "`@autodeploy help`\n"
        "  이 메시지.\n\n"
        f"*유효 TYPE*\n{types_lines}"
    )
    return {
        "text": text,
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"📖  *{text}*"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        ],
    }


# ---------- M-11: 권한 거부 ----------

def permission_denied() -> dict:
    text = "AutoDeploy 명령 권한이 없습니다."
    return {
        "text": text,
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"🚫  *{text}*"}},
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "사용 권한이 필요하면 관리자에게 문의하세요."}],
            },
        ],
    }


# ---------- M-12: 검증 에러 ----------

def validation_error(reason: str, *, suggestion: str | None = None) -> dict:
    text = f"⚠️ {reason}"
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    if suggestion:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": suggestion}],
        })
    return {"text": reason, "blocks": blocks}


# ---------- helpers ----------

def _hospital_summary(job: Job) -> str:
    name = job.hospital_name or ""
    if name:
        return f"`{job.hospital_code}` · {name}"
    return f"`{job.hospital_code}`"


def _hospital_lines(job: Job) -> str:
    parts = [f"• 병원코드: `{job.hospital_code}`"]
    if job.hospital_name:
        parts.append(f"• 병원명: {job.hospital_name}")
    if job.hospital_address:
        parts.append(f"• 주소: {job.hospital_address}")
    return "\n".join(parts)


def _status_icon(status: JobStatus) -> str:
    return {
        JobStatus.QUEUED: "⏸",
        JobStatus.RUNNING: "⏳",
        JobStatus.SUCCEEDED: "✅",
        JobStatus.FAILED: "❌",
        JobStatus.CANCELLED: "⚠️",
    }.get(status, "•")


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    s = int(seconds)
    if s < 60:
        return f"{s}초"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m}분 {sec:02d}초"
    h, m = divmod(m, 60)
    return f"{h}시간 {m:02d}분"


def _truncate(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[: limit - 1] + "…"
