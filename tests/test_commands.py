from __future__ import annotations

import pytest

from autodeploy.commands import (
    CancelCommand,
    HelpCommand,
    InstallCommand,
    ListCommand,
    ParseError,
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
