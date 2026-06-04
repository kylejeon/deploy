from __future__ import annotations

import pytest

from autodeploy.commands import (
    CancelCommand,
    HelpCommand,
    InstallCommand,
    ListCommand,
    ParseError,
    RegisterCommand,
    RetryCommand,
    StatusCommand,
    parse_command,
)

TYPES = frozenset({"on-premise", "hybrid-with-ai", "hybrid-without-ai"})


# ---------- install ----------

def test_install_minimal():
    cmd = parse_command("install 192.168.1.50 --type=on-premise --code=HOSP01", TYPES)
    assert isinstance(cmd, InstallCommand)
    assert cmd.target_ip == "192.168.1.50"
    assert cmd.deployment_type == "on-premise"
    assert cmd.hospital_code == "HOSP01"
    assert cmd.hospital_name is None
    assert cmd.hospital_address is None


def test_install_with_quoted_name_and_address():
    cmd = parse_command(
        'install 10.0.0.1 --type=hybrid-with-ai --code=H01 --name="서울대병원" --address="서울시 종로구"',
        TYPES,
    )
    assert isinstance(cmd, InstallCommand)
    assert cmd.hospital_name == "서울대병원"
    assert cmd.hospital_address == "서울시 종로구"


def test_install_missing_type_returns_parse_error():
    cmd = parse_command("install 192.168.1.50 --code=HOSP01", TYPES)
    assert isinstance(cmd, ParseError)
    assert "--type" in cmd.message
    assert cmd.suggestion is not None
    assert "on-premise" in cmd.suggestion


def test_install_unknown_type_returns_parse_error():
    cmd = parse_command("install 1.2.3.4 --type=hybrid-magic --code=H", TYPES)
    assert isinstance(cmd, ParseError)
    assert "hybrid-magic" in cmd.message


def test_install_missing_code_returns_parse_error():
    cmd = parse_command("install 1.2.3.4 --type=on-premise", TYPES)
    assert isinstance(cmd, ParseError)
    assert "--code" in cmd.message


def test_install_invalid_ip_returns_parse_error():
    cmd = parse_command("install not-an-ip --type=on-premise --code=H", TYPES)
    assert isinstance(cmd, ParseError)
    assert "invalid IP" in cmd.message


def test_install_unwraps_slack_auto_link_with_label():
    # Slack이 IP를 <http://192.168.100.213|192.168.100.213> 식으로 자동 링크화한 케이스.
    cmd = parse_command(
        "install <http://192.168.100.213|192.168.100.213> --type=on-premise --code=H",
        TYPES,
    )
    assert isinstance(cmd, InstallCommand)
    assert cmd.target_ip == "192.168.100.213"


def test_install_unwraps_slack_auto_link_no_label():
    cmd = parse_command(
        "install <http://192.168.100.213> --type=on-premise --code=H",
        TYPES,
    )
    assert isinstance(cmd, InstallCommand)
    assert cmd.target_ip == "192.168.100.213"


def test_install_unwraps_slack_tel_auto_link():
    # Slack은 IP의 점 제거값을 전화번호로 인식해 <tel:N|IP> 형태로 자동링크화한다.
    cmd = parse_command(
        "install <tel:1921681002135|192.168.100.213> --type=on-premise --code=H",
        TYPES,
    )
    assert isinstance(cmd, InstallCommand)
    assert cmd.target_ip == "192.168.100.213"


def test_install_strips_backticks_around_ip():
    # 사용자가 자동링크 회피용으로 IP를 `...`로 감싼 경우.
    cmd = parse_command(
        "install `192.168.100.213` --type=on-premise --code=H",
        TYPES,
    )
    assert isinstance(cmd, InstallCommand)
    assert cmd.target_ip == "192.168.100.213"


def test_install_normalizes_korean_middle_dot():
    # 한글 IME가 '.'을 '·'(U+00B7)로 자동변환한 경우.
    cmd = parse_command("install 192·168·100·213 --type=on-premise --code=H", TYPES)
    assert isinstance(cmd, InstallCommand)
    assert cmd.target_ip == "192.168.100.213"


def test_install_invalid_ip_error_includes_raw_repr():
    # 디버그용: 받은 원시 토큰을 repr로 노출 (보이지 않는 문자 진단).
    cmd = parse_command("install 192,168,100,213 --type=on-premise --code=H", TYPES)
    assert isinstance(cmd, ParseError)
    assert "'192,168,100,213'" in cmd.message


def test_install_normalizes_katakana_middle_dot():
    # U+30FB. 일본어 입력기/일부 복붙 경로에서 들어옴.
    cmd = parse_command("install 192・168・100・213 --type=on-premise --code=H", TYPES)
    assert isinstance(cmd, InstallCommand)
    assert cmd.target_ip == "192.168.100.213"


