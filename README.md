# model-router

Router de modelos Claude para mis proyectos: dada una tarea, elige **modelo + effort**, la ejecuta,
la **verifica según el riesgo**, **escala** si falla y apunta en un registro lo que costó cada tarea
**resuelta**. La idea viene del reel de @donimas («router picks model + effort · verify by risk»);
aquí está hecha con tres cambios que importan.

## En qué se diferencia del reel

| Reel | Aquí | Por qué |
|---|---|---|
| Escalera de modelos: Sonnet ×1 → Opus ×2 → Fable ×5 | **Retícula de celdas (modelo, effort)** ordenada por coste, con una capacidad estimada por celda | La guía de coste de Anthropic dice que Fable a `low` suele salir más barato por tarea resuelta que Opus a `high`. Una escalera por modelo nunca prueba esa celda; la retícula sí. |
| Verificar «by risk» | Verificar por riesgo con **orden de confianza**: checker programático (gratis, manda si falla) → juez `light` (celda barata ≥ trabajador) → juez `full` (otro modelo, claramente más capaz, contexto limpio) | Un checker de código vale más que cualquier juez. Y el juez `full` es un cruce ciego entre modelos, como la mesa Claude + Codex. |
| «Feedback» | Tasa de acierto **a la primera** por (tipo, celda), separada de los intentos escalados; mueve la celda de inicio cuando hay ≥ 10 veredictos; override humano con `router feedback` | Los intentos escalados solo ven las tareas que la celda barata ya falló: mezclarlos infla el fallo de la celda cara. |
| «Simulated workload · real prices» | Igual, pero la simulación declara sus supuestos, incluye la cola difícil, los falsos OK del juez y **la celda honesta que falta en el reel: Fable a low** | Un router que nunca se compara con «el mejor modelo barato» no demuestra nada. |

Lo que NO hace: no decide nada con datos reales hasta que tengas veredictos reales. Las capacidades
por celda de `catalog.py` son supuestos; el registro y el feedback existen para sustituirlos.

## Instalación

```bash
cd ~/code/personal/model-router
uv sync
uv run router --help
```

Para llamadas reales hace falta credencial: `export ANTHROPIC_API_KEY=...` o un perfil de
`ant auth login`. `plan`, `bench --simulate`, `cells` y `stats` no llaman a la API.

## Puesta en marcha

```bash
uv sync                                      # .venv con `router` y `router-hook`
ln -sf "$PWD/.venv/bin/router" ~/.local/bin/router   # `router` en el PATH
python3 claude-code/install.py               # hooks + skill en el perfil activo; recuerda el perfil para `router run`
router doctor --live                         # claude, perfil, hooks, skill, estado y una llamada real con Haiku
```

## Uso

```bash
# Qué haría el router con una tarea, sin gastar nada
uv run router plan "Revisa este script SQL de migración antes de pasarlo a producción"
#   clasificacion: review / high  (rule 'revisa'; risk: high-risk keyword)
#   inicio: opus/medium  -> Claude Code {'model': 'opus', 'effort': 'medium'}
#     - tabla review x high -> opus/medium
#     - escalera: opus/medium -> fable/low -> fable/xhigh
#     - verificacion light (riesgo high); presupuesto $2.00; max 4 intentos
#   verificador (light): opus/medium

# Solo model/effort, para el frontmatter de un agente de Claude Code
uv run router plan "clasifica estos tickets" --claude-code
#   model: haiku
#   effort: low

# Varias tareas reales en paralelo (3 a la vez por defecto, --jobs lo cambia)
uv run router bench tareas.example.yaml --yes --strategies router

# Ejecutar de verdad, con un checker programático
uv run router run "Devuelve un JSON con las claves nombre, nif y fecha a partir de: ..." \
    --kind extract --risk high --check-json nombre,nif,fecha

# Comparar estrategias (offline, sin API)
uv run router bench --simulate --tasks 200 --seed 1

# Comparar estrategias con tareas reales (gasta dinero; pide confirmación)
uv run router bench tareas.yaml --strategies "router,fable/low,opus/high,sonnet/medium>opus/high"

# Lo que ha pasado hasta ahora y lo que ha aprendido
uv run router stats

# Corregir a mano un veredicto (alimenta el feedback)
uv run router feedback <task_id> --ko --note "el juez dio por buena una fecha mal parseada"
```

Formato de `tareas.yaml` para el bench real:

```yaml
tasks:
  - id: nif-01
    prompt: "Extrae nombre y NIF del texto siguiente en JSON: ..."
    kind: extract          # opcional, se infiere por reglas
    risk: high             # opcional
    check_json: [nombre, nif]
  - id: sql-01
    prompt: "Escribe la consulta BigQuery que ..."
    check_regex: "SELECT[\\s\\S]*FROM"
```

## Sin API key: con tu cuenta de Claude

`router run` y `router bench` no necesitan `ANTHROPIC_API_KEY`. Sin ella usan el backend `cli`: cada
llamada es un proceso `claude -p` (modo headless de Claude Code) con el modelo y el esfuerzo de la
celda, sin herramientas, sin MCP y con un system prompt mínimo, unos 900 tokens por llamada (sin
`--system-prompt` Claude Code enviaría su prompt completo, unos 100K). Las salidas estructuradas van
por `--json-schema`. Consume la cuota de tu suscripción, no dólares: los importes del libro son el
equivalente a precio de lista, un proxy del consumo de cuota.

