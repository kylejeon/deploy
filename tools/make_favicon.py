#!/usr/bin/env python3
"""파비콘(SVG + PNG)을 만든다.

컨트롤러에 래스터 도구(rsvg-convert·ImageMagick·Pillow)가 없어서 PNG 를 직접
그린다. 도형이 넷뿐이라 해석적으로 판정하고 픽셀당 4×4 로 슈퍼샘플링한다 —
외부 의존성 없이도 가장자리가 매끄럽다.

    python3 tools/make_favicon.py

산출물은 `src/autodeploy/web/static/` 에 덮어쓴다.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "autodeploy" / "web" / "static"

# console.css 의 --accent / --accent-ink 와 같은 값.
TEAL_LIGHT = "#0B6E75"   # :root
INK_LIGHT = "#FFFFFF"
TEAL_DARK = "#3FB5B0"    # prefers-color-scheme: dark
INK_DARK = "#04201F"
# PNG 은 테마를 못 따라간다. 밝은 탭바·어두운 탭바 어디에 놓아도 보이도록
# 두 값 사이를 쓴다 (흰 글리프 대비 4.9:1).
TEAL_STATIC = "#0F7F86"

# ── 32 단위 격자 위의 도형 ────────────────────────────────────────────
# 서버 랙(가로 막대 2단 + 상태 점) 위로 쐐기가 내려앉는 모양 = "서버에 설치".
#
# 처음에는 화살표(축 + 촉 + 받침)로 그렸는데 브라우저의 다운로드 아이콘과
# 구분이 안 됐다. 축을 빼고 랙에 상태 점을 넣으니 이 앱의 화면(서버 목록과
# 상태)과 같은 이야기가 된다.
#
# 쐐기는 **속을 채운 삼각형**이다. 속 빈 쐐기(V)로도 그려봤지만 16px 에서
# 사선 획이 1px 아래로 내려가 뭉개졌다. 채운 덩어리는 살아남는다.
# 가장 가는 곳이 32 격자에서 4.5 = 16px 에서 2.25px.
TILE_R = 7.0
WEDGE = ((8.0, 4.0), (24.0, 4.0), (16.0, 14.0))
BARS = ((6.5, 16.5, 19.0, 4.5, 2.25), (6.5, 23.0, 19.0, 4.5, 2.25))
DOT_X, DOT_R = 10.6, 1.75   # 막대에서 파내는 상태 점

SAMPLES = 4   # 픽셀당 4×4


def _hex(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def _in_round_rect(px: float, py: float, x: float, y: float,
                   w: float, h: float, r: float) -> bool:
    """둥근 사각형 SDF. 모서리 반지름을 변의 절반으로 묶어 뒤집히지 않게 한다."""
    r = min(r, w / 2, h / 2)
    qx = abs(px - (x + w / 2)) - (w / 2 - r)
    qy = abs(py - (y + h / 2)) - (h / 2 - r)
    outside = ((max(qx, 0.0) ** 2 + max(qy, 0.0) ** 2) ** 0.5)
    return outside + min(max(qx, qy), 0.0) - r <= 0.0


def _in_triangle(px: float, py: float, tri) -> bool:
    (ax, ay), (bx, by), (cx, cy) = tri

    def side(x1, y1, x2, y2):
        return (px - x2) * (y1 - y2) - (x1 - x2) * (py - y2)

    d1, d2, d3 = side(ax, ay, bx, by), side(bx, by, cx, cy), side(cx, cy, ax, ay)
    has_neg = d1 < 0 or d2 < 0 or d3 < 0
    has_pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (has_neg and has_pos)


def _in_glyph(px: float, py: float) -> bool:
    if _in_triangle(px, py, WEDGE):
        return True
    for x, y, w, h, r in BARS:
        if _in_round_rect(px, py, x, y, w, h, r):
            # 상태 점은 막대에서 파낸다 — 타일 색이 비쳐 보이게.
            return (px - DOT_X) ** 2 + (py - (y + h / 2)) ** 2 > DOT_R ** 2
    return False


def render(size: int, *, teal: str, ink: str, rounded: bool) -> bytes:
    """RGBA 픽셀 바이트. `rounded=False` 면 모서리 없이 꽉 채운다.

    apple-touch-icon 은 애플이 알아서 둥글리므로 투명한 모서리를 남기면
    안 된다 — 검은 테두리로 합성되는 경우가 있다.
    """
    tr, tg, tb = _hex(teal)
    ir, ig, ib = _hex(ink)
    scale = 32.0 / size
    step = 1.0 / SAMPLES
    rows = bytearray()
    for py in range(size):
        row = bytearray()
        for px in range(size):
            acc_r = acc_g = acc_b = acc_a = 0
            for sy in range(SAMPLES):
                uy = (py + (sy + 0.5) * step) * scale
                for sx in range(SAMPLES):
                    ux = (px + (sx + 0.5) * step) * scale
                    if not rounded or _in_round_rect(ux, uy, 0.0, 0.0, 32.0, 32.0, TILE_R):
                        if _in_glyph(ux, uy):
                            acc_r += ir; acc_g += ig; acc_b += ib
                        else:
                            acc_r += tr; acc_g += tg; acc_b += tb
                        acc_a += 255
            n = SAMPLES * SAMPLES
            alpha = acc_a // n
            if alpha == 0:
                row += b"\x00\x00\x00\x00"
                continue
            # 커버리지로 평균 낸 값이라 이미 프리멀티플라이드다. 되돌린다.
            cov = acc_a / 255.0
            row += bytes((round(acc_r / cov), round(acc_g / cov), round(acc_b / cov), alpha))
        rows += b"\x00" + row   # filter type 0 (None)
    return bytes(rows)


def write_png(path: Path, size: int, raw: bytes) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)   # 8bit RGBA
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    path.write_bytes(png)


def _svg() -> str:
    """SVG 는 파낸 점을 타일 색 원으로 덮어 그린다 — 테마가 바뀌면 같이 바뀐다."""
    bars = "\n    ".join(
        f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" rx="{r:g}"/>'
        for x, y, w, h, r in BARS
    )
    dots = "\n  ".join(
        f'<circle class="tile" cx="{DOT_X:g}" cy="{y + h / 2:g}" r="{DOT_R:g}"/>'
        for _x, y, _w, h, _r in BARS
    )
    (ax, ay), (bx, by), (cx, cy) = WEDGE
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" role="img" aria-label="AutoDeploy">
  <title>AutoDeploy</title>
  <style>
    /* 탭바 테마를 따라간다. 값은 console.css 의 --accent / --accent-ink 와 같다. */
    .tile {{ fill: {TEAL_LIGHT}; }}
    .glyph {{ fill: {INK_LIGHT}; }}
    @media (prefers-color-scheme: dark) {{
      .tile {{ fill: {TEAL_DARK}; }}
      .glyph {{ fill: {INK_DARK}; }}
    }}
  </style>
  <rect class="tile" width="32" height="32" rx="{TILE_R:g}"/>
  <g class="glyph">
    <path d="M{ax:g} {ay:g} L{bx:g} {by:g} L{cx:g} {cy:g} Z"/>
    {bars}
  </g>
  <!-- 상태 점. 막대를 파낸 자리를 타일 색으로 덮는다. -->
  {dots}
</svg>
"""


def main() -> None:
    (STATIC / "favicon.svg").write_text(_svg(), encoding="utf-8")
    write_png(STATIC / "favicon.png", 32,
              render(32, teal=TEAL_STATIC, ink=INK_LIGHT, rounded=True))
    write_png(STATIC / "apple-touch-icon.png", 180,
              render(180, teal=TEAL_STATIC, ink=INK_LIGHT, rounded=False))
    for name in ("favicon.svg", "favicon.png", "apple-touch-icon.png"):
        print(f"{name}: {(STATIC / name).stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
