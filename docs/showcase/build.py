"""Build the README's animated figure: a rendering of a real `router run`. Every frame is an HTML page on a
1920x1080 canvas; headless Chrome captures it and Pillow stitches the GIF (half size, 64 colours).

    uv run --with pillow docs/showcase/build.py [--frames DIR]

Needs Google Chrome; no other tool. The frames reveal the lines of `router-run.txt`, the exact output of
`router run --file docs/showcase/notice.md --kind write --risk critical` in a terminal of 126 columns
(COLUMNS=126, what the drawn window holds at 22 px), never text typed in.
"""

from __future__ import annotations

import argparse
import html
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CANVAS = (1920, 1080)
GIF_WIDTH = 960
COLOURS = 64
FRAME_MS, LAST_MS = 1400, 2600

LIGHT, DARK = "#f6f5f1", "#1d2733"
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


def main(argv: list[str] | None = None) -> int:
    """Build docs/showcase/router-run.gif (and keep the frames when asked)."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
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
        run_pngs = capture(terminal_frames(command, output), work, "run")
        size = gif(run_pngs, HERE / "router-run.gif")
        print(f"router-run.gif: {len(run_pngs)} frames, {size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
