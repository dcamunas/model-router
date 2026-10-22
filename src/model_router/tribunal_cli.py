"""The `tribunal` command: two judges blind to each other, a Claude model and a Codex model, and an arbiter on a
split. `tribunal qa` reviews the code changed in a git repository, as the plugin's hook does at the end of a turn;
`tribunal judge` reviews a text written elsewhere; `tribunal config` sets who judges. It shares the router's
engine (the verifier, the ledger, the doors to other providers) but none of the router's own decisions.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import qa
from ._fmt import money
from .catalog import UnknownModelError, load_external, parse_cell
from .cli import (
    _add_backend_args,
    _add_task_args,
    _attempts_table,
    _live_client,
    _prompt_from,
    _task_from,
    _with_providers,
)
from .client import ClientUnavailableError
from .paths import config_path, read_config, write_config

CELL_SETTINGS = ("claude", "codex", "arbiter")
EXIT = {"solved": 0, "unsolved": 1, "error": 2}  # pass, fail, could not judge


def _cmd_qa(args: argparse.Namespace) -> int:
    repo = qa.repo_root((args.repo or Path.cwd()).expanduser())
    if repo is None:
        return _cannot(f"not a git repository: {args.repo or Path.cwd()}")
    brief = args.brief_file.read_text(encoding="utf-8") if args.brief_file else (args.brief or "")
    s = qa.settings(repo, brief)
    try:
        base = qa.tree_of(repo, args.base)
    except RuntimeError as exc:
        return _cannot(f"cannot measure the change from {args.base!r}: {exc}")
    current = qa.snapshot(repo)
    files = qa.changed_files(repo, base, current)
    print(f"tribunal QA in {repo}: {s.describe()}; {len(files)} files changed since {args.base}")
    for note in s.notes:
        print(f"  note: {note}")
    if not files:
        print("nothing to judge")
        return 0
    if args.dry_run:
        print("\n".join(f"  {f}" for f in files) + "\ndry run: nothing was called")
        return 0
    result = qa.run_qa(repo, brief, qa.diff_text(repo, base, current), s)
    return _report(result)


def _cmd_judge(args: argparse.Namespace) -> int:
    prompt = _prompt_from(args)
    answer = sys.stdin.read() if args.answer == "-" else Path(args.answer).read_text(encoding="utf-8")
    if not answer.strip():
        return _cannot("the answer is empty")
    client = _with_providers(_live_client(args))
    task, cls = _task_from(args, prompt, client)
    author = parse_cell(args.cell) if args.cell else None
    s = qa.resolve(qa.raw_settings(None, prompt), author.spec.short if author else None)  # never the author's model
    if cls is not None:
        print(f"classification: {cls.kind} / {cls.risk}  ({cls.reason})")
    print(f"tribunal: {s.describe()}")
    result = qa.judge_answer(task, answer, s, client, worker=author)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=1))
        return EXIT[result.status]
    print(_attempts_table(result))
    return _report(result)


def _report(result: Any) -> int:
    attempt = result.attempts[0]
    for v in attempt.verdicts:
        print(f"  {v.role} {v.cell.label}: {'pass' if v.passed else 'fail'}: {v.reason}")
    if attempt.verdict_source == "judge-error":
        print(f"  {attempt.reason}")
    verdict = {"solved": "pass", "unsolved": "fail"}.get(result.status, result.status)
    print(f"verdict {verdict} · verification {money(attempt.verify_cost_usd)} · id {result.task_id}")
    return EXIT[result.status]


def _cannot(message: str) -> int:
    """Say why the command could not run and return its exit code, 2."""
    print(f"tribunal: {message}", file=sys.stderr)
    return 2


def _cmd_config(args: argparse.Namespace) -> int:
    stored = read_config().get("qa")
    current: dict[str, Any] = dict(stored) if isinstance(stored, dict) else {}  # a malformed value is replaced
    changes = {key: getattr(args, key) for key in ("mode", "rounds", *CELL_SETTINGS) if getattr(args, key) is not None}
    for key, value in changes.items():
        value = str(value).strip()
        if value.lower() == "default":
            current.pop(key, None)
        elif key in CELL_SETTINGS:
            current[key] = parse_cell(value).label  # an unknown model stops here, before anything is written
        elif key == "rounds":
            current[key] = int(value)
        else:
            current[key] = value
    if changes:
        try:
            qa.resolve({**qa.DEFAULTS, **{k: current[k] for k in changes if k in current}})
        except ValueError as exc:  # judges on one model, an arbiter on a judge's: refused, nothing written
            return _cannot(str(exc))
        write_config(qa=current)
    shown = ", ".join(f"{k}={v}" for k, v in current.items()) or "defaults"
    try:
        s = qa.resolve(qa.raw_settings(None))
    except (ValueError, UnknownModelError) as exc:
        print(f"tribunal: the stored settings do not resolve: {exc} ({shown}, in {config_path()}); "
              f"fix them with `tribunal config --KEY default`")
        return 2
    print(f"tribunal: mode {s.mode}, judges {s.describe()}, {s.rounds} reviews per turn ({shown}, in {config_path()})")
    for note in s.notes:
        print(f"  note: {note}")
    return 0


def _rounds(text: str) -> str:
    if text.lower() != "default" and (not text.isdigit() or not 1 <= int(text) <= qa.MAX_ROUNDS):
        raise argparse.ArgumentTypeError(f"a whole number from 1 to {qa.MAX_ROUNDS}, or default")
    return text


def build_parser() -> argparse.ArgumentParser:
    """Return the `tribunal` argument parser; each subcommand sets `func` to its handler."""
    p = argparse.ArgumentParser(prog="tribunal", description="Two judges blind to each other, a Claude model and a "
                                "Codex model, and an arbiter on a split: QA of code changes and of critical texts.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sq = sub.add_parser("qa", help="review the code changed in a git repository, read-only inside it")
    sq.add_argument("--repo", type=Path, help="the repository (default: the current directory)")
    sq.add_argument("--base", default="HEAD", help="what the change is measured from (default HEAD: uncommitted work)")
    sq.add_argument("--brief", help="what the change was meant to do")
    sq.add_argument("--brief-file", type=Path, help="the brief, from a file")
    sq.add_argument("--dry-run", action="store_true", help="show the judges and the changed files; call nothing")
    sq.set_defaults(func=_cmd_qa)

    sj = sub.add_parser("judge", help="review a text written elsewhere (critical prose, an analysis, a review)")
    _add_task_args(sj, with_verify=False)
    _add_backend_args(sj)
    sj.add_argument("--answer", required=True, metavar="FILE",
                    help="the answer to judge; `-` reads it from stdin (then give the task as an argument or with --file)")
    sj.add_argument("--cell", help="the cell that wrote the answer, for the record (default opus/medium)")
    sj.add_argument("--json", action="store_true", help="JSON output with every judge's verdict")
    sj.set_defaults(func=_cmd_judge)

    sc = sub.add_parser("config", help="show or set who judges; `default` goes back to the default")
    sc.add_argument("--mode", choices=(*qa.MODES, "default"), help="off, final (the default) or parallel (not built yet)")
    sc.add_argument("--claude", metavar="CELL", help="the Claude judge (default opus/high; never the chat's model)")
    sc.add_argument("--codex", metavar="CELL", help="the Codex judge (default: the declared codex model of the highest tier)")
    sc.add_argument("--arbiter", metavar="CELL", help="the arbiter of a split (default: a Claude model neither judge is on)")
    sc.add_argument("--rounds", type=_rounds, help=f"reviews per turn, 1 to {qa.MAX_ROUNDS}: a fail goes back to Claude until "
                                                   "the last review, whose fail is reported to you (default 2)")
    sc.set_defaults(func=_cmd_config)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    """Run `tribunal` with `argv` (the process arguments when None) and return the exit code."""
    args = build_parser().parse_args(argv)
    try:
        load_external(read_config())
        return int(args.func(args))
    except (UnknownModelError, ValueError, ClientUnavailableError, OSError, RuntimeError) as exc:
        print(f"tribunal: {exc}", file=sys.stderr)
        return 2


def entry() -> None:
    """Console script `tribunal`."""
    sys.exit(main())
