"""`router` on the command line: plan (the start cell of a task, no API), stats, doctor, cells, config, project
(what the hook learned in a repository, its floors, its team setup), agents (advice for agents fixed by hand),
limits (the models over their usage limit and their exact reset), usage (what moves the account's seven-day window)
and statusline (the status line command).
docs/cli.md documents each command.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any  # Any: decoded JSON (records, tunings, configs)

from . import agents, clock, floors, limits, natives, projects, shared, team, usage
from .catalog import (
    MODELS,
    PLAN_INPUT_TOKENS,
    PLAN_OUTPUT_TOKENS_AT_HIGH,
    PRICES_AS_OF,
    SHORT_TO_ID,
    UnknownModelError,
    configured_models,
    lattice,
    parse_cell,
    parse_models,
    unknown_listed,
)
from .classify import Classification, classify_rules
from .cli_client import CliClient, ClientUnavailableError, Completion
from .fmt import money, table
from .hook import start_in_force
from .paths import (
    DECISION_LOG,
    claude_profile,
    config_path,
    home,
    is_child,
    logging_on,
    read_config,
    read_jsonl,
    write_config,
)
from .policy import DEFAULT_JUDGE, FLOOR, Policy, default_judge
from .subagents import LOG_FILE as SUBAGENT_LOG
from .subagents import finalize as finalize_subagents
from .subagents import load as load_subagents
from .subagents import summary_rows
from .task import KINDS, RISKS, Task
from .team import PLUGIN_ID

if TYPE_CHECKING:
    from .projects import Change, Counts

DECLARED_ONLY_PROMPT = "(kind and risk declared, no text)"  # what `router plan` plans when it is given no text
CLAUDE_VERSION_SECONDS = 20  # `claude --version` answers in well under a second; this only guards a hung binary
MINUTE_CHARS = 16  # an ISO moment cut to the minute: 2026-10-23T12:13
CHANGES_SHOWN = 5  # the last changes of a project's tuning `router project` prints
ANSWER_SHOWN_CHARS = 20  # how much of the live check's answer doctor prints
CANNOT_RUN = 2  # the exit code of a command that could not run; 1 is a check of `router doctor` failed


class CommandError(ValueError):
    """A command that cannot run as asked: `main` prints the message and exits with CANNOT_RUN."""


def _here() -> str | None:
    """Return the git repository the command runs in, or None outside one."""
    return projects.project_of(str(Path.cwd()))


def _add_task_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("prompt", nargs="?", help="the task; or use --file")
    p.add_argument("--file", "-f", help="file with the task")
    p.add_argument("--kind", "-k", choices=KINDS, help="task kind (inferred by rules when missing)")
    p.add_argument("--risk", "-r", choices=RISKS, help="how bad a wrong answer is (inferred when missing)")


def _prompt_from(args: argparse.Namespace) -> str:
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    if args.prompt:
        return args.prompt
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise CommandError("no task given: pass it as an argument, with --file or on stdin")


def _task_from(args: argparse.Namespace, prompt: str) -> tuple[Task, Classification | None]:
    """Build the task from the arguments and its text, the kind and risk declared or classified, and raised by the
    repository's floors when there is text to search (not the placeholder of a text-less `router plan`).
    """
    kind, risk, cls = args.kind, args.risk, None
    if kind is None or risk is None:
        cls = classify_rules(prompt)
        kind = kind or cls.kind
        risk = risk or cls.risk
    if prompt != DECLARED_ONLY_PROMPT:
        raised = floors.apply(risk, floors.head(prompt), floors=floors.load(_here()))
        if raised.rule is not None:
            print(f"risk raised to {raised.risk} by this repository's floor: {raised.rule.label}", file=sys.stderr)
        for label in raised.unsearched:
            print(f"floor not searched (too slow, or no timer here): {label}", file=sys.stderr)
        risk = raised.risk
    return Task(prompt=prompt, kind=kind, risk=risk), cls


def _cmd_plan(args: argparse.Namespace) -> int:
    # With the kind and the risk declared there is nothing to classify: no text needed (and stdin is not read).
    declared_only = args.kind is not None and args.risk is not None and not args.file and not args.prompt
    prompt = DECLARED_ONLY_PROMPT if declared_only else _prompt_from(args)
    task, cls = _task_from(args, prompt)
    tuning = projects.load_tuning(_here())  # what the hook would start a subagent on, here
    plan = Policy(tuning=tuning).plan(task)
    if args.claude_code:
        cc = plan.start.claude_code()
        print(f"model: {cc['model']}\neffort: {cc['effort']}")
        return 0
    if args.workflow:
        cc = plan.start.claude_code()
        print(f"{{model: '{cc['model']}', effort: '{cc['effort']}'}}")
        return 0
    if cls is not None:
        print(f"classification: {cls.kind} / {cls.risk}  ({cls.reason})")
    print(f"start: {plan.start}  -> Claude Code {plan.start.claude_code()}")
    for line in plan.rationale:
        print("  - " + line)
    return 0


def _cmd_stats(_args: argparse.Namespace) -> int:
    hook_rows = _hook_decisions(home() / DECISION_LOG)
    if hook_rows:
        print("Claude Code hook decisions (routed subagents)")
        print(table(["cell", "kind", "risk", "times"], hook_rows))
    with contextlib.suppress(OSError):
        finalize_subagents(home() / SUBAGENT_LOG)  # transcripts that have settled since their subagent ended
    measured = load_subagents(home() / SUBAGENT_LOG)
    if measured:
        rows = summary_rows(measured)
        print(("\n" if hook_rows else "") + "Claude Code subagents, measured from their transcripts (list price, at "
              "least: output tokens are a lower bound; cache reads are the context read again every turn)")
        print(table(["agent", "model/effort", "n", "turns", "peak ctx", "cache read", "cost ≥", "total ≥"], rows))
    if not hook_rows and not measured:
        print(f"no hook decisions and no measured subagents in {home()} yet")
    return 0


def _cmd_project(args: argparse.Namespace) -> int:
    where = args.path or Path.cwd()
    project = projects.project_of(str(where.expanduser()))
    if project is None:
        raise CommandError(f"not inside a git repository: {where}. The router learns per repository: run it inside "
                           "one, or pass a path inside one")
    if args.team:
        return _turn_on_for_team(project)
    if args.reset:
        projects.reset(project)
        print(f"tuning of {project} removed; evidence counts from now on, so nothing comes back on its own")
        return 0
    with contextlib.suppress(OSError):
        finalize_subagents(home() / SUBAGENT_LOG)
    records = load_subagents(home() / SUBAGENT_LOG)
    if args.share:
        _turn_on_sharing(project, records)
    tuning = projects.load_tuning(project)
    evidence = _project_evidence(project, records, since=tuning.get("since"))
    _print_evidence(project, evidence, since=tuning.get("since"))
    _print_tuning(project, tuning)
    _print_floors(project)
    proposals = _proposals(project, tuning, evidence=evidence)
    _print_proposals(proposals)
    if proposals and args.apply:
        print(f"applied: {projects.apply(project, proposals)}")
    elif proposals:
        print("`router project --apply` applies them; a start that fails here too is raised on its own at session start")
    return 0


def _cmd_limits(args: argparse.Namespace) -> int:
    if args.check is not None:
        held = [n for n in limits.active() if n != limits.ALL_MODELS and n in SHORT_TO_ID]
        names = args.check or sorted({limits.PROBE_MODEL, *held})
        config_dir = claude_profile()
        for name in names:
            spec = MODELS.get(SHORT_TO_ID.get(name, name))
            if spec is None:
                known = ", ".join(s.short for s in MODELS.values())
                raise CommandError(f"unknown Claude model: {name}; try one of {known}")
            limit = limits.record_check(spec.short, limits.probe(spec.id, config_dir=config_dir))
            print(f"{spec.short}: " + (f"limited until {limit.when()} ({limit.window})" if limit else "available"))
    held = limits.active()
    if held:
        print(table(["model", "limited until", "window"],
                    [["every model" if n == limits.ALL_MODELS else n, lim.when(), lim.window]
                     for n, lim in sorted(held.items())]))
    else:
        print("no model is over its usage limit")
    seen = limits.windows()
    if seen:
        rows = [[name, f"{float(w.get('utilization') or 0):.0%}", _local(w.get("resets_at")),
                 _local(w.get("seen") or limits.checked())] for name, w in sorted(seen.items())]
        print("\naccount windows (seven_day_overage_included is Fable's own week)")
        print(table(["window", "used", "resets", "seen"], rows))
    return 0


def _cmd_statusline(_args: argparse.Namespace) -> int:
    """Print the status line for the JSON Claude Code sends on stdin and keep a sample of it. A status line must
    never fail: unreadable input prints nothing, a sample that cannot be written is skipped.
    """
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    try:
        status = usage.read_status(data if isinstance(data, dict) else {})
        fable = usage.fable_window()
        if fable is not None:
            status.fable, status.fable_resets = fable
        print(usage.statusline_text(status), flush=True)
        if not is_child():
            if logging_on():
                usage.keep_sample(status)
            usage.refresh_fable()  # not a log: Fable's window is read whenever Fable is among the models
    except Exception:  # noqa: BLE001 - a status line never fails; the next update tries again
        return 0
    return 0


def _cmd_usage(args: argparse.Namespace) -> int:
    profiles = _usage_profiles(args.profile)
    spent = usage.spent(profiles, since=usage.since_days(args.days))
    print(f"last {args.days} days, from the transcripts of {', '.join(_home_relative(p) for p in profiles)}")
    if not spent.calls:
        print("no calls found")
        return 0
    rows = usage.samples(profiles)
    spans = usage.intervals(rows, since=spent.since.timestamp())  # only intervals whose calls were all read
    print(f"{len(spent.calls):,} calls; seven-day window: {len(rows)} status line samples, {len(spans)} intervals")
    daily = _daily_window(spans)
    if daily:
        print(table(["day", "points of the week used"], daily))
    fits = usage.fit(spent.calls, spans)
    if not fits:
        print(f"which tokens move the window: not enough samples yet ({len(spans)} intervals, "
              f"{usage.MIN_INTERVALS} needed); `router statusline` keeps them while you work")
        return 0
    print("\nwhich tokens move the window: one point of the week priced in each weighting; the steadiest price "
          "(lowest spread) is the weighting the window counts")
    print(table(["weighting", "intervals", "spread", "one point of the week"],
                [[f.name, f.intervals, f"{f.spread:.0%}", _per_point(f)] for f in fits]))
    return 0


def _usage_profiles(given: list[Path] | None) -> list[Path]:
    """Return the profiles `router usage` reads: the ones given, else the profile in use plus `usage_profiles` from
    config.json (other profiles signed in to the same account, whose windows they share).
    """
    if given:
        paths = [p.expanduser().absolute() for p in given]
    else:
        extra = read_config().get("usage_profiles")
        listed = [Path(str(p)).expanduser().absolute() for p in extra] if isinstance(extra, list) else []
        paths = [claude_profile(), *listed]
    return list(dict.fromkeys(paths))


def _home_relative(path: Path) -> str:
    with contextlib.suppress(ValueError):
        return "~/" + str(path.relative_to(Path.home()))
    return str(path)


def _daily_window(spans: list[usage.Interval]) -> list[list[object]]:
    days: dict[str, float] = {}
    for span in spans:
        day = datetime.fromtimestamp(span.end).astimezone().strftime("%a %d/%m")
        days[day] = days.get(day, 0.0) + span.used
    return [[day, f"{used:.1f}"] for day, used in days.items()]


def _per_point(f: usage.Fit) -> str:
    return _millions(int(f.per_point)) + " tokens" if f.unit == "tokens" else f"{money(f.per_point)} ({f.unit})"


def _millions(n: int) -> str:
    return f"{n / 1_000_000:,.1f}M"


def _local(moment: object) -> str:
    parsed = clock.parse_moment(moment) if moment else None
    return clock.local(parsed) if parsed else "?"


def _cmd_agents(args: argparse.Namespace) -> int:
    where = args.path or Path.cwd()
    project = projects.project_of(str(where.expanduser()))
    if project is None:
        raise CommandError(f"not inside a git repository: {where}. Agents are advised per repository")
    with contextlib.suppress(OSError):
        finalize_subagents(home() / SUBAGENT_LOG)
    committed = agents.committed_agents(project) or {}
    pooled, teammates = shared.pooled_agents(
        project, keep=lambda agent, label: agent in committed and agents.names_cell(committed[agent], parse_cell(label)))
    advice = agents.advise(load_subagents(home() / SUBAGENT_LOG), project, cells=lattice(), team=pooled)
    if args.json:
        print(json.dumps([a.to_dict() for a in advice], indent=2))
        return 0
    if not advice:
        print(f"no agent with a fixed model has been measured in {project} yet")
        return 0
    rows = [[a.agent, a.cell.label, a.runs, a.team_runs, a.repeated, f"{a.mean_turns:.1f}" if a.runs > a.team_runs
             else "-", money(a.total_cost), _advice_text(a)] for a in advice]
    shared_from = (f"; runs include {teammates} teammate installation{'s' if teammates != 1 else ''} through the "
                   "shared counts" if teammates else "")
    print(f"agents with a fixed model in {project} (measured; turns and spend from this machine, list price at "
          f"least{shared_from})")
    print(table(["agent", "cell", "runs", "team", "repeated", "turns", "spent", "advice"], rows))
    for a in advice:
        if a.to is not None and (path := agents.agent_file(project, a.agent)):
            print(f"  {a.agent}: {path}")
    print("Advice only: change `model:` and `effort:` in the agent yourself. A saving reprices the measured tokens at "
          "the new model's prices; a change of effort also changes the tokens, which is not estimated.")
    return 0


def _advice_text(advice: agents.Advice) -> str:
    if advice.action == "measuring":
        return f"measuring ({advice.runs} of {agents.MIN_N} runs)"
    if advice.to is None:
        return "keep"
    saving = "" if advice.saving is None else f", {money(abs(advice.saving))} {'less' if advice.saving >= 0 else 'more'}"
    return f"{advice.action} to {advice.to}{saving}"


def _turn_on_for_team(project: str) -> int:
    """Declare the marketplace and turn the plugin on in the repository's settings, and say what each person does."""
    try:
        changed = team.enable(project)
    except team.SettingsError as exc:
        raise CommandError(str(exc)) from exc
    settings = team.settings_path(project)
    print(f"{settings}: " + ("; ".join(changed) if changed else "the plugin was already on for the team"))
    print(f"Commit it. Each person, once, in this repository: `{team.INSTALL_COMMAND}` (needs uv), then a new session.")
    return 0


