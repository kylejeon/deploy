"""백엔드 Product API 연동 — Lookup API 캐싱 + Product POST.

dev-spec F8-4 (Lookup), F8-5 (POST).

사용법:
    client = ProductRegistrationClient(base_url, token, api_env, host_header)
    success, fail = await client.register_products(job, issues)

인증: x-auth-token 헤더 (site_register 단계에서 받은 토큰 재사용).
캐싱: 동일 단계 실행 내에서 Lookup API 결과를 메모리에 유지.
멱등성: 409 또는 duplicate/already/exist 키워드 포함 4xx → already_exists 정상 처리.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from autodeploy.jira_description_parser import ProductInfo, parse_product_info
from autodeploy.gs1_parser import parse_gs1
from autodeploy.models import Job


class ProductAPIError(RuntimeError):
    pass


def _is_duplicate_error(status: int, body_text: str) -> bool:
    """site_registration._is_duplicate_error와 동일 로직."""
    if status == 409:
        return True
    if 400 <= status < 500:
        low = body_text.lower()
        return any(kw in low for kw in ("duplicate", "already", "exists", "exist"))
    return False


def _latest_version_id(items: list[dict[str, Any]]) -> str | None:
    """data[] 중 semver 최신 항목의 id. version 필드 없으면 None.

    D8-6: tuple(int(p) for p in version.split('.')) 비교. 외부 의존성 없음.
    """
    if not items:
        return None

    def _ver_key(item: dict) -> tuple:
        ver = item.get("version", "")
        try:
            return tuple(int(p) for p in str(ver).split(".") if p.isdigit())
        except (ValueError, AttributeError):
            return ()

    best = max(items, key=_ver_key)
    return best.get("id")


@dataclass
class ProductRegistrationClient:
    base_url: str
    token: str
    api_env: str = "dev"
    host_header: str | None = None
    timeout_s: float = 15.0

    # 인스턴스 캐시 (동일 실행 내 재사용 — DB에 저장 X)
    _site_id_cache: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _product_code_cache: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _gateway_version_cache: str | None = field(default=None, init=False, repr=False)
    _ai_engine_version_cache: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _is_version_cache: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def _make_headers(self, *, content_type: str | None = None) -> dict[str, str]:
        h: dict[str, str] = {
            "x-auth-token": self.token,
            "x-api-env": self.api_env,
            "Accept": "application/json",
        }
        if content_type:
            h["Content-Type"] = content_type
        if self.host_header:
            h["Host"] = self.host_header
        return h

    def _url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}{path}"

    async def _get_json(self, session: aiohttp.ClientSession, path: str, **params) -> Any:
        url = self._url(path)
        async with session.get(url, headers=self._make_headers(), params=params or None) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise ProductAPIError(f"GET {path} 실패 (HTTP {resp.status}): {text[:200]}")
            try:
                return json.loads(text)
            except ValueError as exc:
                raise ProductAPIError(f"GET {path} JSON 파싱 실패: {exc}") from exc

    async def lookup_site_id(
        self, session: aiohttp.ClientSession, hospital_code: str
    ) -> str | None:
        """GET /api/v1/sites → data.content[]에서 name == hospital_code 매칭."""
        if hospital_code in self._site_id_cache:
            return self._site_id_cache[hospital_code]
        body = await self._get_json(session, "/api/v1/sites")
        content = body.get("data", {})
        if isinstance(content, dict):
            items = content.get("content", [])
        elif isinstance(content, list):
            items = content
        else:
            items = []
        for item in items:
            if item.get("name") == hospital_code:
                site_id = item.get("id")
                if site_id is not None:
                    self._site_id_cache[hospital_code] = str(site_id)
                    return str(site_id)
        return None

    async def lookup_product_code_id(
        self, session: aiohttp.ClientSession, product_name: str
    ) -> str | None:
        """GET /api/v1/products/product-codes → data[]에서 name == product_name."""
        if product_name in self._product_code_cache:
            return self._product_code_cache[product_name]
        body = await self._get_json(session, "/api/v1/products/product-codes")
        items = body.get("data", [])
        for item in items:
            if item.get("name") == product_name:
                pid = item.get("id")
                if pid is not None:
                    self._product_code_cache[product_name] = str(pid)
                    return str(pid)
        return None

    async def lookup_gateway_version_id(
        self, session: aiohttp.ClientSession
    ) -> str | None:
        """GET /api/v1/products/gateway-version → data[] semver 최신 id."""
        if self._gateway_version_cache is not None:
            return self._gateway_version_cache
        body = await self._get_json(session, "/api/v1/products/gateway-version")
        items = body.get("data", [])
        vid = _latest_version_id(items)
        if vid is not None:
            self._gateway_version_cache = str(vid)
        return self._gateway_version_cache

    async def lookup_ai_engine_version_id(
        self, session: aiohttp.ClientSession, product_name: str
    ) -> str | None:
        """GET /api/v1/products/ai-engine-version?modal=<productName> → 최신 id."""
        if product_name in self._ai_engine_version_cache:
            return self._ai_engine_version_cache[product_name]
        body = await self._get_json(
            session, "/api/v1/products/ai-engine-version", modal=product_name
        )
        items = body.get("data", [])
        vid = _latest_version_id(items)
        if vid is not None:
            self._ai_engine_version_cache[product_name] = str(vid)
            return str(vid)
        return None

    async def lookup_is_version_id(
        self, session: aiohttp.ClientSession, product_name: str
    ) -> str | None:
        """GET /api/v1/products/inference-server-version → 클라이언트 modal 필터 후 최신."""
        if product_name in self._is_version_cache:
            return self._is_version_cache[product_name]
        body = await self._get_json(session, "/api/v1/products/inference-server-version")
        items = body.get("data", [])
        filtered = [it for it in items if it.get("modal") == product_name]
        vid = _latest_version_id(filtered)
        if vid is not None:
            self._is_version_cache[product_name] = str(vid)
            return str(vid)
        return None

    async def post_product(
        self, session: aiohttp.ClientSession, body: dict[str, Any]
    ) -> str:
        """POST /api/v1/products. 'created' | 'already_exists'. 실패 시 ProductAPIError."""
        url = self._url("/api/v1/products")
        async with session.post(
            url,
            json=body,
            headers=self._make_headers(content_type="application/json"),
        ) as resp:
            text = await resp.text()
            if 200 <= resp.status < 300:
                return "created"
            if _is_duplicate_error(resp.status, text):
                return "already_exists"
            raise ProductAPIError(
                f"POST /api/v1/products 실패 (HTTP {resp.status}): {text[:200]}"
            )

    async def register_products(
        self,
        job: Job,
        issues: list[dict],
        *,
        on_event: Any = None,
    ) -> tuple[int, int]:
        """이슈 리스트를 순회하며 제품 등록. (성공건수, 실패건수) 반환.

        on_event(level, message) — 단계별 이벤트 콜백 (None이면 무시).
        개별 이슈 실패는 warn으로 기록하고 다음 이슈 계속 (부분 성공 허용).
        """
        success_count = 0
        fail_count = 0
        timeout = aiohttp.ClientTimeout(total=self.timeout_s)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            for issue in issues:
                fields = issue.get("fields", {})
                summary = fields.get("summary", "")
                description_adf = fields.get("description")

                info = parse_product_info(summary, description_adf)
                if info.error:
                    fail_count += 1
                    if on_event:
                        await on_event(
                            "warn",
                            f"[{info.product_name or summary!r}] 파싱 실패: {info.error}",
                        )
                    continue

                product_name = info.product_name

                # warn 이벤트 (알 수 없는 licenseType, 복수 GS1 등)
                for w in info.warnings:
                    if on_event:
                        await on_event("warn", f"[{product_name}] {w}")

                # --- Lookup IDs ---
                try:
                    site_id = await self.lookup_site_id(session, job.hospital_code)
                    if site_id is None:
                        fail_count += 1
                        if on_event:
                            await on_event(
                                "warn",
                                f"[{product_name}] siteId 매칭 없음 "
                                f"(hospital_code={job.hospital_code!r})",
                            )
                        continue

                    product_code_id = await self.lookup_product_code_id(session, product_name)
                    if product_code_id is None:
                        fail_count += 1
                        if on_event:
                            await on_event(
                                "warn",
                                f"[{product_name}] product-codes에서 "
                                f"{product_name!r} 매칭 없음",
                            )
                        continue

                    gateway_version_id = await self.lookup_gateway_version_id(session)
                    if gateway_version_id is None:
                        fail_count += 1
                        if on_event:
                            await on_event(
                                "warn",
                                f"[{product_name}] gatewayVersionId 조회 실패 (빈 목록)",
                            )
                        continue

                    ai_engine_version_id = await self.lookup_ai_engine_version_id(
                        session, product_name
                    )
                    if ai_engine_version_id is None:
                        fail_count += 1
                        if on_event:
                            await on_event(
                                "warn",
                                f"[{product_name}] aiEngineVersionId 조회 실패 "
                                f"(modal={product_name!r})",
                            )
                        continue

                    is_version_id = await self.lookup_is_version_id(session, product_name)
                    if is_version_id is None:
                        fail_count += 1
                        if on_event:
                            await on_event(
                                "warn",
                                f"[{product_name}] isVersionId 조회 실패 "
                                f"(modal={product_name!r})",
                            )
                        continue

                except ProductAPIError as exc:
                    fail_count += 1
                    if on_event:
                        await on_event("warn", f"[{product_name}] Lookup API 오류: {exc}")
                    continue

                # --- POST ---
                body = {
                    "siteId": site_id,
                    "productCodeId": product_code_id,
                    "gatewayVersionId": gateway_version_id,
                    "aiEngineVersionId": ai_engine_version_id,
                    "isVersionId": is_version_id,
                    "serialNumber": info.serial_number,
                    "pcSerialNumber": info.pc_serial_number,
                    "variant": info.variant,
                    "mfgDate": info.mfg_date,
                    "license": {
                        "licenseType": info.license_type,
                        "startDate": info.start_date,
                        "endDate": info.end_date,
                        "licenseLimit": info.license_limit,
                    },
                }
                try:
                    result = await self.post_product(session, body)
                except ProductAPIError as exc:
                    fail_count += 1
                    if on_event:
                        await on_event("warn", f"[{product_name}] POST 실패: {exc}")
                    continue

                success_count += 1
                if on_event:
                    if result == "already_exists":
                        await on_event("info", f"[{product_name}] 이미 등록됨 (멱등)")
                    else:
                        await on_event("info", f"[{product_name}] 등록 완료")

        return success_count, fail_count
