---
name: router
description: Verificar y escalar por riesgo el trabajo de subagentes usando el router de modelos (`router` en la línea de comandos). El hook ya elige modelo y effort solo; usa esta skill cuando el usuario pida «usa el router», «verifica esto», «hazlo barato pero verificado», o cuando una subtarea sea de riesgo alto o crítico y haya que verificarla con un segundo subagente.
---

# Router de modelos dentro de Claude Code

Qué ya pasa solo: un hook `PreToolUse` sobre la herramienta Agent fija `model` y `effort` en cada
subagente general-purpose que lances sin `model`, según el tipo y el riesgo que infiere del prompt.
Lo que esta skill añade es el resto del ciclo: verificar según el riesgo y escalar si falla.

## Texto puro: `router run`, no un subagente

Un subagente de Claude Code arrastra el prompt completo de la sesión (herramientas, MCP, skills:
unos 100K tokens). Para una subtarea que no necesita herramientas (clasificar, extraer, resumir,
redactar, revisar un texto que ya tienes) usa el router directamente: unos 900 tokens de contexto,
modelo y esfuerzo por tipo y riesgo, checker opcional, juez según riesgo y entrada en el registro.

```bash
router run "<tarea>" --kind <tipo> --risk <riesgo>            # la respuesta sale por stdout
router run --file brief.md --check-json nombre,fecha,importe   # checker programático
router run "..." --json                                       # respuesta + intentos en JSON
```

Usa la cuenta de Claude del perfil fijado en `~/.model-router/config.json` (lo escribe `router
install`), sin API key. Si `router` no está en el PATH o algo falla, `router doctor` lo dice.

## Cuándo intervenir tú

- No pongas `model` en el Agent salvo que el usuario lo pida o la tarea sea de riesgo alto. Si lo pones,
  el hook lo respeta.
- Los agentes definidos en `.claude/agents/*.md` no pasan por el hook: su frontmatter manda.
- Riesgo alto o crítico (producción, dinero, clientes, borrados, legal): pide el plan y sigue su
  verificación. Esto el hook no lo hace.

## Pasos para una subtarea con riesgo

1. Plan, sin coste:

   ```bash
   router plan "<la subtarea en una frase>" --kind <tipo> --risk <riesgo>
   ```

   Tipos: classify, extract, summarize, search, chat, write, code, review, analysis, agentic. Riesgos:
   low, medium, high, critical. Imprime celda de inicio, escalera, modo de verificación y verificador.

2. Lanza el subagente con `model` y `effort` de la celda de inicio y el brief completo.

3. Verifica como diga el plan:
   - `none`: acepta.
   - `light`: comprueba tú el resultado contra el brief, o con un checker (tests, JSON válido).
   - `full`: lanza **otro** subagente con la celda del verificador, contexto limpio, con el brief y el
     resultado, y pídele `passed / reason / issues`. Nunca el mismo subagente que hizo el trabajo.

4. Si falla, relanza en la siguiente celda de la escalera con el motivo del fallo en el brief. Para
   cuando pase, cuando se acabe la escalera o cuando el coste no compense.

5. En el mensaje final di qué celda resolvió y cuántos intentos hicieron falta.

## Verificar tu propio diseño

Tú, el chat principal, no tienes verificador ni escalada automáticos: ningún hook puede cambiar tu
modelo. Cuando el diseño lo hayas hecho tú y toque producción, dinero, clientes o borrados:

1. Lanza un subagente general-purpose **sin `model`** cuyo prompt empiece por «Revisa» y diga que
   es crítico, con el brief, el diseño completo y la petición de `passed / reason / issues`.
2. El hook lo clasifica como review/critical y lo manda a un modelo distinto del tuyo (chat en
   Opus → fable/low). Verás «cruce con el chat» en el aviso.
3. Si falla, corrige el diseño y repite una vez; si vuelve a fallar, cuéntaselo al usuario antes de
   ejecutar nada.

## Reglas

- Un checker programático manda sobre cualquier juez. Si hay tests, el veredicto es «pasan o no».
- No cambies de celda dentro de una misma conversación de subagente: la caché es por modelo.
- Las decisiones del hook quedan en `~/.model-router/hook.jsonl`; `router stats` las resume.
