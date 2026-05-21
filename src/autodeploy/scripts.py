"""설치 스크립트 실행. dev-spec F2.3/F2.4 + F8 config."""
from __future__ import annotations

import shlex

from autodeploy.config import ScriptSpec, render_args
from autodeploy.ssh import LineCallback, SSHClient


async def run_script(
    ssh: SSHClient,
    *,
    workdir: str,
    spec: ScriptSpec,
    code: str,
    on_line: LineCallback | None = None,
) -> int:
    """workdir에서 spec.script를 위치 인자(args 템플릿에 {code} 치환)와 함께 실행.

    spec.sudo=True면 sudo prefix. 모든 인자는 shlex.quote로 셸 주입 방지.
    """
    rendered = render_args(spec.args, code)
    cmd_parts = [f"./{spec.script}", *rendered]
    cmd = " ".join(shlex.quote(p) for p in cmd_parts)
    if spec.sudo:
        cmd = "sudo " + cmd
    full = f"cd {shlex.quote(workdir)} && {cmd}"
    return await ssh.exec(full, on_line=on_line)
