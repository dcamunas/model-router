"""`router` on the command line: plan (no API), run (live), judge (verify an answer written elsewhere),
bench (simulated or a file of real tasks), stats, feedback, db (the SQLite index of the ledger), doctor,
cells, config, install and uninstall. The README documents each command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._fmt import money, table
from .calibration import (
    BUILTIN,
    MIN_N,
    MIN_RATE,
    MIN_SAVING,
    NO_CALIBRATION,
    active_calibration_name,
    calibrations_dir,
    classifier_report,
    load_calibration,
    measure,
)
from .calibration import report as calibration_report
from .catalog import (
    MODELS,
    MODELS_ENV,
    PRICES_AS_OF,
    SHORT_TO_ID,
    UnknownModelError,
    available_models,
    lattice,
    parse_cell,
)
from .classify import Classification, classify_llm, classify_rules
from .client import ClaudeClient, ClientUnavailableError, Completion
from .feedback import FeedbackStore
from .ledger import Ledger, summarize
from .paths import (
    CALIBRATION_ENV,
    CONFIG_DIR_ENV,
    JUDGE_ENV,
    MAIN_LEDGER,
    config_path,
    home,
    read_config,
    write_config,
)
from .policy import (
    DEFAULT_JUDGE,
    FLOOR,
    JUDGE_INPUT_TOKENS,
    JUDGE_OUTPUT_TOKENS_AT_HIGH,
    START,
    LadderPolicy,
    Policy,
    PolicyProtocol,
    default_judge,
)
from .runner import Router, TaskResult
from .store import Store
from .subagents import LOG_FILE as SUBAGENT_LOG
from .subagents import load as load_subagents
from .subagents import summary_rows
from .task import (
    DEFAULT_MAX_TOKENS,
    KINDS,
    RISKS,
    ROUTER_VERIFY_MODES,
    VERIFIED_SOURCES,
    Task,
    json_checker,
    regex_checker,
)
from .version import router_version

ESCALATION_COST_FACTOR = 1.6  # a strategy that may escalate spends about this much more than its first attempt
REASON_COLUMN_CHARS = 70  # the reason column of the attempts table, at most; it shrinks to fit the terminal
MIN_REASON_CHARS = 16
PLUGIN_ID = "model-router@dcamunas"  # the plugin id Claude Code uses once installed from the marketplace


def _add_task_args(p: argparse.ArgumentParser, *, with_prompt: bool = True, with_verify: bool = True) -> None:
    if with_prompt:
        p.add_argument("prompt", nargs="?", help="the task; or use --file")
        p.add_argument("--file", "-f", help="file with the task")
    p.add_argument("--kind", "-k", choices=KINDS, help="task kind (inferred by rules when missing)")
    p.add_argument("--risk", "-r", choices=RISKS, help="how bad a wrong answer is (inferred when missing)")
    if with_verify:
        p.add_argument("--verify", choices=ROUTER_VERIFY_MODES, help="force the verification mode")
    p.add_argument("--budget", type=float, help="spending cap in USD for the task")
    p.add_argument("--max-attempts", type=int, help="maximum attempts")
    p.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS,
                   help="api backend only; the cli backend manages it itself")
    p.add_argument("--system", help="file with the worker's system prompt")
    p.add_argument("--check-regex", help="the answer must match this regex (programmatic checker)")
    p.add_argument("--check-json",
                   help="the answer must be JSON with these comma-separated keys (empty = any valid JSON)")
    p.add_argument("--classify-llm", action="store_true", help="classify with Haiku instead of rules")


def _prompt_from(args: argparse.Namespace) -> str:
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    if args.prompt:
        return args.prompt
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("no task given: pass it as an argument, with --file or on stdin")


def _task_from(
    args: argparse.Namespace, prompt: str, client: ClaudeClient | None = None
) -> tuple[Task, Classification | None]:
    kind, risk, cls = args.kind, args.risk, None
    if kind is None or risk is None:
        cls = classify_llm(client, prompt) if (args.classify_llm and client is not None) else classify_rules(prompt)
        kind = kind or cls.kind
        risk = risk or cls.risk
    checker = None
    if args.check_regex:
        checker = regex_checker(args.check_regex)
    elif args.check_json is not None:
        checker = json_checker(tuple(k.strip() for k in args.check_json.split(",") if k.strip()))
    system = Path(args.system).read_text(encoding="utf-8") if args.system else None
    task = Task(prompt=prompt, kind=kind, risk=risk, system=system, checker=checker, max_tokens=args.max_tokens,
                budget_usd=args.budget, max_attempts=args.max_attempts, verify=getattr(args, "verify", None))
    return task, cls


def _cmd_plan(args: argparse.Namespace) -> int:
    prompt = _prompt_from(args)
    task, cls = _task_from(args, prompt)
    policy = Policy(feedback=FeedbackStore(home() / "feedback.json"))
    plan = policy.plan(task)
    if args.claude_code:
        cc = plan.start.claude_code()
        print(f"model: {cc['model']}\neffort: {cc['effort']}")
        return 0
    if cls is not None:
        print(f"classification: {cls.kind} / {cls.risk}  ({cls.reason})")
    print(f"start: {plan.start}  -> Claude Code {plan.start.claude_code()}")
    for line in plan.rationale:
        print("  - " + line)
    judges = policy.judges_for(plan.start, plan.verify)
    if judges:
        print(f"verifier ({plan.verify}): {' then '.join(map(str, judges))}")
    first = plan.start.expected_cost(task.estimated_input_tokens, task.planned_output_tokens)
    print(f"estimated cost of the first attempt: {money(first)}"
          f"  (prices {PRICES_AS_OF})")
    return 0


def _attempts_table(result: TaskResult, *, width: int | None = None) -> str:
    """Render the attempts as a table that fits `width` columns (the terminal's when None): the reason
    column takes what the other columns leave, between MIN_REASON_CHARS and REASON_COLUMN_CHARS.
    """
    headers = ["#", "cell", "fallback", "stop", "tok in/out", "cost", "verdict", "verif.", "reason"]
    rows = []
    for a in result.attempts:
        verdict = "-" if a.verdict is None else ("pass" if a.verdict else "fail")
        served = a.served_model if a.fallback_ran else ""
        judged = "+".join(j.label for j in a.judges) + (f" > {a.arbiter.label}" if a.arbiter else "")
        rows.append([a.n, a.cell.label, served, a.stop_reason, f"{a.usage.input_tokens:,}/{a.usage.output_tokens:,}",
                     money(a.cost_usd), f"{verdict} ({a.verdict_source}{', ' + judged if judged else ''})",
                     money(a.verify_cost_usd), a.reason])
    columns = width if width is not None else shutil.get_terminal_size((200, 24)).columns
    fixed = sum(max(len(str(r[i])) for r in [headers, *rows]) for i in range(len(headers) - 1)) + 2 * (len(headers) - 1)
    reason_chars = max(MIN_REASON_CHARS, min(REASON_COLUMN_CHARS, columns - fixed))
    for r in rows:
        r[-1] = str(r[-1])[:reason_chars]
    return table(headers, rows)


def _store() -> Store:
    return Store(home() / "router.db")


def _ledger_files() -> list[tuple[Path, str | None]]:
    """Return the main ledger and every bench ledger, oldest first; a bench ledger's strategy is in its first line."""
    files: list[tuple[Path, str | None]] = [(home() / MAIN_LEDGER, None)]
    files += [(p, None) for p in sorted((home() / "bench").glob("*.jsonl"))]
    return files


def _warn_index(store: Store) -> None:
    """Say on stderr when the index could not be written; the ledger has the line and `router stats` rebuilds the index."""
    for text in dict.fromkeys(store.errors):
        print(f"warning: the index was not updated ({text}); `router stats` rebuilds it from the ledger", file=sys.stderr)
    store.errors.clear()


def _add_backend_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--backend", choices=("auto", "api", "cli"), default="auto",
                   help="api = SDK with ANTHROPIC_API_KEY; cli = `claude -p` with your account; "
                        "auto = api when there is a key, else cli")
    p.add_argument("--claude-config-dir", help=f"Claude profile for the cli backend (default: {CONFIG_DIR_ENV})")
    p.add_argument("--no-fallbacks", action="store_true", help="api backend: no server-side refusal fallbacks")


