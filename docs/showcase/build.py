"""Build the README's two animated figures. Every frame is an HTML page on a 1920x1080 canvas; headless Chrome
captures it and Pillow stitches the GIF (half size, 64 colours).

    uv run --with pillow docs/showcase/build.py [--carousel out.pdf] [--frames DIR]

Needs Google Chrome; no other tool.

- `sprint.gif`: the router's rules told as sprint planning, an illustration. The same board in every frame, so
  only what changes moves; each ticket's plan is what `router plan` gives with the code tables and all four
  models. `--carousel` also writes its frames as a PDF, one page each.
- `router-run.gif`: the lines of `router-run.txt`, the exact output of
  `router run --file docs/showcase/notice.md --kind write --risk critical` in a terminal of 126 columns
  (COLUMNS=126, what the drawn window holds at 22 px), never text typed in.
"""

from __future__ import annotations

import argparse
import html
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CANVAS = (1920, 1080)
GIF_WIDTH = 960
COLOURS = 64
FRAME_MS, LAST_MS = 1400, 2600

SPRINT_FRAME_MS, SPRINT_LAST_MS = 2600, 3800  # a sprint frame has a sentence and a rule to read

LIGHT, LIGHT2, DARK = "#f6f5f1", "#eceae4", "#1d2733"
TEXT, SECONDARY, ACCENT, ACCENT2, LINE, MUTED = "#1f2328", "#5b6670", "#2b64a8", "#a35a1f", "#7d858e", "#c9c4b8"
MODEL_COLOUR = {"haiku": "#8a9bb0", "sonnet": "#7b5ea7", "opus": DARK, "fable": ACCENT}  # as in the tribunal's figures
TITLE_FONT = "Georgia, 'Times New Roman', serif"
BODY_FONT = "'Helvetica Neue', Helvetica, Arial, sans-serif"
MONO_FONT = "Menlo, 'SF Mono', Consolas, monospace"


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


# ---- the sprint: the rules as an illustration -------------------------------------------------------------------

STAFF = [("Junior developer", "haiku"), ("Developer", "sonnet"), ("Senior developer", "opus"), ("Staff engineer", "fable")]
STAFF_X = [560, 860, 1160, 1460]  # left edge of each person's box
STAFF_Y, STAFF_W, STAFF_H = 330, 260, 300
DESK = (90, 400, 380, 190)  # x, y, width, height
REPORT = (1460, 775, 260, 160)
NOTE_Y = STAFF_Y + STAFF_H + 70


@dataclass(frozen=True)
class SprintFrame:
    """One frame of the sprint: the sentence on top, the router's rule underneath, the ticket and what happens."""

    head: str
    rule: str
    ticket: str
    tag: str  # its kind and risk
    plan: str  # the cell `router plan` gives it
    active: int | None = None  # who does the work, by index in STAFF
    muted: tuple[int, ...] = ()  # who the risk does not allow
    judge: int | None = None  # who reviews it
    escalate: tuple[int, int] | None = None  # from, to, after a failed check
    note: str = ""  # under the active box, or under the one the ticket climbs to
    fail: str = ""  # under the box whose check failed
    report: bool = False


SPRINT = [
    SprintFrame("A ticket comes in: sort 40 bug reports by area. Low risk.",
                "the hook reads the task's kind and its risk",
                "Sort 40 bug reports", "classify · low risk", "haiku/low"),
    SprintFrame("A junior takes it, with a short timebox. No review.",
                "low risk: the cheapest model that does the job, at low effort, with no check",
                "Sort 40 bug reports", "classify · low risk", "haiku/low", active=0, note="short timebox · low effort"),
    SprintFrame("Payments migration. However full the sprint, seniors only.",
                "a critical task never starts below opus/medium: the risk floor",
                "Migrate payments", "code · critical risk", "opus/high", active=2, muted=(0, 1),
                note="senior · high effort"),
    SprintFrame("It does not merge until someone else reviews it.",
                "critical work is checked by a judge on another model: opus/high checked by fable/low",
                "Migrate payments", "code · critical risk", "opus/high", active=2, muted=(0, 1), judge=3),
    SprintFrame("CI goes red on the CSV export. A senior takes it, with the log.",
                "a failed check climbs the ladder (sonnet/medium, then opus/low), with the reason, within budget",
                "CSV export", "code · low risk", "sonnet/medium", active=1, muted=(0,), escalate=(1, 2),
                note="takes it with the failing log", fail="✗ CI fails"),
    SprintFrame("Every ticket is logged. The retro moves CSV work up a level.",
                "the ledger keeps every run and its cost; each repository learns from its own relaunches",
                "Next CSV ticket", "code · low risk", "opus/low", active=2, note="this repo starts here", report=True),
]


def _box(x: int, y: int, size: tuple[int, int], inner: str, *, border: str, bg: str = "#ffffff", width: int = 3,
         dashed: bool = False) -> str:
    style = "dashed" if dashed else "solid"
    return (f'<div style="position:absolute;left:{x}px;top:{y}px;width:{size[0]}px;height:{size[1]}px;'
            f'box-sizing:border-box;border:{width}px {style} {border};border-radius:18px;background:{bg};padding:22px">'
            f"{inner}</div>")