@dataclass(frozen=True)
class _Evidence:
    """A project's evidence: this machine's, the other installations', both together, and how many of those gave
    some.
    """

    mine: Counts
    others: Counts
    both: Counts
    installs: int


def _turn_on_sharing(project: str, records: list[dict[str, Any]]) -> None:
    """Create the repository's shared-counts directory, prune it, and add this installation's settled weeks."""
    folder = shared.shared_dir(project)
    if folder.parent.is_symlink() or folder.is_symlink():
        raise CommandError(f"{folder} is reached through a link: shared counts need a plain directory in the "
                           "repository. Replace the link with a directory and run `router project --share` again")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".gitkeep").touch()  # git keeps no empty directory, and the directory is the opt-in
    if not shared.sharing(project):
        raise CommandError(f"{folder} cannot be used for shared counts because {shared.SHARE_ENV}=0 keeps this "
                           "installation out. Unset it and run `router project --share` again")
    shared.prune(project)
    written = shared.write_own(project, records, decision_log=home() / DECISION_LOG)
    print(f"{folder}: shared counts on; commit it, and every teammate's router takes part"
          + (f" (added {len(written)} week file(s))" if written else
             " (a week is added a day after it closes, if the router routed subagents in it)"))


def _project_evidence(project: str, records: list[dict[str, Any]], *, since: str | None) -> _Evidence:
    """Return a project's evidence since `since`: this machine's and, when the repository shares, the team's."""
    mine = projects.evidence(project, records, relaunched=projects.relaunched_ids(home() / DECISION_LOG), since=since)
    others, installs = shared.pooled(project, since=since)
    return _Evidence(mine=mine, others=others, both=projects.combine(mine, others), installs=installs)