def _live_client(args: argparse.Namespace) -> ClaudeClient:
    backend = args.backend
    if backend == "auto":
        backend = "api" if os.environ.get("ANTHROPIC_API_KEY") else "cli"
    if backend == "api":
        from .client import LiveClient

        return LiveClient(server_fallbacks=not args.no_fallbacks)
    from .cli_client import CliClient

    config_dir = Path(args.claude_config_dir).expanduser() if args.claude_config_dir else None
    return CliClient(config_dir=config_dir)


def _cmd_run(args: argparse.Namespace) -> int:
    prompt = _prompt_from(args)
    client = _live_client(args)
    task, cls = _task_from(args, prompt, client)
    feedback = FeedbackStore(home() / "feedback.json")
    store = _store()
    ledger = Ledger(home() / "ledger.jsonl", store=store)
    router = Router(client, policy=Policy(feedback=feedback), ledger=ledger, feedback=feedback)
    result = router.run(task)
    _warn_index(store)
    if args.json:
        print(json.dumps({**result.to_dict(), "answer": result.answer}, ensure_ascii=False, indent=1))
        return 0 if result.solved else 1
    print(result.answer)
    print()
    if cls is not None:
        print(f"classification: {cls.kind} / {cls.risk}  ({cls.reason})")
    print(_attempts_table(result))
    print(f"status {result.status} · cost {money(result.total_cost_usd)} (verification {money(result.verify_cost_usd)})"
          f" · retries {result.retries} · id {task.id}")
    if not result.solved:
        print("the last answer did not pass verification; review it before using it", file=sys.stderr)
    return 0 if result.solved else 1


def _cmd_judge(args: argparse.Namespace) -> int:
    prompt = _prompt_from(args)
    answer = sys.stdin.read() if args.answer == "-" else Path(args.answer).read_text(encoding="utf-8")
    if not answer.strip():
        raise SystemExit("the answer is empty")
    client = _live_client(args)
    task, cls = _task_from(args, prompt, client)
    policy = Policy(feedback=FeedbackStore(home() / "feedback.json"))
    store = _store()
    router = Router(client, policy=policy, ledger=Ledger(home() / "ledger.jsonl", store=store))
    result = router.judge(task, answer, worker=parse_cell(args.cell) if args.cell else None)
    _warn_index(store)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=1))
        return 0 if result.solved else 1
    if cls is not None:
        print(f"classification: {cls.kind} / {cls.risk}  ({cls.reason})")
    print(_attempts_table(result))
    verdict = {"solved": "pass", "unsolved": "fail"}.get(result.status, result.status)
    print(f"verdict {verdict} · cost {money(result.verify_cost_usd)} · id {task.id}")
    return 0 if result.solved else 1


