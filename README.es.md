# model-router

English documentation: [README.md](README.md).

Router de modelos Claude: dada una tarea, elige modelo y effort, la ejecuta, la verifica según el
riesgo, escala si falla y apunta en un registro lo que costó cada tarea resuelta. Se usa desde la
línea de comandos, como librería Python y dentro de Claude Code, donde un hook decide el modelo y el
esfuerzo de cada subagente que se lanza sin elegirlo.

Estado: experimental. Las capacidades por celda son supuestos declarados, no medidas; el registro y el
feedback existen para sustituirlos con veredictos reales (ver «Supuestos»).

La idea viene del reel de @donimas («router picks model + effort · verify by risk»). Aquí cambian
cuatro cosas, y cada una tiene su porqué en la tabla.

## En qué se diferencia del reel

| Reel | Aquí | Por qué |
|---|---|---|
| Escalera de modelos: Sonnet ×1, luego Opus ×2, luego Fable ×5 | Retícula de celdas (modelo, effort) ordenada por coste, con una capacidad estimada por celda | La guía de coste de Anthropic dice que Fable a `low` suele salir más barato por tarea resuelta que Opus a `high`. Una escalera por modelo nunca prueba esa celda; la retícula sí. |
| Verificar «by risk» | Verificar por riesgo con un orden de confianza: primero el checker programático (gratis, manda si falla), luego un juez `light` (la celda más barata al menos tan capaz como el trabajador), al final un juez `full` (otro modelo, claramente más capaz, contexto limpio) | Un checker de código vale más que cualquier juez. Y el juez `full` es un cruce ciego entre modelos. |
| «Feedback» | Tasa de acierto a la primera por (tipo, celda), separada de los intentos escalados; mueve la celda de inicio cuando hay ≥ 10 veredictos; override humano con `router feedback` | Los intentos escalados solo ven las tareas que la celda barata ya falló: mezclarlos infla el fallo de la celda cara. |
| «Simulated workload · real prices» | Igual, pero la simulación declara sus supuestos, incluye la cola difícil, los falsos OK del juez y la celda honesta que falta en el reel: Fable a low | Un router que nunca se compara con «el mejor modelo barato» no demuestra nada. |

Lo que no hace: no decide nada con datos reales hasta que tengas veredictos reales.

## Instalación