def _line(content: str, *, size: int, colour: str = TEXT, font: str = BODY_FONT, weight: int = 400, top: int = 0) -> str:
    return f'<div style="margin-top:{top}px;font:{weight} {size}px/1.3 {font};color:{colour}">{content}</div>'


def _at(x: int, y: int, width: int, content: str) -> str:
    return f'<div style="position:absolute;left:{x}px;top:{y}px;width:{width}px">{content}</div>'


def _svg(path: str, colour: str, *, dashed: bool = False, head: bool = True) -> str:
    """Return a full-canvas SVG layer with one stroked path, an arrowhead at its end unless `head` is False."""
    marker_id = f"m{colour[1:]}"  # one marker per colour; a repeated definition is the same arrowhead
    marker = (f'<defs><marker id="{marker_id}" markerWidth="10" markerHeight="10" refX="8" refY="5" orient="auto">'
              f'<path d="M0,0 L10,5 L0,10 z" fill="{colour}"/></marker></defs>') if head else ""
    dash = ' stroke-dasharray="14 10"' if dashed else ""
    end = f' marker-end="url(#{marker_id})"' if head else ""
    return (f'<svg style="position:absolute;left:0;top:0" width="{CANVAS[0]}" height="{CANVAS[1]}">{marker}'
            f'<path d="{path}" fill="none" stroke="{colour}" stroke-width="5" stroke-linejoin="round"{dash}{end}/></svg>')