def _load_specs(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"cannot read {path}: {exc.strerror or exc}") from exc
    if path.suffix.lower() in (".yaml", ".yml"):
        import yaml

        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    specs = data["tasks"] if isinstance(data, dict) else data
    if not isinstance(specs, list) or not specs:
        raise SystemExit(f"{path}: expected a list of tasks")
    return specs


def _spec_task(spec: dict[str, Any]) -> Task:
    prompt = spec["prompt"]
    cls = classify_rules(prompt)
    checker = None
    if spec.get("check_regex"):
        checker = regex_checker(spec["check_regex"])
    elif "check_json" in spec:
        keys = spec["check_json"]
        checker = json_checker(tuple(keys) if isinstance(keys, list) else ())
    return Task(prompt=prompt, kind=spec.get("kind", cls.kind), risk=spec.get("risk", cls.risk),
                id=str(spec.get("id") or Task(prompt="x").id), system=spec.get("system"), checker=checker,
                max_tokens=int(spec.get("max_tokens", DEFAULT_MAX_TOKENS)), budget_usd=spec.get("budget"),
                max_attempts=spec.get("max_attempts"), verify=_router_verify(spec))


def _router_verify(spec: dict[str, Any]) -> str | None:
    """Return a task file's `verify`, refusing a mode the router does not offer (two judges are the tribunal's)."""
    verify = spec.get("verify")
    if verify is not None and verify not in ROUTER_VERIFY_MODES:
        raise SystemExit(f"task {spec.get('id', '?')}: verify {verify!r} is not a router mode "
                         f"({', '.join(ROUTER_VERIFY_MODES)}); two judges are the tribunal plugin's (`tribunal judge`)")
    return verify


def _parse_strategy(text: str, verify: str | None = None) -> tuple[str, PolicyProtocol]:
    """Parse one strategy: `router`, or cells joined by `>`; `verify` sets the mode of a fixed ladder (default:
    light when it can escalate, none for one cell; `auto` = what the router would pick by risk).
    """
    text = text.strip()
    if text == "router":
        return "router", Policy()
    if text.startswith("router@"):  # the router with a given calibration, or `router@builtin` for the code tables
        name = text[len("router@"):].strip()
        policy = Policy(calibration=None if name.lower() in NO_CALIBRATION else name)
        if policy.calibration_missing:
            raise SystemExit(f"strategy {text}: calibration {name!r} cannot be loaded (`router calibrate --apply` makes one)")
        return text, policy
    cells = [parse_cell(c) for c in text.split(">")]
    return text, LadderPolicy(cells, verify=verify or ("light" if len(cells) > 1 else "none"))


def _bench_estimate(tasks: list[Task], strategies: list[tuple[str, PolicyProtocol]]) -> float:
    """Return a rough cost of running `tasks` once under every strategy: first attempts, escalation, judges."""
    estimate = 0.0
    for _, policy in strategies:
        for t in tasks:
            plan = policy.plan(t)
            first = plan.start.expected_cost(t.estimated_input_tokens, t.planned_output_tokens)
            estimate += first * (1.0 if len(plan.ladder) == 1 else ESCALATION_COST_FACTOR)
            judges = policy.judges_for(plan.start, plan.verify)
            estimate += sum(j.expected_cost(JUDGE_INPUT_TOKENS, JUDGE_OUTPUT_TOKENS_AT_HIGH) for j in judges)
    return estimate


def _simulate(args: argparse.Namespace) -> int:
    from .simulate import format_report, run_simulation

    if args.strategies != "router" or args.repeat != 1 or args.verify or args.dry_run:
        print("simulation: --strategies, --repeat, --verify and --dry-run apply to a file of tasks; "
              "the simulation runs its own strategies", file=sys.stderr)
    tasks, reports = run_simulation(args.tasks, args.seed)
    print(format_report(tasks, reports, args.seed, detail=args.detail))
    return 0


def _bench_strategies(args: argparse.Namespace) -> list[tuple[str, PolicyProtocol]]:
    """Return the strategies a bench compares: with `--matrix`, one single-cell strategy per cell (`--cells`, or
    the whole lattice), verified as the router would unless `--verify` says otherwise; else `--strategies`.
    """
    if args.matrix:
        cells = [parse_cell(c) for c in args.cells.split(",") if c.strip()] if args.cells else lattice()
        return [(c.label, LadderPolicy([c], verify=args.verify or "auto")) for c in cells]
    return [_parse_strategy(s, args.verify) for s in args.strategies.split(",")]


def _confirm() -> bool:
    try:
        answer = input("Make real calls? [y/N] ").strip().lower()
    except EOFError:  # no terminal to answer from: that is a no
        answer = ""
    return answer in ("y", "yes")


