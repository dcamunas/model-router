"""Build the README's animated figures from one spec per frame: the tribunal flow and a rendering of a
real `router run`. Every frame is an HTML page on a 1920x1080 canvas with the same boxes in the same
places, so only what changes moves; headless Chrome captures it and Pillow stitches the GIFs (half
size, 64 colours) and, with --carousel, a PDF with one page per frame.

    uv run --with pillow docs/showcase/build.py [--carousel out.pdf]

Needs Google Chrome; no other tool. The terminal frames reveal the lines of `router-run.txt`, the exact
output of `router run --file docs/showcase/notice.md --kind write --risk critical` in a terminal of 126
columns (COLUMNS=126, what the drawn window holds at 22 px), never text typed in.
"""

from __future__ import annotations

import argparse
import html
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CANVAS = (1920, 1080)
GIF_WIDTH = 960
COLOURS = 64
FRAME_MS, LAST_MS = 1400, 2600

LIGHT, LIGHT2, DARK = "#f6f5f1", "#eceae4", "#1d2733"
TEXT, SECONDARY, ACCENT, ACCENT2, LINE = "#1f2328", "#5b6670", "#2b64a8", "#a35a1f", "#7d858e"
TITLE_FONT = "Georgia, 'Times New Roman', serif"
BODY_FONT = "'Helvetica Neue', Helvetica, Arial, sans-serif"
MONO_FONT = "Menlo, 'SF Mono', Consolas, monospace"


@dataclass(frozen=True)
class Box:
    """One box of the diagram: where it is and what it says; the same in every frame."""

    key: str
    x: int
    y: int
    w: int
    h: int
    title: str
    sub: str = ""


# The tribunal, left to right: the same seven boxes in every frame.
BOXES = [
    Box("task", 96, 440, 300, 150, "task", "write · critical"),
    Box("worker", 476, 440, 300, 150, "worker", "opus/medium"),
    Box("a", 836, 290, 360, 150, "judge A", "opus/medium, fresh context"),
    Box("b", 836, 590, 360, 150, "judge B", "fable/low, another model"),
    Box("arbiter", 1256, 440, 300, 150, "arbiter", "fable/medium"),
    Box("verdict", 1636, 440, 200, 150, "verdict", ""),
]
EDGES = [("task", "worker"), ("worker", "a"), ("worker", "b"), ("a", "arbiter"), ("b", "arbiter"), ("arbiter", "verdict")]


@dataclass(frozen=True)
class Frame:
    """What is lit in one frame: active boxes, passes, fails, which edges carry flow, and the caption."""

    caption: str
    active: frozenset[str] = frozenset()
    passed: frozenset[str] = frozenset()
    failed: frozenset[str] = frozenset()
    flowing: frozenset[tuple[str, str]] = frozenset()
    notes: dict[str, str] = field(default_factory=dict)


TRIBUNAL = [
    Frame("A critical writing task starts on the cell the policy picks: opus/medium.",
          active={"task", "worker"}, flowing={("task", "worker")}),
    Frame("Two judges read the answer, each in a fresh context, blind to each other.",
          active={"a", "b"}, passed={"worker"}, flowing={("worker", "a"), ("worker", "b")}),
    Frame("They split: judge A passes it, judge B fails it for a fact the task did not give.",
          passed={"worker", "a"}, failed={"b"}, notes={"a": "pass", "b": "fail"}),
    Frame("The arbiter reads the task, the answer and both reasoned verdicts, without the judges' names.",
          active={"arbiter"}, passed={"worker", "a"}, failed={"b"}, flowing={("a", "arbiter"), ("b", "arbiter")},
          notes={"a": "pass", "b": "fail"}),
    Frame("Verdict: pass, siding with judge A. One extra call, only on a split.",
          passed={"worker", "a", "arbiter", "verdict"}, failed={"b"}, flowing={("arbiter", "verdict")},
          notes={"a": "pass", "b": "fail", "arbiter": "sides with A", "verdict": "pass"}),
    Frame("Every judge's own verdict goes to the ledger; `router stats` keeps the scoreboard.",
          passed={"worker", "a", "arbiter", "verdict"}, failed={"b"},
          notes={"a": "pass", "b": "fail", "arbiter": "sides with A", "verdict": "pass"}),
]


