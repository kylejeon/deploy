"""Slack 명령 파서. 봇 멘션은 호출자가 제거했다고 가정."""
from __future__ import annotations

import ipaddress
import shlex
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class InstallCommand:
    target_ip: str
    deployment_type: str
    hospital_code: str
    hospital_name: str | None
    hospital_address: str | None


@dataclass(frozen=True, slots=True)
class StatusCommand:
    job_id: int | None  # None = 최신


@dataclass(frozen=True, slots=True)
class ListCommand:
    limit: int


@dataclass(frozen=True, slots=True)
class CancelCommand:
    job_id: int


@dataclass(frozen=True, slots=True)
class RetryCommand:
    job_id: int | None  # None = derive from thread context (스레드 안에서 실행)


@dataclass(frozen=True, slots=True)
class RegisterCommand:
    """site_register 단계만 단독 실행 — 기존 작업의 hospital 데이터로 API 호출."""
    job_id: int


@dataclass(frozen=True, slots=True)
class HelpCommand:
    pass


@dataclass(frozen=True, slots=True)
class ParseError:
    message: str
    suggestion: str | None = None


Command = (
    InstallCommand | StatusCommand | ListCommand | CancelCommand
    | RetryCommand | RegisterCommand | HelpCommand | ParseError
)


def parse_command(text: str, valid_types: frozenset[str] | set[str]) -> Command:
    """슬랙 본문 → Command. 봇 멘션 토큰은 사전 제거 전제."""
    text = text.strip()
    if not text:
        return HelpCommand()

    parts = text.split(None, 1)
    verb = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    if verb == "install":
        return _parse_install(rest, valid_types)
    if verb == "status":
        return _parse_status(rest)
    if verb == "list":
        return _parse_list(rest)
    if verb == "cancel":
        return _parse_cancel(rest)
    if verb == "retry":
        return _parse_retry(rest)
    if verb == "register":
        return _parse_register(rest)
    if verb in {"help", "?"}:
        return HelpCommand()
    return ParseError(f"unknown command: {verb}", suggestion="@autodeploy help")


def _parse_install(rest: str, valid_types: frozenset[str] | set[str]) -> Command:
    try:
        tokens = shlex.split(rest)
    except ValueError as exc:
        return ParseError(f"argument parse failed: {exc}")

    if not tokens:
        return ParseError(
            "install requires <IP> --type=<TYPE> --code=<병원코드>",
            suggestion="@autodeploy help",
        )

    target_ip = tokens[0]
    if not _is_valid_ip(target_ip):
        return ParseError(f"invalid IP: {target_ip}")

    flags: dict[str, str] = {}
    for tok in tokens[1:]:
        if not tok.startswith("--"):
            return ParseError(f"unexpected argument: {tok}")
        body = tok[2:]
        if "=" not in body:
            return ParseError(f"flag missing value (use --key=value): {tok}")
        key, _, value = body.partition("=")
        if not key:
            return ParseError(f"empty flag name: {tok}")
        flags[key] = value

    valid_list = ", ".join(sorted(valid_types))

    dtype = flags.pop("type", None)
    if dtype is None:
        return ParseError("missing --type", suggestion=f"valid types: {valid_list}")
    if dtype not in valid_types:
        return ParseError(f"unknown type: {dtype}", suggestion=f"valid types: {valid_list}")

    code = flags.pop("code", None)
    if code is None or not code.strip():
        return ParseError("missing --code (병원코드)")

    name = flags.pop("name", None)
    address = flags.pop("address", None)

    if flags:
        return ParseError(f"unknown flag(s): {', '.join(sorted(flags))}")

    return InstallCommand(
        target_ip=target_ip,
        deployment_type=dtype,
        hospital_code=code,
        hospital_name=name,
        hospital_address=address,
    )


def _parse_status(rest: str) -> Command:
    rest = rest.strip()
    if not rest:
        return StatusCommand(job_id=None)
    try:
        return StatusCommand(job_id=int(rest))
    except ValueError:
        return ParseError(f"status expects integer job-id, got: {rest}")


def _parse_list(rest: str) -> Command:
    rest = rest.strip()
    if not rest:
        return ListCommand(limit=10)
    try:
        n = int(rest)
    except ValueError:
        return ParseError(f"list expects integer N, got: {rest}")
    if n <= 0:
        return ParseError("list N must be positive")
    if n > 50:
        n = 50
    return ListCommand(limit=n)


def _parse_cancel(rest: str) -> Command:
    rest = rest.strip()
    if not rest:
        return ParseError("cancel requires <job-id>")
    try:
        return CancelCommand(job_id=int(rest))
    except ValueError:
        return ParseError(f"cancel expects integer job-id, got: {rest}")


def _parse_retry(rest: str) -> Command:
    rest = rest.strip()
    if not rest:
        # 스레드 컨텍스트에서 원본 작업을 찾는다 — 호출자(slack_app)가 처리
        return RetryCommand(job_id=None)
    try:
        return RetryCommand(job_id=int(rest))
    except ValueError:
        return ParseError(f"retry expects integer job-id, got: {rest}")


def _parse_register(rest: str) -> Command:
    rest = rest.strip()
    if not rest:
        return ParseError("register requires <job-id>")
    try:
        return RegisterCommand(job_id=int(rest))
    except ValueError:
        return ParseError(f"register expects integer job-id, got: {rest}")


def _is_valid_ip(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False
