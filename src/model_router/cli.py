"""`router` on the command line: plan (no API), run (live), bench (simulated or a file of real tasks),
stats, feedback, doctor, cells, install and uninstall. Everything a user sees here is Spanish; the
README documents each command.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._fmt import money, table
from .catalog import MODELS, MODELS_ENV, PRICES_AS_OF, SHORT_TO_ID, UnknownModelError, available_models, lattice, parse_cell
from .classify import Classification, classify_llm, classify_rules
from .client import ClaudeClient, ClientUnavailableError
from .feedback import FeedbackStore
from .ledger import Ledger, summarize
from .paths import CONFIG_DIR_ENV, config_path, home, read_config
from .policy import LadderPolicy, Policy, PolicyProtocol
from .runner import Router, TaskResult
from .task import DEFAULT_MAX_TOKENS, KINDS, RISKS, VERIFY_MODES, Task, json_checker, regex_checker

ESCALATION_COST_FACTOR = 1.6  # a strategy that may escalate spends about this much more than its first attempt
JUDGE_COST_ESTIMATE_USD = {"light": 0.005, "full": 0.02}  # per verified attempt, rough, for the bench estimate
REASON_COLUMN_CHARS = 70  # keeps the attempts table on one line per attempt
PLUGIN_ID = "model-router@dcamunas"  # the plugin id Claude Code uses once installed from the marketplace


def _add_task_args(p: argparse.ArgumentParser, *, with_prompt: bool = True) -> None:
    if with_prompt:
        p.add_argument("prompt", nargs="?", help="la tarea; o usa --file")
        p.add_argument("--file", "-f", help="fichero con la tarea")
    p.add_argument("--kind", "-k", choices=KINDS, help="tipo de tarea (si falta, se infiere por reglas)")
    p.add_argument("--risk", "-r", choices=RISKS, help="riesgo de equivocarse (si falta, se infiere)")
    p.add_argument("--verify", choices=VERIFY_MODES, help="fuerza el modo de verificación")
    p.add_argument("--budget", type=float, help="tope en USD para la tarea")
    p.add_argument("--max-attempts", type=int, help="intentos máximos")
    p.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS, help="solo backend api; el cli lo gestiona solo")
    p.add_argument("--system", help="fichero con el system prompt del trabajador")
    p.add_argument("--check-regex", help="la respuesta debe casar con esta regex (checker programático)")
    p.add_argument("--check-json", help="la respuesta debe ser JSON con estas claves (coma-separadas; vacío = solo JSON válido)")
    p.add_argument("--classify-llm", action="store_true", help="clasifica con Haiku en vez de con reglas")


def _prompt_from(args: argparse.Namespace) -> str:
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    if args.prompt:
        return args.prompt
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("falta la tarea: pásala como argumento, con --file o por stdin")


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
                budget_usd=args.budget, max_attempts=args.max_attempts, verify=args.verify)
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
        print(f"clasificación: {cls.kind} / {cls.risk}  ({cls.reason})")
    print(f"inicio: {plan.start}  -> Claude Code {plan.start.claude_code()}")
    for line in plan.rationale:
        print("  - " + line)
    verifier = policy.verifier_for(plan.start, plan.verify)
    if verifier is not None:
        print(f"verificador ({plan.verify}): {verifier}")
    first = plan.start.expected_cost(task.estimated_input_tokens, task.planned_output_tokens)
    print(f"coste estimado del primer intento: {money(first)}"
          f"  (precios {PRICES_AS_OF})")
    return 0


def _attempts_table(result: TaskResult) -> str:
    rows = []
    for a in result.attempts:
        verdict = "-" if a.verdict is None else ("OK" if a.verdict else "KO")
        served = a.served_model if a.fallback_ran else ""
        rows.append([a.n, a.cell.label, served, a.stop_reason, f"{a.usage.input_tokens:,}/{a.usage.output_tokens:,}",
                     money(a.cost_usd), f"{verdict} ({a.verdict_source}{', ' + a.verifier.label if a.verifier else ''})",
                     money(a.verify_cost_usd), a.reason[:REASON_COLUMN_CHARS]])
    return table(["#", "celda", "fallback", "stop", "tok in/out", "coste", "veredicto", "verif.", "motivo"], rows)


def _add_backend_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--backend", choices=("auto", "api", "cli"), default="auto",
                   help="api = SDK con ANTHROPIC_API_KEY; cli = `claude -p` con tu cuenta; auto = api si hay clave, si no cli")
    p.add_argument("--claude-config-dir", help=f"perfil de Claude para el backend cli (si falta: {CONFIG_DIR_ENV})")
    p.add_argument("--no-fallbacks", action="store_true", help="backend api: sin fallbacks de refusal en el servidor")


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
    router = Router(client, policy=Policy(feedback=feedback), ledger=Ledger(home() / "ledger.jsonl"), feedback=feedback)
    result = router.run(task)
    if args.json:
        print(json.dumps({**result.to_dict(), "answer": result.answer}, ensure_ascii=False, indent=1))
        return 0 if result.solved else 1
    print(result.answer)
    print()
    if cls is not None:
        print(f"clasificación: {cls.kind} / {cls.risk}  ({cls.reason})")
    print(_attempts_table(result))
    print(f"estado {result.status} · coste {money(result.total_cost_usd)} (verificación {money(result.verify_cost_usd)})"
          f" · reintentos {result.retries} · id {task.id}")
    if not result.solved:
        print("la última respuesta no pasó la verificación; revisa antes de usarla", file=sys.stderr)
    return 0 if result.solved else 1


def _load_specs(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        import yaml

        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    specs = data["tasks"] if isinstance(data, dict) else data
    if not isinstance(specs, list) or not specs:
        raise SystemExit(f"{path}: se esperaba una lista de tareas")
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
                max_attempts=spec.get("max_attempts"), verify=spec.get("verify"))


def _parse_strategy(text: str) -> tuple[str, PolicyProtocol]:
    text = text.strip()
    if text == "router":
        return "router", Policy()
    cells = [parse_cell(c) for c in text.split(">")]
    return text, LadderPolicy(cells, verify="light" if len(cells) > 1 else "none")


def _cmd_bench(args: argparse.Namespace) -> int:
    if args.file is None or args.simulate:
        from .simulate import format_report, run_simulation

        tasks, reports = run_simulation(args.tasks, args.seed)
        print(format_report(tasks, reports, args.seed, detail=args.detail))
        return 0
    specs = _load_specs(Path(args.file))
    tasks = [_spec_task(s) for s in specs]
    strategies = [_parse_strategy(s) for s in args.strategies.split(",")]
    estimate = 0.0
    for _, policy in strategies:
        for t in tasks:
            plan = policy.plan(t)
            first = plan.start.expected_cost(t.estimated_input_tokens, t.planned_output_tokens)
            estimate += first * (1.0 if len(plan.ladder) == 1 else ESCALATION_COST_FACTOR)
            estimate += JUDGE_COST_ESTIMATE_USD.get(plan.verify, 0.0)
    print(f"{len(tasks)} tareas x {len(strategies)} estrategias · estimación {money(estimate)} "
          "(orientativa, puede quedarse corta)")
    if not args.yes:
        answer = input("¿Lanzar llamadas reales? [s/N] ").strip().lower()
        if answer not in ("s", "si", "sí", "y", "yes"):
            print("cancelado")
            return 1
    client = _live_client(args)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    for name, policy in strategies:
        ledger = Ledger(home() / "bench" / f"{stamp}-{name.replace('/', '-').replace('>', '_')}.jsonl")
        router = Router(client, policy=policy, ledger=ledger)
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            for t, res in zip(tasks, pool.map(router.run, tasks), strict=True):
                print(f"  [{name}] {t.id} {res.status} {money(res.total_cost_usd)} via {res.final_cell}")
        print(f"\n== {name} ==")
        print(summarize(ledger.tasks()).text())
        print(f"registro: {ledger.path}\n")
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    ledger = Ledger(Path(args.ledger) if args.ledger else home() / "ledger.jsonl")
    records = ledger.tasks()
    if ledger.corrupt:
        print(f"aviso: {ledger.corrupt} líneas ilegibles en {ledger.path}, no se cuentan", file=sys.stderr)
    if not records:
        print(f"sin tareas en {ledger.path}")
    else:
        print(summarize(records).text())
    feedback = FeedbackStore(home() / "feedback.json")
    rows = feedback.rows()
    if rows:
        print("\nfeedback (acierto a la primera / total por tipo y celda)")
        print(table(["tipo", "celda", "n 1er", "acierto 1er", "n total", "acierto total"], rows))
    hook_rows = _hook_decisions(home() / "hook.jsonl")
    if hook_rows:
        print("\ndecisiones del hook de Claude Code (subagentes enrutados)")
        print(table(["celda", "tipo", "riesgo", "veces"], hook_rows))
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
        return [(False, f"{settings} no es JSON válido: Claude Code ignora todo el fichero")]
    checks: list[tuple[bool, str]] = []
    if (data.get("enabledPlugins") or {}).get(PLUGIN_ID):
        checks.append((True, f"plugin {PLUGIN_ID} activo en {settings}: hooks y skill vienen del plugin"))
        venv_hook = profile / "plugins" / "data" / PLUGIN_ID.replace("@", "-") / "venv" / "bin" / "router-hook"
        ready = venv_hook.exists()
        note = "" if ready else " (se crea al abrir una sesión; necesita uv)"
        checks.append((ready, f"entorno del plugin: {venv_hook}{note}"))
    else:
        hooks = data.get("hooks", {})

        def wired(event: str, matcher: str | None = None) -> bool:
            for entry in hooks.get(event, []):
                if matcher and entry.get("matcher") != matcher:
                    continue
                for h in entry.get("hooks", []):
                    if "router-hook" in str(h.get("command", "")):
                        return Path(h["command"]).exists()
            return False

        fix = " (falta: el plugin o router install)"
        pre, start = wired("PreToolUse", "Agent"), wired("SessionStart")
        checks.append((pre, f"hook PreToolUse(Agent) en {settings}" + ("" if pre else fix)))
        checks.append((start, f"hook SessionStart en {settings}" + ("" if start else fix)))
        link = profile / "skills" / "router"
        checks.append((link.is_dir(), f"skill router: {link}" + ("" if link.is_dir() else fix)))
    model = str(data.get("model", "por defecto"))
    model_id = SHORT_TO_ID.get(model, model)  # settings may name the model by its short name
    effort = (data.get("modelSettings") or {}).get(model_id, {}).get("effortLevel")
    checks.append((True, f"chat principal: model {model}" + (f", effort {effort}" if effort else "")))
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
        checks.append((True, f"claude en PATH: {claude} ({version.strip()})"))
    else:
        checks.append((False, "claude no está en PATH: instala Claude Code o usa --backend api con ANTHROPIC_API_KEY"))
    profile: Path | None = None
    try:
        profile, source = resolve_profile()
        exists = profile.is_dir()
        checks.append((exists, f"perfil de Claude: {profile} (via {source})" + ("" if exists else " NO EXISTE")))
    except ClientUnavailableError as exc:
        checks.append((False, str(exc)))
    from .install import hook_binary

    hook_bin = hook_binary()
    missing = "no encontrado (instala el paquete: uv tool install ... o uv sync)"
    checks.append((hook_bin is not None, f"binario del hook: {hook_bin or missing}"))
    try:
        avail = available_models()
        source = MODELS_ENV if os.environ.get(MODELS_ENV) else ("config.json" if len(avail) < len(MODELS) else "catálogo")
        checks.append((True, f"modelos disponibles: {', '.join(MODELS[m].short for m in avail)} (via {source})"))
    except UnknownModelError as exc:
        checks.append((False, f"modelos disponibles: {exc.name!r} no existe (revisa {MODELS_ENV} o `router install --models`)"))
    except ValueError:
        checks.append((False, f"modelos disponibles: lista vacía (revisa {MODELS_ENV} o `router install --models`)"))
    if profile is not None and profile.is_dir():
        checks += _profile_checks(profile)
    ledger_n = len(Ledger(home() / "ledger.jsonl").tasks())
    hook_n = sum(n for *_, n in _hook_decisions(home() / "hook.jsonl"))
    checks.append((True, f"estado en {home()}: {ledger_n} tareas en el registro, {hook_n} decisiones del hook"))
    if args.live and claude and profile is not None:
        try:
            c = CliClient(config_dir=profile).complete(parse_cell("haiku/low"), prompt="Responde solo OK.",
                                                      system="Responde solo lo pedido.")
            summary = f"{c.model} respondió {c.text.strip()[:20]!r} en {c.seconds:.1f}s, {c.usage.total} tokens"
            checks.append((True, f"llamada real: {summary}"))
        except ClientUnavailableError as exc:
            checks.append((False, f"llamada real: {exc}"))
    for ok_, text in checks:
        print(("ok     " if ok_ else "FALLO  ") + text)
    return 0 if all(ok_ for ok_, _ in checks) else 1


def _cmd_feedback(args: argparse.Namespace) -> int:
    ledger = Ledger(home() / "ledger.jsonl")
    record = ledger.find(args.task_id)
    if record is None:
        raise SystemExit(f"no hay tarea {args.task_id} en {ledger.path}")
    passed = bool(args.ok)
    ledger.override(args.task_id, passed, note=args.note or "")
    if record.get("final_cell"):
        feedback = FeedbackStore(home() / "feedback.json")
        feedback.apply_override(record["kind"], record["final_cell"], passed, first=len(record["attempts"]) == 1)
        feedback.save()
    print(f"{args.task_id}: marcada como {'OK' if passed else 'KO'}")
    return 0


def _cmd_cells(_args: argparse.Namespace) -> int:
    rows = [[c.label, c.tier, f"{c.capability:.2f}", money(c.expected_cost()), c.spec.input, c.spec.output] for c in lattice()]
    print(table(["celda", "tier", "capacidad*", "coste ref.", "$/M in", "$/M out"], rows))
    print(f"\n* capacidad supuesta (catalog.py), no medida · coste ref. = 2.000 tokens in, 1.500 out a effort high"
          f" · precios {PRICES_AS_OF}")
    off = [MODELS[m].short for m in MODELS if m not in available_models()]
    if off:
        print(f"fuera de esta instalación: {', '.join(off)} ({MODELS_ENV} o `router install --models`)")
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

    if args.models is None and args.profile_dir is None:
        cfg = read_config()
        print(f"perfil para `router run`: {cfg.get('claude_config_dir') or 'sin fijar'}")
        print(f"modelos disponibles: {', '.join(cfg.get('models') or [m.short for m in MODELS.values()])}")
        print(f"(en {config_path()})")
        return 0
    names = model_names(args.models) if args.models is not None else None
    profile = args.profile_dir.expanduser().resolve() if args.profile_dir is not None else None
    remember(profile, names, reset_models=args.models is not None and names is None)
    return 0


def _add_profile_args(p: argparse.ArgumentParser) -> None:
    from .install import default_settings

    p.add_argument("--settings", type=Path, default=default_settings(),
                   help="settings.json a tocar (por defecto el del perfil: $CLAUDE_CONFIG_DIR o ~/.claude)")
    p.add_argument("--skills-dir", type=Path, help="carpeta de skills (por defecto, junto al settings)")
    p.add_argument("--no-skill", action="store_true", help="solo hooks, sin la skill")


def build_parser() -> argparse.ArgumentParser:
    """Return the `router` argument parser; each subcommand sets `func` to its handler."""
    p = argparse.ArgumentParser(
        prog="router",
        description="Router de modelos Claude: elige modelo + effort, verifica por riesgo, escala si falla y lleva la cuenta.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan", help="qué haría el router con esta tarea (sin llamar a la API)")
    _add_task_args(sp)
    sp.add_argument("--claude-code", action="store_true", help="imprime solo model/effort para un agente de Claude Code")
    sp.set_defaults(func=_cmd_plan)

    sr = sub.add_parser("run", help="ejecuta la tarea con el router (llamadas reales)")
    _add_task_args(sr)
    _add_backend_args(sr)
    sr.add_argument("--json", action="store_true", help="salida JSON con la respuesta y los intentos")
    sr.set_defaults(func=_cmd_run)

    sb = sub.add_parser("bench", help="compara estrategias: simulación offline o fichero de tareas reales")
    sb.add_argument("file", nargs="?", help="YAML/JSON con tareas (sin fichero: simulación)")
    sb.add_argument("--simulate", action="store_true", help="fuerza la simulación")
    sb.add_argument("--tasks", type=int, default=60, help="tareas simuladas")
    sb.add_argument("--seed", type=int, default=1, help="semilla de la simulación (1 por defecto)")
    sb.add_argument("--detail", action="store_true", help="simulación: desglose por celda y tipo de cada estrategia")
    sb.add_argument("--strategies", default="router",
                    help='p. ej. "router,fable/xhigh,opus/high,sonnet/medium>opus/high"')
    sb.add_argument("--yes", "-y", action="store_true", help="no pedir confirmación antes de gastar")
    sb.add_argument("--jobs", "-j", type=int, default=3, help="tareas en paralelo (3 por defecto)")
    _add_backend_args(sb)
    sb.set_defaults(func=_cmd_bench)

    ss = sub.add_parser("stats", help="resumen del registro y del feedback")
    ss.add_argument("--ledger", help="otro fichero de registro")
    ss.set_defaults(func=_cmd_stats)

    sf = sub.add_parser("feedback", help="corrige a mano el veredicto de una tarea")
    sf.add_argument("task_id", help="id de la tarea, el que imprime `router run` y lista `router stats`")
    g = sf.add_mutually_exclusive_group(required=True)
    g.add_argument("--ok", action="store_true", help="la respuesta era buena")
    g.add_argument("--ko", action="store_true", help="la respuesta era mala")
    sf.add_argument("--note", help="por qué, para el registro")
    sf.set_defaults(func=_cmd_feedback)

    sd = sub.add_parser("doctor", help="comprueba claude, perfil, hooks, skill y estado; --live hace una llamada con Haiku")
    sd.add_argument("--live", action="store_true", help="además, una llamada real con Haiku para probar la cuenta")
    sd.set_defaults(func=_cmd_doctor)

    sc = sub.add_parser("cells", help="la retícula de celdas con su coste y capacidad supuesta")
    sc.set_defaults(func=_cmd_cells)

    si = sub.add_parser("install", help="activa el router en un perfil de Claude Code (hooks + skill)")
    _add_profile_args(si)
    si.add_argument("--link", action="store_true", help="enlaza la skill en vez de copiarla (desarrollo)")
    si.add_argument("--profile-dir", type=Path,
                    help="perfil de Claude que usará `router run` (por defecto, el del settings si es un perfil)")
    si.add_argument("--models", help="modelos que puede elegir esta instalación, p. ej. haiku,sonnet,opus; all = todos")
    si.set_defaults(func=_cmd_install)

    su = sub.add_parser("uninstall", help="quita los hooks y la skill del perfil")
    _add_profile_args(su)
    su.set_defaults(func=_cmd_uninstall)

    sg = sub.add_parser("config", help="muestra o fija el perfil de `router run` y los modelos disponibles (sin tocar hooks)")
    sg.add_argument("--profile-dir", type=Path, help="perfil de Claude que usará `router run`")
    sg.add_argument("--models", help="modelos que puede elegir esta instalación, p. ej. haiku,sonnet,opus; all = todos")
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
        raise SystemExit(f"modelo desconocido {exc.name!r}; válidos: {', '.join(exc.known)} "
                         f"(revisa {MODELS_ENV}, config.json o --models)") from exc
    except ValueError as exc:
        raise SystemExit(f"valor no válido: {exc}") from exc


if __name__ == "__main__":
    sys.exit(main())
