"""hub-provisioning 의 inventory/sites.yml 읽기·쓰기.

dev-spec-web-console §F2. 실 사이트 인벤토리는 hub-provisioning 의 .gitignore
대상이라 커밋 충돌은 없지만, 사람이 손으로 고칠 수 있으므로
(a) 원자적 교체 (b) 쓰기 전 백업 (c) mtime 기반 낙관적 잠금으로 덮어쓰기를 막는다.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

# group_vars/all/main.yml 의 platform_by_profile 키와 일치해야 한다
PROFILES: tuple[str, ...] = ("onprem", "hybrid-with-ai", "hybrid-without-ai")

_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_HEADER = (
    "# inventory/sites.yml — AutoDeploy 웹 콘솔이 관리합니다.\n"
    "# 실 IP/계정이 들어있어 hub-provisioning 의 .gitignore 대상입니다.\n"
    "# profile: onprem | hybrid-with-ai | hybrid-without-ai\n"
)
_BACKUP_KEEP = 10


def is_valid_host(name: str) -> bool:
    """인벤토리 호스트명으로 쓸 수 있는가.

    ansible `--limit` 과 clean 의 `-e confirm=` 에 그대로 들어가므로, 쉼표·공백·
    따옴표가 섞이면 대상이 엉뚱해진다. hubctl 호출 전 게이트로도 쓴다.
    """
    return bool(_HOST_RE.match(name))


class InventoryError(RuntimeError):
    """스키마 위반·파싱 실패 등."""


class InventoryConflict(InventoryError):
    """읽은 뒤 파일이 바뀌었다 (다른 곳에서 수정)."""


@dataclass(frozen=True, slots=True)
class Server:
    host: str
    ansible_host: str
    ansible_user: str
    site_name: str
    profile: str

    def to_yaml_dict(self) -> dict[str, str]:
        return {
            "ansible_host": self.ansible_host,
            "ansible_user": self.ansible_user,
            "site_name": self.site_name,
            "profile": self.profile,
        }


@dataclass(frozen=True, slots=True)
class Inventory:
    servers: tuple[Server, ...]
    mtime_ns: int

    def get(self, host: str) -> Server | None:
        return next((s for s in self.servers if s.host == host), None)


def validate_server(server: Server) -> None:
    if not _HOST_RE.match(server.host):
        raise InventoryError(
            f"서버 이름이 올바르지 않습니다: {server.host!r} — 영숫자로 시작하고 영숫자/-/_/. 만 사용"
        )
    if not server.ansible_host.strip():
        raise InventoryError("ansible_host(IP)는 비울 수 없습니다")
    if not _is_addr(server.ansible_host):
        raise InventoryError(f"ansible_host 가 IP/호스트명 형식이 아닙니다: {server.ansible_host!r}")
    if not server.ansible_user.strip():
        raise InventoryError("ansible_user 는 비울 수 없습니다")
    if not server.site_name.strip():
        raise InventoryError("site_name 은 비울 수 없습니다")
    if server.profile not in PROFILES:
        raise InventoryError(
            f"profile 이 올바르지 않습니다: {server.profile!r} — {' | '.join(PROFILES)}"
        )


def _is_addr(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return bool(_HOST_RE.match(value))


def load_inventory(path: str | Path) -> Inventory:
    p = Path(path).expanduser()
    if not p.exists():
        return Inventory(servers=(), mtime_ns=0)
    mtime_ns = p.stat().st_mtime_ns
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise InventoryError("sites.yml 최상위가 매핑이 아닙니다")
    hosts = (raw.get("sites") or {}).get("hosts") or {}
    if not isinstance(hosts, dict):
        raise InventoryError("sites.hosts 가 매핑이 아닙니다")

    servers: list[Server] = []
    for host, body in hosts.items():
        body = body or {}
        if not isinstance(body, dict):
            raise InventoryError(f"{host}: 항목이 매핑이 아닙니다")
        servers.append(
            Server(
                host=str(host),
                ansible_host=str(body.get("ansible_host", "")),
                ansible_user=str(body.get("ansible_user", "")),
                site_name=str(body.get("site_name", "")),
                profile=str(body.get("profile", "")),
            )
        )
    return Inventory(servers=tuple(servers), mtime_ns=mtime_ns)


def render(servers: tuple[Server, ...] | list[Server]) -> str:
    body = {"sites": {"hosts": {s.host: s.to_yaml_dict() for s in servers}}}
    dumped = yaml.safe_dump(body, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return _HEADER + dumped


def save_inventory(
    path: str | Path,
    servers: tuple[Server, ...] | list[Server],
    *,
    expect_mtime_ns: int | None = None,
) -> int:
    """원자적 교체 + 백업. 반환값은 새 mtime_ns (다음 낙관적 잠금에 사용)."""
    p = Path(path).expanduser()
    seen: set[str] = set()
    for s in servers:
        validate_server(s)
        if s.host in seen:
            raise InventoryError(f"서버 이름이 중복됩니다: {s.host}")
        seen.add(s.host)

    if expect_mtime_ns is not None:
        current = p.stat().st_mtime_ns if p.exists() else 0
        if current != expect_mtime_ns:
            raise InventoryConflict(
                "다른 곳에서 sites.yml 이 수정됐습니다. 새로고침 후 다시 시도하세요."
            )

    p.parent.mkdir(parents=True, exist_ok=True)
    _backup(p)

    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".sites-", suffix=".yml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(render(servers))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return p.stat().st_mtime_ns


def _backup(p: Path) -> None:
    if not p.exists():
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = p.with_name(f"{p.name}.bak-{stamp}")
    # 같은 초에 두 번 저장하면 덮어쓰지 않고 접미사를 붙인다
    n = 1
    while dest.exists():
        dest = p.with_name(f"{p.name}.bak-{stamp}-{n}")
        n += 1
    shutil.copy2(p, dest)
    _prune_backups(p)


def _prune_backups(p: Path) -> None:
    backups = sorted(
        p.parent.glob(f"{p.name}.bak-*"), key=lambda f: f.stat().st_mtime, reverse=True
    )
    for old in backups[_BACKUP_KEEP:]:
        old.unlink(missing_ok=True)
        log.info("오래된 인벤토리 백업 삭제: %s", old)


def upsert_server(
    path: str | Path, server: Server, *, expect_mtime_ns: int | None = None
) -> int:
    inv = load_inventory(path)
    if inv.get(server.host) is not None:
        # 기존 항목 교체 — 파일 내 순서를 유지한다
        servers = [server if s.host == server.host else s for s in inv.servers]
    else:
        servers = [*inv.servers, server]
    return save_inventory(path, servers, expect_mtime_ns=expect_mtime_ns)


def remove_server(path: str | Path, host: str, *, expect_mtime_ns: int | None = None) -> int:
    inv = load_inventory(path)
    if inv.get(host) is None:
        raise InventoryError(f"등록되지 않은 서버입니다: {host}")
    servers = [s for s in inv.servers if s.host != host]
    return save_inventory(path, servers, expect_mtime_ns=expect_mtime_ns)
