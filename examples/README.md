# Ejemplos de uso

Todos se corren igual: `cachai scan -c <archivo>`, o se copia el archivo como `cachai.toml` en la carpeta donde se trabaja y basta con `cachai scan`. Las claves se leen del entorno o de un `.env`; en los archivos va el nombre de la variable, nunca la clave.

Si todavía no se instaló: `uv tool install git+https://github.com/platanus-hack/platanus-hack-26-cl-team-43` (o `pipx install` con la misma URL).

`cachai scan` pide una cuenta gratuita: corre `cachai login` una vez y cada auditoría queda en tu panel, en [cachai.jjato.com/panel](https://cachai.jjato.com/panel). `cachai demo` no la pide. Con `--no-subir` la ficha queda solo en tu equipo.

| Archivo | Para qué | Claves |
|---|---|---|
| (ninguno) `cachai demo` | Ver cómo funciona en 10 segundos: un proveedor honesto y dos tramposos de juguete | Ninguna |
| [`openrouter.toml`](openrouter.toml) | Auditar los proveedores de un modelo abierto en OpenRouter contra el del autor | `OPENROUTER_API_KEY` |
| [`autor-contra-revendedores.toml`](autor-contra-revendedores.toml) | Comparar la API del autor con las de proveedores que lo revenden, directo y sin OpenRouter | La de cada proveedor |
| [`servidor-propio.toml`](servidor-propio.toml) | Comprobar que tu vLLM, SGLang u Ollama (o una cuantización nueva) responde igual que la referencia antes de pasar a producción | Opcional |
| [`demo-en-vivo.toml`](demo-en-vivo.toml) | Auditar la demo en vivo: tres APIs dicen servir Qwen2.5 y una hace trampa (`cachai ronda` la arma y la revela) | Ninguna |
| [`github-actions/cachai.yml`](github-actions/cachai.yml) | Auditoría semanal automática; el job falla si un endpoint sale distinguible (`--strict`) | `OPENROUTER_API_KEY` y `CACHAI_API_KEY` como secretos |

## Lo mismo sin archivo

```bash
# los 6 proveedores más baratos de un modelo en OpenRouter, contra el del autor
cachai scan z-ai/glm-5.3 --providers 6 --max-usd 2

# solo algunos proveedores de OpenRouter (los nombres salen de `cachai endpoints`)
cachai scan z-ai/glm-5.3 --only z-ai/fp8,novita/fp8,deepinfra/fp4/flex

# endpoints directos: el primero es la referencia
cachai scan -e deepseek:deepseek-chat -e together:deepseek-ai/DeepSeek-V3

# un servidor propio o cualquier API compatible con OpenAI: NOMBRE=URL[,VARIABLE_DE_CLAVE[,MODELO]]
cachai scan -e ollama:qwen2.5:7b -e mio=http://10.0.0.5:8000/v1,MI_API_KEY,qwen2.5-7b

# repetir exactamente una auditoría anterior (la semilla está en la ficha)
cachai scan -c cachai.toml --seed 3331723469

# más tareas = más potencia para detectar desvíos parciales (y más gasto)
cachai scan z-ai/glm-5.3 --tasks 80 --max-usd 5

# solo lo que declara cada proveedor (cuantización y precio), sin clave y sin gasto
cachai endpoints z-ai/glm-5.3
```

## Atajos de proveedor

`-e PROVEEDOR:MODELO` completa la URL y el nombre de la variable de la clave:

| Atajo | Variable de la clave |
|---|---|
| `openrouter` | `OPENROUTER_API_KEY` |
| `together` | `TOGETHER_API_KEY` |
| `fireworks` | `FIREWORKS_API_KEY` |
| `deepinfra` | `DEEPINFRA_API_KEY` |
| `groq` | `GROQ_API_KEY` |
| `cerebras` | `CEREBRAS_API_KEY` |
| `sambanova` | `SAMBANOVA_API_KEY` |
| `novita` | `NOVITA_API_KEY` |
| `hyperbolic` | `HYPERBOLIC_API_KEY` |
| `nebius` | `NEBIUS_API_KEY` |
| `deepseek`, `moonshot`, `zai`, `mistral` (el autor del modelo) | `DEEPSEEK_API_KEY`, `MOONSHOT_API_KEY`, `ZAI_API_KEY`, `MISTRAL_API_KEY` |
| `ollama`, `vllm`, `sglang`, `lmstudio`, `llamacpp` (en tu máquina) | Ninguna |

Cualquier otro servidor compatible con la API de OpenAI se usa con `NOMBRE=URL`.

## Qué elegir como referencia

La referencia es contra qué se compara. Lo mejor es la API del autor del modelo (DeepSeek para DeepSeek, Z.ai para GLM). En OpenRouter, Cachai la elige sola si el autor está entre los proveedores; si no, usa el consenso. Con `-e`, la referencia es el primer endpoint, o la que digas con `--reference`. La ficha siempre dice contra qué se comparó.

## Qué cuesta

Una auditoría manda 52 pruebas cortas a cada endpoint y a la referencia dos veces. Con un modelo de precio medio son centavos de dólar por endpoint; `--max-usd` corta la auditoría si se pasa del tope (2 USD por defecto) y la ficha registra lo gastado. Contra servidores propios el costo es solo el cómputo.
