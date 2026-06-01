"""Jira ADF(Atlassian Document Format) JSON → 구조화된 ProductInfo 변환.

dev-spec F8-3.1 ~ F8-3.9 구현.

주요 함수:
  flatten_adf_text(adf_node, skip_strike=True) -> list[str]
      ADF 노드 트리를 줄 단위 텍스트 리스트로 평탄화.
      skip_strike=True면 `strike` mark가 있는 텍스트 노드를 건너뜀.

  parse_product_info(summary, description_adf) -> ProductInfo
      summary + description ADF에서 제품 정보 추출.
      필수 필드가 없거나 파싱 실패하면 ProductInfo.error에 원인을 담아 반환.
      호출자가 error를 확인해야 함.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# summary → productName 패턴: `[병원명] CONNEVO <productName> <version>`
_SUMMARY_PATTERN = re.compile(r"\[.*?\]\s+CONNEVO\s+(\S+)")

# 날짜: `2026년 4월 27일` → group (year, month, day)
_DATE_PATTERN = re.compile(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일")

# 기간: `기간: <startDate> ~ <endDate>`
_PERIOD_PATTERN = re.compile(
    r"기간\s*:\s*"
    r"(\d{4}년\s*\d{1,2}월\s*\d{1,2}일)"
    r"\s*~\s*"
    r"(\d{4}년\s*\d{1,2}월\s*\d{1,2}일)"
)

# N수: `N수: 20만장` — 단일 단위만 지원 (D8-4: 복합 단위는 파싱 실패 → warn)
_NCOUNT_PATTERN = re.compile(r"N수\s*:\s*([\d.]+)\s*(만|억|천)")

_UNIT_MAP = {"만": 10_000, "억": 100_000_000, "천": 1_000}

# GS1 바코드: `(01)`로 시작하는 줄
_GS1_START = "(01)"

# 목적 → licenseType 매핑
_LICENSE_TYPE_MAP = {"데모": "Demo"}


@dataclass
class ProductInfo:
    product_name: str = ""
    pc_serial_number: str = ""
    license_type: str = ""
    start_date: str = ""
    end_date: str = ""
    license_limit: int = 0
    gs1_barcode: str = ""
    mfg_date: str = ""
    variant: str = ""
    serial_number: str = ""
    # 알 수 없는 licenseType을 발견했을 때 warn 플래그
    unknown_license_type: bool = False
    # 활성 GS1 바코드가 2개 이상이면 warn 플래그
    multiple_gs1_warn: bool = False
    # 파싱 실패 원인. 비어있으면 성공
    error: str = ""
    warnings: list[str] = field(default_factory=list)


def _has_strike(node: dict[str, Any]) -> bool:
    marks = node.get("marks", [])
    return any(m.get("type") == "strike" for m in marks)


def flatten_adf_text(adf_node: Any, *, skip_strike: bool = True) -> list[str]:
    """ADF 노드 트리를 줄 단위 텍스트 리스트로 평탄화.

    paragraph, bulletList, listItem, text 노드를 재귀 처리.
    paragraph 단위로 줄 구분 (인접 text 노드들을 같은 줄로 합침).
    skip_strike=True면 strike mark가 있는 text 노드를 무시.
    """
    lines: list[str] = []
    _collect_lines(adf_node, lines, skip_strike=skip_strike, current_parts=[])
    return lines


def _collect_lines(
    node: Any,
    lines: list[str],
    *,
    skip_strike: bool,
    current_parts: list[str],
) -> None:
    if not isinstance(node, dict):
        return
    node_type = node.get("type", "")

    if node_type == "text":
        if skip_strike and _has_strike(node):
            return
        text = node.get("text", "")
        if text:
            current_parts.append(text)
        return

    if node_type in ("paragraph", "listItem", "tableCell"):
        # paragraph 단위로 새 줄 시작
        parts: list[str] = []
        for child in node.get("content", []):
            _collect_lines(child, lines, skip_strike=skip_strike, current_parts=parts)
        line = "".join(parts).strip()
        if line:
            lines.append(line)
        return

    # doc, bulletList, orderedList, table, tableRow 등 — 그냥 자식으로 내려감
    for child in node.get("content", []):
        _collect_lines(child, lines, skip_strike=skip_strike, current_parts=current_parts)


def _parse_date(korean_date: str) -> str:
    """'2026년 4월 27일' → '2026-04-27'. 실패 시 빈 문자열."""
    m = _DATE_PATTERN.search(korean_date)
    if not m:
        return ""
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _find_gs1_barcodes(lines: list[str]) -> list[str]:
    """취소선 없는 줄 중 (01)로 시작하는 GS1 바코드 줄 목록 반환."""
    return [ln for ln in lines if ln.startswith(_GS1_START)]


def parse_product_info(summary: str, description_adf: Any) -> ProductInfo:
    """summary + description ADF에서 ProductInfo 추출.

    info.error가 비어있으면 필수 필드 모두 파싱 성공.
    error가 있으면 해당 이슈를 skip해야 함.
    """
    info = ProductInfo()

    # summary → productName
    m = _SUMMARY_PATTERN.search(summary or "")
    if not m:
        info.error = f"summary 형식 불일치 — productName 추출 불가: {summary!r}"
        return info
    info.product_name = m.group(1)

    if not description_adf:
        info.error = "description ADF가 없음"
        return info

    # strike mark 있는 텍스트 포함 전체 줄 (GS1 필터링용)
    all_lines_no_strike = flatten_adf_text(description_adf, skip_strike=True)

    # strike 포함 전체 (GS1 바코드 줄 중 어떤 게 활성인지 확인용)
    all_lines_with_strike: list[str] = []
    _collect_lines_include_strike(description_adf, all_lines_with_strike)

    # --- 목적 (licenseType) ---
    for ln in all_lines_no_strike:
        if ln.startswith("목적"):
            val = re.sub(r"^목적\s*:\s*", "", ln).strip()
            mapped = _LICENSE_TYPE_MAP.get(val)
            if mapped:
                info.license_type = mapped
            else:
                info.license_type = val
                info.unknown_license_type = True
                info.warnings.append(f"알 수 없는 licenseType: {val!r}")
            break

    # --- 서버 (pcSerialNumber) ---
    for ln in all_lines_no_strike:
        if ln.startswith("서버"):
            val = re.sub(r"^서버\s*:\s*", "", ln).strip()
            info.pc_serial_number = val
            break

    # --- 기간 (startDate, endDate) ---
    full_text = "\n".join(all_lines_no_strike)
    pm = _PERIOD_PATTERN.search(full_text)
    if pm:
        info.start_date = _parse_date(pm.group(1))
        info.end_date = _parse_date(pm.group(2))
        if not info.start_date or not info.end_date:
            info.error = f"기간 날짜 파싱 실패: {pm.group(1)!r} ~ {pm.group(2)!r}"
            return info
    else:
        info.error = "기간 정보를 찾을 수 없음 (예: 기간: 2026년 4월 27일 ~ 2026년 6월 30일)"
        return info

    # --- N수 (licenseLimit) ---
    nm = _NCOUNT_PATTERN.search(full_text)
    if nm:
        try:
            count = float(nm.group(1))
            unit_str = nm.group(2)
            unit = _UNIT_MAP[unit_str]
            info.license_limit = int(count * unit)
        except (ValueError, KeyError):
            info.error = f"N수 파싱 실패: {nm.group(0)!r}"
            return info
    else:
        # N수 없는 이슈도 있을 수 있으니 error가 아닌 warn (0으로 둠)
        info.warnings.append("N수 항목을 찾지 못함 — licenseLimit = 0 사용")

    # --- GS1 바코드 ---
    active_barcodes = _find_gs1_barcodes(all_lines_no_strike)
    if not active_barcodes:
        info.error = "description에서 GS1 바코드를 찾지 못함 (활성 (01)로 시작하는 줄 없음)"
        return info
    if len(active_barcodes) > 1:
        info.multiple_gs1_warn = True
        info.warnings.append(
            f"활성 GS1 바코드 {len(active_barcodes)}개 발견 — 첫 번째 사용"
        )
    info.gs1_barcode = active_barcodes[0]

    # GS1 segment 파싱 (gs1_parser에 위임)
    from autodeploy.gs1_parser import parse_gs1
    segments = parse_gs1(info.gs1_barcode)
    info.mfg_date = segments.get("mfgDate", "")
    info.variant = segments.get("variant", "")
    info.serial_number = segments.get("serialNumber", "")

    return info


def _collect_lines_include_strike(node: Any, lines: list[str]) -> None:
    """strike mark 포함 전체 텍스트 평탄화 (GS1 라인 구분을 위한 보조 함수)."""
    if not isinstance(node, dict):
        return
    node_type = node.get("type", "")
    if node_type in ("paragraph", "listItem", "tableCell"):
        parts: list[str] = []
        for child in node.get("content", []):
            _collect_text_only(child, parts)
        line = "".join(parts).strip()
        if line:
            lines.append(line)
        return
    for child in node.get("content", []):
        _collect_lines_include_strike(child, lines)


def _collect_text_only(node: Any, parts: list[str]) -> None:
    if not isinstance(node, dict):
        return
    if node.get("type") == "text":
        parts.append(node.get("text", ""))
        return
    for child in node.get("content", []):
        _collect_text_only(child, parts)
