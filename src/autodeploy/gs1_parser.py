"""GS1 바코드 문자열 파싱. `(NN)value` segment 분리.

dev-spec F8-3.8: 추출 대상 — (11) mfgDate, (20) variant, (21) serialNumber,
(APP) productName. 알 수 없는 segment는 무시 (forward compatibility).

segment 형식: `(AI)value(AI)value...`
AI는 2자리 숫자 또는 알파벳 문자열 (예: APP, Site, PV).
value 끝 경계 = 다음 `(` 또는 문자열 끝.
"""
from __future__ import annotations

import re


_SEGMENT_PATTERN = re.compile(r"\(([^)]+)\)([^(]*)")

_KNOWN_AIS: frozenset[str] = frozenset({"11", "20", "21", "APP"})

_AI_TO_KEY: dict[str, str] = {
    "11": "mfgDate",
    "20": "variant",
    "21": "serialNumber",
    "APP": "productName",
}


def parse_gs1(barcode: str) -> dict[str, str]:
    """GS1 바코드에서 알려진 Application Identifier segment를 추출.

    알 수 없는 AI는 결과에서 제외. 빈 문자열 입력이면 빈 dict 반환.
    """
    result: dict[str, str] = {}
    for m in _SEGMENT_PATTERN.finditer(barcode):
        ai = m.group(1)
        value = m.group(2).strip()
        key = _AI_TO_KEY.get(ai)
        if key is not None:
            result[key] = value
    return result
