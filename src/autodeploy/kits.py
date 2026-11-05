"""설치 킷(.run) — 빌드 PC 의 킷 목록, 맥미니 캐시, 헤더 읽기.

CONNEVO Gateway 는 hubctl 이 아니라 **설치 킷 하나**로 깔린다 (newhub
`deploy/kit/`). 킷은 빌드 PC 의 `dist/` 에 `connevo-gateway-<버전>.run`(전체)·
`connevo-gateway-patch-<버전>.run`(패치)로 쌓인다. 안은 키트 비밀번호로 암호화돼
있지만 **맨 앞의 셸 헤더는 평문**이라, 비밀번호 없이도 버전·종류·패치 기준 버전·
페이로드 sha256 을 읽을 수 있다::

    VERSION="1.2.0.2"
    KIND="patch"
    BASES="1.2.0 1.2.0.0 1.2.0.1"   # 1.2.0.2 이전 킷에는 없다
    PAYLOAD_OFFSET="000000012345"
    PAYLOAD_SHA256="…"
    PAYLOAD_SIZE="807094000"

빌드 PC 와 맥미니는 같은 사무실 LAN 이다. 킷을 미리 옮겨두지 않고, 작업이 필요할
때 LAN 으로 받아 캐시에 둔다. 캐시는 최근 N개 버전만 남긴다 (16GB 짜리라).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# 작업 화면의 단계 키. 서버(이 모듈·kit_runner)와 console.js 의 PHASES.kit_* 가
# 같은 이름을 쓴다. `ansible_log.is_step` 도 이것을 단계로 인정한다.
#   kit_0 … kit_8 은 킷이 내는 `::step:: N/8 이름` 의 N 이다.
KIT_STEPS: tuple[str, ...] = (
    "kit_check", "kit_fetch", "kit_send",
    "kit_0", "kit_1", "kit_2", "kit_3", "kit_4", "kit_5", "kit_6", "kit_7", "kit_8",
    "kit_remove", "kit_verify",
)

# 화면에서 고르는 작업. install 은 설치된 서버에서는 업그레이드·패치가 된다.
KIT_ACTIONS: tuple[str, ...] = ("install", "uninstall-keep", "uninstall-all")
ADDONS: tuple[str, ...] = ("suite", "orthanc", "none")

_HEADER_KEYS = ("VERSION", "KIND", "BASES", "PAYLOAD_OFFSET", "PAYLOAD_SHA256", "PAYLOAD_SIZE")
_HEADER_RE = re.compile(r'^(?P<k>' + "|".join(_HEADER_KEYS) + r')="(?P<v>[^"]*)"')
_VERSION_RE = re.compile(r"^\d+(?:\.\d+)*$")
_NAME_RE = re.compile(r"^connevo-gateway(?:-patch)?-[0-9A-Za-z._-]+\.run$")
# 헤더 변수는 파일 첫 몇 KB 안에 있다. 페이로드(수 GB)를 읽지 않으려고 자른다.
HEADER_BYTES = 16384


class KitError(RuntimeError):
    """킷 목록·캐시 문제. 사람에게 그대로 보여줄 문장이다."""


def version_key(version: str) -> tuple[int, ...]:
    """`1.2.0.2` → (1, 2, 0, 2).

    0 으로 채우지 **않는다.** 1.2.0 과 1.2.0.0 은 이미지 구성이 다른 별개의 릴리스다
    (manifests/1.2.0.txt ≠ 1.2.0.0.txt). 튜플 비교로 1.2.0 < 1.2.0.0 < 1.2.0.1 이 되어
    릴리스 순서와도 맞는다.
    """
    if not _VERSION_RE.match(version or ""):
        return ()
    return tuple(int(p) for p in version.split("."))


@dataclass(frozen=True, slots=True)
class Kit:
    name: str               # connevo-gateway-1.2.0.2.run
    version: str
    kind: str               # full | patch
    size: int               # 파일 전체 바이트
    payload_offset: int     # 1부터 센 페이로드 시작 위치 (tail -c +N)
    payload_sha256: str
    bases: tuple[str, ...] = ()
    # 무인 모드(--upgrade·::step::·종료 코드)가 있는 킷인가. 그 킷부터 헤더에 BASES= 줄이
    # 생겼다 (전체 킷은 빈 값). 1.2.0.2 이하 킷은 --install 하나로 설치·업그레이드·패치를 한다.
    unattended: bool = True

    @property
    def is_patch(self) -> bool:
        return self.kind == "patch"

    def applies_to(self, installed: str | None) -> bool:
        """이 패치가 그 설치 버전에 맞는가. 기준 목록에 있어야 한다."""
        if not self.is_patch or not installed:
            return False
        key = version_key(installed)
        return bool(key) and any(version_key(b) == key for b in self.bases)

    def as_dict(self) -> dict:
        return {
            "name": self.name, "version": self.version, "kind": self.kind,
            "size": self.size, "bases": list(self.bases),
        }


def parse_header(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _HEADER_RE.match(line)
        if m and m["k"] not in out:
            out[m["k"]] = m["v"]
    return out


def kit_from_header(name: str, size: int, header: dict[str, str],
                    base_files: Sequence[str] = ()) -> Kit | None:
    """헤더로 Kit 을 만든다. 킷이 아니거나 망가진 헤더면 None.

    BASES 는 1.2.0.2 이전 킷 헤더에 없다. 그때는 빌드 PC 에 남은 킷 폴더의
    PATCH 파일(`base: 1.2.0.txt`)에서 읽어 온 값(base_files)을 쓴다.
    """
    version, kind = header.get("VERSION", ""), header.get("KIND", "")
    if not version_key(version) or kind not in ("full", "patch"):
        return None
    try:
        offset = int(header.get("PAYLOAD_OFFSET", ""), 10)
    except ValueError:
        return None
    bases = tuple(header.get("BASES", "").split()) or tuple(base_files)
    return Kit(
        name=name, version=version, kind=kind, size=size, payload_offset=offset,
        payload_sha256=header.get("PAYLOAD_SHA256", ""),
        bases=tuple(b for b in bases if version_key(b)) if kind == "patch" else (),
        unattended="BASES" in header,
    )


def list_command(directory: str) -> str:
    """빌드 PC 에서 돌릴 목록 명령. 파일마다 `@@ 이름 크기` 다음에 헤더 줄."""
    return (
        f"cd {shlex.quote(directory)} 2>/dev/null || exit 3; "
        "for f in connevo-gateway*.run; do [ -f \"$f\" ] || continue; "
        "echo \"@@ $f $(stat -c %s \"$f\")\"; "
        f"head -c {HEADER_BYTES} \"$f\" | grep -aE '^(VERSION|KIND|BASES|PAYLOAD_OFFSET|PAYLOAD_SHA256|PAYLOAD_SIZE)='; "
        "d=\"${f%.run}\"; [ -f \"$d/PATCH\" ] && sed -n 's/^base: \\(.*\\)\\.txt$/BASE_FILE=\\1/p' \"$d/PATCH\"; "
        "done; true"
    )


def parse_listing(lines: Sequence[str]) -> list[Kit]:
    kits: list[Kit] = []
    name: str | None = None
    size = 0
    header_lines: list[str] = []
    base_files: list[str] = []

    def flush() -> None:
        if name and _NAME_RE.match(name):
            kit = kit_from_header(name, size, parse_header("\n".join(header_lines)), base_files)
            if kit is not None:
                kits.append(kit)

    for raw in lines:
        line = raw.rstrip("\r\n")
        if line.startswith("@@ "):
            flush()
            parts = line[3:].rsplit(" ", 1)
            name = parts[0]
            size = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 0
            header_lines, base_files = [], []
        elif line.startswith("BASE_FILE="):
            base_files.append(line.split("=", 1)[1].strip())
        else:
            header_lines.append(line)
    flush()
    kits.sort(key=lambda k: (version_key(k.version), k.kind), reverse=True)
    return kits


def choose_kit(kits: Sequence[Kit], version: str, installed: str | None) -> Kit:
    """그 버전으로 가는 데 쓸 파일. 패치가 맞으면 패치, 아니면 전체.

    내려가는 설치(설치된 버전이 더 새로움)는 막는다 — 킷은 DB 를 거꾸로 돌리지
    않는다. 같은 버전은 전체 킷으로 다시 까는 것(복구)으로 허락한다.
    """
    target = version_key(version)
    if not target:
        raise KitError(f"버전이 올바르지 않습니다: {version!r}")
    have = version_key(installed or "")
    if have and have > target:
        raise KitError(f"설치된 버전({installed})이 고른 버전({version})보다 새롭습니다 — 내려 설치하지 않습니다")
    same = [k for k in kits if version_key(k.version) == target]
    patch = next((k for k in same if k.applies_to(installed)), None)
    if patch is not None:
        return patch
    full = next((k for k in same if not k.is_patch), None)
    if full is None:
        if installed:
            raise KitError(
                f"{version} 전체 킷이 없고, 패치는 설치된 {installed} 에 맞지 않습니다"
                f" (패치 기준: {', '.join(sorted({b for k in same for b in k.bases})) or '없음'})"
            )
        raise KitError(f"{version} 전체 킷이 빌드 PC 에 없습니다")
    return full


# ── 캐시 (맥미니) ───────────────────────────────────────────────


def payload_sha256(path: Path, offset: int, *, chunk: int = 8 << 20) -> str:
    """헤더를 뺀 페이로드의 sha256. 킷의 `--check` 와 같은 값이다 (tail -c +offset)."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        f.seek(max(0, offset - 1))
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


