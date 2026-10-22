"""The `tribunal` command: two judges blind to each other, a Claude model and a Codex model, and an arbiter on a
split. `tribunal qa` reviews the code changed in a git repository, as the plugin's hook does at the end of a turn;
`tribunal judge` reviews a text written elsewhere; `tribunal config` sets who judges. It shares the router's
engine (the verifier, the ledger, the door to Codex) but none of the router's own decisions.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import qa
from ._fmt import money
from .catalog import ModelSpec, UnknownModelError, external_models, load_external, parse_cell, parse_external
from .cli import (
    _add_backend_args,
    _add_task_args,
    _attempts_table,
    _live_client,
    _prompt_from,
    _task_from,
)
from .client import ClientUnavailableError
from .codex_models import Proposal
from .paths import CODEX_HOME_ENV, config_path, read_config, save_config, write_config
from .providers import MultiClient

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
    result = qa.run_qa(repo, brief, qa.diff_text(repo, base, current), s,
                       current_files=qa.files_now(repo, current, files))
    return _report(result)


def _cmd_judge(args: argparse.Namespace) -> int:
    prompt = _prompt_from(args)
    answer = sys.stdin.read() if args.answer == "-" else Path(args.answer).read_text(encoding="utf-8")
    if not answer.strip():
        return _cannot("the answer is empty")
    client = MultiClient(_live_client(args))  # Claude cells to Claude, the Codex judge to Codex
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


def _codex_setup(args: argparse.Namespace) -> None:
    """Apply `--declare`, `--codex-home` and `--codex-models`, in that order, before the judges are set."""
    for text in args.declare or []:
        _declare_external(text)
    if args.codex_home is not None:
        home_dir = args.codex_home.expanduser().resolve()
        write_config(codex_home=str(home_dir))
        _codex_models(args.codex_models or "", home_dir)
    elif args.codex_models is not None:
        _codex_models(args.codex_models)


def _cmd_config(args: argparse.Namespace) -> int:
    _codex_setup(args)
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
    for spec in external_models():
        ok_, line = _external_check(spec)
        print(("  ok    " if ok_ else "  FAIL  ") + line)
    return 0


def _rounds(text: str) -> str:
    if text.lower() != "default" and (not text.isdigit() or not 1 <= int(text) <= qa.MAX_ROUNDS):
        raise argparse.ArgumentTypeError(f"a whole number from 1 to {qa.MAX_ROUNDS}, or default")
    return text


# ---- the Codex judge: its home, the models it lists, their declarations --------------------------------------


def _external_check(spec: ModelSpec) -> tuple[bool, str]:
    """Return one line about a declared Codex model: whether its door can open (the `codex` executable, the home
    configured and signed in, the model in the home's list) and whether its declared price is the list one.
    """
    from .providers import resolve_codex_home

    head = f"{spec.short}: codex {spec.id}, "
    if shutil.which("codex") is None:
        return False, head + "the `codex` executable is not on the PATH"
    try:
        home_dir, source = resolve_codex_home()
    except ClientUnavailableError as exc:
        return False, head + str(exc)
    from .codex_models import listed_models

    signed = (home_dir / "auth.json").exists()
    state = "signed in" if signed else f"not signed in: run `CODEX_HOME={home_dir} codex login`"
    known = listed_models(home_dir, include_hidden=True)
    if signed and known is not None and spec.id not in known:
        return False, head + f"home {home_dir} (via {source}), signed in, but {spec.id} is not among the models it " \
                             f"knows (Codex refreshes that list when it runs; `tribunal config --codex-models` shows it)"
    return signed, head + f"home {home_dir} (via {source}), {state}" + _price_suffix(spec)


def _price_suffix(spec: ModelSpec) -> str:
    """Return `; <note>` when a declared OpenAI model's price differs from the list price, else nothing."""
    from .codex_models import price_note

    note = price_note(spec.id, spec.input, spec.output)
    return f"; {note}" if note else ""



def _forget_external(name: str) -> None:
    """Drop a removed declared model from `models`, `judge`, `arbiter` and the tribunal's settings, saying so: a reference
    left behind would stop every command with an unknown model.
    """
    cfg = read_config()
    changes: dict[str, Any] = {}
    listed = cfg.get("models")
    if isinstance(listed, list) and name in listed:
        changes["models"] = [m for m in listed if m != name]
    for key in ("judge", "arbiter"):
        if str(cfg.get(key) or "").partition("/")[0] == name:
            changes[key] = None
    raw = cfg.get("qa")
    tribunal: dict[str, Any] = raw if isinstance(raw, dict) else {}
    kept = {k: v for k, v in tribunal.items() if str(v).partition("/")[0] != name}
    if kept != tribunal:
        changes["qa"] = kept  # the tribunal's judge or arbiter
    if not changes:
        return
    for key, value in changes.items():
        if value is None:
            cfg.pop(key, None)
        else:
            cfg[key] = value
    save_config(cfg)
    print(f"{name} was also removed from: {', '.join(changes)}")



def _declare_external(text: str) -> None:
    """Apply one `--declare` value to config.json (declare, replace or, with `NAME=`, remove) and say so."""
    name, spec = parse_external(text)
    external = dict(read_config().get("external") or {})
    if spec is None:
        external.pop(name, None)
        _forget_external(name)
    else:
        external[name] = {"provider": spec.provider, "id": spec.id, "input": spec.input, "output": spec.output,
                          "tier": spec.tier}
    path = write_config(external=external)
    print(f"Codex models declared: {', '.join(external) or 'none'} (in {path})")
    load_external(read_config())


def _declare_proposals(proposals: list[Proposal], names: list[str]) -> None:
    """Declare the proposals named in `names`, all or none: a name the home does not list, or a model with no
    known price, stops the command before anything is written.
    """
    by_name = {p.name: p for p in proposals}
    unknown = [n for n in names if n not in by_name]
    if unknown:
        raise SystemExit(f"not in this home's list: {', '.join(unknown)}; it has {', '.join(by_name)}")
    unpriced = [n for n in names if by_name[n].declared_as is None and by_name[n].declaration is None]
    if unpriced:
        raise SystemExit(f"no list price known here for {', '.join(unpriced)}; declare it by hand with --declare "
                         "(nothing was declared)")
    for n in dict.fromkeys(names):
        proposal = by_name[n]
        if proposal.declared_as is not None:
            print(f"{n}: already declared ({_door_note(proposal)})")
        elif proposal.declaration is not None:
            _declare_external(proposal.declaration)


def _door_note(proposal: Proposal) -> str:
    """Say through which door a declared model runs: through the Codex home, or another door that blocks it."""
    if proposal.declared_door == "codex":
        return "through this Codex home"
    return (f"through the {proposal.declared_door} door, not this Codex home; "
            f"`tribunal config --declare {proposal.declared_as}=` frees it")


def _codex_models(wanted: str, home_dir: Path | None = None) -> None:
    """Print what the Codex home can run, as ready declarations; with `wanted` (comma-separated names from that
    list), declare those. `home_dir` is the home just given; otherwise the one `codex` cells use. Nothing is
    declared that was not named.
    """
    from .codex_models import OPENAI_PRICES_DATE, listed_models, propose
    from .providers import resolve_codex_home

    if home_dir is None:
        try:
            home_dir, _ = resolve_codex_home()
        except ClientUnavailableError as exc:
            raise SystemExit(str(exc)) from exc
    elif os.environ.get(CODEX_HOME_ENV):
        print(f"note: {CODEX_HOME_ENV} is set, so `codex` cells run on {os.environ[CODEX_HOME_ENV]}, not on {home_dir}")
    ids = listed_models(home_dir)
    if ids is None:
        print(f"no model list in {home_dir} yet: run `CODEX_HOME={home_dir} codex` once and ask again "
              "(or declare by hand with --declare)")
        return
    proposals = propose(ids, {s.id: (s.short, s.provider) for s in external_models()})
    names = [n.strip() for n in wanted.split(",") if n.strip()]
    if names:
        _declare_proposals(proposals, names)
        return
    print(f"models this Codex home can run ({home_dir}):")
    for proposal in proposals:
        if proposal.declared_as is not None:
            what = f"declared as {proposal.declared_as}, {_door_note(proposal)}"
        elif proposal.declaration is not None:
            what = f"--declare {proposal.declaration}"
        else:
            what = "no list price known here: declare it by hand with --declare"
        print(f"  {proposal.name:<10} {proposal.model_id:<16} {what}")
    print(f"prices: OpenAI's list as of {OPENAI_PRICES_DATE}; tiers (after the last /): an assumption, "
          "`router calibrate` measures them")
    if any(p.declared_as is None for p in proposals):
        print("the ones not declared stay out until you name them: `tribunal config --codex-models NAME,NAME`; "
              "they judge, and work once --models lists them")


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
    sc.add_argument("--codex-home", type=Path, help="the Codex home signed in with your ChatGPT account, e.g. "
                    "~/.codex-personal; never the ambient ~/.codex by accident. Lists what it can run")
    sc.add_argument("--codex-models", nargs="?", const="", metavar="NAME,NAME",
                    help="list the models the Codex home can run, with OpenAI's list price and a tier; with names, "
                         "declare those")
    sc.add_argument("--declare", action="append", metavar="NAME=codex/MODEL_ID@IN/OUT[/TIER]",
                    help="declare a Codex model by hand (prices in USD per million tokens); NAME= removes it")
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