def _cmd_bench(args: argparse.Namespace) -> int:
    if args.file is None or args.simulate:
        return _simulate(args)
    path = Path(args.file)
    tasks = [_spec_task(s) for s in _load_specs(path)]
    eval_id = hashlib.sha256(path.read_bytes()).hexdigest()[:12]  # names this exact file in every record it produces
    repeat = max(1, args.repeat)
    strategies = _bench_strategies(args)
    with_checker = sum(t.checker is not None for t in tasks)
    estimate = _bench_estimate(tasks, strategies) * repeat
    print(f"{len(tasks)} tasks ({with_checker} with a checker) x {len(strategies)} strategies x {repeat} repeat(s) · "
          f"eval {eval_id} · estimate {money(estimate)} (a rough one; it may fall short)")
    if args.dry_run:
        for name, policy in strategies:
            modes = sorted({policy.plan(t).verify for t in tasks})
            print(f"  {name}: verification {', '.join(modes)}")
        print("dry run: the file is valid; nothing was called")
        return 0
    if args.max_usd is not None and estimate > args.max_usd:
        print(f"the estimate is over --max-usd {money(args.max_usd)}; nothing was called")
        return 1
    if not args.yes and not _confirm():
        print("cancelled")
        return 1
    return _run_bench(args, path, tasks, strategies, eval_id=eval_id, repeat=repeat)


def _run_bench(
    args: argparse.Namespace, path: Path, tasks: list[Task], strategies: list[tuple[str, PolicyProtocol]], *,
    eval_id: str, repeat: int,
) -> int:
    """Run every strategy over `tasks` (`repeat` times each), one bench ledger per strategy; stop between
    strategies once `--max-usd` is spent.
    """
    client = _live_client(args)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    jobs = [(replace(t, id=f"{t.id}.r{k}") if repeat > 1 else t, k) for k in range(1, repeat + 1) for t in tasks]
    store = _store()
    spent = 0.0
    for name, policy in strategies:
        if args.max_usd is not None and spent >= args.max_usd:
            print(f"stopped before {name}: {money(spent)} spent, --max-usd {money(args.max_usd)} reached")
            return 1
        ledger = Ledger(home() / "bench" / f"{stamp}-{name.replace('/', '-').replace('>', '_')}.jsonl",
                        store=store, strategy=name)
        ledger.note(strategy=name, eval_id=eval_id, file=str(path), repeat=repeat, started=stamp)
        router = Router(client, policy=policy, ledger=ledger)

        def run_one(job: tuple[Task, int], router: Router = router) -> TaskResult:
            return router.run(job[0], eval_id=eval_id, repeat=job[1])

        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            for (t, _), res in zip(jobs, pool.map(run_one, jobs), strict=True):
                spent += res.total_cost_usd
                print(f"  [{name}] {t.id} {res.status} {money(res.total_cost_usd)} via {res.final_cell}")
        print(f"\n== {name} ==")
        print(summarize(ledger.tasks()).text())
        print(f"ledger: {ledger.path}\n")
        _warn_index(store)
    return 0


def _cmd_calibrate(args: argparse.Namespace) -> int:
    if args.classifier:
        specs = _load_specs(Path(args.classifier))
        client = _live_client(args) if args.llm else None
        print(classifier_report(specs, start=lambda t: Policy().plan(t).start, client=client))
        return 0
    store = _store()
    store.sync(_ledger_files())
    current, against = START, BUILTIN
    active = active_calibration_name()
    if active:
        try:
            loaded = load_calibration(active)
            current, against = loaded.start, loaded.id
        except (OSError, ValueError) as exc:
            print(f"warning: the active calibration {active} cannot be loaded ({exc}); comparing with the built-in table",
                  file=sys.stderr)
    cal = measure(store.task_records(tagged=True), current=current, floors=FLOOR, min_n=args.min_n,
                  min_rate=args.min_rate, min_saving=args.min_saving, eval_id=args.eval, against=against,
                  version=router_version() if args.version == "this" else args.version)
    print(calibration_report(cal))
    if not args.apply:
        print(f"\n--apply writes {calibrations_dir() / (cal.id + '.json')} and makes it the calibration that decides")
        return 0
    from .install import remember

    saved = cal.save(calibrations_dir())
    print(f"\nsaved {saved}")
    remember(None, None, calibration=cal.id)
    print("`router plan` and `router doctor` name it; `router config --calibration none` goes back to the built-in tables")
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    ledger = Ledger(Path(args.ledger) if args.ledger else home() / "ledger.jsonl")
    if args.ledger or args.source == "ledger":
        records = ledger.tasks()
        if ledger.corrupt:
            print(f"warning: {ledger.corrupt} unreadable lines in {ledger.path}, not counted", file=sys.stderr)
    else:
        store = _store()
        store.sync(_ledger_files())  # a line written without the index (another version, a library user) still counts
        records = store.task_records(source=MAIN_LEDGER)
        if store.corrupt:
            print(f"warning: {store.corrupt} unreadable lines in the ledger files, not counted", file=sys.stderr)
    if not records:
        print(f"no tasks in {ledger.path}")
    else:
        print(summarize(records).text())
    feedback = FeedbackStore(home() / "feedback.json")
    rows = feedback.rows()
    if rows:
        print("\nfeedback (first-attempt / total pass rate per kind and cell)")
        print(table(["kind", "cell", "n first", "pass first", "n total", "pass total"], rows))
    hook_rows = _hook_decisions(home() / "hook.jsonl")
    if hook_rows:
        print("\nClaude Code hook decisions (routed subagents)")
        print(table(["cell", "kind", "risk", "times"], hook_rows))
    measured = load_subagents(home() / SUBAGENT_LOG)
    if measured:
        rows = summary_rows(measured)
        print("\nClaude Code subagents, measured from their transcripts (list price; cache reads are the context "
              "read again every turn)")
        print(table(["agent", "model/effort", "n", "turns", "peak ctx", "cache read", "cost", "total"], rows))
    return 0


