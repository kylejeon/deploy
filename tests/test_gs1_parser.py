"""GS1 바코드 파서 단위 테스트. dev-spec §10-1."""
from __future__ import annotations

from autodeploy.gs1_parser import parse_gs1


# T-GS1-1: 전형적인 GS1 바코드에서 알려진 segment 추출
def test_typical_gs1_barcode_extracts_all_known_segments():
    barcode = "(01)08801234567890(11)230101(20)01(21)SN-ABC123(APP)KOA"
    result = parse_gs1(barcode)
    assert result["mfgDate"] == "230101"
    assert result["variant"] == "01"
    assert result["serialNumber"] == "SN-ABC123"
    assert result["productName"] == "KOA"


# T-GS1-2: 알 수 없는 segment 포함 시 무시하고 알려진 것만 반환
def test_unknown_segments_are_ignored():
    barcode = "(01)08801234567890(Site)HOSP(PV)1.0(21)SN001(APP)Spin(AM)extra"
    result = parse_gs1(barcode)
    assert "productName" in result
    assert result["productName"] == "Spin"
    assert result["serialNumber"] == "SN001"
    assert "Site" not in result
    assert "PV" not in result
    assert "AM" not in result


# T-GS1-3: 빈 문자열 입력 시 빈 dict 반환
def test_empty_string_returns_empty_dict():
    assert parse_gs1("") == {}


# T-GS1-4: segment 값에 특수문자 포함 시 정상 파싱
def test_segment_value_with_special_chars():
    barcode = "(21)SN-123/456_ABC(APP)Metric"
    result = parse_gs1(barcode)
    assert result["serialNumber"] == "SN-123/456_ABC"
    assert result["productName"] == "Metric"


def test_gs1_without_app_segment_returns_partial():
    barcode = "(01)12345(11)230601(20)02(21)XYZ"
    result = parse_gs1(barcode)
    assert result["mfgDate"] == "230601"
    assert result["variant"] == "02"
    assert result["serialNumber"] == "XYZ"
    assert "productName" not in result


def test_gs1_only_app_segment():
    result = parse_gs1("(APP)KOA")
    assert result == {"productName": "KOA"}


def test_gs1_duplicate_segment_last_wins():
    # 동일 AI가 두 번 나오면 나중 값으로 덮임 (regex는 순서대로 처리)
    result = parse_gs1("(21)FIRST(21)SECOND")
    assert result["serialNumber"] == "SECOND"
