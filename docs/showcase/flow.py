"""Build the flow figures at the top of the READMEs, in one visual language: rounded squares with a stroke
icon, a title and one short line under each, grey arrows for the way through, blue for a pass or an
agreement, orange for a fail or a split. Each comes in a light and a dark version; a README picks one with
`<picture>` and the reader's colour scheme.

- `flow-*.png`: the router's loop (README.md): task, start cheap, check, log, and a fail one step up.
- `tribunal-*.png`: the tribunal (plugins/tribunal/README.md): the change, two blind judges, the verdict,
  back to Claude, and the arbiter on a split.

    uv run docs/showcase/flow.py

Each version is an HTML page drawn with inline SVG on a fixed canvas, captured by headless Chrome at twice
the size so it stays sharp on high-density screens. Needs Google Chrome at its macOS path (CHROME), like
build.py; no Python package beyond the standard library.
"""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
SCALE = 2  # captured at twice the canvas, so the figure is sharp at any width GitHub shows it
NODE = 170  # side of a symbol's square
SMALL_NODE = 130  # the arbiter's: it sits off the main way
ICON = 92  # side of an icon's drawing box
CAPTION_W = 350  # a caption's width: the gap between two centres, less a margin
ARROW_LENGTH, ARROW_HALF_WIDTH = 16, 9
GAP = 18  # between a square and the arrow that leaves or reaches it
TITLE_FONT = "Georgia, 'Times New Roman', serif"
BODY_FONT = "'Helvetica Neue', Helvetica, Arial, sans-serif"


@dataclass(frozen=True)
class Theme:
    """The colours of one version of a figure."""

    name: str
    page: str
    node: str
    border: str
    text: str
    secondary: str
    accent: str  # a pass, an agreement, the path taken
    accent2: str  # a fail, a split, the way back
    muted: str  # a step the risk does not allow


LIGHT = Theme("light", "#ffffff", "#f6f5f1", "#d8d4ca", "#1f2328", "#5b6670", "#2b64a8", "#a35a1f", "#c9c4b8")
DARK = Theme("dark", "#0d1117", "#161b22", "#30363d", "#e6edf3", "#9198a1", "#6ea8ff", "#e3955a", "#3b4048")

Icon = Callable[[Theme], str]


@dataclass(frozen=True)
class Node:
    """One symbol: where its square sits, how big it is, its icon, and the words that go with it."""

    cx: int
    top: int
    title: str
    line: str
    icon: Icon
    size: int = NODE
    caption: str = "below"  # or "right", for a node off the main way

    @property
    def cy(self) -> float:
        """Return the y of the square's centre."""
        return self.top + self.size / 2


# ---- icons ------------------------------------------------------------------------------------------------------


def _stroke(t: Theme, path: str, *, colour: str | None = None, width: int = 4) -> str:
    return (f'<path d="{path}" fill="none" stroke="{colour or t.text}" stroke-width="{width}" '
            'stroke-linecap="round" stroke-linejoin="round"/>')


def _icon_task(t: Theme) -> str:
    return (_stroke(t, "M22 10 h40 l18 18 v62 h-58 z") + _stroke(t, "M62 10 v18 h18")
            + _stroke(t, "M32 44 h36", colour=t.secondary) + _stroke(t, "M32 58 h28", colour=t.secondary)
            + f'<rect x="30" y="70" width="40" height="14" rx="7" fill="{t.accent2}"/>')


def _icon_stairs(t: Theme) -> str:
    bars = []
    for i, colour in enumerate((t.muted, t.accent, t.text, t.text)):
        height = 22 + i * 18
        bars.append(f'<rect x="{8 + i * 21}" y="{92 - height}" width="17" height="{height}" rx="3" fill="{colour}"'
                    + (' opacity="0.35"' if i > 1 else "") + "/>")
    return "".join(bars) + _stroke(t, "M37 18 v14 m-6 -6 l6 6 l6 -6", colour=t.accent)


def _icon_check(t: Theme) -> str:
    return (f'<circle cx="42" cy="42" r="27" fill="none" stroke="{t.text}" stroke-width="5"/>'
            + _stroke(t, "M62 62 L84 84", width=7) + _stroke(t, "M30 43 l9 9 l16 -18", colour=t.accent, width=5))


def _icon_log(t: Theme) -> str:
    return (_stroke(t, "M20 10 h52 v76 l-9 -6 l-8 6 l-9 -6 l-8 6 l-9 -6 l-9 6 z")
            + _stroke(t, "M30 30 h32", colour=t.secondary) + _stroke(t, "M30 44 h24", colour=t.secondary)
            + f'<text x="46" y="72" text-anchor="middle" font-family="{BODY_FONT}" font-size="22" font-weight="700" '
              f'fill="{t.accent}">$</text>')


