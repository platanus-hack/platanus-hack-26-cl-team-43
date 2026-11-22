<img src="./project-logo.png" alt="Project Logo" width="200" />

Platanus Hack 26: Santiago ft. Buk · Track: 🛡️ AI Security · team-43

- Mauricio Genta ([@5y5F4il](https://github.com/5y5F4il))
- Jaime Aza ([@Jjat00](https://github.com/Jjat00))
- Tomás Degese ([@tomileonel](https://github.com/tomileonel))
- Christian Rodrigo Rojas Rodriguez ([@Christian-Rojas-Rodriguez](https://github.com/Christian-Rojas-Rodriguez))
- Federico Hörl ([@fede-h](https://github.com/fede-h))

**¿Cachai qué modelo te están sirviendo?** Las evaluaciones de seguridad de un modelo se hacen sobre un modelo y una configuración concretos. Cuando ese modelo lo sirve un tercero, nadie comprueba que lo que responde siga siendo lo que se evaluó. Cachai lo audita desde afuera: le manda a cada endpoint pruebas generadas en el momento, que el proveedor no puede reconocer, compara las respuestas con una referencia y escribe una ficha que separa lo que el proveedor declara de lo que se midió.

## Entregables

| Entregable | Dónde |
|---|---|
| Página del proyecto | https://cachai-ai.vercel.app |
| Auditor (`cachai scan`) | [`src/cachai/`](src/cachai/), con 21 tests en [`tests/`](tests/) |
| Diseño y arquitectura | [`docs/arquitectura.md`](docs/arquitectura.md): modelo de amenaza, decisiones con alternativas descartadas, plan de medición y riesgos |
| Experimento de línea base | [`results/linea-base/`](results/linea-base/): benchmark de prompts fijos contra el juego ciego de Cachai, con un proveedor tramposo propio |
| Guía de uso | [`docs/cachai-auditor.md`](docs/cachai-auditor.md) |

## El problema

Un modelo abierto no llega a producción desde su autor: llega a través de un proveedor de inferencia, y decenas de proveedores sirven el mismo modelo con el mismo nombre. GLM-5.3 tiene 47 endpoints en OpenRouter y 14 no declaran con qué cuantización lo sirven (API pública de OpenRouter, 21-11-2026). Quien usa el modelo ve el nombre, no lo que hay detrás.

### Por qué es un problema de seguridad de IA

- **Las evaluaciones no se transfieren.** El red teaming, las evaluaciones de riesgo y las salvaguardas de un modelo se miden sobre ese modelo y esa configuración. Si el endpoint sirve otra cosa (otro modelo, una versión degradada o un prompt agregado), esas evaluaciones ya no describen lo que está en producción.
- **Un prompt oculto es un ataque a la cadena de suministro.** El proveedor ve cada petición y puede agregar instrucciones propias delante del pedido del cliente, sin que nadie lo note. En nuestra demo, el prompt inyectado hace que el modelo revele el secreto de su system prompt 8 de 8 veces.
- **El auditado puede reconocer la auditoría.** Es el mismo problema de toda evaluación de seguridad: un sistema que distingue la prueba del uso real puede portarse bien solo cuando lo evalúan. Los benchmarks públicos por endpoint usan suites fijas y conocidas, al menos en parte, así que son vulnerables a esa evasión (Cai et al., [arXiv 2504.04715](https://arxiv.org/abs/2504.04715)).
- **Es un problema abierto de gobernanza técnica de IA**: verificar las características declaradas de un modelo sin tener acceso a él ([Reuel, Bucknall et al., *Open Problems in Technical AI Governance*, arXiv 2407.14981](https://arxiv.org/abs/2407.14981), §5.4.1).

### La diferencia existe y está medida

- *Model Equality Testing* ([Gao, Liang y Guestrin, arXiv 2410.20247](https://arxiv.org/abs/2410.20247)): 11 de 31 endpoints de Llama servían una distribución distinta a la de referencia.
- *Endpoint Accuracy Index* de Artificial Analysis (medido entre el 8-10 y el 4-11-2026): 24 de 103 endpoints quedaron por debajo de una referencia con los pesos oficiales; en GLM-5.3, 10 de 26.
- K2 Vendor Verifier (Moonshot): la precisión del esquema de tool calls de Kimi va de 84,63 % a 100 % según el proveedor.

### El adversario

El adversario es el proveedor. Controla el servidor, ve cada petición y puede cambiar qué modelo responde. No controla la referencia ni el auditor, y no conoce la semilla de una auditoría antes de recibirla.

| Qué puede hacer el proveedor | Cómo lo detecta Cachai | Estado |
|---|---|---|
| Inyectar un system prompt propio delante del pedido | Excedente constante de tokens de prompt frente a la referencia, y el canario si el prompt cambia el comportamiento | Medido con el proveedor tramposo propio |
| Desviar todo el tráfico a un modelo más barato y responder con el nombre pedido | Exactitud y coincidencia con la referencia en tareas con respuesta calculable | Medido |
| Desviar solo una fracción del tráfico | Prueba combinada sobre todas las fallas; la potencia crece con el número de tareas | Medido: el 30 % se detecta en 6/30 con 20 tareas y en 30/30 con 80 |
| Romper las tool calls (parser o plantilla) | Una herramienta con esquema estricto | Implementado, sin medición propia |
| **Reconocer las auditorías** y portarse bien solo con ellas | Juego ciego: pruebas generadas con una semilla nueva en cada auditoría | Medido: el benchmark fijo lo detecta 0/120 veces; el ciego, 30/30 |
| Reconocer además la forma de las pruebas ciegas | No lo detecta. Mitigación: auditar con tráfico real | Medido como límite: 0/30 |
| Mirar o guardar los datos | No se puede medir desde afuera | Fuera del alcance |

El modelo de amenaza completo, con supuestos y casos difíciles, está en [`docs/arquitectura.md`](docs/arquitectura.md#11-modelo-de-amenaza).

## La solución

Cachai es un auditor de caja negra. Usa la misma API que cualquier cliente, sin acceso al modelo ni cooperación del proveedor, y responde una sola pregunta por endpoint: **¿se distingue de la referencia?**

Se apoya en tres ideas:

1. **Pruebas ciegas.** El juego de pruebas se genera con una semilla nueva en cada auditoría, así que el proveedor no puede memorizarlo ni reconocerlo de antemano. La semilla queda en la ficha para que cualquiera repita exactamente la misma auditoría.
2. **Controles deterministas, sin juez LLM.** Todas las pruebas tienen respuesta calculable. El auditor no puede ser inyectado por la respuesta que audita y da el mismo veredicto cada vez.
3. **Evidencia con su nivel a la vista.** Cada dato de la ficha dice si lo declara el proveedor (E1), si se midió desde afuera (E2) o si está atestado criptográficamente (E3). Un sello único de «proveedor confiable» mezclaría lo que el proveedor dice con lo que se midió. Hoy el código cubre E1 y E2.

Qué mide en cada endpoint (38 pruebas por auditoría):

| Control | Qué delata | Cómo |
|---|---|---|
| Tokens de prompt de sobra | Un prompt oculto | 6 mensajes mínimos; un excedente constante frente a la referencia es la firma de un prefijo agregado |
| Exactitud y coincidencia con la referencia | Un modelo sustituido o degradado | 20 tareas con respuesta calculable (sumas, productos, ordenar, repetir), a temperatura 0 |
| Tool calls válidas | Un parser o una plantilla distintos | 4 llamadas a una herramienta con esquema estricto |
| Canario | Que el endpoint filtre el secreto del system prompt | 8 secretos distintos y un juez determinista: el secreto aparece literal en la respuesta o no |
| Confiabilidad | Un endpoint inestable | Errores y latencia mediana de cada endpoint durante la corrida |

Cada comparación con la referencia es un test exacto de Fisher con un umbral fijado antes de medir (p < 0,01). El veredicto es «distinguible de la referencia» o «sin desviación detectada», con la muestra, el p-valor y los límites de lo que se afirma.

## Arquitectura

![Arquitectura de Cachai](docs/arquitectura.png)

Son tres piezas: el **auditor**, que mide endpoints reales; el **laboratorio**, que mide al auditor contra proveedores tramposos propios; y la **ficha**, que publica lo medido con su evidencia.

**El auditor** (`cachai scan`) recorre cinco pasos:

1. **Lo declarado** (`endpoints.py`). Lee de la API pública de OpenRouter la cuantización y el precio de cada endpoint del modelo, y elige cuáles auditar.
2. **Juego ciego** (`suite.py`). Con una semilla nueva genera las 38 pruebas. Sin semilla genera el juego público, siempre igual, que sirve de comparación.
3. **Las mismas pruebas a todos** (`client.py`, `scan.py`). Las manda a cada endpoint fijado (`provider.only`, sin fallback) y a una referencia, a temperatura 0, en paralelo y con un tope de gasto. Un error no detiene la auditoría: se registra como dato de confiabilidad.
4. **Controles** (`checks.py`). Convierte las respuestas crudas en una medición por endpoint y la compara con la referencia. Si ningún control individual dispara, una prueba combinada suma todas las fallas.
5. **Ficha** (`ficha.py`). JSON y Markdown con lo declarado, lo medido, el motivo de cada marca, los límites, la semilla y el SHA-256 de las respuestas crudas, que se guardan aparte.

**El laboratorio** (`cachai experimento`) responde otra pregunta: ¿el auditor detecta lo que dice detectar? No se puede saber la verdad sobre un tercero, y atacarlo no es ético, así que el auditor se mide contra un proveedor tramposo propio (`villain.py`): un proxy que inyecta un prompt, desvía una fracción del tráfico a un modelo más barato y se porta bien cuando reconoce una auditoría. Una referencia y cinco proxies hacia el mismo modelo (uno honesto, como control de falsas alarmas, y cuatro tramposos) se auditan con el juego fijo y con el ciego. Corre sin claves contra un modelo de juguete (`toy.py`) o contra OpenRouter.

**La referencia** es el endpoint del autor del modelo cuando existe (si no, uno que se elige con `--reference`), consultado por la misma ruta que los demás. Así la comparación aísla al proveedor, sin GPU propia. Es una referencia más débil que correr el modelo original y la ficha lo dice.

Las decisiones, con las alternativas descartadas y su porqué, están en [`docs/arquitectura.md`](docs/arquitectura.md#3-decisiones-y-alternativas-descartadas).

## Resultados

### Experimento de línea base: ¿quién detecta a un proveedor que reconoce las pruebas?

Un proveedor tramposo propio (un proxy hacia el mismo modelo) aplica cuatro trampas y se porta bien cuando reconoce el benchmark. Un quinto proxy, honesto, sirve de control de falsas alarmas. Cada celda son 30 auditorías con semillas distintas. Se corre con `uv run cachai experimento --toy` y el detalle está en [`results/linea-base/`](results/linea-base/).

| Trampa del proveedor | Benchmark de prompts fijos | Cachai, juego ciego (20 tareas) | Cachai, juego ciego (80 tareas) |
|---|---|---|---|
| Ninguna (control) | 0/30 | 0/30 | 0/30 |
| Prompt oculto | 0/30 | 30/30 | 30/30 |
| Desvía el 30 % del tráfico a un modelo más barato | 0/30 | 6/30 | 30/30 |
| Desvía el 100 % | 0/30 | 30/30 | 30/30 |
| Prompt oculto y desvío del 30 % | 0/30 | 30/30 | 30/30 |

- **Un benchmark fijo no detecta a un proveedor que lo reconoce**: 0 de 120 auditorías con trampa. Con el juego ciego, Cachai lo marca en todas, salvo el desvío parcial con 20 tareas.
- **La potencia depende del tamaño de la muestra**: con 80 tareas, el desvío del 30 % pasa de 6/30 a 30/30.
- **El límite, medido igual**: un tramposo que además reconoce las plantillas del juego ciego evade a Cachai en todas las condiciones (0/30). Por eso el diseño final audita con una muestra del tráfico real del comprador ([`docs/arquitectura.md`](docs/arquitectura.md#7-lo-que-sigue)).
- **Alcance**: el upstream es un modelo de juguete con reglas fijas. Prueba que el mecanismo funciona; no es un hallazgo sobre ningún proveedor.

### Medición contra proveedores reales

En curso a 2026-11-22. Se agrega en `results/proveedores/` con el mismo formato de ficha.

## Lo que no prometemos

- «Sin desviación detectada» no significa «es el mismo modelo».
- No certificamos privacidad: desde afuera no se puede probar que un proveedor no mire ni guarde los datos. Eso exige un TEE con atestación o un contrato, y la ficha lo marca con ese nivel de evidencia.
- «Distinguible de la referencia» no es una acusación: puede deberse a la plantilla, al parser o a la cuantización.
- La referencia también es un endpoint. Si está cuantizada, medimos equivalencia con ella, no con los pesos.
- Un proveedor que conozca el generador de pruebas puede evadirlo (lo medimos arriba).

## Cómo está hecho

```
src/cachai/        el auditor
  endpoints.py       lo que declara cada endpoint en OpenRouter (E1) y cuáles auditar
  suite.py           juego público (fijo) y ciego (con semilla): 38 pruebas con respuesta calculable
  client.py          una llamada a un endpoint: proveedor fijado, temperatura 0, nunca lanza
  scan.py            corre las pruebas en paralelo por endpoint, con tope de gasto
  checks.py          controles deterministas, Fisher exacto, Wilson y el veredicto por endpoint
  ficha.py           la ficha en JSON y Markdown, con límites y SHA-256 de la evidencia
  villain.py         proveedor tramposo propio para la demo: prompt oculto, desvío, evasión
  toy.py             modelo de juguete con reglas fijas, para probar sin claves
  experiment.py      experimento de línea base: benchmark fijo contra juego ciego
tests/             21 tests de punta a punta, sin red ni claves
results/linea-base/  resultados del experimento (JSON y Markdown)
```

```bash
uv sync
uv run pytest -q                                   # 21 tests, sin red ni claves
uv run cachai experimento --toy --reps 30          # la línea base, sin claves (~2 min)
uv run cachai endpoints z-ai/glm-5.3               # lo que declara cada proveedor, sin clave
uv run cachai scan z-ai/glm-5.3 --providers 6 --max-usd 2   # necesita OPENROUTER_API_KEY
```

La demo en vivo con el proveedor tramposo y el ensayo sin claves están en [`docs/cachai-auditor.md`](docs/cachai-auditor.md).

## El negocio

- **Cliente.** Equipos que ya gastan en tokens de modelos abiertos a través de varios proveedores y tienen agentes en producción. Lo usa quien elige proveedor para decidir a cuál mandarle tráfico y a cuál sacarlo.
- **Qué vendemos.** Una ficha por endpoint, medida en forma continua, con tres niveles de evidencia: E1 declarado (lo afirma el proveedor), E2 medido (desde afuera, con método y muestra) y E3 atestado (evidencia criptográfica, como pesos medidos dentro de un TEE). Hoy el código cubre E1 y E2.
- **Quién paga.** Quien compra los tokens, no quien los vende, para que el auditado no financie su auditoría. Los proveedores se suman gratis y obtienen una ficha pública.
- **Competencia y cuña.** No somos los primeros en verificar proveedores de inferencia:

| Quién | Qué hace | En qué nos diferenciamos |
|---|---|---|
| Artificial Analysis | Benchmarks públicos contra una referencia con los pesos oficiales | Es una foto puntual con una suite fija |
| Benchlist | Atestación de benchmarks, USD 499 al mes | Lo paga el proveedor |
| OpenRouter Exacto | Calidad de tool calls sobre tráfico real | Es del intermediario y no publica una ficha |
| Tinfoil, Phala | Prueba criptográfica de los pesos (TEE) | Solo en sus propios endpoints |

  La cuña: auditoría continua, del lado del comprador, con pruebas que el proveedor no puede reconocer de antemano y con el nivel de evidencia de cada dato a la vista.
- **Mercado.** El gasto empresarial en modelos e inferencia fue de USD 94,2 mil millones a septiembre de 2026 (Menlo Ventures). Estimamos que USD 3,8 a 4,5 mil millones al año van a modelos abiertos por API; cobrando entre 0,5 y 1 % del gasto del comprador, el techo sería de USD 20 a 45 millones al año. Las dos últimas cifras son estimaciones nuestras.
- **Validación.** Un comprador dijo que lo compraría para elegir proveedores, que no conoce ningún certificado en el que confíe y que hace falta un estándar propio, no una ISO. Un proveedor contó que pierde ventas porque le piden certificaciones que no tiene. Es una entrevista por lado: no está validado cuánto pagarían ni quién firma la compra.
