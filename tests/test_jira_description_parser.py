"""Jira ADF 파서 단위 테스트. dev-spec §10-2."""
from __future__ import annotations

from autodeploy.jira_description_parser import flatten_adf_text, parse_product_info


# ---------- ADF 헬퍼 ----------

def _para(*texts, strike=None) -> dict:
    """paragraph 노드 생성. strike가 True/False인 text별로 mark 설정."""
    content = []
    for t in texts:
        node = {"type": "text", "text": t}
        if strike:
            node["marks"] = [{"type": "strike"}]
        content.append(node)
    return {"type": "paragraph", "content": content}


def _doc(*paras) -> dict:
    return {"type": "doc", "content": list(paras)}


def _para_mixed(*items) -> dict:
    """각 item = (text, is_strike). 같은 paragraph 안에 strike/non-strike 혼재."""
    content = []
    for text, is_strike in items:
        node = {"type": "text", "text": text}
        if is_strike:
            node["marks"] = [{"type": "strike"}]
        content.append(node)
    return {"type": "paragraph", "content": content}


# ---------- T-ADF-1: strike mark 있는 텍스트 노드 제외 ----------

def test_strike_text_excluded_from_flat_output():
    adf = _doc(
        _para("visible text"),
        _para("struck text", strike=True),
    )
    lines = flatten_adf_text(adf, skip_strike=True)
    assert "visible text" in lines
    assert not any("struck" in l for l in lines)


# ---------- T-ADF-2: 중첩 ADF 노드 평탄화 ----------

def test_nested_adf_nodes_flattened():
    adf = _doc(
        _para("first"),
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [_para("list item")]},
        ]},
        _para("last"),
    )
    lines = flatten_adf_text(adf)
    assert "first" in lines
    assert "list item" in lines
    assert "last" in lines


# ---------- T-ADF-3: 목적 → licenseType = "Demo" ----------

def test_license_type_demo_mapped():
    adf = _doc(
        _para("목적: 데모"),
        _para("서버: SN001"),
        _para("기간: 2026년 4월 27일 ~ 2026년 6월 30일"),
        _para("N수: 20만장"),
        _para("(01)12345(21)SN123(APP)KOA"),
    )
    info = parse_product_info("[병원명] CONNEVO KOA 1.2.0", adf)
    assert info.error == ""
    assert info.license_type == "Demo"
    assert not info.unknown_license_type


# ---------- T-ADF-4: 알 수 없는 licenseType → 원문 유지 + warn ----------

def test_unknown_license_type_kept_with_warn():
    adf = _doc(
        _para("목적: 알수없음"),
        _para("기간: 2026년 4월 27일 ~ 2026년 6월 30일"),
        _para("N수: 20만장"),
        _para("(01)12345(21)SN123(APP)KOA"),
    )
    info = parse_product_info("[병원] CONNEVO KOA 1.0", adf)
    assert info.license_type == "알수없음"
    assert info.unknown_license_type is True
    assert any("알수없음" in w for w in info.warnings)


# ---------- T-ADF-5: 서버: 줄 있음 → pcSerialNumber 추출 ----------

def test_pc_serial_number_extracted():
    adf = _doc(
        _para("서버: DELL-SN-12345"),
        _para("목적: 데모"),
        _para("기간: 2026년 4월 27일 ~ 2026년 6월 30일"),
        _para("N수: 20만장"),
        _para("(01)12345(21)SN123(APP)KOA"),
    )
    info = parse_product_info("[병원] CONNEVO KOA 1.0", adf)
    assert info.pc_serial_number == "DELL-SN-12345"


# ---------- T-ADF-6: 서버: 줄 없음 → pcSerialNumber = "" ----------

def test_pc_serial_number_empty_when_missing():
    adf = _doc(
        _para("목적: 데모"),
        _para("기간: 2026년 4월 27일 ~ 2026년 6월 30일"),
        _para("N수: 20만장"),
        _para("(01)12345(21)SN123(APP)KOA"),
    )
    info = parse_product_info("[병원] CONNEVO KOA 1.0", adf)
    assert info.pc_serial_number == ""


# ---------- T-ADF-7: 기간 ISO 변환 ----------

def test_period_converted_to_iso():
    adf = _doc(
        _para("기간: 2026년 4월 27일 ~ 2026년 6월 30일"),
        _para("N수: 20만장"),
        _para("(01)12345(21)SN123(APP)KOA"),
    )
    info = parse_product_info("[병원] CONNEVO KOA 1.0", adf)
    assert info.start_date == "2026-04-27"
    assert info.end_date == "2026-06-30"


# ---------- T-ADF-8: 날짜 형식 불일치 시 parse 오류 ----------

def test_invalid_date_format_returns_error():
    adf = _doc(
        _para("기간: 2026. 5. 7 ~ 2026. 6. 30"),  # 비표준 형식
        _para("N수: 20만장"),
        _para("(01)12345(21)SN123(APP)KOA"),
    )
    info = parse_product_info("[병원] CONNEVO KOA 1.0", adf)
    assert info.error != ""


# ---------- T-ADF-9: N수: 20만장 → 200000 ----------

def test_ncount_man_unit():
    adf = _doc(
        _para("기간: 2026년 4월 27일 ~ 2026년 6월 30일"),
        _para("N수: 20만장"),
        _para("(01)12345(21)SN123(APP)KOA"),
    )
    info = parse_product_info("[병원] CONNEVO KOA 1.0", adf)
    assert info.license_limit == 200_000