def _print_evidence(project: str, evidence: _Evidence, *, since: str | None) -> None:
    """Print where a project's evidence comes from and its counts per kind, risk and start cell."""
    print(f"project {project}" + (f" (evidence since {since[:MINUTE_CHARS]})" if since else ""))
    if shared.sharing(project):
        print(f"shared counts: on, {evidence.installs} other installation(s) with evidence in "
              f"{shared.shared_dir(project)}")
    if not evidence.both:
        print("no subagent routed here yet with model and effort both chosen by the router")
        return
    rows = [[kind, risk, cell, tally["n"], tally["fails"], evidence.others.get((kind, risk, cell), {}).get("n", 0)]
            for (kind, risk, cell), tally in sorted(evidence.both.items())]
    print("\nsubagents the router routed here (relaunched = launched again higher, same kind and risk: a fail)")
    print(table(["kind", "risk", "start cell", "n", "relaunched", "n from others"], rows))


def _print_tuning(project: str, tuning: dict[str, Any]) -> None:
    """Print a project's tuning in force and its last changes."""
    tuned = [[kind, risk, entry["cell"], entry["base"]] for kind, row in sorted(tuning.get("start", {}).items())
             for risk, entry in sorted(row.items())]
    if not tuned:
        return
    print(f"\ntuning in force ({projects.tuning_path(project)})")
    print(table(["kind", "risk", "starts on", "instead of"], tuned))
    for change in [change for change in tuning.get("changes", []) if change.get("kind")][-CHANGES_SHOWN:]:
        print(f"  {change['at'][:MINUTE_CHARS]} {change['what']} {change['kind']} ({change['risk']}) from {change['from']} "
              f"to {change['to']}, {change['relaunched']} of {change['n']} relaunched")