def _icon_diff(t: Theme) -> str:
    return (_stroke(t, "M22 10 h40 l18 18 v62 h-58 z") + _stroke(t, "M62 10 v18 h18")
            + _stroke(t, "M32 46 h20 m-10 -10 v20", colour=t.accent) + _stroke(t, "M32 72 h20", colour=t.accent2))


def _lens(t: Theme, x: int) -> str:
    return (f'<circle cx="{x}" cy="40" r="16" fill="none" stroke="{t.text}" stroke-width="4"/>'
            + _stroke(t, f"M{x + 11} 52 l12 12", width=5))


def _icon_judges(t: Theme) -> str:
    return _lens(t, 20) + _lens(t, 62) + _stroke(t, "M46 8 v80", colour=t.secondary, width=3)


def _icon_gavel(t: Theme) -> str:
    return (f'<rect x="30" y="12" width="40" height="22" rx="4" transform="rotate(35 50 23)" fill="{t.accent2}"/>'
            + _stroke(t, "M44 34 L18 72", width=6) + _stroke(t, "M48 84 h34", width=5))


def _icon_verdict(t: Theme) -> str:
    return (f'<circle cx="46" cy="46" r="32" fill="none" stroke="{t.text}" stroke-width="5"/>'
            + _stroke(t, "M31 47 l10 10 l20 -22", colour=t.accent, width=6))


def _icon_back(t: Theme) -> str:
    return _stroke(t, "M70 70 V42 a18 18 0 0 0 -18 -18 H24 m12 -12 l-12 12 l12 12", width=6)


# ---- drawing ----------------------------------------------------------------------------------------------------


def _arrow_head(x: float, y: float, colour: str, *, pointing: str) -> str:
    """Return a filled arrow head with its tip at (x, y), pointing right or down."""
    length, half = ARROW_LENGTH, ARROW_HALF_WIDTH
    right = f"{x},{y} {x - length},{y - half} {x - length},{y + half}"
    down = f"{x},{y} {x - half},{y - length} {x + half},{y - length}"
    return f'<polygon points="{right if pointing == "right" else down}" fill="{colour}"/>'


def _label(x: float, y: float, text: str, colour: str, *, anchor: str = "middle") -> str:
    return (f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-family="{BODY_FONT}" font-size="24" '
            f'font-weight="700" fill="{colour}">{text}</text>')


def _across(a: Node, b: Node, colour: str) -> str:
    """Return a straight arrow from square `a` to square `b`, both on the same row."""
    start, end = a.cx + a.size / 2 + GAP, b.cx - b.size / 2 - GAP
    return (f'<line x1="{start}" y1="{a.cy}" x2="{end - ARROW_LENGTH + 2}" y2="{a.cy}" stroke="{colour}" '
            'stroke-width="4"/>' + _arrow_head(end, a.cy, colour, pointing="right"))


def _bent(points: str, colour: str) -> str:
    return f'<path d="{points}" fill="none" stroke="{colour}" stroke-width="4" stroke-linejoin="round"/>'


def _router_wires(t: Theme, nodes: list[Node]) -> str:
    """Return the router's arrows: through the four steps, and a fail back up to the start."""
    task, start, check, log = nodes
    top, loop_y = start.top - GAP, 52
    return (_across(task, start, t.secondary) + _across(start, check, t.secondary) + _across(check, log, t.accent)
            + _label((check.cx + log.cx) / 2, check.cy - 16, "pass", t.accent)
            + _bent(f"M{check.cx} {top} V{loop_y} H{start.cx} V{top - ARROW_LENGTH + 2}", t.accent2)
            + _arrow_head(start.cx, top, t.accent2, pointing="down")
            + _label((start.cx + check.cx) / 2, loop_y - 14, "fail: one step up, with the reason", t.accent2))


def _tribunal_wires(t: Theme, nodes: list[Node]) -> str:
    """Return the tribunal's arrows: through the main row, and a split over the arbiter to the verdict."""
    change, judges, verdict, back, arbiter = nodes
    top = verdict.top - GAP
    arbiter_left, arbiter_right = arbiter.cx - arbiter.size / 2 - GAP, arbiter.cx + arbiter.size / 2 + GAP
    return (_across(change, judges, t.secondary) + _across(judges, verdict, t.accent)
            + _label((judges.cx + verdict.cx) / 2, judges.cy - 16, "agree", t.accent)
            + _across(verdict, back, t.secondary)
            + _bent(f"M{judges.cx} {judges.top - GAP} V{arbiter.cy} H{arbiter_left - ARROW_LENGTH + 2}", t.accent2)
            + _arrow_head(arbiter_left, arbiter.cy, t.accent2, pointing="right")
            + _label(judges.cx + 14, (judges.top + arbiter.cy) / 2 + 8, "split", t.accent2, anchor="start")
            + _bent(f"M{arbiter_right} {arbiter.cy} H{verdict.cx} V{top - ARROW_LENGTH + 2}", t.accent2)
            + _arrow_head(verdict.cx, top, t.accent2, pointing="down"))


