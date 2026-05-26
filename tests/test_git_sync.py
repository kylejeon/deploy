from __future__ import annotations

import pytest

from autodeploy.git_sync import GitSyncError, sync_repo
from autodeploy.ssh import FakeSSHClient, StreamLine

REPO_HOST = "bitbucket.org/connecteve-workspace/gateway-infra-next.git"


@pytest.mark.asyncio
async def test_clone_path_when_dir_missing_keeps_token_in_url():
    fake = FakeSSHClient()
    fake.enqueue("test -d", [], exit_code=1)  # missing → clone path
    fake.enqueue("git clone", [])
    fake.enqueue("git fetch --all && git checkout", [])
    fake.enqueue("git rev-parse HEAD", [StreamLine("stdout", "abc123def456")])

    async with fake as ssh:
        sha = await sync_repo(
            ssh,
            user="youngwoochon",
            app_password="TOKENXYZ",
            repo_host_path=REPO_HOST,
            branch="dev",
            target_dir="~/gateway-infra-next",
        )

    assert sha == "abc123def456"
    clone_cmd = next(c for c in fake.executed if "git clone" in c)
    assert "TOKENXYZ" in clone_cmd
    # 토큰 제거(set-url) 단계는 더 이상 수행하지 않음 — 비대화형 SSH에서 후속 fetch가 죽기 때문
    assert not any("remote set-url" in c for c in fake.executed)


@pytest.mark.asyncio
async def test_update_path_when_dir_exists_resets_token_url():
    fake = FakeSSHClient()
    fake.enqueue("test -d", [], exit_code=0)  # exists → update path
    # 업데이트 명령은 chain 안에 set-url(토큰 URL) + checkout + fetch + checkout + pull
    fake.enqueue("git remote set-url origin", [])
    fake.enqueue("git rev-parse HEAD", [StreamLine("stdout", "deadbeef")])

    async with fake as ssh:
        sha = await sync_repo(
            ssh,
            user="youngwoochon",
            app_password="TOKENXYZ",
            repo_host_path=REPO_HOST,
            branch="dev",
            target_dir="~/gateway-infra-next",
        )

    assert sha == "deadbeef"
    # 업데이트 흐름에서도 토큰을 .git/config에 다시 주입한다 (이전 버전이 토큰을 뗐을 수 있어서)
    update_cmd = next(c for c in fake.executed if "git remote set-url" in c)
    assert "TOKENXYZ" in update_cmd
    assert "git fetch --all" in update_cmd
    assert "git pull" in update_cmd


@pytest.mark.asyncio
async def test_clone_failure_raises():
    fake = FakeSSHClient()
    fake.enqueue("test -d", [], exit_code=1)
    fake.enqueue("git clone", [StreamLine("stderr", "auth failed")], exit_code=128)

    async with fake as ssh:
        with pytest.raises(GitSyncError, match="clone"):
            await sync_repo(
                ssh,
                user="u",
                app_password="bad",
                repo_host_path=REPO_HOST,
                branch="dev",
                target_dir="/tmp/x",
            )


@pytest.mark.asyncio
async def test_update_failure_raises():
    fake = FakeSSHClient()
    fake.enqueue("test -d", [], exit_code=0)
    fake.enqueue("git remote set-url origin", [], exit_code=1)

    async with fake as ssh:
        with pytest.raises(GitSyncError, match="update"):
            await sync_repo(
                ssh,
                user="u",
                app_password="x",
                repo_host_path=REPO_HOST,
                branch="dev",
                target_dir="/tmp/x",
            )


@pytest.mark.asyncio
async def test_rev_parse_failure_raises():
    fake = FakeSSHClient()
    fake.enqueue("test -d", [], exit_code=0)
    fake.enqueue("git remote set-url origin", [])
    fake.enqueue("git rev-parse HEAD", [], exit_code=128)  # no output, fail

    async with fake as ssh:
        with pytest.raises(GitSyncError, match="rev-parse"):
            await sync_repo(
                ssh,
                user="u",
                app_password="x",
                repo_host_path=REPO_HOST,
                branch="dev",
                target_dir="/tmp/x",
            )
