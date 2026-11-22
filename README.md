# Cachai

<img src="./project-logo.png" alt="Project Logo" width="200" />

Platanus Hack 26: Santiago ft. Buk · Track: 🛡️ AI Security · team-43

- Mauricio Genta ([@5y5F4il](https://github.com/5y5F4il))
- Jaime Aza ([@Jjat00](https://github.com/Jjat00))
- Tomás Degese ([@tomileonel](https://github.com/tomileonel))
- Christian Rodrigo Rojas Rodriguez ([@Christian-Rojas-Rodriguez](https://github.com/Christian-Rojas-Rodriguez))
- Federico Hörl ([@fede-h](https://github.com/fede-h))

**¿Cachai qué modelo te están sirviendo?** Cachai audita a los proveedores que venden tokens de un mismo modelo abierto. Les manda pruebas generadas en el momento, que el proveedor no puede reconocer, compara sus respuestas con una referencia y escribe una ficha por endpoint que separa lo que el proveedor declara de lo que se midió.

## Entregables

| Entregable | Dónde |
|---|---|
| Página del proyecto | https://cachai-ai.vercel.app |
| Auditor (`cachai scan`) | [`src/cachai/`](src/cachai/), con 21 tests en [`tests/`](tests/) |
| Diseño y arquitectura | [`docs/arquitectura.md`](docs/arquitectura.md): modelo de amenaza, decisiones con alternativas descartadas, plan de medición y riesgos |
| Experimento de línea base | [`results/linea-base/`](results/linea-base/): benchmark de prompts fijos contra el juego ciego de Cachai, con un proveedor tramposo propio |
| Guía de uso | [`docs/cachai-auditor.md`](docs/cachai-auditor.md) |

## El problema

Decenas de proveedores sirven el mismo modelo abierto, con el mismo nombre y precios muy distintos. GLM-5.3 tiene 47 endpoints en OpenRouter: 14 no declaran cuantización y el precio de salida varía 4,7 veces (API pública de OpenRouter, 21-11-2026). Quien compra no puede comprobar qué recibe, y hay evidencia de que la diferencia existe:

- *Model Equality Testing* ([Gao, Liang y Guestrin, arXiv 2410.20247](https://arxiv.org/abs/2410.20247)): 11 de 31 endpoints de Llama servían una distribución distinta a la de referencia.
- *Endpoint Accuracy Index* de Artificial Analysis (medido entre el 8-10 y el 4-11-2026): 24 de 103 endpoints quedaron por debajo de una referencia con los pesos oficiales; en GLM-5.3, 10 de 26.
- K2 Vendor Verifier (Moonshot): la precisión del esquema de tool calls de Kimi va de 84,63 % a 100 % según el proveedor.

**Por qué es AI Security.** Una evaluación de seguridad vale para unos pesos y una configuración. Si no se puede verificar qué modelo responde, sus evaluaciones y salvaguardas no cubren lo que está en producción. Es un problema abierto de gobernanza técnica: verificar características declaradas de un modelo sin acceso a él ([Reuel, Bucknall et al., *Open Problems in Technical AI Governance*, arXiv 2407.14981](https://arxiv.org/abs/2407.14981), §5.4.1). Y un proveedor que agrega instrucciones ocultas al prompt del cliente es un ataque a la cadena de suministro.

**El adversario.** Un proveedor que puede degradar o sustituir el modelo (siempre o en una fracción del tráfico), inyectar un system prompt propio, romper las tool calls y, sobre todo, **reconocer las auditorías y portarse bien solo con ellas**. Los benchmarks públicos por endpoint usan suites fijas y conocidas, al menos en parte, así que son vulnerables a esa evasión (Cai et al., [arXiv 2504.04715](https://arxiv.org/abs/2504.04715)). El modelo de amenaza completo está en [`docs/arquitectura.md`](docs/arquitectura.md#11-modelo-de-amenaza).

## Cómo funciona

1. **Lo declarado (E1).** Lee de la API pública de OpenRouter qué declara cada endpoint: cuantización y precio.
2. **Un juego de pruebas ciego.** Genera 38 pruebas con una semilla nueva en cada auditoría: 6 de conteo de tokens, 20 tareas con respuesta calculable (sumas, productos, ordenar, repetir), 4 tool calls con esquema estricto y 8 canarios en el system prompt. La semilla queda en la ficha, así cualquiera puede repetir exactamente las mismas pruebas.
3. **Las mismas pruebas a todos.** Las manda a cada endpoint fijado (`provider.only`, sin fallback) y a una referencia, a temperatura 0 y con un tope de gasto.
4. **Controles deterministas, sin juez LLM.** Excedente constante de tokens de prompt (la firma de un prefijo oculto), exactitud y coincidencia con la referencia, tool calls válidas y filtración del canario. Cada comparación es un test exacto de Fisher con un umbral fijado antes de medir (p < 0,01).
5. **La ficha.** Por endpoint: «distinguible de la referencia» o «sin desviación detectada», con la muestra, el p-valor, los límites de lo que se afirma y el SHA-256 de las respuestas crudas.

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
