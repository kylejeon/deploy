"""설치 킷 러너 (kit_runner.py) — 가짜 타겟과 가짜 rsync 로 끝까지 돌린다.

가짜 타겟은 명령을 문자열로 받아 실제 타겟이 낼 출력을 흉내낸다. 접속(ssh_factory)
마다 새 세션이지만 상태(설치 버전, 받은 파일, 비밀번호 파일)는 한 기계로 공유된다.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from autodeploy.ansible_log import LineKind
from autodeploy.kit_runner import (
    KitRequest,
    KitRunner,
    KitTarget,
    ProgressThrottle,
    job_paths,
)
from autodeploy.kits import KitCache
from autodeploy.ssh import StreamLine

from tests.test_kits import make_run

SUDO_PW = "sudo-secret-1"
KIT_PW = "kit-secret-2"
KIT_LOG_OK = [
    "::step:: 0/8 설치 파일 풀기",
    "::step:: 1/8 PC 확인",
    "  ! 메모리 16GB: 32GB 권장",
    "::step:: 3/8 이미지 적재 (수 분 걸립니다)",
    "  준비됨: 웹·API",
    "::step:: 8/8 점검",
    "설치 완료",
    "::result:: ok 0 1.2.0.2 설치 완료",
]


class FakeTarget:
    """병원 PC 한 대."""

    def __init__(self, *, installed: str | None = None, addon: str | None = None,
                 tmp_free_gb: int = 200, log: list[str] | None = None, rc: int = 0) -> None:
        self.installed = installed
        self.addon = addon
        self.tmp_free_gb = tmp_free_gb
        self.log = list(log if log is not None else KIT_LOG_OK)
        self.rc = rc
        self.files: dict[str, int] = {}      # 원격 파일 → 크기
        self.pw_files: dict[str, str] = {}
        self.commands: list[str] = []
        self.stdins: list[str | None] = []
        self.launched: str | None = None
        self.drop_follow_after: int | None = None   # 첫 따라 읽기를 n줄 뒤 끊는다
        self.follow_from: list[int] = []
        self.connects = 0
        self.after_install_version: str | None = None

    def session(self):
        target = self

        class Session:
            async def __aenter__(self):
                target.connects += 1
                return self

            async def __aexit__(self, *exc):
                return None

            async def exec(self, command, on_line=None, *, stdin=None):
                return await target.exec(command, on_line, stdin)

        return Session()

    async def exec(self, command: str, on_line, stdin):
        self.commands.append(command)
        self.stdins.append(stdin)
        out = (lambda s: on_line(StreamLine("stdout", s))) if on_line else (lambda s: None)
        if command.startswith("sudo -S -p '' systemd-run"):
            assert stdin == SUDO_PW + "\n"
            self.launched = command
            return 0
        if "tail -n +" in command:
            start = int(command.split("tail -n +")[1].split()[0])
            self.follow_from.append(start)
            lines = self.log[start - 1:]
            if self.drop_follow_after is not None:
                n, self.drop_follow_after = self.drop_follow_after, None
                for s in lines[:n]:
                    out(s)
                raise ConnectionResetError("simulated drop")
            for s in lines:
                out(s)
            if self.rc in (0, 9) and "uninstall.sh" not in (self.launched or ""):
                self.installed = self.after_install_version
                self.addon = self.addon or "suite"
            if "uninstall.sh" in (self.launched or "") and self.rc == 0:
                self.installed, self.addon = None, None
            out(f"::exit:: {self.rc}")
            return 0
        if command.startswith(". /etc/os-release"):
            out("OS=ubuntu 24.04")
            out("ARCH=x86_64")
            out("MEM_GB=31")
            out(f"TMP_FREE_KB={self.tmp_free_gb * 1024 * 1024}")
            if self.installed:
                out("INSTALL_DIR=/connevo-gateway")
                out(f"INSTALLED={self.installed}")
            out("RSYNC=1")
            out("SYSTEMD_RUN=1")
            return 0
        if command.startswith("sudo -S -p '' sh -c"):
            if stdin != SUDO_PW + "\n":
                return 1
            if self.addon and self.installed:
                out(self.addon)
            return 0
        if command.startswith("stat -c %s"):
            path = command.split()[3]
            out(str(self.files.get(path, 0)))
            return 0
        if command.startswith("umask 077; cat > "):
            self.pw_files[command.split("cat > ")[1]] = stdin
            return 0
        if command.startswith("bash ") and command.endswith("--check"):
            out("  100%")
            out("OK")
            return 0
        if command.startswith("rm -f"):
            for path in command.split()[2:]:
                self.files.pop(path, None)
                self.pw_files.pop(path, None)
            return 0
        if "HEALTH=" in command:
            out(f"VERSION={self.installed or ''}")
            out("HEALTH=200")
            return 0
        raise AssertionError(f"unexpected command: {command!r}")


class FakeRsync:
    """빌드 PC → 캐시는 준비한 파일을 복사, 캐시 → 타겟은 타겟의 파일 목록에 적는다."""

    def __init__(self, source_file: Path, target: FakeTarget, *, fail_first_send: int = 0) -> None:
        self.source_file = source_file
        self.target = target
        self.calls: list[list[str]] = []
        self.fail_first_send = fail_first_send
        self.on_call = None

    async def __call__(self, argv, on_text):
        self.calls.append(list(argv))
        if self.on_call:
            self.on_call(argv)
        src, dst = argv[-2], argv[-1]
        on_text("     1000  50%  1.00MB/s    0:00:10")
        on_text("     2000 100%  1.00MB/s    0:00:20")
        if src.startswith("connecteve@buildpc:"):
            shutil.copy(self.source_file, dst)
            return 0
        if self.fail_first_send:
            self.fail_first_send -= 1
            return 12
        self.target.files[dst.split(":", 1)[1]] = Path(src).stat().st_size
        return 0


@pytest.fixture
def kit_files(tmp_path):
    src = tmp_path / "buildpc"
    src.mkdir()
    full = make_run(src / "connevo-gateway-1.2.0.2.run", "1.2.0.2", "full", b"F" * 4000)
    patch = make_run(src / "connevo-gateway-patch-1.2.0.2.run", "1.2.0.2", "patch", b"P" * 1000,
                     bases="1.2.0.0 1.2.0.1")
    return src, full, patch


def make_runner(tmp_path, kit_files, target, *, action="install", addon="orthanc", rsync=None):
    src, full, patch = kit_files
    lines: list[tuple[str, str, LineKind]] = []

    async def list_kits():
        return [patch, full]

    chosen = full if not target.installed or target.installed not in ("1.2.0.0", "1.2.0.1") else patch
    target.after_install_version = "1.2.0.2"
    runner = KitRunner(
        target=KitTarget(host="alpha", address="192.0.2.10", user="connecteve"),
        request=KitRequest(job_id=7, action=action, version="1.2.0.2", addon=addon),
        list_kits=list_kits,
        cache=KitCache(tmp_path / "cache"),
        ssh_factory=target.session,
        source="connecteve@buildpc:newhub/src/dist",
        key_path="~/.ssh/id_ed25519",
        sudo_password=SUDO_PW,
        kit_password=KIT_PW,
        emit=lambda step, text, kind: lines.append((step, text, kind)),
        rsync=rsync or FakeRsync(src / chosen.name, target),
        sleep=lambda s: asyncio.sleep(0),
    )
    return runner, lines


def no_secret_on_a_command_line(target: FakeTarget) -> None:
    for c in target.commands:
        assert SUDO_PW not in c and KIT_PW not in c


async def test_fresh_install_goes_through_every_step(tmp_path, kit_files):
    target = FakeTarget()
    runner, lines = make_runner(tmp_path, kit_files, target)
    result = await runner.run()

    assert result.status == "succeeded", result.error
    assert result.mode == "install" and result.kit_name == "connevo-gateway-1.2.0.2.run"
    assert result.installed_before is None and result.installed_after == "1.2.0.2"
    # 새 설치는 고른 패키지로 --install
    assert "ADDON=orthanc" in target.launched and "--install" in target.launched
    assert "KIT_PASSWORD_FILE=/var/tmp/connevo-kits/job-7.pw" in target.launched
    # 단계가 킷의 ::step:: 를 따라간다
    steps = [s for s, _, _ in lines]
    for key in ("kit_check", "kit_fetch", "kit_send", "kit_0", "kit_1", "kit_3", "kit_8", "kit_verify"):
        assert key in steps
    assert steps.index("kit_send") < steps.index("kit_0") < steps.index("kit_8") < steps.index("kit_verify")
    assert ("kit_1", "  ! 메모리 16GB: 32GB 권장", LineKind.WARN) in lines
    # 비밀번호는 표준입력으로만, 끝나면 파일도 지운다. 타겟의 .run 도 지운다.
    no_secret_on_a_command_line(target)
    assert target.stdins[target.commands.index("umask 077; cat > /var/tmp/connevo-kits/job-7.pw")] == KIT_PW + "\n"
    assert target.pw_files == {}
    assert "/var/tmp/connevo-kits/connevo-gateway-1.2.0.2.run" not in target.files
    # 캐시에 확인된 채로 남는다 (다음 서버는 다시 받지 않는다)
    assert runner.cache.is_ready(kit_files[1])


async def test_an_install_in_the_patch_bases_gets_the_patch_and_keeps_the_package(tmp_path, kit_files):
    target = FakeTarget(installed="1.2.0.1", addon="suite")
    runner, lines = make_runner(tmp_path, kit_files, target, addon="orthanc")
    result = await runner.run()

    assert result.status == "succeeded", result.error
    assert result.mode == "patch" and result.kit_name == "connevo-gateway-patch-1.2.0.2.run"
    assert "--upgrade" in target.launched and "ADDON=" not in target.launched
    assert result.addon == "suite"


async def test_a_second_server_uses_the_cached_kit(tmp_path, kit_files):
    target = FakeTarget()
    runner, _ = make_runner(tmp_path, kit_files, target)
    assert (await runner.run()).status == "succeeded"
    rsync = runner.rsync

    target2 = FakeTarget()
    runner2, lines2 = make_runner(tmp_path, kit_files, target2, rsync=FakeRsync(rsync.source_file, target2))
    assert (await runner2.run()).status == "succeeded"
    assert not any(a[-2].startswith("connecteve@buildpc:") for a in runner2.rsync.calls)
    assert any("맥미니 캐시에 있습니다" in t for _, t, _ in lines2)


async def test_following_the_log_survives_a_dropped_connection(tmp_path, kit_files):
    target = FakeTarget()
    target.drop_follow_after = 3
    runner, lines = make_runner(tmp_path, kit_files, target)
    result = await runner.run()

    assert result.status == "succeeded", result.error
    # 끊긴 뒤에는 읽은 줄 다음부터 이어 읽는다 — 같은 줄을 두 번 내지 않는다
    assert target.follow_from == [1, 4]
    texts = [t for _, t, _ in lines]
    assert texts.count("━━ 1/8 PC 확인") == 1
    assert any("다시 붙습니다" in t for t in texts)


async def test_a_patch_on_the_wrong_base_fails_with_the_kits_reason(tmp_path, kit_files):
    target = FakeTarget(installed="1.2.0.1", addon="suite", rc=6, log=[
        "::step:: 3/8 이미지 적재",
        "설치 실패: 이미지가 다릅니다: newhub/api:dev (이 패치의 기준 버전이 아닌 설치입니다)",
        "::result:: fail 6 이미지가 다릅니다: newhub/api:dev (이 패치의 기준 버전이 아닌 설치입니다)",
    ])
    runner, lines = make_runner(tmp_path, kit_files, target)
    result = await runner.run()

    assert result.status == "failed" and result.exit_code == 6
    assert "기준 버전이 아닌 설치" in result.error
    assert any(k is LineKind.ERROR for _, _, k in lines)
    assert target.pw_files == {}          # 실패해도 비밀번호 파일은 지운다
    # 다시 시도할 때 보내지 않아도 되게 타겟의 .run 은 남긴다
    assert "/var/tmp/connevo-kits/connevo-gateway-patch-1.2.0.2.run" in target.files


async def test_installed_with_warnings_is_a_success_with_a_note(tmp_path, kit_files):
    target = FakeTarget(rc=9, log=KIT_LOG_OK[:-2] + ["::result:: warn 9 1.2.0.2 설치는 끝났지만 확인할 항목이 있습니다"])
    runner, _ = make_runner(tmp_path, kit_files, target)
    result = await runner.run()
    assert result.status == "succeeded" and result.exit_code == 9
    assert "확인할 항목" in result.warn


async def test_cancel_while_fetching_leaves_the_server_untouched(tmp_path, kit_files):
    target = FakeTarget()
    runner, _ = make_runner(tmp_path, kit_files, target)
    runner.rsync.on_call = lambda argv: runner.cancel()
    result = await runner.run()

    assert result.status == "cancelled"
    assert target.launched is None
    assert target.pw_files == {}


async def test_cancel_after_the_kit_started_is_refused(tmp_path, kit_files):
    target = FakeTarget()
    runner, lines = make_runner(tmp_path, kit_files, target)
    original = target.exec

    async def exec_and_cancel(command, on_line, stdin):
        rc = await original(command, on_line, stdin)
        if command.startswith("sudo -S -p '' systemd-run"):
            assert runner.cancel() is False
        return rc

    target.exec = exec_and_cancel
    result = await runner.run()
    assert result.status == "succeeded"
    assert any("취소하지 않습니다" in t for _, t, _ in lines)


async def test_a_dropped_transfer_resumes(tmp_path, kit_files):
    target = FakeTarget()
    src, full, _ = kit_files
    rsync = FakeRsync(src / full.name, target, fail_first_send=2)
    runner, lines = make_runner(tmp_path, kit_files, target, rsync=rsync)
    result = await runner.run()

    assert result.status == "succeeded", result.error
    sends = [a for a in rsync.calls if a[-1].startswith("connecteve@192.0.2.10:")]
    assert len(sends) == 3 and all("--partial" in a for a in sends)
    assert sum("전송이 끊겼습니다" in t for _, t, _ in lines) == 2
    # 타겟은 호스트 키를 검사하지 않는다 (포맷하면 바뀐다). 빌드 PC 는 검사한다.
    fetch = next(a for a in rsync.calls if a[-2].startswith("connecteve@buildpc:"))
    assert "StrictHostKeyChecking=accept-new" in fetch[fetch.index("-e") + 1]
    assert "UserKnownHostsFile=/dev/null" in sends[0][sends[0].index("-e") + 1]


async def test_not_enough_space_stops_before_sending(tmp_path, kit_files):
    target = FakeTarget(tmp_free_gb=3)
    runner, _ = make_runner(tmp_path, kit_files, target)
    result = await runner.run()
    assert result.status == "failed" and result.exit_code == 8
    assert target.launched is None


async def test_wrong_sudo_password_stops_at_the_check(tmp_path, kit_files):
    target = FakeTarget()
    runner, _ = make_runner(tmp_path, kit_files, target)
    runner.sudo_password = "wrong"
    result = await runner.run()
    assert result.status == "failed" and "sudo" in result.error


async def test_uninstall_all_needs_no_kit(tmp_path, kit_files):
    target = FakeTarget(installed="1.2.0.1", addon="suite", log=[
        "::step:: 1/1 삭제", "삭제 완료", "::result:: ok 0 삭제했습니다"])
    runner, lines = make_runner(tmp_path, kit_files, target, action="uninstall-all")
    result = await runner.run()

    assert result.status == "succeeded", result.error
    assert result.installed_after is None
    assert "uninstall.sh --yes" in target.launched and "--keep-data" not in target.launched
    assert runner.rsync.calls == []
    assert target.pw_files == {} and not any("cat > " in c for c in target.commands)
    assert "kit_remove" in [s for s, _, _ in lines]


async def test_uninstall_keep_data(tmp_path, kit_files):
    target = FakeTarget(installed="1.2.0.1", addon="suite", log=["::result:: ok 0 삭제했습니다"])
    runner, _ = make_runner(tmp_path, kit_files, target, action="uninstall-keep")
    result = await runner.run()
    assert result.status == "succeeded"
    assert "uninstall.sh --yes --keep-data" in target.launched


async def test_uninstall_on_an_empty_pc_is_refused(tmp_path, kit_files):
    target = FakeTarget()
    runner, _ = make_runner(tmp_path, kit_files, target, action="uninstall-all")
    result = await runner.run()
    assert result.status == "failed" and result.exit_code == 5


def test_job_paths_are_per_job():
    p = job_paths(12)
    assert p["pw"].endswith("/job-12.pw") and p["unit"] == "connevo-kit-job12"


def test_progress_throttle_keeps_a_few_lines():
    t = ProgressThrottle(step=5)
    shown = [pct for pct in range(0, 101) if t.want(f"  {pct * 1000} {pct}%  1MB/s")]
    assert shown[0] == 0 and shown[-1] == 100
    assert len(shown) <= 22
    assert t.want("plain text line")


async def test_an_old_kit_upgrades_with_install(tmp_path, kit_files):
    """1.2.0.2 이하 킷에는 --upgrade 가 없다 — 모르는 인자는 메뉴로 빠져 아무것도 안 한다."""
    from dataclasses import replace

    src, full, patch = kit_files
    old_patch = replace(patch, unattended=False)
    target = FakeTarget(installed="1.2.0.1", addon="suite")
    runner, lines = make_runner(tmp_path, kit_files, target)

    async def old_kits():
        return [old_patch, full]

    runner.list_kits = old_kits
    result = await runner.run()
    assert result.status == "succeeded", result.error
    assert "--install" in target.launched and "--upgrade" not in target.launched
    assert "ADDON=" not in target.launched
    assert any("무인 모드 이전 킷" in t for _, t, _ in lines)


def test_old_kits_initial_passwords_are_hidden():
    from autodeploy.kit_runner import redact

    assert "s3cret" not in redact("관리자 초기 비밀번호 (첫 로그인 때 변경): s3cret")
    assert "pw9" not in redact("Orthanc 웹: http://10.0.0.5:8001  (admin / pw9)")
    # 새 킷이 이미 가린 줄과 보통 줄은 그대로
    line = "관리자 초기 비밀번호 (첫 로그인 때 변경): sudo cat /connevo-gateway/install-info.txt"
    assert redact(line) == line
    assert redact("웹:  https://10.0.0.5:8000") == "웹:  https://10.0.0.5:8000"


async def test_the_followed_log_is_redacted(tmp_path, kit_files):
    target = FakeTarget(log=KIT_LOG_OK[:-1] + ["관리자 초기 비밀번호 (첫 로그인 때 변경): Abc123xyz",
                                               "::result:: ok 0 설치 완료"])
    runner, lines = make_runner(tmp_path, kit_files, target)
    assert (await runner.run()).status == "succeeded"
    assert not any("Abc123xyz" in t for _, t, _ in lines)
