"""설치 직후 frontend(site-management) API로 자동 병원 등록.

on-premise 케이스: 타겟 서버의 Frontend NodePort에 직접 HTTP. Postman 캡쳐와
동일하게 `Host: localhost` 헤더를 강제로 보낸다 — Traefik이 Host 기반 라우팅을
하기 때문에 그냥 IP로 치면 404가 나는 환경이 있다.

엔드포인트:
- POST /api/v1/auth/sign-in (application/x-www-form-urlencoded: email, password)
- POST /api/v1/sites (application/json: name=hospital_code, displayName, ...)

토큰은 응답 헤더 `x-auth-token` 또는 JSON body의 token/accessToken에서 추출.
이미 등록된 병원(name 중복)은 멱등 처리 — 409 또는 'duplicate'/'already' 키워드
포함 4xx는 성공으로 간주한다 (재시도/재설치 케이스 흔함).
"""
from __future__ import annotations

import json
from typing import Any

import aiohttp


class SiteAPIError(RuntimeError):
    pass


# install command의 deployment_type → site API의 installationType 값.
# 시리얼라이저가 자유 문자열을 받으므로 백엔드 enum과 정확히 일치해야 함.
INSTALLATION_TYPE_MAP: dict[str, str] = {
    "on-premise": "ON_PREMISE",
    "hybrid-with-ai": "Hybrid On-Premise AI",
    "hybrid-without-ai": "Hybrid Cloud AI",
}


def _extract_token(headers: Any, body_text: str) -> str | None:
    """응답에서 x-auth-token 값을 찾아낸다. 헤더 우선, 없으면 JSON body 탐색."""
    # aiohttp의 CIMultiDictProxy는 case-insensitive
    token = headers.get("x-auth-token") or headers.get("X-Auth-Token")
    if token:
        return token
    try:
        body = json.loads(body_text)
    except (ValueError, TypeError):
        return None
    if not isinstance(body, dict):
        return None
    for key in ("token", "accessToken", "x-auth-token", "xAuthToken"):
        val = body.get(key)
        if isinstance(val, str) and val:
            return val
    data = body.get("data")
    if isinstance(data, dict):
        for key in ("token", "accessToken", "x-auth-token", "xAuthToken"):
            val = data.get(key)
            if isinstance(val, str) and val:
                return val
    return None


def _is_duplicate_error(status: int, body_text: str) -> bool:
    if status == 409:
        return True
    if status == 400:
        low = body_text.lower()
        return any(kw in low for kw in ("duplicate", "already", "exists", "exist"))
    return False


async def login_to_site(
    session: aiohttp.ClientSession,
    base_url: str,
    email: str,
    password: str,
    *,
    host_header: str = "localhost",
) -> str:
    """sign-in 호출 → x-auth-token 반환. 실패 시 SiteAPIError.

    base_url: 'http://<target_ip>:<frontend_port>' 형태. trailing slash 무관.
    host_header: Traefik 라우팅용. on-premise Postman 캡쳐와 동일하게 'localhost' 기본.
    """
    url = f"{base_url.rstrip('/')}/api/v1/auth/sign-in"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "Host": host_header,
    }
    data = {"email": email, "password": password}
    try:
        async with session.post(url, data=data, headers=headers) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise SiteAPIError(
                    f"sign-in 실패 (HTTP {resp.status}): {text[:200]}"
                )
            token = _extract_token(resp.headers, text)
            if not token:
                raise SiteAPIError(
                    "sign-in 응답에서 x-auth-token을 찾지 못함 "
                    f"(headers={list(resp.headers.keys())}, body 앞부분={text[:120]})"
                )
            return token
    except aiohttp.ClientError as exc:
        raise SiteAPIError(f"sign-in 요청 실패: {exc}") from exc


async def register_site(
    session: aiohttp.ClientSession,
    base_url: str,
    token: str,
    *,
    code: str,
    display_name: str,
    address: str,
    installation_type: str,
    host_header: str = "localhost",
) -> str:
    """POST /api/v1/sites. 'created' | 'already_exists' 반환. 실패 시 SiteAPIError.

    멱등성: 동일 hospital_code로 재등록 호출이 와도 안전. 409 또는 'duplicate'
    포함 400은 already_exists로 정상 처리 — 재시도/재설치 흔함.
    """
    url = f"{base_url.rstrip('/')}/api/v1/sites"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-auth-token": token,
        "Host": host_header,
    }
    body = {
        "name": code,
        "displayName": display_name,
        "address": address,
        "adminName": "",
        "contactPhone": "",
        "adminEmail": "",
        "installationType": installation_type,
        "comment": "",
    }
    try:
        async with session.post(url, json=body, headers=headers) as resp:
            text = await resp.text()
            if 200 <= resp.status < 300:
                return "created"
            if _is_duplicate_error(resp.status, text):
                return "already_exists"
            raise SiteAPIError(
                f"sites 등록 실패 (HTTP {resp.status}): {text[:200]}"
            )
    except aiohttp.ClientError as exc:
        raise SiteAPIError(f"sites 등록 요청 실패: {exc}") from exc


async def register_hospital(
    base_url: str,
    *,
    email: str,
    password: str,
    code: str,
    display_name: str,
    address: str,
    installation_type: str,
    host_header: str = "localhost",
    timeout_s: float = 15.0,
) -> str:
    """원샷 헬퍼: 세션 생성 → 로그인 → 등록. workflow가 직접 호출하는 진입점.

    workflow가 이 함수만 알면 되도록 캡슐화 — 테스트도 이 함수만 monkeypatch.
    """
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        token = await login_to_site(
            session, base_url, email, password, host_header=host_header,
        )
        return await register_site(
            session, base_url, token,
            code=code, display_name=display_name, address=address,
            installation_type=installation_type, host_header=host_header,
        )
