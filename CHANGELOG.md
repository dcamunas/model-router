# Cambios

Formato de [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/); versiones según [SemVer](https://semver.org/lang/es/).

## [0.1.1] - 2026-10-19

### Corregido

- `router doctor` resolvía el id del modelo del chat como `claude-X-5-5`, que no vale para Fable, y no mostraba su effort.
- Un modelo desconocido en `--models` o en `MODEL_ROUTER_MODELS` terminaba en un traceback en inglés; ahora es un mensaje de una línea con los nombres válidos.
- `router install` escribía en `settings.json` antes de validar los modelos; ahora comprueba modelos, binario del hook y skill antes de tocar nada.
- Una línea corrupta en `hook.jsonl` o `ledger.jsonl` tiraba `stats` y `doctor`; ahora se salta y se avisa.
- `router uninstall --help` anunciaba flags que no usaba.

### Cambiado

- Variables de entorno bajo un solo prefijo: `ROUTER_HOOK`, `ROUTER_HOOK_LOG`, `ROUTER_HOOK_QUIET` y `ROUTER_CLAUDE_CONFIG_DIR` pasan a `MODEL_ROUTER_HOOK`, `MODEL_ROUTER_HOOK_LOG`, `MODEL_ROUTER_HOOK_QUIET` y `MODEL_ROUTER_CLAUDE_CONFIG_DIR`. Sin compatibilidad con los nombres antiguos.
- Todo lo que ve el usuario va en español con tildes, incluidos los motivos de cada intento y los errores de los dos backends.
- `ClientUnavailable` pasa a llamarse `ClientUnavailableError`; el nombre antiguo sigue como alias en esta versión.
- Librería: los parámetros opcionales son keyword-only en `Policy`, `Router`, `LadderPolicy`, `Verifier`, `step_up`, `step_down`, `ladder`, `run_simulation`, `format_report` y `Ledger.override`. `Task.estimated_input_tokens` y `Task.planned_output_tokens` sustituyen a los cálculos repetidos.
- `install.install` ya no acepta `uninstall=True`; existe `install.uninstall`.

### Añadido

- Docstrings en toda la API pública, exigidas por ruff en módulos, clases y funciones públicas.
- Tests de `router run` y `router feedback` de principio a fin.
- Este fichero.

## [0.1.0] - 2026-10-19

Primera versión pública: retícula de celdas (modelo, effort), verificación por riesgo, escalera, registro, feedback, simulación, backend `claude -p` sin API key, hook y skill para Claude Code, `router install`.
