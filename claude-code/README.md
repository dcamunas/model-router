# El router dentro de Claude Code

Dos piezas, una sola fuente de verdad (el paquete `model_router`):

- **Hook** `router-hook` (entry point del venv): `PreToolUse` sobre Agent fija `model` y `effort` de
  los subagentes general-purpose que Claude lance sin elegir modelo; `SessionStart` le dice a Claude
  que el hook existe. No llama a la API, tarda ~50 ms, nunca bloquea.
- **Skill** `router`: el ciclo completo para subtareas con riesgo (plan, verificación con un segundo
  subagente, escalado). Enlazada con symlink, así que se edita en el repo.

```bash
uv sync                                   # crea .venv/bin/router-hook
python3 claude-code/install.py            # perfil activo ($CLAUDE_CONFIG_DIR o ~/.claude)
python3 claude-code/install.py --settings ~/.claude/settings.json          # otro perfil
python3 claude-code/install.py --settings .claude/settings.json --no-skill  # un repo concreto
python3 claude-code/install.py --uninstall
```

El instalador también apunta en `~/.model-router/config.json` el perfil instalado, que es el que usará
`router run` con el backend `cli` (sin API key). `router doctor` comprueba las dos piezas.

Apagar sin desinstalar: `ROUTER_HOOK=0` en el entorno. Sin aviso en pantalla: `ROUTER_HOOK_QUIET=1`.
Sin registro: `ROUTER_HOOK_LOG=0`.

Cruce con el chat: una revisión cuyo riesgo pide verificación `full` (hoy solo `critical`, tabla
`VERIFY_BY_RISK`) nunca corre en el modelo que sirve la sesión principal; el hook lee ese modelo de
la última entrada del transcript de la sesión y salta a la celda de verificador en otro modelo
(chat en Opus → revisión crítica en fable/low). Es la única verificación que recibe el propio
orquestador. Si el transcript no se puede leer, la regla no hace nada.

Lo que no hace: no cambia el modelo de la sesión principal (ningún hook puede), no toca agentes con
`model:` en su frontmatter, no verifica ni escala por sí solo.