def _sprint_paths(frame: SprintFrame) -> list[str]:
    """Return the arrows of a frame: desk to the worker, the review arc, the climb, the line to the report."""
    centre = [x + STAFF_W // 2 for x in STAFF_X]
    dx, dy, dw, dh = DESK
    x1, y1 = dx + dw + 8, dy + dh // 2
    out = []
    if frame.active == 0:
        out.append(_svg(f"M{x1},{y1} C{x1 + 60},{y1} {STAFF_X[0] - 70},{STAFF_Y + STAFF_H // 2} "
                        f"{STAFF_X[0] - 8},{STAFF_Y + STAFF_H // 2}", ACCENT))
    elif frame.active is not None:  # under the row, then up into the box: never across the people it skips
        out.append(_svg(f"M{x1},{y1} H{x1 + 50} V{STAFF_Y + STAFF_H + 44} H{centre[frame.active]} "
                        f"V{STAFF_Y + STAFF_H + 12}", ACCENT))
    arcs = [(frame.active, frame.judge, ACCENT)] if frame.judge is not None else []
    arcs += [(frame.escalate[0], frame.escalate[1], ACCENT2)] if frame.escalate else []
    for start, end, colour in arcs:
        if start is not None and end is not None:
            y = STAFF_Y - 10
            out.append(_svg(f"M{centre[start]},{y} C{centre[start]},{y - 80} {centre[end]},{y - 80} {centre[end]},{y}",
                            colour, dashed=True))
    if frame.report and frame.active is not None:
        rx, ry, _, rh = REPORT
        sx = STAFF_X[frame.active] + STAFF_W
        out.append(_svg(f"M{sx},{STAFF_Y + STAFF_H - 30} H{sx + 20} V{ry + rh // 2} H{rx - 12}", ACCENT, dashed=True,
                        head=False))
    return out


def _person(i: int, frame: SprintFrame) -> str:
    role, model = STAFF[i]
    lit = i in (frame.active, frame.judge) or bool(frame.escalate and i == frame.escalate[1])
    off = i in frame.muted
    chip = (f'<div style="display:inline-block;padding:4px 14px;border-radius:999px;'
            f'background:{MUTED if off else MODEL_COLOUR[model]};color:#fff;font:600 24px {BODY_FONT}">{model}</div>')
    inner = chip + _line(role, size=30, weight=600, colour=SECONDARY if off else TEXT, top=22)
    if off:
        inner += _line("not for this ticket", size=24, colour=ACCENT2, top=18)
    if i == frame.judge:
        inner += _line("reviews it", size=24, colour=ACCENT, weight=600, top=18)
    border = MUTED if off else (MODEL_COLOUR[model] if lit else LINE)
    return _box(STAFF_X[i], STAFF_Y, (STAFF_W, STAFF_H), inner, border=border, bg=LIGHT2 if off else "#ffffff",
                width=6 if lit else 3, dashed=off)


def _notes(frame: SprintFrame) -> list[str]:
    out = []
    if frame.escalate:
        source, target = frame.escalate
        out.append(_at(STAFF_X[source], NOTE_Y, 290, _line(html.escape(frame.fail), size=26, colour=ACCENT2, weight=600)))
        out.append(_at(STAFF_X[target], NOTE_Y, 560, _line(html.escape(frame.note), size=26, colour=ACCENT2)))
    elif frame.note and frame.active is not None:
        out.append(_at(STAFF_X[frame.active], NOTE_Y, 560, _line(html.escape(frame.note), size=26, colour=ACCENT)))
    return out


def sprint_html(frame: SprintFrame, index: int) -> str:
    """Return frame `index` of the sprint as an HTML page: arrows first, so the boxes sit on top of them."""
    header = (_at(90, 50, 1500, _line("Illustration · the router as sprint planning", size=28, colour=SECONDARY))
              + _at(1650, 50, 180, f'<div style="text-align:right;font:26px {BODY_FONT};color:{SECONDARY}">'
                                   f"{index + 1} / {len(SPRINT)}</div>")
              + _at(90, 96, 1740, _line(html.escape(frame.head), size=50, font=TITLE_FONT)))
    ticket = _box(90, 214, (380, 158), _line(html.escape(frame.ticket), size=30, weight=600)
                  + _line(html.escape(frame.tag), size=24, colour=SECONDARY, font=MONO_FONT, top=6)
                  + _line(f"plan: {html.escape(frame.plan)}", size=24, colour=ACCENT2, font=MONO_FONT, top=2),
                  border=ACCENT2)
    dx, dy, dw, dh = DESK
    desk = _box(dx, dy, (dw, dh), _line("Sprint planning", size=32, weight=600)
                + _line("what the ticket is, what a bug would cost", size=24, colour=SECONDARY, top=10), border=TEXT)
    rx, ry, rw, rh = REPORT
    report = _box(rx, ry, (rw, rh), _line("Sprint report", size=30, weight=600,
                                          colour=TEXT if frame.report else SECONDARY)
                  + _line("every ticket, with what it cost", size=24, colour=SECONDARY, top=8),
                  border=ACCENT if frame.report else MUTED, width=6 if frame.report else 3)
    rule = (f'<div style="position:absolute;left:0;top:960px;width:{CANVAS[0]}px;height:120px;background:{DARK}"></div>'
            + _at(90, 994, 1740, f'<div style="font:32px {BODY_FONT};color:{LIGHT}"><span style="color:#9fc0e8">'
                                 f"router:</span> {html.escape(frame.rule)}</div>"))
    body = (header + "".join(_sprint_paths(frame)) + ticket + desk + "".join(_person(i, frame) for i in range(len(STAFF)))
            + "".join(_notes(frame)) + report + rule)
    return _page(body)


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


def gif(pngs: list[Path], target: Path, *, frame_ms: int = FRAME_MS, last_ms: int = LAST_MS) -> int:
    """Stitch the PNGs into a looping GIF at half size and return its size in bytes."""
    frames = []
    for png in pngs:
        image = Image.open(png).convert("RGB")
        scale = GIF_WIDTH / image.width
        image = image.resize((GIF_WIDTH, round(image.height * scale)), Image.LANCZOS)
        frames.append(image.quantize(colors=COLOURS, method=Image.Quantize.MEDIANCUT))
    durations = [frame_ms] * (len(frames) - 1) + [last_ms]
    frames[0].save(target, save_all=True, append_images=frames[1:], duration=durations, loop=0, optimize=True)
    return target.stat().st_size


def carousel(pngs: list[Path], target: Path) -> None:
    """Save the full-size frames as one PDF page each, the shape LinkedIn wants for a carousel."""
    pages = [Image.open(p).convert("RGB") for p in pngs]
    pages[0].save(target, save_all=True, append_images=pages[1:], resolution=96)


def main(argv: list[str] | None = None) -> int:
    """Build docs/showcase/sprint.gif and router-run.gif (and the carousel and the frames when asked)."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--carousel", type=Path, help="also write the sprint's frames as a PDF carousel here")
    parser.add_argument("--frames", type=Path, help="keep the full-size PNG frames in this directory")
    args = parser.parse_args(argv)
    if not Path(CHROME).exists():
        print(f"Google Chrome not found at {CHROME}", file=sys.stderr)
        return 1
    output = (HERE / "router-run.txt").read_text(encoding="utf-8")
    command = "router run --file docs/showcase/notice.md --kind write --risk critical"
    with tempfile.TemporaryDirectory() as tmp:
        work = args.frames or Path(tmp)
        work.mkdir(parents=True, exist_ok=True)
        sprint_pngs = capture([sprint_html(f, i) for i, f in enumerate(SPRINT)], work, "sprint")
        size = gif(sprint_pngs, HERE / "sprint.gif", frame_ms=SPRINT_FRAME_MS, last_ms=SPRINT_LAST_MS)
        print(f"sprint.gif: {len(sprint_pngs)} frames, {size / 1024:.0f} KB")
        if args.carousel:
            carousel(sprint_pngs, args.carousel)
            print(f"carousel: {args.carousel}")
        run_pngs = capture(terminal_frames(command, output), work, "run")
        size = gif(run_pngs, HERE / "router-run.gif")
        print(f"router-run.gif: {len(run_pngs)} frames, {size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
