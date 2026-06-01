"""site_registration 모듈 단위 테스트.

aiohttp.ClientSession은 무겁고 네트워크가 필요하니, 동일한 async-context
인터페이스를 구현한 FakeSession으로 대체. login/register 함수가 받는 세션
객체는 .post(url, **kwargs) → async-cm-of-response만 만족하면 충분.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from autodeploy.site_registration import (
    INSTALLATION_TYPE_MAP,
    SiteAPIError,
    login_to_site,
    register_site,
)


@dataclass
class FakeResp:
    status: int
    body: str
    headers: dict[str, str] = field(default_factory=dict)

    async def __aenter__(self) -> "FakeResp":
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def text(self) -> str:
        return self.body


class FakeSession:
    """aiohttp.ClientSession 모사 — post()가 async context manager를 반환."""

    def __init__(self, responses: list[FakeResp]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs) -> FakeResp:
        self.calls.append((url, kwargs))
        if not self._responses:
            raise AssertionError(f"unexpected extra POST: {url}")
        return self._responses.pop(0)


# ---------- login_to_site ----------

@pytest.mark.asyncio
async def test_login_extracts_token_from_response_header():
    session = FakeSession([
        FakeResp(status=200, body='{"ok":true}', headers={"x-auth-token": "TOK-HEADER"}),
    ])
    token = await login_to_site(
        session, "http://10.0.0.1:31435", "admin@x.com", "pw",
    )
    assert token == "TOK-HEADER"
    url, kwargs = session.calls[0]
    assert url == "http://10.0.0.1:31435/api/v1/auth/sign-in"
    assert kwargs["data"] == {"email": "admin@x.com", "password": "pw"}
    assert kwargs["headers"]["Host"] == "localhost"
    assert kwargs["headers"]["Content-Type"] == "application/x-www-form-urlencoded"


@pytest.mark.asyncio
async def test_login_falls_back_to_json_body_token_field():
    session = FakeSession([
        FakeResp(status=200, body='{"token":"TOK-BODY"}'),
    ])
    token = await login_to_site(session, "http://x", "e", "p")
    assert token == "TOK-BODY"


@pytest.mark.asyncio
async def test_login_falls_back_to_nested_data_accessToken():
    session = FakeSession([
        FakeResp(status=200, body='{"data":{"accessToken":"TOK-NESTED"}}'),
    ])
    token = await login_to_site(session, "http://x", "e", "p")
    assert token == "TOK-NESTED"


@pytest.mark.asyncio
async def test_login_raises_on_4xx():
    session = FakeSession([
        FakeResp(status=401, body='{"error":"bad credentials"}'),
    ])
    with pytest.raises(SiteAPIError) as exc:
        await login_to_site(session, "http://x", "e", "p")
    assert "401" in str(exc.value)
    assert "bad credentials" in str(exc.value)


@pytest.mark.asyncio
async def test_login_raises_when_no_token_found():
    session = FakeSession([
        FakeResp(status=200, body='{"unrelated":"field"}'),
    ])
    with pytest.raises(SiteAPIError) as exc:
        await login_to_site(session, "http://x", "e", "p")
    assert "토큰" in str(exc.value) or "x-auth-token" in str(exc.value)


@pytest.mark.asyncio
async def test_login_strips_trailing_slash_from_base_url():
    session = FakeSession([
        FakeResp(status=200, body="", headers={"x-auth-token": "T"}),
    ])
    await login_to_site(session, "http://x:1234/", "e", "p")
    assert session.calls[0][0] == "http://x:1234/api/v1/auth/sign-in"


@pytest.mark.asyncio
async def test_login_custom_host_header_overrides_localhost():
    session = FakeSession([
        FakeResp(status=200, body="", headers={"x-auth-token": "T"}),
    ])
    await login_to_site(session, "http://x", "e", "p", host_header="hospital.local")
    assert session.calls[0][1]["headers"]["Host"] == "hospital.local"


# ---------- register_site ----------

@pytest.mark.asyncio
async def test_register_site_sends_correct_body_and_headers():
    session = FakeSession([FakeResp(status=201, body='{"id":42}')])
    result = await register_site(
        session, "http://x:31435", "TOK",
        code="cmc-ep",
        display_name="은평성모병원",
        address="서울시 은평구",
        installation_type="ON_PREMISE",
    )
    assert result == "created"
    url, kwargs = session.calls[0]
    assert url == "http://x:31435/api/v1/sites"
    assert kwargs["headers"]["x-auth-token"] == "TOK"
    assert kwargs["headers"]["Host"] == "localhost"
    assert kwargs["json"] == {
        "name": "cmc-ep",
        "displayName": "은평성모병원",
        "address": "서울시 은평구",
        "adminName": "",
        "contactPhone": "",
        "adminEmail": "",
        "installationType": "ON_PREMISE",
        "comment": "",
    }


@pytest.mark.asyncio
async def test_register_site_treats_409_as_already_exists():
    session = FakeSession([FakeResp(status=409, body='{"error":"duplicate"}')])
    result = await register_site(
        session, "http://x", "TOK",
        code="HOSP01", display_name="병원", address="",
        installation_type="ON_PREMISE",
    )
    assert result == "already_exists"


@pytest.mark.asyncio
async def test_register_site_treats_400_with_duplicate_keyword_as_already_exists():
    session = FakeSession([FakeResp(status=400, body='site already exists')])
    result = await register_site(
        session, "http://x", "TOK",
        code="HOSP01", display_name="병원", address="",
        installation_type="ON_PREMISE",
    )
    assert result == "already_exists"


@pytest.mark.asyncio
async def test_register_site_raises_on_other_4xx():
    session = FakeSession([FakeResp(status=400, body='{"error":"validation"}')])
    with pytest.raises(SiteAPIError) as exc:
        await register_site(
            session, "http://x", "TOK",
            code="HOSP01", display_name="병원", address="",
            installation_type="ON_PREMISE",
        )
    assert "400" in str(exc.value)


@pytest.mark.asyncio
async def test_register_site_raises_on_5xx():
    session = FakeSession([FakeResp(status=500, body='internal error')])
    with pytest.raises(SiteAPIError):
        await register_site(
            session, "http://x", "TOK",
            code="HOSP01", display_name="병원", address="",
            installation_type="ON_PREMISE",
        )


# ---------- installation type mapping ----------

def test_installation_type_map_covers_all_deployment_types():
    assert INSTALLATION_TYPE_MAP["on-premise"] == "ON_PREMISE"
    assert INSTALLATION_TYPE_MAP["hybrid-with-ai"] == "Hybrid On-Premise AI"
    assert INSTALLATION_TYPE_MAP["hybrid-without-ai"] == "Hybrid Cloud AI"
