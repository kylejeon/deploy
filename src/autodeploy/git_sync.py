"""gateway-infra-next 레포 동기화 (clone or update). dev-spec F2.2.

Bitbucket App Password를 URL에 포함한 채로 `.git/config`에 유지한다.
봇은 비대화형 SSH라 자격증명 프롬프트를 처리할 수 없으므로 토큰 URL 유지가
유일하게 안정적인 방법. 마스킹은 workflow.mask_url_secrets()가 담당해
Slack/DB 로그에는 토큰이 노출되지 않는다. 디스크상 `.git/config`의 평문
토큰은 단일 사용자 `connecteve` 소유 폴더 안에서만 접근 가능 — 병원 단독
서버 운영 환경에서 수용한 트레이드오프.
"""
from __future__ import annotations

import shlex

from autodeploy.ssh import LineCallback, SSHClient


class GitSyncError(RuntimeError):
    pass


async def sync_repo(
    ssh: SSHClient,
    *,
    user: str,
    app_password: str,
    repo_host_path: str,
    branch: str,
    target_dir: str,
    on_line: LineCallback | None = None,
) -> str:
    """타겟 디렉토리 없으면 clone, 있으면 update. HEAD commit SHA 반환."""
    target_q = shlex.quote(target_dir)
    branch_q = shlex.quote(branch)
    url_token = f"https://{user}:{app_password}@{repo_host_path}"
    url_token_q = shlex.quote(url_token)

    if not await _dir_exists(ssh, target_dir, on_line):
        rc = await ssh.exec(f"git clone {url_token_q} {target_q}", on_line=on_line)
        if rc != 0:
            raise GitSyncError(f"git clone failed (exit {rc})")

        rc = await ssh.exec(
            f"cd {target_q} && git fetch --all && git checkout {branch_q}",
            on_line=on_line,
        )
        if rc != 0:
            raise GitSyncError(f"git checkout {branch} failed (exit {rc})")
    else:
        # 기존 폴더가 있어도 토큰 URL을 강제로 재주입 (이전 버전이 토큰을 제거한
        # .git/config일 수 있고, 그러면 fetch가 비대화형 환경에서 자격증명 프롬프트로
        # 죽음).
        cmd = (
            f"cd {target_q} && "
            f"git remote set-url origin {url_token_q} && "
            f"git checkout -- . && "
            f"git fetch --all && "
            f"git checkout {branch_q} && "
            f"git pull"
        )
        rc = await ssh.exec(cmd, on_line=on_line)
        if rc != 0:
            raise GitSyncError(f"git update failed (exit {rc})")

    return await _rev_parse_head(ssh, target_dir)


async def _dir_exists(ssh: SSHClient, path: str, on_line: LineCallback | None) -> bool:
    rc = await ssh.exec(f"test -d {shlex.quote(path)}", on_line=on_line)
    return rc == 0


async def _rev_parse_head(ssh: SSHClient, target_dir: str) -> str:
    sha_lines: list[str] = []

    async def collect(line):
        if line.stream == "stdout" and line.line.strip():
            sha_lines.append(line.line.strip())

    rc = await ssh.exec(
        f"cd {shlex.quote(target_dir)} && git rev-parse HEAD",
        on_line=collect,
    )
    if rc != 0 or not sha_lines:
        raise GitSyncError("git rev-parse HEAD failed")
    return sha_lines[0]
