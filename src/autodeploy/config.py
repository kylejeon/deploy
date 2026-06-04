"""배포 형태(deployment type) config 로더 및 검증."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True, slots=True)
class ScriptSpec:
    script: str
    sudo: bool
    args: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DeploymentType:
    name: str
    description: str
    infra: ScriptSpec
    app: ScriptSpec
    # app 직후에 동일 단계(APP_INSTALL)에서 추가 실행할 스크립트 목록.
    # 예: on-premise의 freeze-offline.sh. 비어있으면 app만 실행.
    post_app: tuple[ScriptSpec, ...] = ()


class ConfigError(ValueError):
    pass


def load_deployment_types(path: str | Path) -> dict[str, DeploymentType]:
    path = Path(path).expanduser()
    if not path.is_file():
        raise ConfigError(f"deployment types config not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not raw:
        raise ConfigError(f"config root must be a non-empty mapping, got {type(raw).__name__}")
    return {name: _parse_type(name, body) for name, body in raw.items()}


def _parse_type(name: str, body: object) -> DeploymentType:
    if not isinstance(body, dict):
        raise ConfigError(f"type '{name}' must be a mapping")
    raw_post = body.get("post_app", []) or []
    if not isinstance(raw_post, list):
        raise ConfigError(f"type '{name}.post_app' must be a list")
    post_app = tuple(
        _parse_script(name, f"post_app[{i}]", item) for i, item in enumerate(raw_post)
    )
    return DeploymentType(
        name=name,
        description=str(body.get("description", "")),
        infra=_parse_script(name, "infra", body.get("infra")),
        app=_parse_script(name, "app", body.get("app")),
        post_app=post_app,
    )


def _parse_script(type_name: str, key: str, body: object) -> ScriptSpec:
    if not isinstance(body, dict):
        raise ConfigError(f"type '{type_name}' missing '{key}' section")
    script = body.get("script")
    if not isinstance(script, str) or not script:
        raise ConfigError(f"type '{type_name}.{key}.script' must be non-empty string")
    sudo = body.get("sudo", False)
    if not isinstance(sudo, bool):
        raise ConfigError(f"type '{type_name}.{key}.sudo' must be bool")
    args = body.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ConfigError(f"type '{type_name}.{key}.args' must be list[str]")
    return ScriptSpec(script=script, sudo=sudo, args=tuple(args))


def render_args(args: tuple[str, ...], code: str) -> tuple[str, ...]:
    return tuple(a.replace("{code}", code) for a in args)