El perfil de Claude que usa, por orden: `ROUTER_CLAUDE_CONFIG_DIR`, `claude_config_dir` en
`~/.model-router/config.json` (lo escribe `claude-code/install.py`), `CLAUDE_CONFIG_DIR`. Sin
ninguno se niega a arrancar, para no usar otro perfil por accidente. `router doctor` dice cuál
está usando.

Diferencias con el backend `api`: no hay `max_tokens` ni fallbacks de refusal en el servidor, y
cada llamada tarda uno o dos segundos más por el arranque del proceso. `--backend api|cli` fuerza
uno u otro.

## Dentro de Claude Code

Instalado en un perfil, el router arranca solo para los subagentes: un hook `PreToolUse` sobre la
herramienta Agent fija `model` y `effort` de cada subagente general-purpose que Claude lance sin
elegir modelo, según el tipo y el riesgo que infiere del prompt. Un hook `SessionStart` se lo cuenta
a Claude al empezar, y la skill `router` cubre lo que un hook no puede: verificar con un segundo
subagente y escalar. El modelo de la sesión principal no lo cambia nadie: lo fija `settings.json`
o `/model`. Lo que sí hace el hook por el orquestador: una revisión de riesgo crítico nunca corre
en el modelo del chat (cruce ciego, como la mesa).

```bash
uv sync
python3 claude-code/install.py            # perfil activo; --settings para otro; --uninstall para quitarlo
```

Detalles, variables de entorno y límites en `claude-code/README.md`. Las decisiones quedan en
`~/.model-router/hook.jsonl` y las resume `router stats`.

## Como librería

```python
from model_router import Router, Task, json_checker
from model_router.client import LiveClient

router = Router(LiveClient())
result = router.run(Task(
    prompt="Extrae nombre, NIF y fecha del texto en JSON: ...",
    kind="extract", risk="high", checker=json_checker(("nombre", "nif", "fecha")),
))
print(result.status, result.final_cell, f"${result.total_cost_usd:.4f}")
print(result.answer)
```

`Task` admite `output_schema` (un modelo pydantic para salida estructurada), `system`, `budget_usd`,
`max_attempts` y `verify` para forzar el modo. El `Router` acepta cualquier objeto con
`complete(cell, prompt=..., ...)`, así que se puede probar sin red (`model_router.testing.FakeClient`).

## Cómo decide

1. **Tipo y riesgo.** Los pasas o se infieren por reglas (es/en); `--classify-llm` usa Haiku.
2. **Celda de inicio.** Tabla `START` en `policy.py` (tipo × riesgo). Con ≥ 10 veredictos de ese
   tipo, el feedback la sube si su tasa de acierto *a la primera* queda por debajo del punto de
   equilibrio `c1 / c2` (coste del intento en esa celda, verificación incluida, entre el coste del
   siguiente escalón): por debajo de ahí, empezar barato sale más caro. Los suelos de riesgo
   (`high` ≥ sonnet/low, `critical` ≥ opus/medium) no se pueden bajar.
3. **Escalera.** Cada escalón es *la celda más barata que sea claramente más capaz* (+0,08 de
   capacidad). Con los supuestos actuales: `sonnet/medium → opus/low → fable/low → fable/xhigh`.
4. **Verificación.** `low` y `medium` ninguna, `high` light, `critical` full (decisión del
   18/10/2026: la verificación completa era el mayor sumidero de coste; sube a `full` en `high`
   con `--verify full` o en `policy.py` cuando tu bench lo justifique). Si la tarea trae checker,
   el checker va primero y manda si falla.
5. **Parada.** Veredicto OK, escalera agotada, presupuesto por riesgo (0,10 / 0,50 / 2 / 10 USD),
   dos refusals seguidos, o la API caída. Una respuesta cortada por `max_tokens` se repite una vez
   en la misma celda con el doble de margen.
6. **Registro.** `~/.model-router/ledger.jsonl` (cambia con `MODEL_ROUTER_HOME`): cada intento con
   sus tokens reales, coste, modelo que sirvió (si saltó un fallback), veredicto y motivo.

Detalles de API que están puestos a propósito: `output_config.effort` lleva el effort; no se envía
`thinking` (adaptativo por defecto, y `disabled` da 400 en la familia 5.5); los modelos que lo
admiten llevan `fallbacks="default"` para que un refusal del clasificador se reintente en el modelo
que Anthropic recomiende; la salida estructurada va por `parse`; por encima de 16K tokens se hace
streaming.

## Supuestos (léelos antes de creerte un número)

- Precios de `catalog.py`, API de Anthropic a 2026-10-06. El registro usa el `usage` real.
- `EFFORT_OUTPUT_MULT`: tokens de salida por effort respecto a `high` (0,5 / 0,75 / 1 / 1,6 / 2,5).
- `CAPABILITY_TIER` + `CAPABILITY_EFFORT`: capacidad 0-1 por celda. Haiku 0,40, Sonnet 0,60,
  Opus 0,75, Fable 0,90; el effort suma entre −0,06 y +0,05. **Es una opinión, no una medida.**
- La simulación añade: dificultad por tipo, 15 % de cola difícil, juez `light` con 10 % de falsos
  OK y `full` con 3 %.
- Las cachés de prompt son por modelo: cada salto de modelo en la escalera pierde la caché. Para
  una conversación larga, cambia de celda entre conversaciones, no dentro.

## Lo que falta para que sea un router de verdad

Un conjunto de evaluación propio: 20-30 tareas reales congeladas, con checker o respuesta de
referencia, pasadas por `router bench tareas.yaml` con varias estrategias. Hasta entonces, el
único número fiable que da esto es el coste real por tarea resuelta del registro.

## Desarrollo

```bash
uv run pytest -q
uv run ruff check src tests
```