def _cmd_db(args: argparse.Namespace) -> int:
    store = _store()
    if args.action == "rebuild":
        counts = store.rebuild(_ledger_files())
        print(f"rebuilt {store.path}: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
        return 0
    if not store.path.exists():
        print(f"{store.path}: not built yet (`router stats` or `router db rebuild` build it)")
        return 0
    try:
        counts = ", ".join(f"{v} {k}" for k, v in store.counts().items())
    except sqlite3.DatabaseError as exc:
        print(f"{store.path}: not a readable index ({exc}); `router db rebuild` makes a new one")
        return 1
    state = "behind the ledger: run `router db rebuild`" if store.stale(_ledger_files()) else "in sync with the ledger"
    print(f"{store.path}: {counts} · {state}")
    return 0


def _hook_decisions(path: Path) -> list[list[Any]]:
    if not path.exists():
        return []
    counts: dict[tuple[str, str, str], int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:  # a line cut short by two sessions writing at once
            continue
        key = (r.get("cell", "?"), r.get("kind", "?"), r.get("risk", "?"))
        counts[key] = counts.get(key, 0) + 1
    return [[*k, n] for k, n in sorted(counts.items(), key=lambda kv: -kv[1])]


def _profile_checks(profile: Path) -> list[tuple[bool, str]]:
    """Check how the router is wired into the Claude profile at `profile`: through the plugin (hooks and
    skill come with it; its venv appears at the first session) or through `router install`.
    """
    settings = profile / "settings.json"
    try:
        data = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
    except ValueError:
        return [(False, f"{settings} is not valid JSON: Claude Code ignores the whole file")]
    if not isinstance(data, dict):
        return [(False, f"{settings} is not a JSON object: Claude Code ignores the whole file")]
    checks: list[tuple[bool, str]] = []
    plugins = data.get("enabledPlugins")
    if isinstance(plugins, dict) and plugins.get(PLUGIN_ID):
        checks.append((True, f"plugin {PLUGIN_ID} enabled in {settings}: hooks, skill and the read-only agent "
                             f"(model-router:reader) come from the plugin"))
        venv_hook = profile / "plugins" / "data" / PLUGIN_ID.replace("@", "-") / "venv" / "bin" / "router-hook"
        ready = venv_hook.exists()
        note = "" if ready else " (built when a session starts; needs uv)"
        checks.append((ready, f"plugin environment: {venv_hook}{note}"))
    else:
        hooks = data.get("hooks")
        hooks = hooks if isinstance(hooks, dict) else {}

        def wired(event: str, matcher: str | None = None) -> bool:
            entries = hooks.get(event)
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, dict) or (matcher and entry.get("matcher") != matcher):
                    continue
                commands = entry.get("hooks")
                for h in commands if isinstance(commands, list) else []:
                    if isinstance(h, dict) and "router-hook" in str(h.get("command", "")):
                        return Path(str(h["command"])).exists()
            return False

        fix = " (missing: the plugin or router install)"
        pre, start, stop = wired("PreToolUse", "Agent"), wired("SessionStart"), wired("SubagentStop")
        checks.append((pre, f"hook PreToolUse(Agent) in {settings}" + ("" if pre else fix)))
        checks.append((start, f"hook SessionStart in {settings}" + ("" if start else fix)))
        checks.append((stop, f"hook SubagentStop (measures subagents) in {settings}"
                       + ("" if stop else " (missing: run `router install` again)")))
        link = profile / "skills" / "router"
        checks.append((link.is_dir(), f"skill router: {link}" + ("" if link.is_dir() else fix)))
        agent = profile / "agents" / "router-reader.md"
        checks.append((agent.is_file(), f"read-only agent router-reader: {agent}"
                       + ("" if agent.is_file() else " (missing: run `router install` again)")))
    model = str(data.get("model", "default"))
    model_id = SHORT_TO_ID.get(model, model)  # settings may name the model by its short name
    model_settings = data.get("modelSettings")
    per_model = model_settings.get(model_id) if isinstance(model_settings, dict) else None
    effort = per_model.get("effortLevel") if isinstance(per_model, dict) else None
    checks.append((True, f"main chat: model {model}" + (f", effort {effort}" if effort else "")))
    return checks


def _cmd_doctor(args: argparse.Namespace) -> int:  # noqa: C901 - a flat checklist of independent checks
    """Check that everything is in place: claude binary, profile, hooks, skill, models and state; `--live`
    adds one real Haiku call. Print one line per check and return 1 when any failed.
    """
    from .cli_client import CliClient, resolve_profile

    checks: list[tuple[bool, str]] = []
    claude = shutil.which("claude")
    if claude:
        try:
            version = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=20, check=False).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            version = f"? ({exc})"
        checks.append((True, f"claude on PATH: {claude} ({version.strip()})"))
    else:
        checks.append((False, "claude is not on the PATH: install Claude Code or use --backend api with ANTHROPIC_API_KEY"))
    profile: Path | None = None
    try:
        profile, source = resolve_profile()
        exists = profile.is_dir()
        checks.append((exists, f"Claude profile: {profile} (via {source})" + ("" if exists else " DOES NOT EXIST")))
    except ClientUnavailableError as exc:
        checks.append((False, str(exc)))
    from .install import hook_binary

    hook_bin = hook_binary()
    missing = "not found (install the package: uv tool install ... or uv sync)"
    checks.append((hook_bin is not None, f"hook binary: {hook_bin or missing}"))
    try:
        avail = available_models()
        source = MODELS_ENV if os.environ.get(MODELS_ENV) else ("config.json" if read_config().get("models") else "catalogue")
        checks.append((True, f"available models: {', '.join(MODELS[m].short for m in avail)} (via {source})"))
    except UnknownModelError as exc:
        checks.append((False, f"available models: {exc.name!r} does not exist (check {MODELS_ENV} or `router install --models`)"))
    except ValueError:
        checks.append((False, f"available models: empty list (check {MODELS_ENV} or `router install --models`)"))
    try:
        judge = default_judge()
        source = JUDGE_ENV if os.environ.get(JUDGE_ENV) else ("config.json" if read_config().get("judge") else "default")
        checks.append((True, f"judge for full verification: {judge} (via {source})"))
    except ValueError as exc:  # UnknownModelError is one too
        checks.append((False, f"judge for full verification: {exc} (check {JUDGE_ENV} or `router config --judge`)"))
    checks.append(_calibration_check())
    if profile is not None and profile.is_dir():
        checks += _profile_checks(profile)
    ledger_n = len(Ledger(home() / "ledger.jsonl").tasks())
    hook_n = sum(n for *_, n in _hook_decisions(home() / "hook.jsonl"))
    checks.append((True, f"state in {home()}: {ledger_n} tasks in the ledger, {hook_n} hook decisions"))
    if args.live and claude and profile is not None:
        checks.append(_live_check("claude", lambda: CliClient(config_dir=profile).complete(
            parse_cell("haiku/low"), prompt="Answer only OK.", system="Answer only what is asked.")))
    for ok_, text in checks:
        print(("ok     " if ok_ else "FAIL   ") + text)
    return 0 if all(ok_ for ok_, _ in checks) else 1