def _box_html(box: Box, frame: Frame) -> str:
    fill, edge, colour = LIGHT2, LINE, TEXT
    if box.key in frame.active:
        fill, edge, colour = ACCENT, ACCENT, LIGHT
    elif box.key in frame.failed:
        fill, edge, colour = LIGHT, ACCENT2, ACCENT2
    elif box.key in frame.passed:
        fill, edge, colour = LIGHT, ACCENT, ACCENT
    sub_colour = LIGHT if box.key in frame.active else SECONDARY
    note = frame.notes.get(box.key, "")
    note_top = box.y - 44 if box.key == "a" else box.y + box.h + 12  # judge A's note goes above, away from the divider
    note_html = (f'<div style="position:absolute;left:{box.x}px;top:{note_top}px;width:{box.w}px;'
                 f'text-align:center;font:600 28px {BODY_FONT};color:{edge}">{html.escape(note)}</div>') if note else ""
    return (f'<div style="position:absolute;left:{box.x}px;top:{box.y}px;width:{box.w}px;height:{box.h}px;'
            f'box-sizing:border-box;border:4px solid {edge};border-radius:12px;background:{fill};padding:0 16px;'
            f'display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;text-align:center">'
            f'<div style="font:600 34px {BODY_FONT};color:{colour}">{html.escape(box.title)}</div>'
            f'<div style="font:26px/1.2 {BODY_FONT};color:{sub_colour}">{html.escape(box.sub)}</div></div>{note_html}')


def _edges_svg(frame: Frame) -> str:
    by_key = {b.key: b for b in BOXES}
    lines = []
    for src, dst in EDGES:
        a, b = by_key[src], by_key[dst]
        x1, y1 = a.x + a.w, a.y + a.h // 2
        x2, y2 = b.x, b.y + b.h // 2
        colour = ACCENT if (src, dst) in frame.flowing else LINE
        width = 8 if (src, dst) in frame.flowing else 4
        lines.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{colour}" stroke-width="{width}"/>')
    blind = (f'<line x1="1016" y1="452" x2="1016" y2="578" stroke="{LINE}" stroke-width="4" stroke-dasharray="14 12"/>')
    return (f'<svg style="position:absolute;left:0;top:0" width="{CANVAS[0]}" height="{CANVAS[1]}">'
            + "".join(lines) + blind + "</svg>")


def tribunal_html(frame: Frame, index: int) -> str:
    """Return the page of one tribunal frame: title, the fixed diagram with this frame's state, a caption."""
    boxes = "".join(_box_html(b, frame) for b in BOXES)
    blind_label = (f'<div style="position:absolute;left:1040px;top:500px;width:260px;text-align:left;'
                   f'font:24px {BODY_FONT};color:{SECONDARY}">blind to each other</div>')
    title = (f'<div style="position:absolute;left:96px;top:96px;font:64px {TITLE_FONT};color:{TEXT}">'
             f'Tribunal: two blind judges and an arbiter</div>')
    subtitle = (f'<div style="position:absolute;left:96px;top:184px;font:32px {BODY_FONT};color:{SECONDARY}">'
                f'model-router · verification at critical risk when no checker can decide</div>')
    caption = (f'<div style="position:absolute;left:96px;top:860px;width:1728px;font:36px {BODY_FONT};color:{TEXT}">'
               f'{html.escape(frame.caption)}</div>')
    footer = (f'<div style="position:absolute;left:96px;top:980px;font:24px {BODY_FONT};color:{SECONDARY}">'
              f'{index + 1} / {len(TRIBUNAL)} · github.com/dcamunas/model-router</div>')
    return _page(title + subtitle + _edges_svg(frame) + boxes + blind_label + caption + footer)


def _page(body: str) -> str:
    return (f'<!doctype html><html><head><meta charset="utf-8"></head>'
            f'<body style="margin:0;width:{CANVAS[0]}px;height:{CANVAS[1]}px;background:{LIGHT};'
            f'position:relative;overflow:hidden">{body}</body></html>')


