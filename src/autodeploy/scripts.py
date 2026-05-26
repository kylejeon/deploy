"""설치 스크립트 실행. dev-spec F2.3/F2.4 + F8 config."""
from __future__ import annotations

import shlex

from autodeploy.config import ScriptSpec, render_args
from autodeploy.ssh import LineCallback, SSHClient


def _sudo_prefix(password: str) -> str:
    """sudo 호출 prefix를 만든다.

    password가 비어있으면 NOPASSWD 가정 plain `sudo`.
    값이 있으면 `printf` 빌트인으로 stdin에 흘리는 `sudo -S` 형태.
    printf는 bash 내장이라 별도 프로세스가 안 생겨 ps에 비밀번호가 노출되지 않는다.
    """
    if not password:
        return "sudo"
    pw_q = shlex.quote(password)
    return f"printf '%s\\n' {pw_q} | sudo -S -p ''"


async def run_script(
    ssh: SSHClient,
    *,
    workdir: str,
    spec: ScriptSpec,
    code: str,
    sudo_password: str = "",
    on_line: LineCallback | None = None,
) -> int:
    """workdir에서 spec.script를 위치 인자(args 템플릿에 {code} 치환)와 함께 실행.

    spec.sudo=True면 sudo prefix. sudo_password가 주어지면 stdin으로 자동 주입(`sudo -S`).
    모든 인자는 shlex.quote로 셸 주입 방지.
    """
    rendered = render_args(spec.args, code)
    cmd_parts = [f"./{spec.script}", *rendered]
    cmd = " ".join(shlex.quote(p) for p in cmd_parts)
    if spec.sudo:
        cmd = f"{_sudo_prefix(sudo_password)} {cmd}"
    full = f"cd {shlex.quote(workdir)} && {cmd}"
    return await ssh.exec(full, on_line=on_line)
