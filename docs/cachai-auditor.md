# Cachai auditor: cómo se usa

Audita los endpoints que sirven un mismo modelo open-weight y escribe una ficha por
corrida. La idea y sus límites están en el repo de research
(`docs/README-cachai-auditor.md`). Código en `src/radar/audit/`, tests en `tests/audit/`.

## Qué reutiliza de lo que ya había

| Pieza existente | Para qué se usa acá |
|---|---|
| Jailbreak Radar: clasificación de errores de OpenRouter (`radar.llm.openrouter`) | Distinguir modelo inexistente, clave sin crédito y fallas del proveedor |
| Jailbreak Radar: canario y su juez determinista (`radar.testing`) | Medir si el endpoint filtra el secreto del system prompt, sin juez LLM |
| CachAI de Fede: blog y `POST /api/posts` | Publicar la ficha como reporte (`--publish`) |
| CachAI de Fede: `litellm/config.yaml` ya apunta a 50x | 50x entra como un `--direct` más |

## Qué mide

| Control | Cómo | Nivel |
|---|---|---|
| Cuantización y precio | API pública de OpenRouter | E1, declarado |
| Tokens de prompt de sobra | `prompt_tokens` de mensajes mínimos contra la referencia; un excedente constante es la firma de un prefijo oculto | E2 |
| Exactitud y coincidencia con la referencia | Tareas con respuesta calculable (sumas, productos, ordenar, repetir), temperatura 0, test exacto de Fisher | E2 |
| Tool calls válidas | Una tool, esquema estricto | E2 |
| Secreto del system prompt | Canario del Radar | E2 |
| Confiabilidad | Errores y latencia mediana | E2 |

Un endpoint queda **distinguible de la referencia** si tiene excedente de tokens o si
alguna comparación da p < 0,01 (umbral fijo en `checks.ALPHA`). Si no, queda **sin
desviación detectada**, que no significa "es el mismo modelo".

## Comandos

```bash
uv sync
uv run cachai endpoints z-ai/glm-5.3          # sin clave: qué declara cada proveedor
uv run cachai scan deepseek/deepseek-v4.1-flash --providers 6 --max-usd 2
uv run cachai scan deepseek/deepseek-v4.1-flash --only wafer/us,sail-research/fp4 \
  --direct 50x=https://api.ai.50x.energy/v1,FIFTYX_API_KEY,deepseek-ai/DeepSeek-V4.1-Flash-1017
```

- `scan` necesita `OPENROUTER_API_KEY` en `.env`. La referencia es el proveedor
  first-party si está en la lista; si no, `--reference NOMBRE` o `consenso`.
- Por defecto el juego de pruebas es ciego: se genera con una semilla que queda en la
  ficha. `--seed N` lo repite; `--public` usa el juego fijo.
- La ficha queda en `audit/fichas/` (JSON, Markdown y las respuestas crudas en JSONL,
  con su SHA-256). `--publish` la sube al blog de CachAI; necesita `INTERNAL_API_TOKEN`
  y `CACHAI_API_URL` (por defecto `http://localhost:8001`).

## Demo con el proveedor tramposo

El villano es un proxy propio que reenvía a un proveedor real con nuestra cuenta. Nunca
se apunta contra terceros.

```bash
uv run cachai villain --upstream https://openrouter.ai/api/v1 --upstream-key-env OPENROUTER_API_KEY \
  --inject --swap-model deepseek/deepseek-v4.1-flash --fraction 0.3 --evade
```

En otra terminal, primero la auditoría de prompts fijos y después la ciega:

```bash
uv run cachai scan z-ai/glm-5.3 --only z-ai/fp8 --direct tramposo=http://127.0.0.1:9102/v1 --public
uv run cachai scan z-ai/glm-5.3 --only z-ai/fp8 --direct tramposo=http://127.0.0.1:9102/v1
```

Con `--evade` el tramposo reconoce el juego público y se porta bien: la primera corrida
no lo marca y la segunda sí.

### Ensayo sin claves

`cachai toy` levanta un modelo de juguete (reglas fijas, no es un LLM) para ensayar el
flujo completo:

```bash
uv run cachai toy
uv run cachai villain --upstream http://127.0.0.1:9101/v1 --inject --swap-model toy-small --fraction 0.3 --evade
uv run cachai scan toy-large --no-openrouter --reference oficial \
  --direct oficial=http://127.0.0.1:9101/v1 --direct tramposo=http://127.0.0.1:9102/v1
```

Lo que se mida contra el juguete no es un hallazgo: sirve para probar el auditor.

## Estado (21-nov, 23:50)

- Corre de punta a punta contra el juguete y el tramposo; 16 tests propios.
- `cachai endpoints` está probado contra la API real de OpenRouter.
- **Nunca corrió contra proveedores reales**: no había clave en `.env`. Falta confirmar
  con una llamada que OpenRouter devuelve `prompt_tokens` nativos con el proveedor
  fijado, que `provider.only` acepta el `tag` del endpoint y que los modelos con
  razonamiento responden dentro de `max_tokens`.
- Un proveedor que además falsee el `usage` no deja excedente de tokens; ahí solo queda
  la prueba de comportamiento.
- Una sustitución del 30 % con 20 tareas no siempre se detecta por exactitud sola; por
  eso hay una prueba que suma todos los controles. `--tasks 80` da más potencia.
