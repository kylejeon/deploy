"""Product Registration 단위 테스트. dev-spec §10-4.

ProductRegistrationClient의 각 Lookup API + POST 동작을 검증.
aiohttp.ClientSession은 MagicMock으로 대체.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from autodeploy.models import Job
from autodeploy.product_registration import (
    ProductAPIError,
    ProductRegistrationClient,
    _latest_version_id,
)


def _job(code: str = "HOSP01") -> Job:
    return Job(
        id=1,
        target_ip="192.168.1.1",
        deployment_type="hybrid-with-ai",
        hospital_code=code,
        started_by="U01",
        slack_channel="C01",
    )


def _client() -> ProductRegistrationClient:
    return ProductRegistrationClient(
        base_url="https://dev-gateway.connecteve.com",
        token="FAKE_TOKEN",
        api_env="dev",
    )


def _resp(status: int, body: dict | list) -> MagicMock:
    r = MagicMock()
    r.status = status
    r.text = AsyncMock(return_value=json.dumps(body))
    r.__aenter__ = AsyncMock(return_value=r)
    r.__aexit__ = AsyncMock(return_value=None)
    return r


def _session(*resps) -> MagicMock:
    """순서대로 응답을 반환하는 fake session."""
    queue = list(resps)
    call_count = [0]

    def _get_or_post(*args, **kwargs):
        idx = call_count[0]
        call_count[0] += 1
        if idx < len(queue):
            return queue[idx]
        raise AssertionError(f"예상치 못한 HTTP 호출 #{idx + 1}")

    sess = MagicMock()
    sess.get = MagicMock(side_effect=_get_or_post)
    sess.post = MagicMock(side_effect=_get_or_post)
    return sess


# ---------- semver 정렬 헬퍼 ----------

def test_latest_version_id_picks_highest_semver():
    items = [
        {"id": "1", "version": "1.2.3"},
        {"id": "2", "version": "1.10.0"},
        {"id": "3", "version": "1.3.0"},
    ]
    assert _latest_version_id(items) == "2"


def test_latest_version_id_empty_returns_none():
    assert _latest_version_id([]) is None


def test_latest_version_id_single_item():
    assert _latest_version_id([{"id": "42", "version": "2.0.0"}]) == "42"


# ---------- T-PR-1: siteId 조회 성공 ----------

@pytest.mark.asyncio
async def test_lookup_site_id_matches_hospital_code():
    client = _client()
    sess = _session(_resp(200, {
        "data": {"content": [
            {"id": "10", "name": "OTHER"},
            {"id": "99", "name": "HOSP01"},
        ]}
    }))
    result = await client.lookup_site_id(sess, "HOSP01")
    assert result == "99"


# ---------- T-PR-2: siteId 매칭 없음 ----------

@pytest.mark.asyncio
async def test_lookup_site_id_no_match_returns_none():
    client = _client()
    sess = _session(_resp(200, {
        "data": {"content": [{"id": "10", "name": "DIFFERENT"}]}
    }))
    result = await client.lookup_site_id(sess, "HOSP01")
    assert result is None


# ---------- T-PR-3: productCodeId 대소문자 정확히 일치 ----------

@pytest.mark.asyncio
async def test_lookup_product_code_id_case_sensitive():
    client = _client()
    sess = _session(_resp(200, {
        "data": [
            {"id": "5", "name": "koa"},  # 소문자 → 불일치
            {"id": "6", "name": "KOA"},  # 대문자 → 일치
        ]
    }))
    result = await client.lookup_product_code_id(sess, "KOA")
    assert result == "6"


@pytest.mark.asyncio
async def test_lookup_product_code_id_lowercase_not_matched():
    client = _client()
    sess = _session(_resp(200, {"data": [{"id": "5", "name": "koa"}]}))
    result = await client.lookup_product_code_id(sess, "KOA")
    assert result is None


# ---------- T-PR-4: gatewayVersionId 여러 버전 중 semver 최신 ----------

@pytest.mark.asyncio
async def test_lookup_gateway_version_id_picks_latest():
    client = _client()
    sess = _session(_resp(200, {
        "data": [
            {"id": "1", "version": "1.0.0"},
            {"id": "3", "version": "1.2.0"},
            {"id": "2", "version": "1.1.0"},
        ]
    }))
    result = await client.lookup_gateway_version_id(sess)
    assert result == "3"


# ---------- T-PR-5: aiEngineVersionId modal 필터 + 최신 ----------

@pytest.mark.asyncio
async def test_lookup_ai_engine_version_id_latest():
    client = _client()
    sess = _session(_resp(200, {
        "data": [
            {"id": "10", "version": "1.0.0"},
            {"id": "20", "version": "2.0.0"},
        ]
    }))
    result = await client.lookup_ai_engine_version_id(sess, "KOA")
    assert result == "20"


# ---------- T-PR-6: isVersionId 클라이언트 modal 필터 + 최신 ----------

@pytest.mark.asyncio
async def test_lookup_is_version_id_client_filters_by_modal():
    client = _client()
    sess = _session(_resp(200, {
        "data": [
            {"id": "1", "version": "1.0.0", "modal": "Spin"},
            {"id": "2", "version": "1.0.0", "modal": "KOA"},
            {"id": "3", "version": "2.0.0", "modal": "KOA"},
        ]
    }))
    result = await client.lookup_is_version_id(sess, "KOA")
    assert result == "3"  # Spin은 필터링되고 KOA 중 최신


@pytest.mark.asyncio
async def test_lookup_is_version_id_no_modal_match():
    client = _client()
    sess = _session(_resp(200, {
        "data": [{"id": "1", "version": "1.0.0", "modal": "Spin"}]
    }))
    result = await client.lookup_is_version_id(sess, "KOA")
    assert result is None


# ---------- T-PR-7: POST 성공 (201) ----------

@pytest.mark.asyncio
async def test_post_product_success_returns_created():
    client = _client()
    sess = _session(_resp(201, {"id": "999"}))
    result = await client.post_product(sess, {"siteId": "1"})
    assert result == "created"


# ---------- T-PR-8: POST 409 → already_exists ----------

@pytest.mark.asyncio
async def test_post_product_409_returns_already_exists():
    client = _client()
    sess = _session(_resp(409, {"message": "duplicate"}))
    result = await client.post_product(sess, {"siteId": "1"})
    assert result == "already_exists"


# ---------- T-PR-9: POST 400 + "duplicate" → already_exists ----------

@pytest.mark.asyncio
async def test_post_product_400_duplicate_keyword_returns_already_exists():
    client = _client()
    sess = _session(_resp(400, "product already exists"))
    result = await client.post_product(sess, {"siteId": "1"})
    assert result == "already_exists"


# ---------- T-PR-10: POST 500 → ProductAPIError ----------

@pytest.mark.asyncio
async def test_post_product_500_raises_error():
    client = _client()
    sess = _session(_resp(500, {"message": "internal server error"}))
    with pytest.raises(ProductAPIError):
        await client.post_product(sess, {"siteId": "1"})


# ---------- T-PR-11: Lookup 결과 캐싱 ----------

@pytest.mark.asyncio
async def test_lookup_caching_prevents_duplicate_api_calls():
    """동일 product_name으로 2회 호출 시 API는 1회만."""
    client = _client()
    call_count = [0]
    r = _resp(200, {"data": [{"id": "5", "name": "KOA"}]})

    def _counted_get(*args, **kwargs):
        call_count[0] += 1
        return r

    sess = MagicMock()
    sess.get = MagicMock(side_effect=_counted_get)

    first = await client.lookup_product_code_id(sess, "KOA")
    second = await client.lookup_product_code_id(sess, "KOA")
    assert first == second == "5"
    assert call_count[0] == 1  # API는 한 번만


@pytest.mark.asyncio
async def test_gateway_version_id_cached():
    client = _client()
    call_count = [0]
    r = _resp(200, {"data": [{"id": "7", "version": "1.0.0"}]})

    def _counted(*args, **kwargs):
        call_count[0] += 1
        return r

    sess = MagicMock()
    sess.get = MagicMock(side_effect=_counted)

    first = await client.lookup_gateway_version_id(sess)
    second = await client.lookup_gateway_version_id(sess)
    assert first == second == "7"
    assert call_count[0] == 1


# ---------- register_products 통합 ----------

def _para(*texts) -> dict:
    return {"type": "paragraph", "content": [{"type": "text", "text": t} for t in texts]}


def _doc(*paras) -> dict:
    return {"type": "doc", "content": list(paras)}


def _minimal_issue(summary: str, gs1_line: str) -> dict:
    adf = _doc(
        _para("목적: 데모"),
        _para("기간: 2026년 1월 1일 ~ 2026년 12월 31일"),
        _para("N수: 10만장"),
        _para(gs1_line),
    )
    return {"fields": {"summary": summary, "description": adf}}


@pytest.mark.asyncio
async def test_register_products_success(mocker):
    """정상 시나리오: 이슈 1개 → 등록 성공."""
    issue = _minimal_issue("[병원] CONNEVO KOA 1.0", "(01)1234(21)SN001(APP)KOA")

    client = ProductRegistrationClient(
        base_url="https://dev-gateway.connecteve.com",
        token="TOK",
        api_env="dev",
    )

    # 각 Lookup API + POST를 순서대로 mock
    mocker.patch.object(client, "lookup_site_id", return_value="SITE_ID")
    mocker.patch.object(client, "lookup_product_code_id", return_value="PC_ID")
    mocker.patch.object(client, "lookup_gateway_version_id", return_value="GW_ID")
    mocker.patch.object(client, "lookup_ai_engine_version_id", return_value="AI_ID")
    mocker.patch.object(client, "lookup_is_version_id", return_value="IS_ID")
    mocker.patch.object(client, "post_product", return_value="created")

    events: list[tuple[str, str]] = []

    async def _event(level, msg):
        events.append((level, msg))

    success, fail = await client.register_products(_job(), [issue], on_event=_event)
    assert success == 1
    assert fail == 0
    assert any("등록 완료" in msg for _, msg in events)


@pytest.mark.asyncio
async def test_register_products_post_failure_is_partial_fail(mocker):
    """POST 실패 → fail_count 1, success_count 0."""
    issue = _minimal_issue("[병원] CONNEVO KOA 1.0", "(01)1234(21)SN001(APP)KOA")

    client = ProductRegistrationClient(
        base_url="https://dev-gateway.connecteve.com",
        token="TOK",
        api_env="dev",
    )
    mocker.patch.object(client, "lookup_site_id", return_value="SITE_ID")
    mocker.patch.object(client, "lookup_product_code_id", return_value="PC_ID")
    mocker.patch.object(client, "lookup_gateway_version_id", return_value="GW_ID")
    mocker.patch.object(client, "lookup_ai_engine_version_id", return_value="AI_ID")
    mocker.patch.object(client, "lookup_is_version_id", return_value="IS_ID")
    mocker.patch.object(
        client, "post_product",
        side_effect=ProductAPIError("HTTP 500: internal error"),
    )

    events: list[tuple[str, str]] = []

    async def _event(level, msg):
        events.append((level, msg))

    success, fail = await client.register_products(_job(), [issue], on_event=_event)
    assert success == 0
    assert fail == 1
    assert any("warn" == level for level, _ in events)