@dataclass(frozen=True)
class Figure:
    """One figure: its file name, canvas, symbols and the arrows between them."""

    name: str
    width: int
    height: int
    nodes: list[Node]
    wires: Callable[[Theme, list[Node]], str]


ROUTER = Figure("flow", 1480, 480, [
    Node(200, 120, "Task", "kind and risk, declared<br>or read from the text", _icon_task),
    Node(560, 120, "Start cheap", "the cheapest model<br>and effort the risk allows", _icon_stairs),
    Node(920, 120, "Check", "a test, if there is one;<br>from high risk, a judge<br>on another model", _icon_check),
    Node(1280, 120, "Log", "every attempt, and<br>what the solved task cost", _icon_log),
], _router_wires)

TRIBUNAL = Figure("tribunal", 1480, 640, [
    Node(200, 250, "The change", "your request, the diff<br>and the changed files", _icon_diff),
    Node(560, 250, "Two judges", "one Claude, one Codex,<br>blind to each other", _icon_judges),
    Node(920, 250, "Verdict", "a pass is one line;<br>a fail lists the findings", _icon_verdict),
    Node(1280, 250, "Back to Claude", "on a fail, it fixes<br>what is right and says<br>why it disagrees", _icon_back),
    Node(740, 40, "Arbiter", "only on a split: reads the<br>work and both verdicts", _icon_gavel,
         size=SMALL_NODE, caption="right"),
], _tribunal_wires)

RIGHT_CAPTION_OFFSET = 210  # how far right of an off-row square its words start: past the arrow leaving it


def _node_html(node: Node, t: Theme) -> str:
    """Return the HTML of one symbol: its square with the icon, its title and its line."""
    square = (f'<div class="node" style="left:{node.cx - node.size / 2}px;top:{node.top}px;width:{node.size}px;'
              f'height:{node.size}px"><svg width="{ICON}" height="{ICON}" viewBox="0 0 {ICON} {ICON}">'
              f"{node.icon(t)}</svg></div>")
    if node.caption == "right":
        left = node.cx + node.size / 2 + GAP + RIGHT_CAPTION_OFFSET
        return (square + f'<div class="title" style="left:{left}px;top:{node.top - 4}px;text-align:left">'
                f'{node.title}</div><div class="line" style="left:{left}px;top:{node.top + 44}px;text-align:left">'
                f"{node.line}</div>")
    left = node.cx - CAPTION_W / 2
    return (square + f'<div class="title" style="left:{left}px;top:{node.top + node.size + 16}px">{node.title}</div>'
            f'<div class="line" style="left:{left}px;top:{node.top + node.size + 64}px">{node.line}</div>')


def page(figure: Figure, t: Theme) -> str:
    """Return the HTML of `figure` in theme `t`."""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
      html, body {{ margin:0; background:{t.page}; }}
      .canvas {{ position:relative; width:{figure.width}px; height:{figure.height}px; background:{t.page}; }}
      .node {{ position:absolute; box-sizing:border-box; border-radius:28px; background:{t.node};
               border:2px solid {t.border}; display:flex; align-items:center; justify-content:center; }}
      .title {{ position:absolute; width:{CAPTION_W}px; text-align:center; font:700 36px {TITLE_FONT};
                color:{t.text}; }}
      .line {{ position:absolute; width:{CAPTION_W}px; text-align:center; font:400 26px/1.3 {BODY_FONT};
               color:{t.secondary}; }}
      svg.wires {{ position:absolute; left:0; top:0; }}
    </style></head><body><div class="canvas">
      <svg class="wires" width="{figure.width}" height="{figure.height}">{figure.wires(t, figure.nodes)}</svg>
      {"".join(_node_html(node, t) for node in figure.nodes)}
    </div></body></html>"""


def capture(figure: Figure, t: Theme, out: Path) -> None:
    """Write `figure` in theme `t` to `out` as a PNG, through headless Chrome."""
    with tempfile.NamedTemporaryFile("w", suffix=".html", encoding="utf-8", delete=False) as handle:
        handle.write(page(figure, t))
    subprocess.run([CHROME, "--headless=new", f"--screenshot={out}", f"--window-size={figure.width},{figure.height}",
                    f"--force-device-scale-factor={SCALE}", "--hide-scrollbars", "--virtual-time-budget=1000",
                    f"file://{handle.name}"], check=True, capture_output=True)
    Path(handle.name).unlink()


def main() -> None:
    """Build every figure in both themes next to this script."""
    for figure in (ROUTER, TRIBUNAL):
        for theme in (LIGHT, DARK):
            out = HERE / f"{figure.name}-{theme.name}.png"
            capture(figure, theme, out)
            print(out)


if __name__ == "__main__":
    main()
