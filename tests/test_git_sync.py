from __future__ import annotations

import pytest

from autodeploy.git_sync import GitSyncError, sync_repo
from autodeploy.ssh import FakeSSHClient, StreamLine

REPO_HOST = "bitbucket.org/connecteve-workspace/gateway-infra-next.git"


@pytest.mark.asyncio
async def test_clone_path_when_dir_missing():
    fake = FakeSSHClient()
    fake.enqueue("test -d", [], exit_code=1)  # missing
    fake.enqueue("git clone", [])
    fake.enqueue("remote set-url", [])
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
    # set-url 이후엔 토큰 없어야 함
    set_url_cmd = next(c for c in fake.executed if "remote set-url" in c)
    assert "TOKENXYZ" not in set_url_cmd


@pytest.mark.asyncio
async def test_update_path_when_dir_exists():
    fake = FakeSSHClient()
    fake.enqueue("test -d", [], exit_code=0)  # exists
    fake.enqueue("git checkout -- . && git fetch --all && git checkout", [])
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
    # update 흐름에선 clone 안 함 → 토큰 노출 0
    assert not any("git clone" in c for c in fake.executed)
    assert not any("TOKENXYZ" in c for c in fake.executed)


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
    fake.enqueue("git checkout -- .", [], exit_code=1)

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
    fake.enqueue("git checkout -- . && git fetch --all && git checkout", [])
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