def _print_floors(project: str) -> None:
    """Print the repository's risk floors, how often each raised a subagent's risk here, and what is wrong with any."""
    found = floors.load(project)
    if found.path is None:
        return
    hits: dict[str, int] = {}
    for record in read_jsonl(home() / DECISION_LOG):
        if record.get("project") == project and record.get("floor_match"):
            hits[str(record["floor_match"])] = hits.get(str(record["floor_match"]), 0) + 1
    print(f"\nrisk floors ({found.path})")
    if found.rules:
        print(table(["at least", "when the brief says", "raised"],
                    [[rule.risk, rule.label, hits.get(rule.match, 0)] for rule in found.rules]))
    for problem in found.problems:
        print(f"  not used: {problem}")


def _team_check(profile: Path) -> tuple[Path | None, tuple[bool, str] | None]:
    """Check a repository that turns the plugin on for its team (`router project --team`): is it installed in this
    profile? Return the repository's settings when it is (the plugin is on through them), and the check.
    """
    project = _here()
    if project is None or not team.enabled_in(project):
        return None, None
    settings = team.settings_path(project)
    if team.installed_in(profile, project):
        return settings, (True, f"plugin turned on for the team in {settings} and installed in {profile}")
    return None, (False, f"plugin turned on for the team in {settings} but not installed in {profile}: run "
                         f"`{team.INSTALL_COMMAND}` here once, then start a new session")


