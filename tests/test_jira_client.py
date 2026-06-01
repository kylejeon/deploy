"""Jira 클라이언트 단위 테스트. dev-spec §10-3.

aiohttp는 FakeSession 패턴 대신 aiohttp.web 서버가 필요하다.
여기서는 JiraClient._auth_header 생성, JQL 구성, 응답 파싱만 단위 테스트.
네트워크 호출은 mocker.patch + AsyncMock으로 대체.
"""
from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from autodeploy.jira_client import JiraAPIError, JiraClient


def _client(email="test@example.com", token="MYTOKEN", key="PMFM"):
    return JiraClient(
        base_url="https://connecteve.atlassian.net",
        email=email,
        api_token=token,
        project_key=key,
        timeout_s=5.0,
    )


def _fake_response(status: int, body: dict | str) -> MagicMock:
    body_str = json.dumps(body) if isinstance(body, dict) else body
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=body_str)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


def _fake_session(resp: MagicMock) -> MagicMock:
    session = MagicMock()
    session.get = MagicMock(return_value=resp)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    return session


# T-JIRA-1: search_issues 정상 응답 → issues 리스트 반환
@pytest.mark.asyncio
async def test_search_issues_returns_list(mocker):
    issues = [
        {"id": "1", "fields": {"summary": "[병원] CONNEVO KOA 1.0", "description": None}},
        {"id": "2", "fields": {"summary": "[병원] CONNEVO Spin 2.0", "description": None}},
    ]
    resp = _fake_response(200, {"issues": issues, "total": 2})
    session = _fake_session(resp)
    mocker.patch("aiohttp.ClientSession", return_value=session)

    client = _client()
    result = await client.search_issues("병원")
    assert len(result) == 2
    assert result[0]["id"] == "1"


# T-JIRA-2: 0건 응답 → 빈 리스트 반환
@pytest.mark.asyncio
async def test_search_issues_empty_returns_empty_list(mocker):
    resp = _fake_response(200, {"issues": [], "total": 0})
    session = _fake_session(resp)
    mocker.patch("aiohttp.ClientSession", return_value=session)

    client = _client()
    result = await client.search_issues("없는병원")
    assert result == []


# T-JIRA-3: 401 응답 → JiraAPIError
@pytest.mark.asyncio
async def test_search_issues_401_raises_auth_error(mocker):
    resp = _fake_response(401, {"message": "Unauthorized"})
    session = _fake_session(resp)
    mocker.patch("aiohttp.ClientSession", return_value=session)

    client = _client()
    with pytest.raises(JiraAPIError) as exc:
        await client.search_issues("병원")
    assert "인증 실패" in str(exc.value) or "JIRA_EMAIL" in str(exc.value)


# T-JIRA-4: 타임아웃 → JiraAPIError
@pytest.mark.asyncio
async def test_search_issues_timeout_raises(mocker):
    import aiohttp as _aiohttp
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.get = MagicMock(side_effect=_aiohttp.ServerTimeoutError())
    mocker.patch("aiohttp.ClientSession", return_value=session)

    client = _client()
    with pytest.raises(JiraAPIError):
        await client.search_issues("병원")


# T-JIRA-5: JQL에 특수문자(대괄호, 따옴표) → URL 인코딩 확인
@pytest.mark.asyncio
async def test_search_issues_jql_contains_hospital_name(mocker):
    """검색 파라미터에 병원명이 포함됐는지 확인 (실제 URL 인코딩은 aiohttp에 위임)."""
    resp = _fake_response(200, {"issues": []})
    session = _fake_session(resp)
    mocker.patch("aiohttp.ClientSession", return_value=session)

    client = _client()
    await client.search_issues("중앙보훈병원")
    call_args = session.get.call_args
    params = call_args.kwargs.get("params") or call_args[1].get("params", {})
    jql = params.get("jql", "")
    assert "중앙보훈병원" in jql
    assert "PMFM" in jql


def test_auth_header_is_basic_base64():
    """Authorization 헤더가 Basic base64(email:token) 형태인지 확인."""
    client = _client(email="user@example.com", token="SECRET")
    expected_raw = base64.b64encode(b"user@example.com:SECRET").decode()
    assert client._auth_header == f"Basic {expected_raw}"
    assert "SECRET" not in client._auth_header.replace("Basic ", "")  # 평문 아님


def test_base_url_trailing_slash_stripped():
    client = JiraClient(
        base_url="https://connecteve.atlassian.net/",
        email="a@b.com",
        api_token="T",
    )
    assert not client._base_url.endswith("/")
