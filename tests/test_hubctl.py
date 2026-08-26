"""hubctl 명령 조립 + 서브프로세스 실행/취소 테스트.

실행 테스트는 진짜 서브프로세스를 띄운다 (모킹하지 않는다). 프로세스 그룹 종료·
become 파일 권한·긴 줄 처리는 모킹하면 검증되는 게 없는 항목들이라서다.
로그인 셸(zsh -lc)은 사용자 rc 에 좌우되므로 테스트에서는 bash -c 를 쓴다.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import textwrap
from pathlib import Path

import pytest

from autodeploy.hubctl import (
    CLEAN_MODES,
    HubctlError,
    HubctlRunner,
    build_command,
    build_preflight_command,
)
from autodeploy.masking import SecretMasker
from autodeploy.models import JobKind

# asyncio_mode = "auto" 라 async 테스트에는 마크가 필요 없다.


# ── 명령 조립 ───────────────────────────────────────────────────────


def test_install_command():
    assert build_command(JobKind.INSTALL, env="stage", hosts=["yonseiwa"]) == (
        "./bin/hubctl install -e stage -l yonseiwa"
    )


def test_install_with_multiple_hosts_uses_one_limit():
    """다중 서버 설치는 작업 여러 개가 아니라 -l a,b,c 하나다 (AC-4)."""
    cmd = build_command(JobKind.INSTALL, env="prod", hosts=["a", "b", "c"])
    assert cmd == "./bin/hubctl install -e prod -l a,b,c"
    assert cmd.count("-l") == 1


def test_install_with_ref_uses_passthrough():
    cmd = build_command(
        JobKind.INSTALL, env="dev", hosts=["a"], ref="v1.0.2", ref_type="tag"
    )
    assert cmd == (
        "./bin/hubctl install -e dev -l a -- -e hub_deploy_ref=v1.0.2 "
        "-e hub_deploy_ref_type=tag"
    )


def test_configure_command():
    assert build_command(JobKind.CONFIGURE, env="dev", hosts=["a"]) == (
        "./bin/hubctl configure -e dev -l a"
    )


def test_verify_command_takes_no_env():
    assert build_command(JobKind.VERIFY, hosts=["a", "b"]) == "./bin/hubctl verify -l a,b"


def test_rollback_command_takes_no_env():
    """hubctl 의 cmd_rollback 이 -e ENV 를 명시적으로 거부한다."""
    assert build_command(JobKind.ROLLBACK, hosts=["a"]) == "./bin/hubctl rollback -l a"


def test_patch_create_has_no_limit():
    """hubctl 이 `patch create` 에 -l 을 거부한다 — 컨트롤러 로컬 실행이다."""
    cmd = build_command(JobKind.PATCH, phase="create", ref="v1.0.2", ref_type="tag")
    assert cmd == (
        "./bin/hubctl patch create -- -e hub_deploy_ref=v1.0.2 -e hub_deploy_ref_type=tag"
    )
    assert " -l " not in cmd


def test_patch_create_rejects_hosts():
    with pytest.raises(HubctlError, match="-l"):
        build_command(JobKind.PATCH, phase="create", ref="v1", hosts=["a"])


def test_patch_create_requires_ref():
    with pytest.raises(HubctlError, match="ref"):
        build_command(JobKind.PATCH, phase="create")


def test_patch_apply_never_passes_ref():
    """번들 메타가 SoT — roles/patch_apply/tasks/main.yml:180 이 불일치 시 죽인다."""
    cmd = build_command(JobKind.PATCH, phase="apply", hosts=["a"], ref="v1.0.2")
    assert cmd == "./bin/hubctl patch apply -l a"
    assert "hub_deploy_ref" not in cmd


def test_patch_requires_explicit_phase():
    """phase 없는 원샷은 [y/N] 프롬프트를 띄워 봇이 답할 수 없다."""
    with pytest.raises(HubctlError, match="phase"):
        build_command(JobKind.PATCH, hosts=["a"], ref="v1")


@pytest.mark.parametrize(
    "mode,level,keep",
    [("reset", "reset", "false"), ("reset-keep", "reset", "true"), ("uninstall", "uninstall", "false")],
)
def test_clean_bypasses_hubctl_and_calls_playbook(mode, level, keep):
    """cmd_clean 이 read -r 로 호스트명을 받고 -y 를 거부하므로 playbook 을 직접 부른다."""
    cmd = build_command(JobKind.CLEAN, hosts=["yonseiwa"], clean_mode=mode)
    assert cmd == (
        "ansible-playbook clean.yml -i inventory/sites.yml -l yonseiwa "
        f"-e confirm=yonseiwa -e level={level} -e keep_data={keep}"
    )


def test_clean_keep_data_mapping_covers_every_mode():
    assert set(CLEAN_MODES) == {"reset", "reset-keep", "uninstall"}


def test_clean_refuses_multiple_hosts():
    """confirm= 이 호스트명 하나와 대조되는 사고 방지 장치라 우회하지 않는다."""
    with pytest.raises(HubctlError, match="한 대"):
        build_command(JobKind.CLEAN, hosts=["a", "b"], clean_mode="reset")


def test_clean_requires_mode():
    with pytest.raises(HubctlError, match="clean_mode"):
        build_command(JobKind.CLEAN, hosts=["a"])


def test_ssh_key_is_not_a_hubctl_job():
    with pytest.raises(HubctlError):
        build_command(JobKind.SSH_KEY, hosts=["a"])


def test_preflight_command():
    assert build_preflight_command() == "./bin/hubctl preflight"


# ── 입력 검증 ───────────────────────────────────────────────────────


@pytest.mark.parametrize("env", ["", None, "production", "DEV", "dev; rm -rf /"])
def test_invalid_env_rejected(env):
    with pytest.raises(HubctlError, match="env"):
        build_command(JobKind.INSTALL, env=env, hosts=["a"])


def test_empty_hosts_rejected():
    with pytest.raises(HubctlError, match="호스트"):
        build_command(JobKind.VERIFY, hosts=[])


def test_duplicate_hosts_rejected():
    with pytest.raises(HubctlError, match="중복"):
        build_command(JobKind.VERIFY, hosts=["a", "a"])


@pytest.mark.parametrize(
    "host",
    ["a b", "a,b", "a;rm -rf /", "$(whoami)", "`id`", "-l", "a|b", "'a'", "../etc"],
)
def test_hostnames_that_could_change_the_target_are_rejected(host):
    """-l 과 -e confirm= 에 그대로 들어가므로 구분자·치환이 섞이면 대상이 바뀐다."""
    with pytest.raises(HubctlError):
        build_command(JobKind.VERIFY, hosts=[host])


def test_ref_is_shell_quoted_not_interpolated():
    cmd = build_command(JobKind.INSTALL, env="dev", hosts=["a"], ref="v1; rm -rf /")
    # shlex.join 이 통째로 한 인자로 묶어야 한다.
    assert "'-e' " not in cmd
    assert "-e 'hub_deploy_ref=v1; rm -rf /'" in cmd


def test_ref_type_without_ref_rejected():
    with pytest.raises(HubctlError, match="ref_type"):
        build_command(JobKind.INSTALL, env="dev", hosts=["a"], ref_type="tag")


def test_invalid_ref_type_rejected():
    with pytest.raises(HubctlError, match="ref_type"):
        build_command(JobKind.INSTALL, env="dev", hosts=["a"], ref="v1", ref_type="sha")


# ── 실행 ────────────────────────────────────────────────────────────


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    (tmp_path / "bin").mkdir()
    return tmp_path


def runner(repo: Path, **kw) -> HubctlRunner:
    kw.setdefault("login_shell", ("bash", "-c"))
    return HubctlRunner(repo, **kw)


async def collect(r: HubctlRunner, command: str):
    lines: list[tuple[str, object]] = []
    result = await r.run(command, on_line=lambda stream, parsed: lines.append((stream, parsed)))
    return result, lines


async def test_runs_and_reports_exit_code(fake_repo):
    result, _ = await collect(runner(fake_repo), "exit 7")
    assert result.exit_code == 7
    assert result.succeeded is False


async def test_parses_ansible_output_end_to_end(fake_repo):
    script = textwrap.dedent(
        """
        echo 'PLAY [hubctl verify] ****'
        echo 'TASK [verify : node ready] ****'
        echo 'ok: [alpha]'
        echo 'fatal: [beta]: FAILED! =>'
        echo '    rc: 1'
        echo 'PLAY RECAP ****'
        echo 'alpha : ok=1 changed=0 unreachable=0 failed=0 skipped=0 rescued=0 ignored=0'
        echo 'beta  : ok=0 changed=0 unreachable=0 failed=1 skipped=0 rescued=0 ignored=0'
        """
    )
    result, lines = await collect(runner(fake_repo), script)
    assert result.exit_code == 0
    assert result.steps == ("verify",)
    assert result.recaps["alpha"].succeeded is True
    assert result.recaps["beta"].succeeded is False
    assert result.line_count == len(lines)
    hosts = [p.host for _, p in lines]
    assert "alpha" in hosts and "beta" in hosts


async def test_stdout_and_stderr_are_labelled(fake_repo):
    _, lines = await collect(fake_repo and runner(fake_repo), "echo out; echo err >&2")
    by_stream = {stream: p.text for stream, p in lines if p.text}
    assert by_stream == {"stdout": "out", "stderr": "err"}


async def test_secrets_are_masked_before_reaching_the_handler(fake_repo):
    r = runner(fake_repo, masker=SecretMasker(["hvs.SUPERSECRETTOKEN"]))
    _, lines = await collect(r, "echo 'VAULT_TOKEN=hvs.SUPERSECRETTOKEN in output'")
    text = "\n".join(p.text for _, p in lines)
    assert "hvs.SUPERSECRETTOKEN" not in text
    assert "***" in text


async def test_become_password_is_masked_without_being_registered(fake_repo):
    """호출자가 마스킹 등록을 잊어도 become 비밀번호는 로그로 새면 안 된다 (AC-13)."""
    r = runner(fake_repo, become_password="hunter2-long-enough")
    _, lines = await collect(r, "echo 'sudo password is hunter2-long-enough'")
    text = "\n".join(p.text for _, p in lines)
    assert "hunter2-long-enough" not in text


async def test_long_line_is_truncated_not_fatal(fake_repo):
    """ansible 은 실패 태스크의 인자를 통째로 되뱉는다. 한 줄이 커도 스트림이 살아야 한다."""
    r = runner(fake_repo)
    script = "python3 -c \"print('x'*200000)\"; echo AFTER"
    result, lines = await collect(r, script)
    assert result.exit_code == 0
    texts = [p.text for _, p in lines]
    assert any(t.endswith("…[잘림]") for t in texts), texts[:3]
    assert "AFTER" in texts, "긴 줄 뒤의 출력이 유실되면 안 된다"


async def test_line_without_trailing_newline_is_delivered(fake_repo):
    _, lines = await collect(runner(fake_repo), "printf 'no-newline'")
    assert any(p.text == "no-newline" for _, p in lines)


# ── become 비밀번호 파일 ────────────────────────────────────────────


async def test_become_file_is_readable_not_executable_and_removed(fake_repo, tmp_path):
    """ansible 은 파일에 실행권한이 있으면 스크립트로 실행한다 (cli/__init__.py).

    0600 은 기밀성뿐 아니라 '비밀번호로 읽히게 하는' 정확성 요건이다.
    """
    side = tmp_path / "observed.txt"
    r = runner(fake_repo, become_password="hunter2-long-enough")
    script = textwrap.dedent(
        f"""
        F="$ANSIBLE_BECOME_PASSWORD_FILE"
        {{
          echo "path=$F"
          if [ -x "$F" ]; then echo "exec=yes"; else echo "exec=no"; fi
          echo "content=$(cat "$F")"
        }} > {side}
        """
    )
    result, _ = await collect(r, script)
    assert result.exit_code == 0

    observed = dict(
        line.split("=", 1) for line in side.read_text().strip().splitlines()
    )
    assert observed["exec"] == "no"
    # ansible 이 .strip() 하므로 개행은 무해하다. 값 자체가 온전해야 한다.
    assert observed["content"] == "hunter2-long-enough"
    assert not Path(observed["path"]).exists(), "작업 종료 후 즉시 삭제돼야 한다"


async def test_no_become_file_when_no_password(fake_repo, tmp_path):
    """빈 비밀번호로 파일을 만들면 ansible 이 'Empty password' 로 죽는다."""
    side = tmp_path / "observed.txt"
    r = runner(fake_repo)
    _, _ = await collect(r, f'echo "v=${{ANSIBLE_BECOME_PASSWORD_FILE:-unset}}" > {side}')
    assert side.read_text().strip() == "v=unset"


async def test_color_is_disabled_for_the_child(fake_repo, tmp_path):
    side = tmp_path / "env.txt"
    r = runner(fake_repo, env_overrides={"ANSIBLE_FORCE_COLOR": "1"})
    await collect(
        r, f'echo "no_color=$NO_COLOR force=${{ANSIBLE_FORCE_COLOR:-unset}}" > {side}'
    )
    assert side.read_text().strip() == "no_color=1 force=unset"


async def test_env_overrides_reach_the_child(fake_repo, tmp_path):
    side = tmp_path / "env.txt"
    r = runner(fake_repo, env_overrides={"VAULT_ADDR": "https://vault.example"})
    await collect(r, f'echo "$VAULT_ADDR" > {side}')
    assert side.read_text().strip() == "https://vault.example"


# ── 취소 ────────────────────────────────────────────────────────────


async def test_cancel_kills_the_whole_process_group(fake_repo, tmp_path):
    """zsh 만 죽이면 ansible 이 고아로 남아 감독 없이 서버를 계속 건드린다 (AC-8)."""
    pidfile = tmp_path / "child.pid"
    script = f"sleep 60 & echo $! > {pidfile}; wait"
    r = runner(fake_repo, kill_grace=0.5)

    task = asyncio.create_task(collect(r, script))
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(0.05)
    child_pid = int(pidfile.read_text().strip())
    assert _alive(child_pid), "손자 프로세스가 떠 있어야 의미 있는 테스트다"

    assert await r.cancel() is True
    result, _ = await asyncio.wait_for(task, timeout=15)

    assert result.cancelled is True
    assert result.exit_code != 0
    for _ in range(100):
        if not _alive(child_pid):
            break
        await asyncio.sleep(0.05)
    assert not _alive(child_pid), "그룹의 손자까지 정리돼야 한다"


async def test_sigkill_escalation_for_a_process_ignoring_sigterm(fake_repo, tmp_path):
    ready = tmp_path / "ready"
    script = f"trap '' TERM; touch {ready}; sleep 60"
    r = runner(fake_repo, kill_grace=0.5)

    task = asyncio.create_task(collect(r, script))
    for _ in range(100):
        if ready.exists():
            break
        await asyncio.sleep(0.05)
    assert ready.exists()

    await r.cancel()
    result, _ = await asyncio.wait_for(task, timeout=15)
    assert result.cancelled is True


async def test_cancel_before_start_never_spawns(fake_repo, tmp_path):
    """큐에서 꺼내고 프로세스가 뜨기 직전에 눌린 취소가 유실되면 안 된다."""
    marker = tmp_path / "ran"
    r = runner(fake_repo)
    assert await r.cancel() is False  # 아직 프로세스가 없다
    result, _ = await collect(r, f"touch {marker}")
    assert result.cancelled is True
    assert result.exit_code == -1
    assert not marker.exists(), "취소 후에는 명령이 실행되면 안 된다"


async def test_cancel_after_finish_is_a_noop(fake_repo):
    r = runner(fake_repo)
    result, _ = await collect(r, "echo done")
    assert result.exit_code == 0
    assert await r.cancel() is False


async def test_runner_is_single_use(fake_repo):
    r = runner(fake_repo)
    await collect(r, "true")
    with pytest.raises(HubctlError, match="재사용"):
        await r.run("true")


async def test_missing_repo_path_is_reported(tmp_path):
    r = runner(tmp_path / "nope")
    with pytest.raises(HubctlError, match="저장소 경로"):
        await r.run("true")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ── 환경 전달 (.env → os.environ → hubctl 자식) ──────────────────────


async def test_parent_environment_reaches_the_child(fake_repo, monkeypatch):
    """`.env` 에 넣은 자격이 hubctl 까지 도달해야 한다.

    데몬은 기동할 때 load_dotenv() 로 .env 를 os.environ 에 올린다. 그 뒤
    _child_env 가 dict(os.environ) 에서 출발하므로 자식이 그대로 물려받는다.
    운영 가이드가 이 경로를 권장으로 적고 있으니 실제 셸을 태워 고정해 둔다.
    """
    monkeypatch.setenv("VAULT_ADDR", "https://vault.example.com")
    result, lines = await collect(runner(fake_repo), 'echo "seen=$VAULT_ADDR"')
    assert result.exit_code == 0
    assert any("seen=https://vault.example.com" in p.text for _, p in lines)


async def test_a_login_shell_does_not_read_zshrc(tmp_path):
    """`zsh -lc` 는 .zshrc 를 읽지 않는다 — 그래서 .env 를 권장한다.

    "터미널에서 되는데 콘솔에서만 안 되는" 사고의 원인이 이것이었다.
    zsh 는 .zshrc 를 대화형 셸에서만 읽는다. 문서가 이 사실에 기대고 있으므로
    zsh 가 있는 환경에서는 실제로 확인한다.
    """
    zsh = shutil.which("zsh")
    if zsh is None:
        pytest.skip("zsh 없음")
    zdotdir = tmp_path / "zdot"
    zdotdir.mkdir()
    (zdotdir / ".zshenv").write_text("export MARK_ZSHENV=1\n", encoding="utf-8")
    (zdotdir / ".zprofile").write_text("export MARK_ZPROFILE=1\n", encoding="utf-8")
    (zdotdir / ".zshrc").write_text("export MARK_ZSHRC=1\n", encoding="utf-8")

    proc = await asyncio.create_subprocess_exec(
        zsh, "-lc", 'echo "${MARK_ZSHENV:-0}${MARK_ZPROFILE:-0}${MARK_ZSHRC:-0}"',
        env={"ZDOTDIR": str(zdotdir), "PATH": os.environ.get("PATH", ""),
             "HOME": str(tmp_path)},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    # zshenv=1, zprofile=1, zshrc=0
    assert out.decode().strip() == "110"