def _limits_checks() -> list[tuple[bool, str]]:
    """Report the models over their usage limit and until when; informative, so never a failure."""
    return [(True, f"usage limit: {'every model' if name == limits.ALL_MODELS else name} until {limit.when()} "
                   f"({limit.window}); `router limits` for the windows")
            for name, limit in sorted(limits.active().items())]


def _floors_check() -> tuple[bool, str] | None:
    """Check the floors of the repository the command runs in, when it has a floors file."""
    found = floors.load(_here())
    if found.path is None:
        return None
    if found.problems:
        return False, f"risk floors in {found.path}: {len(found.rules)} usable; " + "; ".join(found.problems)
    return True, f"risk floors in {found.path}: {len(found.rules)} rules"


def _proposals(project: str, tuning: dict[str, Any], *, evidence: _Evidence) -> list[Change]:
    """Return the changes the evidence supports that wait for a hand: raises on other installations' counts alone,
    and cheaper starts.
    """
    current = start_in_force(tuning)
    return (projects.team_raises(evidence.both, current, local=evidence.mine)
            + projects.propose_lower(project, evidence.both, floors=FLOOR, current=current))


def _print_proposals(proposals: list[Change]) -> None:
    """Print the changes that wait for a hand, if there are any."""
    if not proposals:
        return
    print("\nchanges the evidence supports, not applied (raises from other installations' counts alone, "
          f"cheaper starts after {projects.DOWN_N}+ launches with none relaunched):")
    for change in proposals:
        print(f"  {change['what']} {change['kind']} ({change['risk']}) from {change['from']} to {change['to']}, "
              f"{change['relaunched']} of {change['n']} relaunched")