def terminal_html(command: str, lines: list[str]) -> str:
    """Return a terminal window on the canvas showing `command` and the output lines revealed so far."""
    shown = "\n".join(html.escape(line) for line in lines)
    window = (f'<div style="position:absolute;left:48px;top:72px;width:1824px;height:936px;background:{DARK};'
              f'border-radius:16px;box-sizing:border-box;padding:32px;overflow:hidden">')
    prompt = f'<div style="font:22px {MONO_FONT};color:#9fc0e8;white-space:pre-wrap">$ {html.escape(command)}</div>'
    output = (f'<pre style="margin:24px 0 0;font:22px/1.45 {MONO_FONT};color:{LIGHT};white-space:pre-wrap;'
              f'word-break:break-word">{shown}</pre>')
    return _page(window + prompt + output + "</div>")


def terminal_frames(command: str, output: str) -> list[str]:
    """Split a real output into the reveal steps: the command alone, the answer, the attempts table, the status line."""
    lines = output.rstrip("\n").split("\n")
    table_start = next((i for i, line in enumerate(lines) if line.startswith("#  cell")), len(lines))
    steps = [[], lines[:table_start], lines[: table_start + 3], lines]
    return [terminal_html(command, step) for step in steps]


def capture(pages: list[str], workdir: Path, name: str) -> list[Path]:
    """Write each page to disk and screenshot it with headless Chrome; return the PNG paths."""
    out = []
    for i, page in enumerate(pages):
        page_path = workdir / f"{name}-{i}.html"
        png = workdir / f"{name}-{i}.png"
        page_path.write_text(page, encoding="utf-8")
        subprocess.run(
            [CHROME, "--headless=new", f"--screenshot={png}", f"--window-size={CANVAS[0]},{CANVAS[1]}",
             "--virtual-time-budget=1500", "--hide-scrollbars", page_path.as_uri()],
            check=True, capture_output=True,
        )
        out.append(png)
    return out


def gif(pngs: list[Path], target: Path, *, crop_height: int | None = None) -> int:
    """Stitch the PNGs into a looping GIF at half size and return its size in bytes."""
    frames = []
    for png in pngs:
        image = Image.open(png).convert("RGB")
        if crop_height:
            image = image.crop((0, 0, CANVAS[0], crop_height))
        scale = GIF_WIDTH / image.width
        image = image.resize((GIF_WIDTH, round(image.height * scale)), Image.LANCZOS)
        frames.append(image.quantize(colors=COLOURS, method=Image.Quantize.MEDIANCUT))
    durations = [FRAME_MS] * (len(frames) - 1) + [LAST_MS]
    frames[0].save(target, save_all=True, append_images=frames[1:], duration=durations, loop=0, optimize=True)
    return target.stat().st_size


def carousel(pngs: list[Path], target: Path) -> None:
    """Save the full-size frames as one PDF page each, the shape LinkedIn wants for a carousel."""
    pages = [Image.open(p).convert("RGB") for p in pngs]
    pages[0].save(target, save_all=True, append_images=pages[1:], resolution=96)


def main(argv: list[str] | None = None) -> int:
    """Build docs/showcase/tribunal.gif and docs/showcase/router-run.gif (and the carousel when asked)."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--carousel", type=Path, help="also write the tribunal frames as a PDF carousel here")
    args = parser.parse_args(argv)
    if not Path(CHROME).exists():
        print(f"Google Chrome not found at {CHROME}", file=sys.stderr)
        return 1
    output = (HERE / "router-run.txt").read_text(encoding="utf-8")
    command = "router run --file docs/showcase/notice.md --kind write --risk critical"
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        tribunal_pngs = capture([tribunal_html(f, i) for i, f in enumerate(TRIBUNAL)], work, "tribunal")
        size = gif(tribunal_pngs, HERE / "tribunal.gif")
        print(f"tribunal.gif: {len(tribunal_pngs)} frames, {size / 1024:.0f} KB")
        run_pngs = capture(terminal_frames(command, output), work, "run")
        size = gif(run_pngs, HERE / "router-run.gif")
        print(f"router-run.gif: {len(run_pngs)} frames, {size / 1024:.0f} KB")
        if args.carousel:
            carousel(tribunal_pngs, args.carousel)
            print(f"carousel: {args.carousel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