def _calibration_check() -> tuple[bool, str]:
    """Return the doctor's line about the calibration: which one decides, or that the named one cannot be loaded."""
    name = active_calibration_name()
    if not name:
        return True, "calibration: none, the built-in tables decide (`router calibrate` measures one)"
    source = CALIBRATION_ENV if os.environ.get(CALIBRATION_ENV) else "config.json"
    try:
        cal = load_calibration(name)
    except (OSError, ValueError) as exc:
        return False, f"calibration {name} (via {source}): cannot be loaded ({exc}); `router config --calibration none`"
    return True, (f"calibration: {cal.id} (made {cal.created[:10]} from {cal.evidence} verified first attempts, "
                  f"{len(cal.changes)} changes to the table) via {source}")


def _live_check(name: str, call: Callable[[], Completion]) -> tuple[bool, str]:
    """Make one real call through `call` and report it as one doctor line."""
    try:
        c = call()
    except ClientUnavailableError as exc:
        return False, f"live call ({name}): {exc}"
    return True, f"live call ({name}): {c.model} answered {c.text.strip()[:20]!r} in {c.seconds:.1f}s, {c.usage.total} tokens"


def _cmd_feedback(args: argparse.Namespace) -> int:
    store = _store()
    ledger = Ledger(home() / "ledger.jsonl", store=store)
    record = ledger.find(args.task_id)
    if record is None:
        raise SystemExit(f"no task {args.task_id} in {ledger.path}")
    passed = bool(args.ok)
    prior = ledger.last_override(args.task_id)
    if prior is None:
        prior = _verified_verdict(record)
    ledger.override(args.task_id, passed, note=args.note or "")
    external = (record.get("attempts") or [{}])[-1].get("stop_reason") == "external"  # never routed: nothing to correct
    if record.get("final_cell") and prior is not None and prior != passed and not external:
        feedback = FeedbackStore(home() / "feedback.json")
        feedback.apply_override(record["kind"], record["final_cell"], passed, first=_was_first_verdict(record))
        feedback.save()
    _warn_index(store)
    print(f"{args.task_id}: marked as {'pass' if passed else 'fail'}")
    return 0


def _verified_verdict(record: dict[str, Any]) -> bool | None:
    """Return the final attempt's verdict when a checker or a judge gave it, else None: there is nothing in
    the feedback store to correct.
    """
    attempts = record.get("attempts") or []
    if not attempts:
        return None
    last = attempts[-1]
    if last.get("verdict") is None or last.get("verdict_source") not in VERIFIED_SOURCES:
        return None
    return bool(last["verdict"])


def _was_first_verdict(record: dict[str, Any]) -> bool:
    """Return whether the final attempt is the one the feedback store counted as a first attempt: on the
    start cell, with no verified verdict before it.
    """
    attempts, plan = record.get("attempts") or [], record.get("plan") or []
    if not attempts or not plan or attempts[-1].get("cell") != plan[0]:
        return False
    return all(a.get("verdict") is None or a.get("verdict_source") not in VERIFIED_SOURCES for a in attempts[:-1])


def _cmd_cells(_args: argparse.Namespace) -> int:
    rows = [[c.label, c.tier, f"{c.capability:.2f}", money(c.expected_cost()), c.spec.input, c.spec.output] for c in lattice()]
    print(table(["cell", "tier", "capability*", "ref. cost", "$/M in", "$/M out"], rows))
    print(f"\n* assumed capability (catalog.py), not measured · ref. cost = 2,000 tokens in, 1,500 out at effort high"
          f" · prices {PRICES_AS_OF}")
    off = [spec.short for m, spec in MODELS.items() if spec.provider == "anthropic" and m not in available_models()]
    if off:
        print(f"not in this installation: {', '.join(off)} ({MODELS_ENV} or `router config --models`)")
    return 0