def _hook_decisions(path: Path) -> list[list[Any]]:
    """Return the hook's decisions in the log at `path` counted per cell, kind and risk, the most frequent first."""
    counts: dict[tuple[str, str, str], int] = {}
    for r in read_jsonl(path):
        key = (r.get("cell", "?"), r.get("kind", "?"), r.get("risk", "?"))
        counts[key] = counts.get(key, 0) + 1
    return [[*k, n] for k, n in sorted(counts.items(), key=lambda kv: -kv[1])]


def _profile_checks(profile: Path, *, enabled_by: Path | None = None) -> list[tuple[bool, str]]:
    """Check that the plugin is on in the Claude profile at `profile` (or through the repository's settings at
    `enabled_by`) and that its environment is built, and report the main chat's model and effort.
    """
    settings = profile / "settings.json"
    try:
        data = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
    except ValueError:
        return [(False, f"{settings} is not valid JSON: Claude Code ignores the whole file")]
    if not isinstance(data, dict):
        return [(False, f"{settings} is not a JSON object: Claude Code ignores the whole file")]
    plugins = data.get("enabledPlugins")
    on_here = isinstance(plugins, dict) and bool(plugins.get(PLUGIN_ID))
    if not on_here and enabled_by is None:
        checks = [(False, f"plugin {PLUGIN_ID} is not enabled in {settings}: `claude plugin install {PLUGIN_ID}`")]
    else:
        venv_hook = profile / "plugins" / "data" / PLUGIN_ID.replace("@", "-") / "venv" / "bin" / "router-hook"
        ready = venv_hook.exists()
        checks = [(True, f"plugin {PLUGIN_ID} enabled in {settings if on_here else enabled_by}"),
                  (ready, f"plugin environment: {venv_hook}" + ("" if ready else " (built when a session starts; needs uv)"))]
    model = str(data.get("model", "default"))
    model_settings = data.get("modelSettings")
    per_model = model_settings.get(SHORT_TO_ID.get(model, model)) if isinstance(model_settings, dict) else None
    effort = per_model.get("effortLevel") if isinstance(per_model, dict) else None
    checks.append((True, f"main chat: model {model}" + (f", effort {effort}" if effort else "")))
    return checks


def _claude_check() -> tuple[bool, str]:
    """Check that the `claude` command is on the PATH and say its version."""
    claude = shutil.which("claude")
    if not claude:
        return False, "claude is not on the PATH: install Claude Code"
    try:
        version = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=CLAUDE_VERSION_SECONDS,
                                 check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        version = f"? ({exc})"
    return True, f"claude on PATH: {claude} ({version})"


