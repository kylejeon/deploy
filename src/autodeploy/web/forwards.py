"""사내망 타겟으로 가는 임시 TCP 중계.

콘솔은 사내 LAN 안(맥미니)에서 돈다. 밖에 있는 사람은 Tailscale 로 맥미니까지는
오지만 그 뒤 타겟(`192.168.100.x:8000`)으로는 갈 수 없다. 여기서 맥미니에 임시
리스너를 열어 타겟으로 이어준다. 브라우저는 콘솔에 접속할 때 쓴 것과 같은 주소의
다른 포트로 붙으면 된다.

**콘솔을 사내망 프록시로 만들지 않는 것이 이 모듈의 핵심 제약이다.** 임의의
host:port 를 열어주면 로그인한 사람이 병원망 전체를 훑을 수 있다. 그래서:

- 대상 호스트는 인벤토리에 있는 것만 (호출부가 이름 → 주소로 해석해서 넘긴다)
- 포트는 `ALLOWED_PORTS` 만 — 설치가 실제로 여는 traefik hostPort 다
- 바인드 주소는 웹 콘솔과 같다. 노출 범위는 운영자가 `WEB_HOST` 로 이미 내린
  결정이고, 중계가 그보다 넓어지면 안 된다
- 놀고 있으면 스스로 닫는다. 열어둔 것을 잊어도 영원히 남지 않게.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# 설치가 타겟에 여는 traefik hostPort (roles/platform_component/vars/values/traefik.yml).
# DICOM(1113)은 브라우저로 열 수 없는 원시 TCP 라 뺐다.
HTTP_PORTS: dict[int, str] = {
    8000: "프론트",
    8001: "Temporal",
    8002: "WebPACS",
    8003: "Grafana",
}

# 터미널. 중계는 위와 똑같지만 브라우저로 여는 것이 아니라 `ssh://` 링크로 넘긴다 —
# macOS 가 ssh 스킴을 Terminal.app 에 물려두고 있어서, 누르면 맥북에 터미널 창이
# 뜨고 `ssh -p <중계포트> <사용자>@<콘솔주소>` 가 그대로 실행된다.
#
# 이걸 여는 것과 콘솔 안에 셸을 다는 것은 다르다. 중계는 타겟의 sshd 까지 길만
# 내주고, 인증은 여전히 맥북 ↔ 타겟 사이에서 일어난다. 콘솔에 로그인했다는 것만으로
# 셸이 열리지는 않는다 — 타겟 자격증명이 따로 있어야 한다.
SSH_PORT = 22

ALLOWED_PORTS: dict[int, str] = {**HTTP_PORTS, SSH_PORT: "터미널"}

# hybrid 프로파일은 앱이 사이트가 아니라 중앙(클라우드)에 있다. 그쪽은 이미 공인
# 주소로 열려 있으므로 중계할 것이 없고, 중계해봐야 사이트의 traefik 이 404 를
# 돌려줄 뿐이다. 그래서 환경별 실제 주소로 보낸다.
#
# 8002(WebPACS)는 hybrid 에서도 사이트에 있다 — 영상은 병원 안에 남는다. 그래서
# 여기 없고, 프로파일과 무관하게 중계한다.
CLOUD_URLS: dict[str, dict[int, str]] = {
    "dev": {
        8000: "https://dev-gateway.connecteve.com",
        8001: "https://dev-temporal-web.connecteve.com",
        8003: "https://dev-grafana.connecteve.com",
    },
    "stage": {
        8000: "https://stage-gateway.connecteve.com",
        8001: "https://stage-temporal-web.connecteve.com",
        8003: "https://stage-grafana.connecteve.com",
    },
    "prod": {
        8000: "https://hub.connecteve.com",
        8001: "https://temporal-web.connecteve.com",
        8003: "https://grafana.connecteve.com",
    },
}

# hybrid 에서 중앙으로 가는 포트들. 어느 환경이든 목록은 같다.
HYBRID_CLOUD_PORTS: frozenset[int] = frozenset().union(*(m for m in CLOUD_URLS.values()))


def is_hybrid(profile: str) -> bool:
    return profile.startswith("hybrid")


def plan(*, profile: str, env: str | None) -> list[dict]:
    """포트별로 무엇을 해야 하는지. 화면이 규칙을 들고 있지 않도록 서버가 정한다.

    mode 는 셋 중 하나다.
      relay   — 맥미니가 사이트로 중계한다 (onprem 전부, hybrid 의 8002·22)
      cloud   — 중앙 주소로 바로 간다 (hybrid)
      unknown — hybrid 인데 작업에 env 가 없어 어느 환경인지 모른다.
                verify/clean 처럼 `-e ENV` 를 받지 않는 작업에서 생긴다.
                이때 중계를 내주면 사이트 traefik 이 404 를 돌려줘 사람을 헷갈리게 한다.

    via 는 화면이 중계 포트를 무엇으로 넘길지다 — http 는 새 탭, ssh 는 `ssh://`.
    터미널은 프로파일과 무관하게 항상 사이트다 (중앙에는 그 PC 가 없다). 순서상
    맨 뒤에 둔다 — 설치 끝나고 웹을 먼저 보고, 필요할 때 터미널로 내려간다.
    """
    entries = []
    for port in [*sorted(HTTP_PORTS), SSH_PORT]:
        if is_hybrid(profile) and port in HYBRID_CLOUD_PORTS:
            url = CLOUD_URLS.get(env or "", {}).get(port)
            mode = "cloud" if url else "unknown"
        else:
            url, mode = None, "relay"
        entries.append({
            "port": port,
            "label": ALLOWED_PORTS[port],
            "mode": mode,
            "url": url,
            "via": "ssh" if port == SSH_PORT else "http",
        })
    return entries


IDLE_TIMEOUT = 30 * 60.0   # 무사용 자동 종료
REAP_INTERVAL = 30.0
CONNECT_TIMEOUT = 5.0
CHUNK = 64 * 1024


class ForwardError(RuntimeError):
    """열 수 없는 요청 (허용되지 않은 포트 등)."""


@dataclass(slots=True)
class Forward:
    host: str            # 인벤토리 이름 (표시용)
    address: str         # 실제 접속 주소
    port: int
    listen_port: int
    server: asyncio.AbstractServer
    last_active: float
    conns: int = 0
    tasks: set[asyncio.Task] = field(default_factory=set)

    @property
    def key(self) -> str:
        return f"{self.host}:{self.port}"

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "host": self.host,
            "address": self.address,
            "port": self.port,
            "label": ALLOWED_PORTS.get(self.port, str(self.port)),
            "listen_port": self.listen_port,
            "connections": self.conns,
            "idle_timeout": int(IDLE_TIMEOUT),
        }


class ForwardManager:
    """(호스트, 포트)당 중계 하나. 같은 조합을 다시 열면 기존 것을 돌려준다."""

    def __init__(self, *, bind_host: str = "127.0.0.1") -> None:
        # 0.0.0.0 은 "모든 인터페이스"라는 뜻이라 그대로 쓴다. 콘솔과 같은 범위.
        self._bind = bind_host
        self._forwards: dict[str, Forward] = {}
        self._reaper: asyncio.Task | None = None

    # -- 수명 --

    def start(self) -> None:
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_loop(), name="forward-reaper")

    async def stop(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            with suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None
        for key in list(self._forwards):
            await self.close(key)

    # -- 조작 --

    async def open(self, *, host: str, address: str, port: int) -> Forward:
        if port not in ALLOWED_PORTS:
            raise ForwardError(
                f"열 수 없는 포트입니다: {port} "
                f"(허용: {', '.join(str(p) for p in sorted(ALLOWED_PORTS))})"
            )
        key = f"{host}:{port}"
        existing = self._forwards.get(key)
        if existing is not None:
            existing.last_active = _now()
            return existing

        forward: Forward | None = None

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            assert forward is not None
            await self._relay(forward, reader, writer)

        server = await asyncio.start_server(handle, host=self._bind, port=0)
        listen_port = server.sockets[0].getsockname()[1]
        forward = Forward(
            host=host, address=address, port=port,
            listen_port=listen_port, server=server, last_active=_now(),
        )
        self._forwards[key] = forward
        log.info(
            "중계 열림: %s:%d → %s:%d (무사용 %d분 후 자동 종료)",
            self._bind, listen_port, address, port, IDLE_TIMEOUT // 60,
        )
        return forward

    async def close(self, key: str) -> bool:
        forward = self._forwards.pop(key, None)
        if forward is None:
            return False
        for task in list(forward.tasks):
            task.cancel()
        forward.server.close()
        with suppress(Exception):
            await forward.server.wait_closed()
        log.info("중계 닫힘: %s (listen=%d)", key, forward.listen_port)
        return True

    def list(self) -> list[dict]:
        return [f.as_dict() for f in sorted(self._forwards.values(), key=lambda f: f.key)]

    def get(self, key: str) -> Forward | None:
        return self._forwards.get(key)

    # -- 내부 --

    async def _relay(
        self,
        forward: Forward,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        try:
            target_reader, target_writer = await asyncio.wait_for(
                asyncio.open_connection(forward.address, forward.port), CONNECT_TIMEOUT
            )
        except (OSError, TimeoutError) as exc:
            # 타겟이 아직 안 떴거나 방화벽에 막힌 경우. 중계 자체는 살려둔다.
            log.warning("중계 대상 접속 실패 %s:%d — %s", forward.address, forward.port, exc)
            _shutdown(client_writer)
            return

        forward.conns += 1
        forward.last_active = _now()
        task = asyncio.current_task()
        if task is not None:
            forward.tasks.add(task)
        try:
            await asyncio.gather(
                self._pipe(forward, client_reader, target_writer),
                self._pipe(forward, target_reader, client_writer),
            )
        finally:
            if task is not None:
                forward.tasks.discard(task)
            forward.conns -= 1
            forward.last_active = _now()
            _shutdown(target_writer)
            _shutdown(client_writer)

    async def _pipe(
        self, forward: Forward, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                data = await reader.read(CHUNK)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
                forward.last_active = _now()
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            # 한쪽이 끝나면 반대쪽 read 도 EOF 가 나도록 write 끝을 닫는다.
            with suppress(OSError):
                if writer.can_write_eof():
                    writer.write_eof()

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(REAP_INTERVAL)
            now = _now()
            for key, forward in list(self._forwards.items()):
                # 연결이 붙어 있는 동안은 살려둔다 — 오래 열어둔 탭이 끊기면 안 된다.
                if forward.conns == 0 and now - forward.last_active > IDLE_TIMEOUT:
                    log.info("중계 자동 종료(무사용 %d분): %s", IDLE_TIMEOUT // 60, key)
                    await self.close(key)


def _now() -> float:
    return asyncio.get_running_loop().time()


def _shutdown(writer: asyncio.StreamWriter) -> None:
    with suppress(OSError):
        writer.close()