def _cmd_install(args: argparse.Namespace) -> int:
    from .install import install

    return install(args.settings, skills_dir=args.skills_dir, no_skill=args.no_skill, link=args.link,
                   profile_dir=args.profile_dir, models=args.models)


def _cmd_uninstall(args: argparse.Namespace) -> int:
    from .install import uninstall

    return uninstall(args.settings, skills_dir=args.skills_dir, no_skill=args.no_skill)


def _cmd_config(args: argparse.Namespace) -> int:
    from .install import model_names, remember

    if args.lean is not None:
        path = write_config(lean=args.lean == "on")
        print(f"read-only agent for reading tasks: {args.lean} (in {path})")
    if args.models is None and args.profile_dir is None and args.judge is None and args.calibration is None:
        if args.lean is not None:
            return 0
        cfg = read_config()
        print(f"profile for `router run`: {cfg.get('claude_config_dir') or 'not set'}")
        print(f"available models: {', '.join(MODELS[m].short for m in available_models())}")
        print(f"judge for full verification: {cfg.get('judge') or DEFAULT_JUDGE + ' (default)'}")
        print(f"calibration: {cfg.get('calibration') or 'none (the built-in tables decide)'}")
        print(f"read-only agent for reading tasks: {'off' if cfg.get('lean') is False else 'on'}")
        print(f"(in {config_path()})")
        return 0
    names = model_names(args.models) if args.models is not None else None
    profile = args.profile_dir.expanduser().resolve() if args.profile_dir is not None else None
    judge = parse_cell(args.judge).label if args.judge is not None else None
    calibration = None
    if args.calibration is not None:
        calibration = "" if args.calibration.strip().lower() in NO_CALIBRATION else args.calibration.strip()
        if calibration:
            load_calibration(calibration)  # OSError or ValueError when it cannot be used; main() makes them one line
    remember(profile, names, reset_models=args.models is not None and names is None, judge=judge,
             calibration=calibration)
    return 0


def _add_profile_args(p: argparse.ArgumentParser) -> None:
    from .install import default_settings

    p.add_argument("--settings", type=Path, default=default_settings(),
                   help="settings.json to edit (default: the profile's, $CLAUDE_CONFIG_DIR or ~/.claude)")
    p.add_argument("--skills-dir", type=Path, help="skills directory (default: next to the settings file)")
    p.add_argument("--no-skill", action="store_true", help="hooks only, no skill")


