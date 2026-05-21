from __future__ import annotations

import pytest

from autodeploy import messages
from autodeploy.models import Job, JobStatus, Step


def _job(**kw) -> Job:
    base = dict(
        id=42,
        target_ip="192.168.1.50",
        deployment_type="hybrid-with-ai",
        hospital_code="HOSP01",
        hospital_name="서울대병원",
        started_by="U01",
        slack_channel="C01",
    )
    base.update(kw)
    return Job(**base)


def _has_text(blocks, needle):
    return any(needle in str(b) for b in blocks)


# ---------- parent ----------

def test_parent_running_has_blue_icon():
    out = messages.parent_message(_job(status=JobStatus.RUNNING))
    assert "🔵" in str(out["blocks"])
    assert "#42" in out["text"]
    assert _has_text(out["blocks"], "192.168.1.50")
    assert _has_text(out["blocks"], "hybrid-with-ai")
    assert _has_text(out["blocks"], "HOSP01")
    assert _has_text(out["blocks"], "서울대병원")
    assert _has_text(out["blocks"], "<@U01>")


def test_parent_succeeded_updates_to_check():
    out = messages.parent_message(_job(status=JobStatus.SUCCEEDED), total_duration_s=252)
    assert "✅" in str(out["blocks"])
    assert "완료" in out["text"]
    assert "4분 12초" in str(out["blocks"])


def test_parent_failed_shows_step_in_header():
    out = messages.parent_message(_job(status=JobStatus.FAILED, current_step=Step.APP_INSTALL))
    assert "❌" in str(out["blocks"])
    assert "app_install" in str(out["blocks"])


def test_parent_cancelled():
    out = messages.parent_message(_job(status=JobStatus.CANCELLED))
    assert "⚠️" in str(out["blocks"])
    assert "취소" in out["text"]


# ---------- ack / step ----------

def test_ack_includes_cancel_command():
    out = messages.ack_message(42)
    assert "#42" in out["text"]
    assert "cancel 42" in out["text"]


def test_step_started_has_step_number_and_icon():
    out = messages.step_started(Step.INFRA_INSTALL)
    text = out["text"]
    assert "단계 3/5" in text
    assert "⚙️" in text
    assert "인프라 설치" in text


def test_step_finished_success_vs_fail():
    ok = messages.step_finished(Step.GIT_PULL, success=True, duration_s=14)
    bad = messages.step_finished(Step.GIT_PULL, success=False, duration_s=14)
    assert "완료" in ok["text"]
    assert "실패" in bad["text"]


# ---------- stdout preview ----------

def test_stdout_preview_truncates_to_last_10_lines():
    lines = [f"line{i}" for i in range(25)]
    out = messages.stdout_preview(Step.INFRA_INSTALL, lines, last_update_kst="16:24:55")
    body = str(out["blocks"])
    assert "line24" in body
    assert "line15" in body
    assert "line14" not in body  # 15-24만 (마지막 10)
    assert "16:24:55" in body


def test_stdout_preview_handles_long_lines():
    out = messages.stdout_preview(Step.APP_INSTALL, ["x" * 200], last_update_kst="00:00:00")
    body = str(out["blocks"])
    # 80자 + ellipsis로 잘려야 함
    assert "x" * 200 not in body


# ---------- success ----------

def test_success_summary_includes_admin_url():
    job = _job(status=JobStatus.SUCCEEDED, admin_web_url="http://192.168.1.50/", script_commit_sha="abc123")
    out = messages.success_summary(job, total_duration_s=252)
    assert _has_text(out["blocks"], "http://192.168.1.50/")
    assert _has_text(out["blocks"], "abc123")
    assert out["attachment_color"] == "good"
    assert "4분 12초" in str(out["blocks"])


def test_success_summary_lists_extra_urls():
    job = _job(
        status=JobStatus.SUCCEEDED,
        admin_web_url="http://192.168.1.50:31489",
        extra_urls={
            "Frontend": "http://192.168.1.50:31489",
            "Temporal Web": "http://192.168.1.50:31917",
            "Web-PACS": "http://192.168.1.50:30080/",
        },
    )
    out = messages.success_summary(job, total_duration_s=100)
    body = str(out["blocks"])
    # admin URL은 한 번만, 나머지는 "기타 URL" 섹션에
    assert "31489" in body  # Frontend (= admin)
    assert "Temporal Web" in body
    assert "31917" in body
    assert "Web-PACS" in body
    assert "30080" in body


# ---------- failure ----------

def test_failure_summary_includes_stderr_tail():
    out = messages.failure_summary(
        _job(status=JobStatus.FAILED, current_step=Step.APP_INSTALL, error_message="container died"),
        step=Step.APP_INSTALL,
        stderr_tail=[f"err line {i}" for i in range(40)],
        duration_s=120,
    )
    body = str(out["blocks"])
    assert "어플리케이션 설치" in body
    assert "err line 39" in body  # 마지막 줄
    assert "err line 19" not in body  # 마지막 20줄만
    assert out["attachment_color"] == "danger"
    assert "container died" in body


# ---------- cancel ----------

def test_cancel_summary_warning_color():
    out = messages.cancel_summary(
        _job(status=JobStatus.CANCELLED, current_step=Step.INFRA_INSTALL),
        step_at_cancel=Step.INFRA_INSTALL,
        duration_s=78,
    )
    assert out["attachment_color"] == "warning"
    assert "취소" in out["text"]


# ---------- status ----------

def test_status_response_no_job():
    out = messages.status_response(None)
    assert "진행 중인 작업이 없습니다" in out["text"]


def test_status_response_running_job():
    job = _job(status=JobStatus.RUNNING, current_step=Step.APP_INSTALL)
    out = messages.status_response(job)
    assert "#42" in out["text"]
    assert _has_text(out["blocks"], "어플리케이션 설치")


# ---------- list ----------

def test_list_response_empty():
    out = messages.list_response([], limit=10)
    assert "이력이 없습니다" in out["text"]


def test_list_response_with_jobs_shows_table():
    jobs = [
        _job(id=42, status=JobStatus.SUCCEEDED, target_ip="1.1.1.1"),
        _job(id=43, status=JobStatus.RUNNING, target_ip="2.2.2.2", deployment_type="on-premise"),
    ]
    out = messages.list_response(jobs, limit=10)
    body = str(out["blocks"])
    assert "#42" in body
    assert "#43" in body
    assert "1.1.1.1" in body
    assert "on-premise" in body


# ---------- help ----------

def test_help_lists_valid_types_dynamically():
    out = messages.help_response(["on-premise", "hybrid-with-ai", "hybrid-without-ai"])
    body = str(out["blocks"])
    assert "install" in body
    assert "status" in body
    assert "list" in body
    assert "cancel" in body
    assert "on-premise" in body
    assert "hybrid-with-ai" in body


# ---------- permission denied / validation ----------

def test_permission_denied_uses_no_entry_icon():
    out = messages.permission_denied()
    assert "🚫" in str(out["blocks"])


def test_validation_error_with_suggestion():
    out = messages.validation_error("missing --type", suggestion="valid types: a, b")
    body = str(out["blocks"])
    assert "missing --type" in body
    assert "valid types: a, b" in body
