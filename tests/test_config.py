from __future__ import annotations

from pathlib import Path

import pytest

from autodeploy.config import (
    ConfigError,
    load_deployment_types,
    render_args,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "deployment_types.yaml"


def test_loads_three_initial_types():
    types = load_deployment_types(CONFIG_PATH)
    assert set(types) == {"on-premise", "hybrid-with-ai", "hybrid-without-ai"}


def test_on_premise_scripts():
    t = load_deployment_types(CONFIG_PATH)["on-premise"]
    assert t.infra.script == "setup-onpremise.sh"
    assert t.infra.sudo is True
    assert t.infra.args == ("{code}",)
    assert t.app.script == "deploy-applications-onpremise.sh"
    assert t.app.sudo is False
    assert t.app.args == ("{code}",)


def test_hybrid_with_ai_app_takes_w_ai_arg():
    t = load_deployment_types(CONFIG_PATH)["hybrid-with-ai"]
    assert t.app.args == ("w-ai", "{code}")
    assert t.infra.args == ("{code}",)


def test_hybrid_without_ai_app_takes_wo_ai_arg():
    t = load_deployment_types(CONFIG_PATH)["hybrid-without-ai"]
    assert t.app.args == ("wo-ai", "{code}")
    assert t.infra.script == "setup-site.sh"


def test_hybrid_shares_infra_script():
    types = load_deployment_types(CONFIG_PATH)
    assert types["hybrid-with-ai"].infra.script == types["hybrid-without-ai"].infra.script == "setup-site.sh"


def test_render_args_substitutes_code():
    assert render_args(("w-ai", "{code}"), "HOSP01") == ("w-ai", "HOSP01")


def test_render_args_no_placeholder_left_untouched():
    assert render_args(("--flag", "value"), "HOSP01") == ("--flag", "value")


def test_missing_file_raises(tmp_path: Path):
    with pytest.raises(ConfigError):
        load_deployment_types(tmp_path / "missing.yaml")


def test_invalid_root_raises(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("- not a mapping\n")
    with pytest.raises(ConfigError):
        load_deployment_types(bad)


def test_missing_script_field_raises(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "on-premise:\n"
        "  infra:\n"
        "    sudo: true\n"
        "    args: ['{code}']\n"
        "  app:\n"
        "    script: x.sh\n"
        "    sudo: false\n"
        "    args: []\n"
    )
    with pytest.raises(ConfigError, match="script"):
        load_deployment_types(bad)