Requisitos: Python 3.12 o superior, [uv](https://docs.astral.sh/uv/), y Claude Code si quieres el
hook o el backend sin API key.

### Como plugin de Claude Code (hook + skill)

Dentro de Claude Code, una línea (2.1.275 o posterior):

```
/plugin install model-router --marketplace dcamunas/model-router
```

O desde la terminal:

```bash
claude plugin marketplace add dcamunas/model-router
claude plugin install model-router@dcamunas
```

El repo es a la vez el marketplace (`dcamunas`) y el plugin (`model-router`). La primera sesión tarda
unos segundos más: el hook crea su propio entorno Python con `uv` en la carpeta de datos del plugin
(`plugins/data/model-router-dcamunas/` dentro del perfil) y lo reutiliza después; con cada versión
nueva del plugin lo rehace solo. Hace falta `uv` en la máquina (`MODEL_ROUTER_UV=/ruta/a/uv` si no
está en el PATH). En `settings.json` solo queda la activación del plugin.

Si tu plan o tu cuenta no tiene todos los modelos (sin Fable, por ejemplo), o quieres `router run`,
instala también el CLI y díselo una vez:

```bash
uv tool install git+https://github.com/dcamunas/model-router@v0.2.0
router config --models haiku,sonnet,opus    # sin tocar hooks ni skill
router doctor                               # claude, perfil, plugin o hooks, modelos disponibles y estado
```

### Con el CLI, cuando no se pueden usar plugins

Perfiles donde una política bloquea los marketplaces, o un repo concreto: `router install` escribe
los mismos hooks y la misma skill en el `settings.json` del perfil.

```bash
uv tool install git+https://github.com/dcamunas/model-router@v0.2.0
router install                              # hooks + skill en el perfil activo ($CLAUDE_CONFIG_DIR o ~/.claude)
router install --models haiku,sonnet,opus   # sin Fable
router doctor
```

Las tablas no cambian: cuando piden un modelo que no está, el router baja al más capaz disponible
con el mismo effort y la escalera sigue desde ahí (`router plan` lo dice en su explicación). La
variable `MODEL_ROUTER_MODELS=haiku,sonnet,opus` hace lo mismo sin tocar la configuración.

Otro perfil, un repo concreto, o quitarlo:

```bash
router install --settings ~/.claude-otro/settings.json --profile-dir ~/.claude-otro
router install --settings .claude/settings.json --no-skill
router uninstall
```

Actualizar: `uv tool upgrade model-router` (o instala otro tag) y `router install` otra vez. Lo que
cambia en cada versión está en `CHANGELOG.es.md`.

Para desarrollar sobre el repo:

```bash
git clone https://github.com/dcamunas/model-router && cd model-router
uv sync
uv run router install --link    # la skill queda enlazada al repo en vez de copiada
uv run pytest -q
claude --plugin-dir .           # o probar el plugin tal cual está en el repo, solo en esa sesión
```

Para llamadas reales desde la línea de comandos hace falta una credencial: `ANTHROPIC_API_KEY` en el
entorno o, sin ella, tu cuenta de Claude Code (ver «Sin API key»). `plan`, `bench --simulate`,
`cells`, `stats` y `doctor` (sin `--live`) no llaman a la API.

## Uso

```bash
# Qué haría el router con una tarea, sin gastar nada
router plan "Revisa este script SQL de migración antes de pasarlo a producción"
#   clasificación: review / high  (regla 'revisa'; riesgo: palabra de riesgo alto)
#   inicio: opus/medium  -> Claude Code {'model': 'opus', 'effort': 'medium'}
#     - tabla review x high -> opus/medium
#     - escalera: opus/medium -> fable/low -> fable/xhigh
#     - verificación light (riesgo high); presupuesto $2.00; máx. 4 intentos
#   verificador (light): opus/medium

# Solo model/effort, para el frontmatter de un agente de Claude Code
router plan "clasifica estos tickets" --claude-code
#   model: haiku
#   effort: low

# Ejecutar de verdad, con un checker programático
router run "Devuelve un JSON con las claves nombre, nif y fecha a partir de: ..." \
    --kind extract --risk high --check-json nombre,nif,fecha

# Comparar estrategias (offline, sin API)
router bench --simulate --tasks 200 --seed 1

# Comparar estrategias con tareas reales (gasta dinero o cuota; pide confirmación)
router bench tareas.yaml --strategies "router,fable/low,opus/high,sonnet/medium>opus/high"

# Lo que ha pasado hasta ahora y lo que ha aprendido
router stats

# Corregir a mano un veredicto (alimenta el feedback)
router feedback <task_id> --ko --note "el juez dio por buena una fecha mal parseada"
```

Formato de `tareas.yaml` para el bench real (ejemplo completo en `tareas.example.yaml`):

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
por `--json-schema`. Consume la cuota de tu suscripción, no dólares: los importes del registro son el
equivalente a precio de lista, un proxy del consumo de cuota.

El perfil de Claude que usa, por orden: `MODEL_ROUTER_CLAUDE_CONFIG_DIR`, `claude_config_dir` en
`~/.model-router/config.json` (lo escriben `router install` o `router config`), `CLAUDE_CONFIG_DIR`. Sin ninguno se
niega a arrancar, para no usar otro perfil por accidente. `router doctor` dice cuál está usando.

Diferencias con el backend `api`: no hay `max_tokens` ni fallbacks de refusal en el servidor, y
cada llamada tarda uno o dos segundos más por el arranque del proceso. `--backend api|cli` fuerza
uno u otro.

## Dentro de Claude Code

Dos piezas, una sola fuente de verdad (el paquete `model_router`), que llegan igual por el plugin
que por `router install`:

- Hook `router-hook`: `PreToolUse` sobre la herramienta Agent fija `model` y `effort` de cada
  subagente general-purpose que Claude lance sin elegir modelo, según el tipo y el riesgo que infiere
  del prompt. `SessionStart` le cuenta a Claude que el hook existe para que no luche contra él. No
  llama a la API, tarda unas décimas de segundo y nunca bloquea: ante cualquier error sale en silencio.
- Skill `router`: lo que un hook no puede hacer. Verificar con un segundo subagente, escalar, y
  mandar las subtareas de solo texto (clasificar, extraer, resumir, redactar) por `router run` en vez
  de por un subagente, que arrastra el contexto completo de la sesión.

El modelo de la sesión principal no lo cambia nadie: lo fija `settings.json` o `/model`. Lo que sí
hace el hook por el orquestador: una revisión cuyo riesgo pide verificación `full` (hoy solo
`critical`) nunca corre en el modelo que sirve el chat; el hook lee ese modelo del transcript de la
sesión y salta a la celda de verificador en otro modelo (chat en Opus, revisión crítica en
fable/low). Es la única verificación que recibe el propio orquestador. Si el transcript no se puede
leer, o no hay otro modelo disponible, la regla no hace nada.

Lo que no hace: no toca los agentes definidos en `.claude/agents/*.md` (su frontmatter manda) ni
los forks, y no verifica ni escala por sí solo.

Variables de entorno: `MODEL_ROUTER_HOOK=0` lo apaga sin desinstalar; `MODEL_ROUTER_HOOK_QUIET=1` quita el aviso
de una línea en la interfaz; `MODEL_ROUTER_HOOK_LOG=0` deja de registrar decisiones; `MODEL_ROUTER_UV`
dice al plugin dónde está `uv`. Las decisiones quedan en `~/.model-router/hook.jsonl` y las resume
`router stats`.

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

1. Tipo y riesgo. Los pasas o se infieren por reglas (es/en); `--classify-llm` usa Haiku.
2. Celda de inicio. Tabla `START` en `policy.py` (tipo × riesgo). Con ≥ 10 veredictos de ese
   tipo, el feedback la sube si su tasa de acierto *a la primera* queda por debajo del punto de
   equilibrio `c1 / c2` (coste del intento en esa celda, verificación incluida, entre el coste del
   siguiente escalón): por debajo de ahí, empezar barato sale más caro. Los suelos de riesgo
   (`high` ≥ sonnet/low, `critical` ≥ opus/medium) no se pueden bajar.
3. Escalera. Cada escalón es *la celda más barata que sea claramente más capaz* (+0,08 de
   capacidad) entre los modelos disponibles. Con los supuestos actuales:
   `sonnet/medium -> opus/low -> fable/low -> fable/xhigh`.
4. Verificación. `low` y `medium` ninguna, `high` light, `critical` full (la verificación
   completa era el mayor sumidero de coste; sube a `full` en `high` con `--verify full` o en
   `policy.py` cuando tu bench lo justifique). Si la tarea trae checker, el checker va primero y
   manda si falla.
5. Parada. Veredicto OK, escalera agotada, presupuesto por riesgo (0,10 / 0,50 / 2 / 10 USD),
   dos refusals en la misma tarea, o la API caída. Una respuesta cortada por `max_tokens` se repite una vez
   en la misma celda con el doble de margen.
6. Registro. `~/.model-router/ledger.jsonl` (cambia con `MODEL_ROUTER_HOME`): cada intento con
   sus tokens reales, coste, modelo que sirvió (si saltó un fallback), veredicto y motivo.

Detalles de API que están puestos a propósito: `output_config.effort` lleva el effort; no se envía
`thinking` (adaptativo por defecto, y `disabled` da 400 en la familia 5.5); los modelos que lo
admiten llevan `fallbacks="default"` para que un refusal del clasificador se reintente en el modelo
que Anthropic recomiende; la salida estructurada va por `parse`; por encima de 16K tokens se hace
streaming.

## Qué sale de tu máquina

- El hook no llama a ninguna API: clasifica por reglas en local. Guarda cada decisión en
  `~/.model-router/hook.jsonl` con los primeros 60 caracteres del prompt del subagente.
- `router run` y `router bench` mandan el prompt a Anthropic, por tu API key o por tu cuenta de
  Claude Code. A nadie más.
- El registro guarda tokens, coste, celda, veredicto y motivo de cada intento; no guarda el prompt ni
  la respuesta.
- `router plan`, `router stats`, `router cells` y `router doctor` sin `--live` no llaman a nada.

## Supuestos (léelos antes de creerte un número)

- Precios de `catalog.py`, API de Anthropic a 2026-10-06. El registro usa el `usage` real.
- `EFFORT_OUTPUT_MULT`: tokens de salida por effort respecto a `high` (0,5 / 0,75 / 1 / 1,6 / 2,5).
- `CAPABILITY_TIER` + `CAPABILITY_EFFORT`: capacidad 0-1 por celda. Haiku 0,40, Sonnet 0,60,
  Opus 0,75, Fable 0,90; el effort suma entre −0,06 y +0,05. Es una opinión, no una medida.
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
uv run ruff check src tests   # estilo, tipos en las firmas, docstrings en la API pública, complejidad
uv run pyright                # tipos
```

La CI de GitHub Actions hace lo mismo en cada push, comprueba que la skill viaja dentro del wheel y
valida el plugin con la CLI de Claude Code. Código, comentarios y docstrings en inglés; documentación
en español y en inglés; todo lo que ve el usuario en la línea de comandos, en español.

## Licencia

MIT. Ver `LICENSE`.
