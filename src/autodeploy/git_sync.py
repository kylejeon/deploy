"""gateway-infra-next 레포 동기화 (clone or update). dev-spec F2.2."""
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
    """타겟 디렉토리 없으면 clone, 있으면 update. HEAD commit SHA 반환.

    clone 시 토큰이 포함된 URL로 clone 후 즉시 git remote set-url 로 origin URL을 토큰
    없는 형태로 재설정. .git/config 평문 토큰 노출을 막는다 (D14).
    """
    target_q = shlex.quote(target_dir)
    branch_q = shlex.quote(branch)

    if not await _dir_exists(ssh, target_dir, on_line):
        url_token = f"https://{user}:{app_password}@{repo_host_path}"
        url_clean = f"https://{repo_host_path}"

        rc = await ssh.exec(f"git clone {shlex.quote(url_token)} {target_q}", on_line=on_line)
        if rc != 0:
            raise GitSyncError(f"git clone failed (exit {rc})")

        rc = await ssh.exec(
            f"cd {target_q} && git remote set-url origin {shlex.quote(url_clean)}",
            on_line=on_line,
        )
        if rc != 0:
            raise GitSyncError(f"git remote set-url failed (exit {rc})")

        rc = await ssh.exec(
            f"cd {target_q} && git fetch --all && git checkout {branch_q}",
            on_line=on_line,
        )
        if rc != 0:
            raise GitSyncError(f"git checkout {branch} failed (exit {rc})")
    else:
        cmd = (
            f"cd {target_q} && "
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
