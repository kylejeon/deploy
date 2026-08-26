"""inventory/sites.yml 읽기·쓰기 (dev-spec-web-console §F2)."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from autodeploy.inventory import (
    Inventory,
    InventoryConflict,
    InventoryError,
    Server,
    load_inventory,
    remove_server,
    render,
    save_inventory,
    upsert_server,
    validate_server,
)

REAL_EXAMPLE = Path("/Users/yonghyuk/hub-provisioning/inventory/sites.example.yml")

EXAMPLE = """\
sites:
  hosts:
    bumin-node1:
      ansible_host: 10.0.0.11
      ansible_user: ubuntu
      site_name: bumin
      profile: onprem
    gangnam-node1:
      ansible_host: 10.0.0.21
      ansible_user: ubuntu
      site_name: gangnam
      profile: hybrid-with-ai
"""


def _write(tmp_path, text=EXAMPLE) -> Path:
    p = tmp_path / "sites.yml"
    p.write_text(text, encoding="utf-8")
    return p


def _srv(host="new-node1", **kw) -> Server:
    base = dict(
        ansible_host="10.9.9.9", ansible_user="connecteve",
        site_name="new", profile="onprem",
    )
    base.update(kw)
    return Server(host=host, **base)


# ── 읽기 ──────────────────────────────────────────────
def test_parses_hosts_and_fields(tmp_path):
    inv = load_inventory(_write(tmp_path))
    assert [s.host for s in inv.servers] == ["bumin-node1", "gangnam-node1"]
    s = inv.get("gangnam-node1")
    assert s.ansible_host == "10.0.0.21"
    assert s.ansible_user == "ubuntu"
    assert s.site_name == "gangnam"
    assert s.profile == "hybrid-with-ai"


def test_missing_file_is_empty_inventory(tmp_path):
    inv = load_inventory(tmp_path / "nope.yml")
    assert inv == Inventory(servers=(), mtime_ns=0)


def test_rejects_non_mapping_root(tmp_path):
    p = tmp_path / "sites.yml"
    p.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(InventoryError):
        load_inventory(p)


@pytest.mark.skipif(not REAL_EXAMPLE.exists(), reason="hub-provisioning 미존재")
def test_parses_real_repo_example():
    inv = load_inventory(REAL_EXAMPLE)
    assert {s.host for s in inv.servers} == {"bumin-node1", "gangnam-node1"}
    assert all(s.profile for s in inv.servers)


# ── 쓰기 ──────────────────────────────────────────────
def test_round_trip_preserves_values(tmp_path):
    p = _write(tmp_path)
    before = load_inventory(p)
    save_inventory(p, before.servers)
    after = load_inventory(p)
    assert after.servers == before.servers


def test_render_includes_header_comment(tmp_path):
    text = render([_srv()])
    assert text.startswith("# inventory/sites.yml")
    assert "profile: onprem" in text


def test_upsert_adds_then_replaces(tmp_path):
    p = _write(tmp_path)
    upsert_server(p, _srv())
    assert len(load_inventory(p).servers) == 3

    upsert_server(p, _srv(site_name="renamed"))
    inv = load_inventory(p)
    assert len(inv.servers) == 3, "같은 이름이면 추가가 아니라 교체"
    assert inv.get("new-node1").site_name == "renamed"


def test_upsert_keeps_position(tmp_path):
    p = _write(tmp_path)
    upsert_server(p, _srv(host="bumin-node1", site_name="bumin", ansible_host="10.0.0.99"))
    assert [s.host for s in load_inventory(p).servers] == ["bumin-node1", "gangnam-node1"]


def test_remove_server(tmp_path):
    p = _write(tmp_path)
    remove_server(p, "bumin-node1")
    assert [s.host for s in load_inventory(p).servers] == ["gangnam-node1"]


def test_remove_unknown_host_raises(tmp_path):
    p = _write(tmp_path)
    with pytest.raises(InventoryError, match="등록되지 않은"):
        remove_server(p, "ghost")


# ── 백업 / 원자성 ─────────────────────────────────────
def test_backup_created_before_overwrite(tmp_path):
    p = _write(tmp_path)
    upsert_server(p, _srv())
    backups = list(tmp_path.glob("sites.yml.bak-*"))
    assert len(backups) == 1
    assert "bumin-node1" in backups[0].read_text(encoding="utf-8")


def test_backups_pruned_to_ten(tmp_path):
    p = _write(tmp_path)
    for i in range(13):
        upsert_server(p, _srv(host=f"n{i}"))
        time.sleep(0.002)
    assert len(list(tmp_path.glob("sites.yml.bak-*"))) == 10


def test_no_temp_file_left_behind(tmp_path):
    p = _write(tmp_path)
    upsert_server(p, _srv())
    assert not list(tmp_path.glob(".sites-*")), "임시 파일이 남으면 안 된다"


def test_failed_validation_leaves_file_untouched(tmp_path):
    p = _write(tmp_path)
    original = p.read_text(encoding="utf-8")
    with pytest.raises(InventoryError):
        upsert_server(p, _srv(profile="bogus"))
    assert p.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob("sites.yml.bak-*")), "검증 실패면 백업도 안 남는다"


# ── 낙관적 잠금 ───────────────────────────────────────
def test_conflict_when_file_changed_since_read(tmp_path):
    p = _write(tmp_path)
    stale = load_inventory(p).mtime_ns
    time.sleep(0.01)
    p.write_text(EXAMPLE + "\n# 다른 사람이 손댐\n", encoding="utf-8")
    with pytest.raises(InventoryConflict):
        upsert_server(p, _srv(), expect_mtime_ns=stale)


def test_save_succeeds_with_fresh_mtime(tmp_path):
    p = _write(tmp_path)
    fresh = load_inventory(p).mtime_ns
    new_mtime = upsert_server(p, _srv(), expect_mtime_ns=fresh)
    assert new_mtime != fresh
    assert load_inventory(p).get("new-node1") is not None


# ── 검증 ──────────────────────────────────────────────
@pytest.mark.parametrize(
    "kw, msg",
    [
        ({"host": "-bad"}, "서버 이름"),
        ({"host": "sp ace"}, "서버 이름"),
        ({"ansible_host": ""}, "ansible_host"),
        ({"ansible_host": "10.0.0.1 ; rm -rf /"}, "ansible_host"),
        ({"ansible_user": ""}, "ansible_user"),
        ({"site_name": ""}, "site_name"),
        ({"profile": "on-premise"}, "profile"),
    ],
)
def test_validation_rejects(tmp_path, kw, msg):
    p = _write(tmp_path)
    with pytest.raises(InventoryError, match=msg):
        upsert_server(p, _srv(**kw))


def test_duplicate_host_rejected(tmp_path):
    p = tmp_path / "sites.yml"
    with pytest.raises(InventoryError, match="중복"):
        save_inventory(p, [_srv(host="dup"), _srv(host="dup", site_name="other")])


def test_accepts_hostname_and_ipv6(tmp_path):
    p = tmp_path / "sites.yml"
    save_inventory(p, [
        _srv(host="a", ansible_host="node.hospital.local"),
        _srv(host="b", ansible_host="2001:db8::1"),
    ])
    assert len(load_inventory(p).servers) == 2


@pytest.mark.parametrize(
    "addr",
    [
        "192.168.100.209",          # 사내 LAN
        "100.116.199.41",           # Tailscale (CGNAT 100.64.0.0/10)
        "testpc.tail1a2b.ts.net",   # Tailscale MagicDNS
        "hub.example.com",
    ],
)
def test_any_reachable_address_can_be_registered(addr):
    """타겟이 어디 있든 맥미니가 SSH 로 닿기만 하면 등록된다.

    대역을 제한하지 않는다 — LAN 만 받도록 좁히면 tailnet 너머의 서버를
    콘솔로 설치할 수 없다.
    """
    validate_server(Server(host="t", ansible_host=addr, ansible_user="connecteve",
                           site_name="t", profile="onprem"))


def test_an_address_with_a_port_is_refused():
    """`ansible_host` 는 주소만이다 — 포트는 못 붙인다.

    `install <IP>:<포트>` 는 v1 슬랙 워크플로의 기능이고(jobs.target_port),
    hubctl 인벤토리에는 그런 칸이 없다. 붙여 넣으면 ansible 이 그 문자열을
    통째로 호스트명으로 해석해 이름 해석에서 죽는다. 여기서 먼저 막는다.
    """
    with pytest.raises(InventoryError):
        validate_server(Server(host="t", ansible_host="1.2.3.4:22022",
                               ansible_user="connecteve", site_name="t", profile="onprem"))
