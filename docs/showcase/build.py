"""Build the README's animated figures from one spec per frame: a critical task on trial (the tribunal
as a courtroom, every seat labelled with its cell) and a rendering of a real `router run`. Every frame
is an HTML page on a 1920x1080 canvas with the same seats in the same places, so only what changes
moves; headless Chrome captures it and Pillow stitches the GIFs (half size, 64 colours) and, with
--carousel, a PDF with one page per frame of the trial.

    uv run --with pillow docs/showcase/build.py [--carousel out.pdf] [--frames DIR] [--lang en]

Needs Google Chrome; no other tool. The terminal frames reveal the lines of `router-run.txt`, the exact
output of `router run --file docs/showcase/notice.md --kind write --risk critical` in a terminal of 126
columns (COLUMNS=126, what the drawn window holds at 22 px), never text typed in. The trial is an
example case on that same task: the cells are what `router plan` gives for it (worker opus/medium,
judges sonnet/low and fable/low, arbiter fable/medium; after a fail, worker fable/low with judges
sonnet/low and opus/xhigh); the split and the retry are made up to show the arbiter and the ladder.
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
TRIAL_FRAME_MS, TRIAL_LAST_MS = 2400, 3600  # two-line captions take longer to read

LIGHT, LIGHT2, DARK = "#f6f5f1", "#eceae4", "#1d2733"
TEXT, SECONDARY, ACCENT, ACCENT2, LINE = "#1f2328", "#5b6670", "#2b64a8", "#a35a1f", "#7d858e"
MODEL_COLOUR = {"opus": DARK, "fable": ACCENT, "sonnet": "#5a7fa8", "haiku": "#8a9bb0"}
TITLE_FONT = "Georgia, 'Times New Roman', serif"
BODY_FONT = "'Helvetica Neue', Helvetica, Arial, sans-serif"
MONO_FONT = "Menlo, 'SF Mono', Consolas, monospace"
FIGURE_W, FIGURE_H = 150, 170
PRESENT_OPACITY = 0.55  # a seat that is on stage but not acting in this frame


@dataclass(frozen=True)
class Seat:
    """One person of the trial: where they stand, what they are called, whether they wear a robe."""

    key: str
    cx: int
    top: int
    robe: bool
    gavel: bool = False


@dataclass(frozen=True)
class Doc:
    """One sheet of paper: the task, the answer, a judge's copy, the arbiter's file, the ledger."""

    key: str
    x: int
    y: int
    w: int = 200
    h: int = 170


SEATS = [
    Seat("worker", 400, 575, robe=False),
    Seat("a", 520, 272, robe=True),
    Seat("b", 1000, 272, robe=True),
    Seat("arbiter", 1420, 272, robe=True, gavel=True),
    Seat("you", 1000, 575, robe=False),
]
DOCS = [
    Doc("task", 72, 585),
    Doc("answer", 520, 585),
    Doc("doc_a", 610, 285, h=200),
    Doc("doc_b", 1090, 285, h=200),
    Doc("doc_arb", 1520, 285, h=200),
    Doc("ledger", 1210, 590, w=640, h=170),
]
WALL_X, WALL_TOP, WALL_BOTTOM = 868, 272, 470
# Polylines between the pieces; drawn grey in every frame, in the accent colour when they carry flow.
EDGES = {
    "task-worker": [(272, 672), (325, 672)],
    "worker-answer": [(475, 672), (520, 672)],
    "answer-a": [(620, 585), (620, 548), (710, 548), (710, 485)],
    "answer-b": [(620, 585), (620, 548), (1190, 548), (1190, 485)],
    "a-arbiter": [(710, 285), (710, 248), (1620, 248), (1620, 285)],
    "b-arbiter": [(1190, 285), (1190, 248), (1620, 248), (1620, 285)],
    "arbiter-ledger": [(1420, 528), (1420, 590)],
    "you-ledger": [(1075, 672), (1210, 672)],
}
DEFAULT_CELLS = {"worker": "opus/medium", "a": "sonnet/low", "b": "fable/low", "arbiter": "fable/medium",
                 "you": "router feedback --ko"}

# Every word on the canvas, so a translation for a carousel elsewhere is one dict, not a copy of the code.
EN = {
    "title": "A critical task on trial",
    "subtitle": "model-router · an example case · every seat is a cell, model/effort, chosen per task",
    "legend": [("sonnet", MODEL_COLOUR["sonnet"]), ("opus", DARK), ("fable", ACCENT), ("you", ACCENT2)],
    "roles": {"worker": "worker", "a": "judge A", "b": "judge B", "arbiter": "arbiter", "you": "you"},
    "blind": "blind",
    "labels": {"task": "the task · write · critical", "answer": "the answer", "ledger": "ledger · scoreboard"},
    "answer2": "the answer, take 2",
    "task_lines": ["customer notice", "about an outage"],
    "pass": "PASS", "fail": "FAIL",
    "reason_pass": ["facts covered,", "nothing added"],
    "reason_fail": ["one sentence", "not in", "the facts"],
    "arb_lines": ["judge A: pass", "judge B: fail"],
    "arb_sides": "sides with B",
    "ledger_lines": ["1 opus/medium A pass B fail arbiter fail", "2 fable/low A pass B pass solved"],
    "footer": "github.com/dcamunas/model-router",
    "captions": [
        "A task comes in: a customer notice about an outage. The router reads it as kind write, risk critical, "
        "and starts on the cheapest cell that should do: opus/medium.",
        "The worker writes the answer. At low or medium risk that would be the end. Critical prose has no "
        "programmatic check, so the answer goes to trial.",
        "Two judges read the task and the answer, each in a fresh context: judge A on a third model at low effort, "
        "judge B on another model. Neither knows the other exists.",
        "They split. Judge A passes it. Judge B fails it: “We apologise for the disruption” is not in the facts.",
        "On a split, the arbiter reads the task, the answer and both reasoned verdicts, with no names on them. "
        "One extra call, only on a split.",
        "Verdict: fail, siding with judge B. The task climbs the ladder. Next cell: fable/low.",
        "Second attempt on fable/low. The seats follow the worker: judge A sonnet/low, judge B opus/xhigh. "
        "Both pass. Solved.",
        "Every verdict and its cost go to the ledger. The scoreboard says which judge was right. router feedback "
        "--ko is your appeal, and it counts against the judges you overrule.",
    ],
}


@dataclass(frozen=True)
class TrialFrame:
    """What is on stage in one frame: who acts, which papers exist, what is stamped on them."""

    caption: int  # index into strings["captions"]
    lit: frozenset[str] = frozenset()
    docs: frozenset[str] = frozenset()
    stamps: dict[str, str] = field(default_factory=dict)  # doc key -> "pass" | "fail"
    content: dict[str, str] = field(default_factory=dict)  # doc key -> strings key with its lines
    cells: dict[str, str] = field(default_factory=dict)  # seat key -> cell, when not the default
    flowing: frozenset[str] = frozenset()
    take2: bool = False
    sides: bool = False
    ledger: bool = False


_SPLIT = {"doc_a": "pass", "doc_b": "fail"}
_SPLIT_CONTENT = {"doc_a": "reason_pass", "doc_b": "reason_fail", "doc_arb": "arb_lines"}
_TAKE2_CELLS = {"worker": "fable/low", "a": "sonnet/low", "b": "opus/xhigh"}
_TAKE2 = {"doc_a": "pass", "doc_b": "pass"}
_TAKE2_CONTENT = {"doc_a": "reason_pass", "doc_b": "reason_pass"}

TRIAL = [
    TrialFrame(0, lit={"task"}, docs={"task"}),
    TrialFrame(1, lit={"worker", "answer"}, docs={"task", "answer"}, flowing={"task-worker", "worker-answer"}),
    TrialFrame(2, lit={"a", "b", "doc_a", "doc_b"}, docs={"task", "answer", "doc_a", "doc_b"},
               flowing={"answer-a", "answer-b"}),
    TrialFrame(3, lit={"a", "b", "doc_a", "doc_b"}, docs={"task", "answer", "doc_a", "doc_b"}, stamps=_SPLIT,
               content={"doc_a": "reason_pass", "doc_b": "reason_fail"}),
    TrialFrame(4, lit={"arbiter", "doc_arb"}, docs={"task", "answer", "doc_a", "doc_b", "doc_arb"}, stamps=_SPLIT,
               content=_SPLIT_CONTENT, flowing={"a-arbiter", "b-arbiter"}),
    TrialFrame(5, lit={"arbiter", "doc_arb"}, docs={"task", "answer", "doc_a", "doc_b", "doc_arb"},
               stamps={**_SPLIT, "doc_arb": "fail"}, content=_SPLIT_CONTENT, sides=True),
    TrialFrame(6, lit={"worker", "answer", "a", "b", "doc_a", "doc_b"}, docs={"task", "answer", "doc_a", "doc_b"},
               stamps=_TAKE2, content=_TAKE2_CONTENT, cells=_TAKE2_CELLS, take2=True,
               flowing={"worker-answer", "answer-a", "answer-b"}),
    TrialFrame(7, lit={"ledger", "you"}, docs={"task", "answer", "doc_a", "doc_b", "ledger"},
               stamps=_TAKE2, content=_TAKE2_CONTENT, cells=_TAKE2_CELLS, take2=True, ledger=True,
               flowing={"arbiter-ledger", "you-ledger"}),
]


def _model_colour(cell: str) -> str:
    return MODEL_COLOUR.get(cell.split("/")[0], SECONDARY)


def _figure_svg(seat: Seat, cell: str, lit: bool) -> str:
    """Draw a person as a pictogram: head, a robe with a white collar for the bench, a plain coat otherwise."""
    colour = ACCENT2 if seat.key == "you" else _model_colour(cell)
    head = f'<circle cx="75" cy="40" r="30" fill="{LIGHT2}" stroke="{TEXT}" stroke-width="3"/>'
    if seat.robe:
        body = (f'<path d="M 22 170 L 44 80 Q 75 62 106 80 L 128 170 Z" fill="{colour}"/>'
                f'<path d="M 62 82 L 75 112 L 88 82 Z" fill="{LIGHT}"/>')
    else:
        body = f'<path d="M 28 170 L 28 110 Q 28 80 58 78 L 92 78 Q 122 80 122 110 L 122 170 Z" fill="{colour}"/>'
    gavel = (f'<g transform="translate(118 96) rotate(-35)"><rect x="-6" y="-24" width="12" height="40" rx="3" '
             f'fill="{TEXT}"/><rect x="-22" y="-32" width="44" height="16" rx="4" fill="{TEXT}"/></g>') if seat.gavel else ""
    halo = f'<ellipse cx="75" cy="168" rx="92" ry="12" fill="{ACCENT}" opacity="0.3"/>' if lit else ""
    opacity = 1 if lit else PRESENT_OPACITY
    left = seat.cx - FIGURE_W // 2
    return (f'<svg style="position:absolute;left:{left}px;top:{seat.top}px;opacity:{opacity}" width="{FIGURE_W}" '
            f'height="{FIGURE_H + 14}" viewBox="0 0 {FIGURE_W} {FIGURE_H + 14}">{halo}{body}{head}{gavel}</svg>')


def _seat_labels(seat: Seat, role: str, cell: str, lit: bool) -> str:
    colour = TEXT if lit else SECONDARY
    badge_colour = ACCENT2 if seat.key == "you" else _model_colour(cell)
    top = seat.top + FIGURE_H + 10
    return (f'<div style="position:absolute;left:{seat.cx - 200}px;top:{top}px;width:400px;text-align:center;'
            f'font:600 30px {BODY_FONT};color:{colour}">{html.escape(role)}</div>'
            f'<div style="position:absolute;left:{seat.cx - 200}px;top:{top + 38}px;width:400px;text-align:center">'
            f'<span style="display:inline-block;padding:2px 14px;border:3px solid {badge_colour};border-radius:20px;'
            f'font:26px {MONO_FONT};color:{badge_colour};background:{LIGHT}">{html.escape(cell)}</span></div>')


def _stamp_html(doc: Doc, text: str, colour: str) -> str:
    return (f'<div style="position:absolute;left:{doc.x + doc.w - 160}px;top:{doc.y + doc.h - 74}px;'
            f'transform:rotate(-12deg);border:4px solid {colour};border-radius:10px;padding:0 14px;'
            f'font:700 34px {BODY_FONT};color:{colour};background:rgba(255,255,255,.88)">{html.escape(text)}</div>')


def _doc_html(doc: Doc, frame: TrialFrame, strings: dict) -> str:
    """One sheet with its lines (text when the frame gives them, grey bars otherwise), stamp and label."""
    if doc.key not in frame.docs:
        return ""
    lit = doc.key in frame.lit
    lines: list[str] = []
    if doc.key == "task":
        lines = strings["task_lines"]
    elif doc.key == "ledger" and frame.ledger:
        lines = strings["ledger_lines"]
    elif doc.key in frame.content:
        lines = strings[frame.content[doc.key]]
    if lines:
        font = f"24px/1.3 {MONO_FONT}" if doc.key == "ledger" else f"24px/1.3 {BODY_FONT}"
        body = "".join(f'<div style="font:{font};color:{TEXT}">{html.escape(line)}</div>' for line in lines)
    else:
        body = "".join(f'<div style="height:10px;border-radius:5px;background:{LIGHT2};margin:0 0 16px;width:{w}%"></div>'
                       for w in (90, 70, 85, 55))
    border = ACCENT if lit else LINE
    sheet = (f'<div style="position:absolute;left:{doc.x}px;top:{doc.y}px;width:{doc.w}px;height:{doc.h}px;'
             f'box-sizing:border-box;border:4px solid {border};border-radius:10px;background:white;padding:14px 16px;'
             f'opacity:{1 if lit or doc.key in frame.stamps else PRESENT_OPACITY}">{body}</div>')
    stamp = ""
    if doc.key in frame.stamps:
        verdict = frame.stamps[doc.key]
        stamp = _stamp_html(doc, strings[verdict], ACCENT if verdict == "pass" else ACCENT2)
    label, label_colour = strings["labels"].get(doc.key, ""), TEXT if lit else SECONDARY
    if doc.key == "answer" and frame.take2:
        label = strings["answer2"]
    if doc.key == "doc_arb" and frame.sides:  # the arbiter's ruling goes under the file, where no stamp covers it
        label, label_colour = strings["arb_sides"], ACCENT2
    label_html = (f'<div style="position:absolute;left:{doc.x - 30}px;top:{doc.y + doc.h + 8}px;width:{doc.w + 60}px;'
                  f'text-align:center;font:600 24px {BODY_FONT};color:{label_colour}">{html.escape(label)}</div>'
                  ) if label else ""
    return sheet + stamp + label_html


def _trial_edges_svg(frame: TrialFrame, blind: str) -> str:
    parts = []
    for key, points in EDGES.items():
        flowing = key in frame.flowing
        pts = " ".join(f"{x},{y}" for x, y in points)
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{ACCENT if flowing else LINE}" '
                     f'stroke-width="{8 if flowing else 4}" stroke-linejoin="round"/>')
    wall = (f'<line x1="{WALL_X}" y1="{WALL_TOP}" x2="{WALL_X}" y2="{WALL_BOTTOM}" stroke="{LINE}" stroke-width="4" '
            f'stroke-dasharray="14 12"/>')
    wall_label = (f'<text x="{WALL_X}" y="{WALL_BOTTOM + 30}" text-anchor="middle" font-family="Helvetica Neue, Helvetica, '
                  f'Arial, sans-serif" font-size="24" fill="{SECONDARY}">{html.escape(blind)}</text>')
    return (f'<svg style="position:absolute;left:0;top:0" width="{CANVAS[0]}" height="{CANVAS[1]}">'
            + "".join(parts) + wall + wall_label + "</svg>")


def trial_html(frame: TrialFrame, index: int, strings: dict = EN) -> str:
    """Return the page of one trial frame: title, legend, the fixed courtroom in this frame's state, a caption."""
    title = (f'<div style="position:absolute;left:96px;top:96px;font:64px {TITLE_FONT};color:{TEXT}">'
             f'{html.escape(strings["title"])}</div>')
    subtitle = (f'<div style="position:absolute;left:96px;top:184px;font:32px {BODY_FONT};color:{SECONDARY}">'
                f'{html.escape(strings["subtitle"])}</div>')
    legend = "".join(
        f'<span style="display:inline-block;margin-left:28px"><span style="display:inline-block;width:22px;height:22px;'
        f'border-radius:4px;background:{colour};vertical-align:-3px;margin-right:10px"></span>{html.escape(name)}</span>'
        for name, colour in strings["legend"])
    legend_html = (f'<div style="position:absolute;right:96px;top:110px;font:26px {BODY_FONT};color:{SECONDARY}">'
                   f'{legend}</div>')
    seats = []
    for seat in SEATS:
        cell = frame.cells.get(seat.key, DEFAULT_CELLS[seat.key])
        lit = seat.key in frame.lit
        seats.append(_figure_svg(seat, cell, lit) + _seat_labels(seat, strings["roles"][seat.key], cell, lit))
    docs = "".join(_doc_html(doc, frame, strings) for doc in DOCS)
    caption = (f'<div style="position:absolute;left:96px;top:850px;width:1728px;font:36px/1.3 {BODY_FONT};color:{TEXT}">'
               f'{html.escape(strings["captions"][frame.caption])}</div>')
    footer = (f'<div style="position:absolute;left:96px;top:980px;font:24px {BODY_FONT};color:{SECONDARY}">'
              f'{index + 1} / {len(TRIAL)} · {html.escape(strings["footer"])}</div>')
    return _page(title + subtitle + legend_html + _trial_edges_svg(frame, strings["blind"]) + "".join(seats) + docs
                 + caption + footer)


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


def build_trial(strings: dict, workdir: Path, target: Path) -> tuple[list[Path], int]:
    """Capture the trial frames with `strings` into `workdir` and write the GIF; return the PNGs and the size."""
    pngs = capture([trial_html(f, i, strings) for i, f in enumerate(TRIAL)], workdir, "trial")
    return pngs, gif(pngs, target, frame_ms=TRIAL_FRAME_MS, last_ms=TRIAL_LAST_MS)


def main(argv: list[str] | None = None) -> int:
    """Build docs/showcase/trial.gif and docs/showcase/router-run.gif (and the carousel and frames when asked)."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--carousel", type=Path, help="also write the trial frames as a PDF carousel here")
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
        trial_pngs, size = build_trial(EN, work, HERE / "trial.gif")
        print(f"trial.gif: {len(trial_pngs)} frames, {size / 1024:.0f} KB")
        run_pngs = capture(terminal_frames(command, output), work, "run")
        size = gif(run_pngs, HERE / "router-run.gif")
        print(f"router-run.gif: {len(run_pngs)} frames, {size / 1024:.0f} KB")
        if args.carousel:
            carousel(trial_pngs, args.carousel)
            print(f"carousel: {args.carousel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