def test_install_strips_zero_width_chars():
    # 점 주변에 ZWSP가 끼는 경우 (메신저 자동완성/특정 복붙 출처).
    cmd = parse_command(
        "install 192.168.​100.213 --type=on-premise --code=H", TYPES
    )
    assert isinstance(cmd, InstallCommand)
    assert cmd.target_ip == "192.168.100.213"


def test_install_invalid_ip_shows_non_ascii_codepoints():
    # 정규화 맵에 없는 비ASCII가 섞이면 코드포인트로 알려준다.
    bad = "192‥168.100.213"  # U+2025 TWO DOT LEADER (변환 안 함)
    cmd = parse_command(f"install {bad} --type=on-premise --code=H", TYPES)
    assert isinstance(cmd, ParseError)
    assert "U+2025" in cmd.message


def test_install_invalid_ip_shows_all_ascii_codepoints():
    # ASCII만 있어도 invalid면 코드포인트 전체 노출 (콤마/슬래시/공백 진단).
    cmd = parse_command("install 192,168,100,213 --type=on-premise --code=H", TYPES)
    assert isinstance(cmd, ParseError)
    # 콤마 U+002C, 1 U+0031 등이 모두 표시
    assert "U+002C" in cmd.message
    assert "U+0031" in cmd.message


def test_install_unknown_flag_returns_parse_error():
    cmd = parse_command("install 1.2.3.4 --type=on-premise --code=H --bogus=x", TYPES)
    assert isinstance(cmd, ParseError)
    assert "bogus" in cmd.message


def test_install_flag_without_value_returns_parse_error():
    cmd = parse_command("install 1.2.3.4 --type --code=H", TYPES)
    assert isinstance(cmd, ParseError)
    assert "value" in cmd.message


# ---------- status ----------

def test_status_no_args_means_latest():
    cmd = parse_command("status", TYPES)
    assert isinstance(cmd, StatusCommand)
    assert cmd.job_id is None


def test_status_with_job_id():
    cmd = parse_command("status 42", TYPES)
    assert isinstance(cmd, StatusCommand)
    assert cmd.job_id == 42


def test_status_with_invalid_job_id():
    cmd = parse_command("status abc", TYPES)
    assert isinstance(cmd, ParseError)


# ---------- list ----------

def test_list_default_limit():
    cmd = parse_command("list", TYPES)
    assert isinstance(cmd, ListCommand)
    assert cmd.limit == 10


def test_list_with_n():
    cmd = parse_command("list 5", TYPES)
    assert isinstance(cmd, ListCommand)
    assert cmd.limit == 5


def test_list_caps_at_50():
    cmd = parse_command("list 9999", TYPES)
    assert isinstance(cmd, ListCommand)
    assert cmd.limit == 50


def test_list_negative_returns_error():
    cmd = parse_command("list 0", TYPES)
    assert isinstance(cmd, ParseError)


# ---------- cancel ----------

def test_cancel_with_id():
    cmd = parse_command("cancel 7", TYPES)
    assert isinstance(cmd, CancelCommand)
    assert cmd.job_id == 7


def test_cancel_without_id_returns_error():
    cmd = parse_command("cancel", TYPES)
    assert isinstance(cmd, ParseError)


# ---------- retry ----------

def test_retry_without_args_defers_to_thread_context():
    cmd = parse_command("retry", TYPES)
    assert isinstance(cmd, RetryCommand)
    assert cmd.job_id is None


def test_retry_with_id():
    cmd = parse_command("retry 3", TYPES)
    assert isinstance(cmd, RetryCommand)
    assert cmd.job_id == 3


def test_retry_with_non_integer_returns_error():
    cmd = parse_command("retry abc", TYPES)
    assert isinstance(cmd, ParseError)
    assert "retry" in cmd.message


# ---------- register ----------

def test_register_with_id():
    cmd = parse_command("register 23", TYPES)
    assert isinstance(cmd, RegisterCommand)
    assert cmd.job_id == 23


def test_register_without_id_returns_error():
    cmd = parse_command("register", TYPES)
    assert isinstance(cmd, ParseError)


def test_register_with_non_integer_returns_error():
    cmd = parse_command("register abc", TYPES)
    assert isinstance(cmd, ParseError)
    assert "register" in cmd.message


# ---------- help / unknown / empty ----------

def test_help():
    assert isinstance(parse_command("help", TYPES), HelpCommand)


def test_question_mark_is_help():
    assert isinstance(parse_command("?", TYPES), HelpCommand)


def test_empty_text_is_help():
    assert isinstance(parse_command("   ", TYPES), HelpCommand)


def test_unknown_command_returns_parse_error():
    cmd = parse_command("destroy everything", TYPES)
    assert isinstance(cmd, ParseError)
    assert "destroy" in cmd.message


def test_case_insensitive_verb():
    cmd = parse_command("LIST 5", TYPES)
    assert isinstance(cmd, ListCommand)