class KitCache:
    """받은 킷을 두는 폴더. `<이름>.ok` 에 확인한 sha256 을 적어 다시 해시하지 않는다."""

    def __init__(self, directory: str | Path, *, keep_versions: int = 2) -> None:
        self.dir = Path(directory).expanduser()
        self.keep_versions = max(1, keep_versions)

    def path(self, kit: Kit) -> Path:
        return self.dir / kit.name

    def _ok_path(self, kit: Kit) -> Path:
        return self.dir / (kit.name + ".ok")

    def is_ready(self, kit: Kit) -> bool:
        p, ok = self.path(kit), self._ok_path(kit)
        try:
            return (p.stat().st_size == kit.size
                    and ok.read_text().strip() == f"{kit.size} {kit.payload_sha256}")
        except OSError:
            return False

    async def verify(self, kit: Kit) -> bool:
        p = self.path(kit)
        if not p.is_file() or p.stat().st_size != kit.size:
            return False
        got = await asyncio.to_thread(payload_sha256, p, kit.payload_offset)
        if got != kit.payload_sha256:
            return False
        self._ok_path(kit).write_text(f"{kit.size} {kit.payload_sha256}\n")
        return True

    def cached_names(self) -> set[str]:
        if not self.dir.is_dir():
            return set()
        return {p.name[:-3] for p in self.dir.glob("*.run.ok")}

    def used_bytes(self) -> int:
        if not self.dir.is_dir():
            return 0
        return sum(p.stat().st_size for p in self.dir.glob("*.run*") if p.is_file())

    def prune(self, keep: Kit, *, remove: Callable[[Path], None] | None = None) -> list[str]:
        """최근 keep_versions 개 버전만 남긴다. 지금 쓰는 킷(keep)은 지우지 않는다.

        캐시 폴더 안의 `connevo-gateway*.run` 과 그 `.ok` 만 본다. 원본은 빌드 PC
        dist/ 에 그대로 있어 언제든 다시 받을 수 있다.
        """
        if not self.dir.is_dir():
            return []
        runs = [p for p in self.dir.iterdir() if p.is_file() and _NAME_RE.match(p.name)]
        def ver(p: Path) -> tuple[int, ...]:
            m = re.match(r"^connevo-gateway(?:-patch)?-(.+)\.run$", p.name)
            return version_key(m.group(1)) if m else ()
        versions = sorted({ver(p) for p in runs if ver(p)}, reverse=True)
        keep_set = set(versions[: self.keep_versions]) | {version_key(keep.version)}
        removed: list[str] = []
        unlink = remove or (lambda p: p.unlink(missing_ok=True))
        for p in runs:
            if p.name == keep.name or ver(p) in keep_set:
                continue
            for q in (p, p.with_name(p.name + ".ok")):
                if q.exists():
                    unlink(q)
            removed.append(p.name)
        return removed


async def list_source_kits(source: str, *, key_path: str | Path, ssh_factory=None) -> list[Kit]:
    """빌드 PC(`user@host:dir`) 의 킷 목록. 헤더만 읽으므로 몇 초면 끝난다."""
    login, _, directory = source.partition(":")
    user, _, host = login.rpartition("@")
    if ssh_factory is None:
        from autodeploy.ssh import AsyncSSHClient

        def ssh_factory():  # type: ignore[misc]
            return AsyncSSHClient(host, username=user, key_path=key_path, connect_attempts=2)

    out: list[str] = []
    err: list[str] = []
    async with ssh_factory() as ssh:
        rc = await ssh.exec(
            list_command(directory or "."),
            lambda line: (out if line.stream == "stdout" else err).append(line.line),
        )
    if rc == 3:
        raise KitError(f"빌드 PC 에 킷 폴더가 없습니다: {source}")
    if rc != 0:
        raise KitError(f"빌드 PC 킷 목록 명령이 실패했습니다 (exit {rc}): {' '.join(err)[-200:]}")
    return parse_listing(out)