def _settings_checks() -> list[tuple[bool, str]]:
    """Check the models and the cross-check judge `router config` set, and report where each came from."""
    cfg = read_config()
    names = ", ".join(MODELS[m].short for m in configured_models())
    checks = [(True, f"available models: {names} (via {'config.json' if cfg.get('models') else 'catalogue'})")]
    unknown = unknown_listed()
    if unknown:
        checks.append((False, f"the models setting names models this router does not know, ignored: "
                              f"{', '.join(unknown)} (`router config --models`)"))
    try:
        checks.append((True, f"judge for critical reviews on the chat's model: {default_judge()} "
                             f"(via {'config.json' if cfg.get('judge') else 'default'})"))
    except ValueError as exc:  # UnknownModelError is one too
        checks.append((False, f"judge for critical reviews on the chat's model: {exc} (`router config --judge`)"))
    return checks


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Check that everything is in place: claude, the plugin, settings, limits, floors and state; `--live` adds
    one real Haiku call. Print one line per check and return 1 when any failed.
    """
    profile = claude_profile()
    claude = _claude_check()
    checks = [claude, (profile.is_dir(), f"Claude profile: {profile}" + ("" if profile.is_dir() else " DOES NOT EXIST"))]
    checks += _settings_checks()
    checks += _limits_checks()
    floors_check = _floors_check()
    if floors_check is not None:
        checks.append(floors_check)
    if profile.is_dir():
        enabled_by, team_check = _team_check(profile)
        if team_check is not None:
            checks.append(team_check)
        checks += _profile_checks(profile, enabled_by=enabled_by)
        checks += [(True, line) for line in natives.report()]
    hook_n = sum(n for *_, n in _hook_decisions(home() / DECISION_LOG))
    checks.append((True, f"state in {home()}: {hook_n} hook decisions"))
    if args.live and claude[0]:
        checks.append(_live_check("claude", lambda: CliClient().complete(
            parse_cell("haiku/low"), prompt="Answer only OK.", system="Answer only what is asked.")))
    for passed, text in checks:
        print(("ok     " if passed else "FAIL   ") + text)
    return 0 if all(passed for passed, _ in checks) else 1


def _live_check(name: str, call: Callable[[], Completion]) -> tuple[bool, str]:
    """Make one real call through `call` and report it as one doctor line."""
    try:
        c = call()
    except ClientUnavailableError as exc:
        return False, f"live call ({name}): {exc}"
    answered = c.text.strip()[:ANSWER_SHOWN_CHARS]
    return True, f"live call ({name}): {c.model} answered {answered!r} in {c.seconds:.1f}s, {c.usage.total} tokens"


def _cmd_cells(_args: argparse.Namespace) -> int:
    rows = [[c.label, c.tier, f"{c.capability:.2f}", money(c.expected_cost()), c.spec.input, c.spec.output] for c in lattice()]
    print(table(["cell", "tier", "capability*", "ref. cost", "$/M in", "$/M out"], rows))
    print(f"\n* assumed capability (catalog.py), not measured · ref. cost = {PLAN_INPUT_TOKENS:,} tokens in, "
          f"{PLAN_OUTPUT_TOKENS_AT_HIGH:,} out at effort high · prices {PRICES_AS_OF}")
    off = [spec.short for m, spec in MODELS.items() if m not in configured_models()]
    if off:
        print(f"not in this installation: {', '.join(off)} (`router config --models`)")
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    if args.lean is not None:
        write_config(lean=None if args.lean == "on" else False)
    if args.natives is not None:
        write_config(natives=None if args.natives == "on" else False)
    if args.models is not None:
        write_config(models=_model_names(args.models))
    if args.judge is not None:
        write_config(judge=parse_cell(args.judge).label)
    if args.usage_profiles is not None:
        listed = [] if args.usage_profiles.strip().lower() == "none" else [
            str(Path(p.strip()).expanduser().absolute()) for p in args.usage_profiles.split(",") if p.strip()]
        write_config(usage_profiles=listed or None)
    cfg = read_config()
    print(f"Claude profile: {claude_profile()}")
    print(f"available models: {', '.join(MODELS[m].short for m in configured_models())}")
    print(f"judge for critical reviews on the chat's model: {cfg.get('judge') or DEFAULT_JUDGE + ' (default)'}")
    print(f"read-only agent for reading tasks: {'off' if cfg.get('lean') is False else 'on'}")
    print(f"native settings written once into settings.json: {'on' if natives.enabled() else 'off'}")
    print(f"profiles `router usage` reads: {', '.join(_home_relative(p) for p in _usage_profiles(None))}")
    print(f"(in {config_path()})")
    return 0


def _model_names(models: str) -> list[str] | None:
    """Return the short names in `models` ('haiku,opus', any order) in catalogue order, or None for 'all'.

    Raises:
        UnknownModelError: a name outside the catalogue, so a typo never silently changes the lattice.
    """
    if models.strip().lower() == "all":
        return None
    return [MODELS[m].short for m in parse_models(models)]


def build_parser() -> argparse.ArgumentParser:
    """Return the `router` argument parser; each subcommand sets `func` to its handler."""
    p = argparse.ArgumentParser(
        prog="router",
        description="Claude model router: picks model + effort for subagents, measures them and learns per repository.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan", help="what the router would do with this task (no API call)")
    _add_task_args(sp)
    shape = sp.add_mutually_exclusive_group()
    shape.add_argument("--claude-code", action="store_true", help="print only model/effort, for a Claude Code agent")
    shape.add_argument("--workflow", action="store_true",
                       help="print only the options of a workflow agent() call, e.g. {model: 'opus', effort: 'medium'}")
    sp.set_defaults(func=_cmd_plan)

    ss = sub.add_parser("stats", help="the hook's decisions and the measured subagents")
    ss.set_defaults(func=_cmd_stats)

    sd = sub.add_parser("doctor", help="check claude, the profile, the plugin, settings, limits, floors and state; "
                                       "--live makes one call")
    sd.add_argument("--live", action="store_true",
                    help="also make one real cheap call on Claude")
    sd.set_defaults(func=_cmd_doctor)

    sc = sub.add_parser("cells", help="the lattice of cells with their cost and assumed capability")
    sc.set_defaults(func=_cmd_cells)

    spj = sub.add_parser("project", help="what the hook learned in this project: subagents routed, relaunched higher, the "
                                        "start cells it raised, and cheaper ones it proposes")
    spj.add_argument("path", nargs="?", type=Path, help="a directory in the project (default: the current one)")
    spj.add_argument("--apply", action="store_true", help="apply the proposed changes (cheaper starts, raises on the "
                     "team's counts alone)")
    spj.add_argument("--reset", action="store_true", help="remove this project's tuning")
    spj.add_argument("--team", action="store_true", help="turn the plugin on for everyone in this repository: writes "
                     "the marketplace and the plugin into .claude/settings.json, to commit")
    spj.add_argument("--share", action="store_true", help="share counts through the repository: creates "
                     ".claude/model-router/, to commit; each installation adds one small file per week there")
    spj.set_defaults(func=_cmd_project)

    sl = sub.add_parser("limits", help="the models over their usage limit and the exact moment each resets, and the "
                                       "account's windows")
    sl.add_argument("--check", nargs="*", metavar="MODEL", help="ask now: the models named (default: haiku, for the "
                    "windows, and every model held). A refused question costs nothing; an allowed one is one tiny call")
    sl.set_defaults(func=_cmd_limits)

    sa = sub.add_parser("agents", help="advice on the agents whose model and effort are fixed by hand, from what they "
                                       "spent and how often they were repeated in this project")
    sa.add_argument("path", nargs="?", type=Path, help="a directory in the project (default: the current one)")
    sa.add_argument("--json", action="store_true", help="the advice as JSON")
    sa.set_defaults(func=_cmd_agents)

    sg = sub.add_parser("config", help="show or set the available models, the judge, the read-only agent and the "
                                       "profiles `router usage` reads")
    sg.add_argument("--models", help="Claude models that may do the work, e.g. haiku,sonnet,opus; all = every Claude model")
    sg.add_argument("--judge", help="cell a critical review moves to when it would run on the chat's model, e.g. "
                    "opus/xhigh (effort capped at xhigh)")
    sg.add_argument("--lean", choices=("on", "off"), help="send safe reading tasks (search, review, analysis, summaries) "
                    "to the read-only agent, which carries a fraction of the general agent's context (default on)")
    sg.add_argument("--natives", choices=("on", "off"), help="let the router write autoCompactWindow and the status line "
                    "into the profile's settings.json once, where they are missing (default on)")
    sg.add_argument("--usage-profiles", metavar="DIR,DIR", help="other Claude profiles signed in to the same account, "
                    "which `router usage` reads too (they share its windows); none clears them")
    sg.set_defaults(func=_cmd_config)

    su = sub.add_parser("usage", help="the account's seven-day window day by day, and which tokens of the transcripts "
                        "move it")
    su.add_argument("--days", type=int, default=7, help="how many days back (default 7)")
    su.add_argument("--profile", type=Path, action="append", help="a Claude profile to read (repeatable; default: the "
                    "one in use plus `router config --usage-profiles`)")
    su.set_defaults(func=_cmd_usage)

    sst = sub.add_parser("statusline", help="the status line command: context and the account's windows, read from "
                         "the JSON Claude Code sends on stdin; keeps a sample of each change")
    sst.set_defaults(func=_cmd_statusline)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    """Run `router` with `argv` (the process arguments when None) and return the exit code: 0 done, 1 a check of
    `router doctor` failed, CANNOT_RUN when the command could not run, said in one line on stderr instead of a
    traceback.
    """
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except UnknownModelError as exc:
        message = f"unknown model {exc.name!r}; valid: {', '.join(exc.known)} (check config.json or --models)"
    except CommandError as exc:
        message = str(exc)
    except ValueError as exc:
        message = f"invalid value: {exc}"
    except OSError as exc:
        message = f"cannot access {exc.filename or 'a file'}: {exc.strerror or exc}"
    print(f"router: {message}", file=sys.stderr)
    return CANNOT_RUN


if __name__ == "__main__":
    sys.exit(main())
