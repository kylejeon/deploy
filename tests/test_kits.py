"""설치 킷 목록·선택·캐시 (kits.py)."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from autodeploy.kits import (
    Kit,
    KitCache,
    KitError,
    choose_kit,
    kit_from_header,
    parse_header,
    parse_listing,
    payload_sha256,
    version_key,
)


def make_run(path: Path, version: str, kind: str, payload: bytes, bases: str | None = None) -> Kit:
    """킷처럼 생긴 파일: 평문 헤더 + 페이로드. 오프셋은 헤더 길이로 정한다."""
    lines = ["#!/usr/bin/env bash", f'VERSION="{version}"', f'KIND="{kind}"']
    if bases is not None:
        lines.append(f'BASES="{bases}"')
    head = "\n".join(lines + ['PAYLOAD_OFFSET="@@@@@@@@@@@@"', f'PAYLOAD_SHA256="{hashlib.sha256(payload).hexdigest()}"',
                              "exit 0", "__PAYLOAD__", ""])
    offset = len(head.encode()) + 1
    head = head.replace("@@@@@@@@@@@@", f"{offset:012d}")
    path.write_bytes(head.encode() + payload)
    header = parse_header(head)
    kit = kit_from_header(path.name, path.stat().st_size, header)
    assert kit is not None
    return kit


def listing(name: str, size: int, version: str, kind: str, *, bases: str | None = None,
            base_files: tuple[str, ...] = ()) -> list[str]:
    out = [f"@@ {name} {size}", f'VERSION="{version}"', f'KIND="{kind}"']
    if bases is not None:
        out.append(f'BASES="{bases}"  # a patch: the installed versions it applies to')
    out += ['PAYLOAD_OFFSET="000000012345"', 'PAYLOAD_SHA256="ab" ']
    out += [f"BASE_FILE={b}" for b in base_files]
    return out


def test_version_key_keeps_releases_apart_and_rejects_garbage():
    # 1.2.0 과 1.2.0.0 은 다른 릴리스다 (이미지 구성이 다르다). 순서는 릴리스 순서.
    assert version_key("1.2.0") != version_key("1.2.0.0")
    assert version_key("1.2.0") < version_key("1.2.0.0") < version_key("1.2.0.1") < version_key("1.2.0.2")
    assert version_key("main") == ()
    assert version_key("") == ()


def test_listing_reads_header_bases_and_old_patch_files():
    lines = (
        listing("connevo-gateway-1.2.0.2.run", 16_000, "1.2.0.2", "full")
        + listing("connevo-gateway-patch-1.2.0.2.run", 800, "1.2.0.2", "patch", bases="1.2.0 1.2.0.0 1.2.0.1")
        # 1.2.0.2 이전 킷: 헤더에 BASES 가 없고 빌드 PC 의 킷 폴더 PATCH 에서 읽는다
        + listing("connevo-gateway-patch-1.2.0.1.run", 370, "1.2.0.1", "patch", base_files=("1.2.0", "1.2.0.0"))
        + ["@@ notes.run 10", 'VERSION="9"']          # 킷 이름이 아니면 버린다
    )
    kits = parse_listing(lines)
    assert [(k.name, k.kind) for k in kits] == [
        ("connevo-gateway-patch-1.2.0.2.run", "patch"),
        ("connevo-gateway-1.2.0.2.run", "full"),
        ("connevo-gateway-patch-1.2.0.1.run", "patch"),
    ]
    assert kits[0].bases == ("1.2.0", "1.2.0.0", "1.2.0.1")
    assert kits[2].bases == ("1.2.0", "1.2.0.0")
    assert kits[1].payload_offset == 12345 and kits[1].size == 16_000


def test_a_full_kit_has_no_bases_even_if_the_header_had_some():
    kit = kit_from_header("connevo-gateway-1.0.run", 1, {"VERSION": "1.0", "KIND": "full",
                                                         "BASES": "0.9", "PAYLOAD_OFFSET": "10"})
    assert kit is not None and kit.bases == ()


KITS = parse_listing(
    listing("connevo-gateway-1.2.0.2.run", 16_000, "1.2.0.2", "full")
    + listing("connevo-gateway-patch-1.2.0.2.run", 800, "1.2.0.2", "patch", bases="1.2.0.0 1.2.0.1")
    + listing("connevo-gateway-patch-1.2.0.3.run", 800, "1.2.0.3", "patch", bases="1.2.0.2")
)


@pytest.mark.parametrize("installed, version, expected", [
    (None, "1.2.0.2", "connevo-gateway-1.2.0.2.run"),            # 새 설치 → 전체
    ("1.2.0.1", "1.2.0.2", "connevo-gateway-patch-1.2.0.2.run"),  # 기준에 있음 → 패치
    ("1.2.0", "1.2.0.2", "connevo-gateway-1.2.0.2.run"),          # 기준 밖 → 전체
    ("1.2.0.2", "1.2.0.2", "connevo-gateway-1.2.0.2.run"),        # 같은 버전 → 전체로 다시
    ("1.2.0.2", "1.2.0.3", "connevo-gateway-patch-1.2.0.3.run"),
])
def test_choose_kit(installed, version, expected):
    assert choose_kit(KITS, version, installed).name == expected


def test_choose_kit_refuses_a_downgrade():
    with pytest.raises(KitError, match="내려 설치하지 않습니다"):
        choose_kit(KITS, "1.2.0.2", "1.2.0.3")


def test_choose_kit_explains_a_patch_that_does_not_fit():
    with pytest.raises(KitError, match="패치 기준: 1.2.0.2"):
        choose_kit(KITS, "1.2.0.3", "1.2.0.1")
    with pytest.raises(KitError, match="전체 킷이 빌드 PC 에 없습니다"):
        choose_kit(KITS, "1.2.0.3", None)


def test_payload_hash_skips_the_header(tmp_path):
    kit = make_run(tmp_path / "connevo-gateway-1.0.run", "1.0", "full", b"payload-bytes" * 1000)
    assert payload_sha256(tmp_path / kit.name, kit.payload_offset) == hashlib.sha256(b"payload-bytes" * 1000).hexdigest()


async def test_cache_verifies_once_and_remembers(tmp_path):
    cache = KitCache(tmp_path / "kits")
    cache.dir.mkdir()
    kit = make_run(cache.dir / "connevo-gateway-1.0.run", "1.0", "full", b"x" * 5000)
    assert not cache.is_ready(kit)
    assert await cache.verify(kit)
    assert cache.is_ready(kit)
    assert cache.cached_names() == {kit.name}


async def test_cache_rejects_a_damaged_file(tmp_path):
    cache = KitCache(tmp_path / "kits")
    cache.dir.mkdir()
    kit = make_run(cache.dir / "connevo-gateway-1.0.run", "1.0", "full", b"x" * 5000)
    data = bytearray((cache.dir / kit.name).read_bytes())
    data[-1] ^= 0xFF
    (cache.dir / kit.name).write_bytes(bytes(data))
    assert not await cache.verify(kit)
    assert not cache.is_ready(kit)


def test_cache_keeps_the_newest_versions_and_the_one_in_use(tmp_path):
    cache = KitCache(tmp_path / "kits", keep_versions=2)
    cache.dir.mkdir()
    names = ["connevo-gateway-1.2.0.0.run", "connevo-gateway-1.2.0.1.run",
             "connevo-gateway-patch-1.2.0.2.run", "connevo-gateway-1.2.0.2.run", "other.txt"]
    for n in names:
        (cache.dir / n).write_text("x")
    (cache.dir / "connevo-gateway-1.2.0.0.run.ok").write_text("ok")
    in_use = Kit("connevo-gateway-1.2.0.2.run", "1.2.0.2", "full", 1, 1, "")
    removed = cache.prune(in_use)
    assert removed == ["connevo-gateway-1.2.0.0.run"]
    left = sorted(p.name for p in cache.dir.iterdir())
    assert left == ["connevo-gateway-1.2.0.1.run", "connevo-gateway-1.2.0.2.run",
                    "connevo-gateway-patch-1.2.0.2.run", "other.txt"]


def test_kits_with_a_bases_line_are_unattended():
    new_full = kit_from_header("connevo-gateway-1.2.0.3.run", 1,
                               {"VERSION": "1.2.0.3", "KIND": "full", "BASES": "", "PAYLOAD_OFFSET": "9"})
    old_full = kit_from_header("connevo-gateway-1.2.0.2.run", 1,
                               {"VERSION": "1.2.0.2", "KIND": "full", "PAYLOAD_OFFSET": "9"})
    assert new_full.unattended and not old_full.unattended
