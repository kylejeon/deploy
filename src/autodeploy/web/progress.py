"""오래 걸리는 요청의 진행 상태 (화면이 폴링해서 본다).

SSH 키 등록은 요청 하나로 여러 일을 한다 — 공개키 설치, 절전 끄기, 시리얼
읽기, hybrid 면 PC 준비(apt 로 AnyDesk 설치)까지. 마지막 것은 몇 분이 걸리는데,
그동안 화면에는 눌린 버튼 말고 아무것도 없어서 **멈춘 것처럼 보였다.**

응답을 스트리밍으로 바꾸면 상태코드로 실패를 알리는 지금 구조가 깨진다. 대신
진행 상태만 따로 여기에 적어두고, 화면이 1초에 한 번 물어보게 한다.

프로세스 메모리에만 있다. 되살릴 값이 아니고 — 데몬이 죽으면 그 등록도 같이
죽는다 — 요청이 끝나면 지운다.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

# 화면에 그대로 흘리는 줄이라 길면 잘라 보낸다. apt 출력은 한 줄이 길다.
DETAIL_MAX = 120


@dataclass(slots=True)
class Progress:
    steps: tuple[str, ...]
    step: str = ""
    # 지금 단계에서 마지막으로 나온 출력 한 줄. "돌고는 있다" 를 보여주는 값이라
    # 정확할 필요는 없지만, **가려져 있어야 한다** (준비 스크립트 출력에는
    # AnyDesk 비밀번호가 섞일 수 있다 — 넣는 쪽에서 가린다).
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "running": True,
            "steps": list(self.steps),
            "step": self.step,
            "detail": self.detail,
        }


@dataclass(slots=True)
class ProgressBoard:
    """대상 이름 하나당 진행 중인 것 하나."""

    _items: dict[str, Progress] = field(default_factory=dict)

    def start(self, key: str, steps: Sequence[str]) -> Progress:
        item = Progress(steps=tuple(steps), step=steps[0] if steps else "")
        self._items[key] = item
        return item

    def step(self, key: str, step: str) -> None:
        item = self._items.get(key)
        if item is not None:
            item.step = step
            # 단계가 바뀌면 앞 단계의 출력은 지운다 — 안 그러면 apt 마지막 줄이
            # 다음 단계 밑에 그대로 남아 무슨 일이 도는지 잘못 읽힌다.
            item.detail = ""

    def detail(self, key: str, line: str) -> None:
        item = self._items.get(key)
        if item is not None:
            text = line.strip()
            item.detail = text[:DETAIL_MAX] if text else item.detail

    def get(self, key: str) -> Progress | None:
        return self._items.get(key)

    def finish(self, key: str) -> None:
        self._items.pop(key, None)
