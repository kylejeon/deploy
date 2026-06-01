"""Jira REST API v3 클라이언트 (Basic auth, aiohttp).

dev-spec F8-2: Jira 이슈 검색.

사용법:
    client = JiraClient(base_url, email, api_token, project_key)
    issues = await client.search_issues(hospital_display_name)

인증: HTTP Basic — username=JIRA_EMAIL, password=JIRA_API_TOKEN
토큰은 메모리에만 두고 로그/Slack/DB에 절대 노출하지 않음.
"""
from __future__ import annotations

import base64
from typing import Any

import aiohttp


class JiraAPIError(RuntimeError):
    pass


class JiraClient:
    def __init__(
        self,
        base_url: str,
        email: str,
        api_token: str,
        project_key: str = "PMFM",
        timeout_s: float = 15.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._project_key = project_key
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        # Basic auth 헤더를 미리 계산 (토큰은 헤더 값으로만 사용, 평문 로그 방지)
        raw = f"{email}:{api_token}".encode()
        self._auth_header = f"Basic {base64.b64encode(raw).decode()}"

    async def search_issues(
        self,
        hospital_display_name: str,
        *,
        fields: list[str] | None = None,
        max_results: int = 50,
        start_at: int = 0,
    ) -> list[dict[str, Any]]:
        """JQL로 Jira 이슈 검색. issues 리스트 반환.

        첫 페이지만 반환 (D8-3). start_at 파라미터는 향후 전체 순회로 확장 시 사용.
        0건이면 빈 리스트 반환 (JiraAPIError 아님 — 호출자가 0건을 step failure로 처리).
        인증 오류(401) 등 API 오류는 JiraAPIError로 raise.
        """
        jql = f'project = "{self._project_key}" AND summary ~ "[{hospital_display_name}]"'
        params: dict[str, Any] = {
            "jql": jql,
            "maxResults": max_results,
            "startAt": start_at,
            "fields": ",".join(fields) if fields else "summary,description",
        }
        url = f"{self._base_url}/rest/api/3/search"
        headers = {
            "Authorization": self._auth_header,
            "Accept": "application/json",
        }
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, params=params, headers=headers) as resp:
                    text = await resp.text()
                    if resp.status == 401:
                        raise JiraAPIError(
                            "Jira 인증 실패 — JIRA_EMAIL/JIRA_API_TOKEN 확인"
                        )
                    if resp.status >= 400:
                        raise JiraAPIError(
                            f"Jira API 오류 (HTTP {resp.status}): {text[:200]}"
                        )
                    try:
                        import json
                        body = json.loads(text)
                    except ValueError as exc:
                        raise JiraAPIError(f"Jira 응답 JSON 파싱 실패: {exc}") from exc
                    return body.get("issues", [])
        except aiohttp.ClientError as exc:
            raise JiraAPIError(f"Jira API 요청 실패: {exc}") from exc
