"""`web.Application` 에 담는 값들의 키.

문자열 키를 쓰면 aiohttp 가 `NotAppKeyWarning` 을 낸다. 오타가 런타임 KeyError 로만
드러나고 타입도 안 잡히기 때문. `AppKey` 는 이름과 타입을 한 곳에 묶어둔다.

`web/__init__.py` 와 `web/api.py` 가 함께 쓰므로 순환 import 를 피하려고 분리했다.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from autodeploy.masking import SecretMasker
    from autodeploy.web.auth import LoginThrottle
    from autodeploy.web.forwards import ForwardManager
    from autodeploy.web.jobs import JobService
    from autodeploy.web.sse import SseBroker

DB_PATH = web.AppKey("db_path", Path)
HUBCTL_REPO = web.AppKey("hubctl_repo", Path)
INVENTORY_PATH = web.AppKey("inventory_path", Path)
STATIC_DIR = web.AppKey("static_dir", Path)
QUEUE: web.AppKey[Any] = web.AppKey("queue")
MASKER: web.AppKey["SecretMasker"] = web.AppKey("masker")
HUBCTL_ENV: web.AppKey[dict] = web.AppKey("hubctl_env", dict)
SESSION_TTL_DAYS = web.AppKey("session_ttl_days", int)
SECURE_COOKIE = web.AppKey("secure_cookie", bool)
TRUST_FORWARDED = web.AppKey("trust_forwarded", bool)
THROTTLE: web.AppKey["LoginThrottle"] = web.AppKey("throttle")
PREFLIGHT_LOCK = web.AppKey("preflight_lock", asyncio.Lock)
# hubctl 을 감싸는 셸. 기본은 로그인 셸(~/.zshrc 의 Vault/AWS 자격 상속)이지만,
# 테스트는 사용자 rc 에 좌우되지 않도록 갈아끼운다.
HUBCTL_SHELL: web.AppKey[tuple] = web.AppKey("hubctl_shell", tuple)
BROKER: web.AppKey["SseBroker"] = web.AppKey("broker")
JOB_SERVICE: web.AppKey["JobService"] = web.AppKey("job_service")
SSH_THROTTLE: web.AppKey["LoginThrottle"] = web.AppKey("ssh_throttle")
HOUSEKEEPER: web.AppKey[Any] = web.AppKey("housekeeper")
FORWARDS: web.AppKey["ForwardManager"] = web.AppKey("forwards")