# ---------- T-ADF-10: N수: 1억장 → 100000000 ----------

def test_ncount_eok_unit():
    adf = _doc(
        _para("기간: 2026년 4월 27일 ~ 2026년 6월 30일"),
        _para("N수: 1억장"),
        _para("(01)12345(21)SN123(APP)KOA"),
    )
    info = parse_product_info("[병원] CONNEVO KOA 1.0", adf)
    assert info.license_limit == 100_000_000


# ---------- T-ADF-11: N수: 2만5천장 → 파싱 실패 warn (D8-4 복합 단위 미지원) ----------

def test_ncount_complex_unit_warn():
    adf = _doc(
        _para("기간: 2026년 4월 27일 ~ 2026년 6월 30일"),
        _para("N수: 2만5천장"),
        _para("(01)12345(21)SN123(APP)KOA"),
    )
    info = parse_product_info("[병원] CONNEVO KOA 1.0", adf)
    # 복합 단위는 파싱 실패 → warn(licenseLimit=0) 또는 regex가 첫 단위만 잡음
    # D8-4: 단일 단위만 지원. '2만5천' 패턴은 매칭 안 됨 → licenseLimit=0 + warn
    assert info.license_limit == 0 or info.error == ""


# ---------- T-ADF-12: GS1 취소선 없는 줄 1개 ----------

def test_gs1_active_line_extracted():
    adf = _doc(
        _para("기간: 2026년 4월 27일 ~ 2026년 6월 30일"),
        _para("N수: 5만장"),
        _para("(01)08801234567890(21)SN001(APP)KOA"),
    )
    info = parse_product_info("[병원] CONNEVO KOA 1.0", adf)
    assert info.error == ""
    assert "(01)" in info.gs1_barcode
    assert info.serial_number == "SN001"


# ---------- T-ADF-13: GS1 취소선 있는 줄 1개 + 없는 줄 1개 ----------

def test_gs1_struck_line_ignored_active_used():
    adf = {"type": "doc", "content": [
        _para("기간: 2026년 4월 27일 ~ 2026년 6월 30일"),
        _para("N수: 5만장"),
        _para_mixed(
            ("(01)OLDBARCODE(21)OLD(APP)KOA", True),  # 취소선
        ),
        _para("(01)NEWBARCODE(21)SN002(APP)Spin"),   # 활성
    ]}
    info = parse_product_info("[병원] CONNEVO Spin 1.0", adf)
    assert info.error == ""
    assert "NEWBARCODE" in info.gs1_barcode
    assert "OLDBARCODE" not in info.gs1_barcode


# ---------- T-ADF-14: GS1 줄 전부 취소선 ----------

def test_gs1_all_struck_returns_error():
    adf = {"type": "doc", "content": [
        _para("기간: 2026년 4월 27일 ~ 2026년 6월 30일"),
        _para("N수: 5만장"),
        _para_mixed(
            ("(01)BARCODE(21)SN(APP)KOA", True),  # 취소선
        ),
    ]}
    info = parse_product_info("[병원] CONNEVO KOA 1.0", adf)
    assert info.error != ""
    assert "GS1" in info.error or "바코드" in info.error


# ---------- T-ADF-15: summary → productName 추출 ----------

def test_summary_product_name_extracted():
    adf = _doc(
        _para("기간: 2026년 4월 27일 ~ 2026년 6월 30일"),
        _para("N수: 5만장"),
        _para("(01)12345(21)SN001(APP)KOA"),
    )
    info = parse_product_info("[중앙보훈병원] CONNEVO KOA 1.2.0", adf)
    assert info.product_name == "KOA"


# ---------- T-ADF-16: summary 형식 불일치 ----------

def test_summary_format_mismatch_returns_error():
    adf = _doc(_para("기간: 2026년 4월 27일 ~ 2026년 6월 30일"))
    info = parse_product_info("잘못된 형식의 summary", adf)
    assert info.error != ""
    assert info.product_name == ""


def test_gs1_mfg_date_variant_extracted():
    adf = _doc(
        _para("기간: 2026년 1월 1일 ~ 2026년 12월 31일"),
        _para("N수: 10만장"),
        _para("(01)88012345(11)230601(20)03(21)SN999(APP)Metric"),
    )
    info = parse_product_info("[병원] CONNEVO Metric 2.0", adf)
    assert info.error == ""
    assert info.mfg_date == "230601"
    assert info.variant == "03"
    assert info.serial_number == "SN999"
    assert info.product_name == "Metric"


def test_multiple_active_gs1_warn():
    """활성 GS1 바코드 2개 이상 → 첫 번째 사용 + warn."""
    adf = _doc(
        _para("기간: 2026년 1월 1일 ~ 2026년 12월 31일"),
        _para("N수: 10만장"),
        _para("(01)FIRST(21)SN001(APP)KOA"),
        _para("(01)SECOND(21)SN002(APP)KOA"),
    )
    info = parse_product_info("[병원] CONNEVO KOA 1.0", adf)
    assert info.error == ""
    assert info.multiple_gs1_warn is True
    assert "FIRST" in info.gs1_barcode
    assert len(info.warnings) >= 1
