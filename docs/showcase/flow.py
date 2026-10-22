"""Build the router's flow figure for the top of the README: four symbols and a loop, in a light and a dark
version (the README picks one with `<picture>` and the reader's colour scheme).

    uv run docs/showcase/flow.py

Each version is an HTML page drawn with inline SVG on a fixed canvas, captured by headless Chrome at twice
the size so it stays sharp on high-density screens. Needs Google Chrome at its macOS path (CHROME), like
build.py; no Python package beyond the standard library.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CANVAS_W, CANVAS_H = 1480, 480
SCALE = 2  # captured at twice the canvas, so the figure is sharp at any width GitHub shows it
NODE = 170  # side of a symbol's square
NODE_TOP = 120
CENTRES = (200, 560, 920, 1280)  # x of the four symbols
CAPTION_W = 350  # a caption's width: the gap between two centres, less a margin
ARROW_LENGTH, ARROW_HALF_WIDTH = 16, 9
TITLE_FONT = "Georgia, 'Times New Roman', serif"
BODY_FONT = "'Helvetica Neue', Helvetica, Arial, sans-serif"


@dataclass(frozen=True)
class Theme:
    """The colours of one version of the figure."""

    name: str
    page: str
    node: str
    border: str
    text: str
    secondary: str
    accent: str  # what passes, the path taken
    accent2: str  # what fails, the way back up
    muted: str  # a step the risk does not allow


LIGHT = Theme("light", "#ffffff", "#f6f5f1", "#d8d4ca", "#1f2328", "#5b6670", "#2b64a8", "#a35a1f", "#c9c4b8")
DARK = Theme("dark", "#0d1117", "#161b22", "#30363d", "#e6edf3", "#9198a1", "#6ea8ff", "#e3955a", "#3b4048")

STEPS = (
    ("Task", "kind and risk, declared<br>or read from the text"),
    ("Start cheap", "the cheapest model<br>and effort the risk allows"),
    ("Check", "a test, if there is one;<br>from high risk, a judge<br>on another model"),
    ("Log", "every attempt, and<br>what the solved task cost"),
)


def _icon_task(t: Theme) -> str:
    return f"""
      <path d="M22 10 h40 l18 18 v62 h-58 z" fill="none" stroke="{t.text}" stroke-width="4" stroke-linejoin="round"/>
      <path d="M62 10 v18 h18" fill="none" stroke="{t.text}" stroke-width="4" stroke-linejoin="round"/>
      <line x1="32" y1="44" x2="68" y2="44" stroke="{t.secondary}" stroke-width="4" stroke-linecap="round"/>
      <line x1="32" y1="58" x2="60" y2="58" stroke="{t.secondary}" stroke-width="4" stroke-linecap="round"/>
      <rect x="30" y="70" width="40" height="14" rx="7" fill="{t.accent2}"/>"""


def _icon_stairs(t: Theme) -> str:
    bars = []
    for i, colour in enumerate((t.muted, t.accent, t.text, t.text)):
        height = 22 + i * 18
        bars.append(f'<rect x="{8 + i * 21}" y="{92 - height}" width="17" height="{height}" rx="3" fill="{colour}"'
                    + (' opacity="0.35"' if i > 1 else "") + "/>")
    return "".join(bars) + f'<path d="M37 18 v14 m-6 -6 l6 6 l6 -6" stroke="{t.accent}" stroke-width="4" fill="none" ' \
                           'stroke-linecap="round" stroke-linejoin="round"/>'


def _icon_check(t: Theme) -> str:
    return f"""
      <circle cx="42" cy="42" r="27" fill="none" stroke="{t.text}" stroke-width="5"/>
      <line x1="62" y1="62" x2="84" y2="84" stroke="{t.text}" stroke-width="7" stroke-linecap="round"/>
      <path d="M30 43 l9 9 l16 -18" fill="none" stroke="{t.accent}" stroke-width="5" stroke-linecap="round"
            stroke-linejoin="round"/>"""


def _icon_log(t: Theme) -> str:
    return f"""
      <path d="M20 10 h52 v76 l-9 -6 l-8 6 l-9 -6 l-8 6 l-9 -6 l-9 6 z" fill="none" stroke="{t.text}"
            stroke-width="4" stroke-linejoin="round"/>
      <line x1="30" y1="30" x2="62" y2="30" stroke="{t.secondary}" stroke-width="4" stroke-linecap="round"/>
      <line x1="30" y1="44" x2="54" y2="44" stroke="{t.secondary}" stroke-width="4" stroke-linecap="round"/>
      <text x="46" y="72" text-anchor="middle" font-family="{BODY_FONT}" font-size="22" font-weight="700"
            fill="{t.accent}">$</text>"""


ICONS = (_icon_task, _icon_stairs, _icon_check, _icon_log)


def _arrow_head(x: float, y: float, colour: str, *, pointing: str) -> str:
    """Return a filled arrow head with its tip at (x, y), pointing right or down."""
    length, half = ARROW_LENGTH, ARROW_HALF_WIDTH
    right = f"{x},{y} {x - length},{y - half} {x - length},{y + half}"
    down = f"{x},{y} {x - half},{y - length} {x + half},{y - length}"
    return f'<polygon points="{right if pointing == "right" else down}" fill="{colour}"/>'


def _connectors(t: Theme) -> str:
    """Return the arrows: task to start, start to check, check to log (pass), and check back to start (fail)."""
    middle = NODE_TOP + NODE / 2
    half, gap = NODE / 2, 18
    parts = []
    for i in range(3):
        start, end = CENTRES[i] + half + gap, CENTRES[i + 1] - half - gap
        colour = t.accent if i == 2 else t.secondary
        parts.append(f'<line x1="{start}" y1="{middle}" x2="{end - ARROW_LENGTH + 2}" y2="{middle}" stroke="{colour}" '
                     'stroke-width="4"/>')
        parts.append(_arrow_head(end, middle, colour, pointing="right"))
    pass_x = (CENTRES[2] + CENTRES[3]) / 2
    parts.append(f'<text x="{pass_x}" y="{middle - 16}" text-anchor="middle" font-family="{BODY_FONT}" '
                 f'font-size="24" font-weight="700" fill="{t.accent}">pass</text>')
    top, loop_y = NODE_TOP - gap, 52
    back = f"M{CENTRES[2]} {top} V{loop_y} H{CENTRES[1]} V{top - ARROW_LENGTH + 2}"
    parts.append(f'<path d="{back}" fill="none" stroke="{t.accent2}" stroke-width="4" stroke-linejoin="round"/>')
    parts.append(_arrow_head(CENTRES[1], top, t.accent2, pointing="down"))
    parts.append(f'<text x="{(CENTRES[1] + CENTRES[2]) / 2}" y="{loop_y - 14}" text-anchor="middle" '
                 f'font-family="{BODY_FONT}" font-size="24" font-weight="700" fill="{t.accent2}">'
                 "fail: one step up, with the reason</text>")
    return "".join(parts)


def page(t: Theme) -> str:
    """Return the HTML of the figure in theme `t`."""
    nodes = []
    for centre, (title, line), icon in zip(CENTRES, STEPS, ICONS, strict=True):
        left = centre - NODE / 2
        nodes.append(f"""
      <div class="node" style="left:{left}px;top:{NODE_TOP}px">
        <svg width="92" height="92" viewBox="0 0 92 92">{icon(t)}</svg>
      </div>
      <div class="title" style="left:{centre - CAPTION_W / 2}px;top:{NODE_TOP + NODE + 16}px">{title}</div>
      <div class="line" style="left:{centre - CAPTION_W / 2}px;top:{NODE_TOP + NODE + 64}px">{line}</div>""")
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
      html, body {{ margin:0; background:{t.page}; }}
      .canvas {{ position:relative; width:{CANVAS_W}px; height:{CANVAS_H}px; background:{t.page}; }}
      .node {{ position:absolute; width:{NODE}px; height:{NODE}px; box-sizing:border-box; border-radius:28px;
               background:{t.node}; border:2px solid {t.border}; display:flex; align-items:center;
               justify-content:center; }}
      .title {{ position:absolute; width:{CAPTION_W}px; text-align:center; font:700 36px {TITLE_FONT}; color:{t.text}; }}
      .line {{ position:absolute; width:{CAPTION_W}px; text-align:center; font:400 26px/1.3 {BODY_FONT};
               color:{t.secondary}; }}
      svg.wires {{ position:absolute; left:0; top:0; }}
    </style></head><body><div class="canvas">
      <svg class="wires" width="{CANVAS_W}" height="{CANVAS_H}">{_connectors(t)}</svg>
      {"".join(nodes)}
    </div></body></html>"""


def capture(t: Theme, out: Path) -> None:
    """Write theme `t`'s figure to `out` as a PNG, through headless Chrome."""
    with tempfile.NamedTemporaryFile("w", suffix=".html", encoding="utf-8", delete=False) as handle:
        handle.write(page(t))
    subprocess.run([CHROME, "--headless=new", f"--screenshot={out}", f"--window-size={CANVAS_W},{CANVAS_H}",
                    f"--force-device-scale-factor={SCALE}", "--hide-scrollbars", "--virtual-time-budget=1000",
                    f"file://{handle.name}"], check=True, capture_output=True)
    Path(handle.name).unlink()


def main() -> None:
    """Build `flow-light.png` and `flow-dark.png` next to this script."""
    for theme in (LIGHT, DARK):
        out = HERE / f"flow-{theme.name}.png"
        capture(theme, out)
        print(out)


if __name__ == "__main__":
    main()