def build_parser() -> argparse.ArgumentParser:
    """Return the `router` argument parser; each subcommand sets `func` to its handler."""
    p = argparse.ArgumentParser(
        prog="router",
        description="Claude model router: picks model + effort, verifies by risk, escalates on failure and keeps the ledger.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan", help="what the router would do with this task (no API call)")
    _add_task_args(sp)
    sp.add_argument("--claude-code", action="store_true", help="print only model/effort, for a Claude Code agent")
    sp.set_defaults(func=_cmd_plan)

    sr = sub.add_parser("run", help="run the task through the router (real calls)")
    _add_task_args(sr)
    _add_backend_args(sr)
    sr.add_argument("--json", action="store_true", help="JSON output with the answer and the attempts")
    sr.set_defaults(func=_cmd_run)

    sj = sub.add_parser("judge", help="verify an answer written elsewhere (a subagent, a person) with the task's judges")
    _add_task_args(sj)
    _add_backend_args(sj)
    sj.add_argument("--answer", required=True, metavar="FILE",
                    help="the answer to judge; `-` reads it from stdin (then give the task as an argument or with --file)")
    sj.add_argument("--cell", help="the cell that produced the answer, e.g. opus/medium (default: the plan's start cell)")
    sj.add_argument("--json", action="store_true", help="JSON output with every judge's verdict")
    sj.set_defaults(func=_cmd_judge)

    sb = sub.add_parser("bench", help="compare strategies: offline simulation or a file of real tasks")
    sb.add_argument("file", nargs="?", help="YAML/JSON with tasks (no file: simulation)")
    sb.add_argument("--simulate", action="store_true", help="force the simulation")
    sb.add_argument("--tasks", type=int, default=60, help="simulated tasks")
    sb.add_argument("--seed", type=int, default=1, help="simulation seed (default 1)")
    sb.add_argument("--detail", action="store_true", help="simulation: breakdown by cell and kind for each strategy")
    sb.add_argument("--strategies", default="router",
                    help='e.g. "router,fable/xhigh,opus/high,sonnet/medium>opus/high"')
    sb.add_argument("--yes", "-y", action="store_true", help="do not ask before spending")
    sb.add_argument("--jobs", "-j", type=int, default=3, help="tasks in parallel (default 3)")
    sb.add_argument("--repeat", type=int, default=1, help="run every task this many times (models are not deterministic)")
    sb.add_argument("--verify", choices=("auto", *ROUTER_VERIFY_MODES),
                    help="verification for fixed-cell strategies: a mode, or auto = what the router picks by risk "
                         "(default: light for a ladder, none for one cell)")
    sb.add_argument("--dry-run", action="store_true", help="validate the file and print the estimate; call nothing")
    sb.add_argument("--matrix", action="store_true",
                    help="one single-cell strategy per cell (every cell, or --cells), verified as the router would "
                         "unless --verify says otherwise; what `router calibrate` measures from")
    sb.add_argument("--cells", help="with --matrix: the cells to run, e.g. haiku/low,sonnet/low,opus/low (default: all)")
    sb.add_argument("--max-usd", type=float,
                    help="do not start when the estimate is above this; stop between strategies once the spend "
                         "reaches it (a strategy in progress runs to its end)")
    _add_backend_args(sb)
    sb.set_defaults(func=_cmd_bench)

    sca = sub.add_parser("calibrate",
                         help="measure the start table from the benches and the ledger; --apply makes it the one that decides")
    sca.add_argument("--eval", help="only the records of this eval id (what `router bench` prints)")
    sca.add_argument("--version", help="only the records written by this router version (`this` = the one running)")
    sca.add_argument("--min-n", type=int, default=MIN_N,
                     help=f"first attempts a (kind, cell) needs before its rate counts (default {MIN_N})")
    sca.add_argument("--min-rate", type=float, default=MIN_RATE,
                     help=f"the lowest first-attempt pass rate a start cell may have (default {MIN_RATE:.0%})".replace("%", "%%"))
    sca.add_argument("--min-saving", type=float, default=MIN_SAVING,
                     help=f"a measured start cell is replaced only by one at least this much cheaper per solved task "
                          f"(default {MIN_SAVING:.0%})".replace("%", "%%"))
    sca.add_argument("--apply", action="store_true", help="save the calibration and make it the one that decides")
    sca.add_argument("--classifier", metavar="FILE",
                     help="instead: compare the rule classifier with the kind and risk this eval file gives each task")
    sca.add_argument("--llm", action="store_true", help="with --classifier: also run the Haiku classifier (real calls)")
    _add_backend_args(sca)
    sca.set_defaults(func=_cmd_calibrate)

    ss = sub.add_parser("stats", help="summary of the ledger and the feedback store")
    ss.add_argument("--ledger", help="another ledger file")
    ss.add_argument("--source", choices=("db", "ledger"), default="db",
                    help="read the SQLite index (synced with the ledger first) or the JSON-lines file directly")
    ss.set_defaults(func=_cmd_stats)

    sdb = sub.add_parser("db", help="the SQLite index of the ledger (~/.model-router/router.db): status, or rebuild it")
    sdb.add_argument("action", nargs="?", choices=("status", "rebuild"), default="status")
    sdb.set_defaults(func=_cmd_db)

    sf = sub.add_parser("feedback", help="correct a task's verdict by hand")
    sf.add_argument("task_id", help="the task id that `router run` prints and `router stats` lists")
    g = sf.add_mutually_exclusive_group(required=True)
    g.add_argument("--ok", action="store_true", help="the answer was good")
    g.add_argument("--ko", action="store_true", help="the answer was bad")
    sf.add_argument("--note", help="why, for the ledger")
    sf.set_defaults(func=_cmd_feedback)

    sd = sub.add_parser("doctor", help="check claude, profile, hooks, skill and state; --live makes one call")
    sd.add_argument("--live", action="store_true",
                    help="also make one real cheap call on Claude and on each model declared through a CLI")
    sd.set_defaults(func=_cmd_doctor)

    sc = sub.add_parser("cells", help="the lattice of cells with their cost and assumed capability")
    sc.set_defaults(func=_cmd_cells)

    si = sub.add_parser("install", help="wire the router into a Claude Code profile (hooks + skill)")
    _add_profile_args(si)
    si.add_argument("--link", action="store_true", help="symlink the skill instead of copying it (development)")
    si.add_argument("--profile-dir", type=Path,
                    help="Claude profile `router run` will use (default: the settings file's, when it is a profile)")
    si.add_argument("--models", help="models that may do the work, e.g. haiku,sonnet,opus, plus any declared external "
                                     "name; all = every Anthropic model")
    si.set_defaults(func=_cmd_install)

    su = sub.add_parser("uninstall", help="remove the hooks and the skill from the profile")
    _add_profile_args(su)
    su.set_defaults(func=_cmd_uninstall)

    sg = sub.add_parser("config", help="show or set the profile for `router run`, the available models, the judge "
                                       "and the calibration (hooks untouched)")
    sg.add_argument("--profile-dir", type=Path, help="Claude profile `router run` will use")
    sg.add_argument("--models", help="models that may do the work, e.g. haiku,sonnet,opus, plus any declared external "
                                     "name; all = every Anthropic model")
    sg.add_argument("--judge", help="cell that judges in full mode, e.g. opus/xhigh (effort capped at xhigh)")
    sg.add_argument("--calibration", help="the calibration that decides: an id from `router calibrate --apply`, or a "
                                          "file; `none` goes back to the built-in tables")
    sg.add_argument("--lean", choices=("on", "off"), help="send safe reading tasks (search, review, analysis, summaries) "
                    "to the read-only agent, which carries a fraction of the general agent's context (default on)")
    sg.set_defaults(func=_cmd_config)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    """Run `router` with `argv` (the process arguments when None) and return the exit code. Bad input
    values become one-line messages with exit code 1 instead of tracebacks.
    """
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except UnknownModelError as exc:
        raise SystemExit(f"unknown model {exc.name!r}; valid: {', '.join(exc.known)} "
                         f"(check {MODELS_ENV}, config.json or --models)") from exc
    except ValueError as exc:
        raise SystemExit(f"invalid value: {exc}") from exc
    except OSError as exc:
        raise SystemExit(f"cannot access {exc.filename or 'a file'}: {exc.strerror or exc}") from exc
    except sqlite3.Error as exc:
        raise SystemExit(f"the index cannot be used ({exc}): `router db rebuild`, or `router stats --source ledger`") from exc


if __name__ == "__main__":
    sys.exit(main())
