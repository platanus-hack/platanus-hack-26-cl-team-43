# Arquitectura y plan por fases

## Vista general

```
                 contraprueba check --repo R --base main --pr rama [--mode ci|restore|full]
                                         │
   ┌─────────────────────────────────────┼──────────────────────────────────────┐
   │ Orquestador (cli.py → pipeline.py), proceso confiable en el host            │
   │  1. integrity: diff base...pr → marcas (hooks, conftest, config)            │
   │  2. sandbox: red Docker --internal (sin internet) por corrida               │
   │       ┌──────────────────────┐   HTTP    ┌───────────────────────────────┐ │
   │       │ contenedor APP       │◀──────────│ contenedor EVALUADOR          │ │
   │       │ código del PR        │           │ imagen fija de Contraprueba   │ │
   │       │ (no confiable)       │           │ tests de la BASE (git archive)│ │
   │       │ cap-drop, mem, pids  │           │ + ataques y tests reservados  │ │
   │       └──────────────────────┘           └───────────────┬───────────────┘ │
   │  3. holdout.run_attacks (dentro del evaluador)           │ resultados JSON │
   │  4. gate.decide → APROBADO / RECHAZADO + evidencia ◀─────┘                 │
   │  5. report: JSON + Markdown/HTML con archivo, línea y ataque que entró     │
   └────────────────────────────────────────────────────────────────────────────┘
   Fuera del núcleo: redteam.py (genera variantes)  ·  fixer.py (modelo, no confiable)
```

## Decisiones de diseño

1. **Los tests de la base son de caja negra por HTTP.** El fixture `client` usa `BASE_URL` si existe; si no, importa la app en el mismo proceso, como haría un CI normal. El evaluador siempre define `BASE_URL` y no tiene el código del PR, así que un hook dentro del paquete no tiene dónde ejecutarse.
2. **La imagen del evaluador es de Contraprueba y fija.** No se construye con nada del repo del PR. Los tests de la base se leen del commit de la rama base y se montan en solo lectura.
3. **Los ataques se confirman con un predicado estructurado**, no con código: `{"status_in": [200], "body_contains": ["FAC-A-001"]}`. La decisión no ejecuta nada que venga de un modelo.
4. **Las líneas base son modos de la misma CLI.** `ci` corre el pytest del PR junto al código; `restore` restaura los tests y la configuración de la base, pero en el mismo contenedor; `full` es Contraprueba. La tabla de comparación sale de correr los tres modos sobre los mismos casos.
5. **Casos materializados desde carpetas.** Cada caso guarda la app de la rama base (`base/`) y cada PR como una capa de archivos (`prs/<nombre>/`). Un script arma un repo git temporal con `main` y una rama por PR. Así los casos viven en el repo sin repos anidados.
6. **App de demo propia.** API de facturas (FastAPI) con la misma falla de autorización por recurso en varias rutas (`/invoices/{id}`, `/invoices/{id}/pdf`, `/v2/invoices/{id}`, `/invoices/export`) y una de acceso a archivos (`/attachments?name=`). Varias rutas para una misma falla permiten que un parche a medias caiga con un ataque reservado.

## Qué se lee y de dónde

- Todo sale de los objetos de git, por hash de commit; nunca del directorio de trabajo.
- La configuración de la evaluación (`.contraprueba.json`: módulo de la app, puerto, ruta de salud, carpeta de tests) vale solo la de la rama base. Si el PR la cambia, se rechaza.
- Los árboles se exportan entrada por entrada, sin `git archive`: ese comando obedece los `.gitattributes` del propio árbol y un PR podría usar `export-ignore` para que el código evaluado no sea el que se integra.
- Si la rama del PR no contiene la punta de la base, no se evalúa: hay que actualizarla.
- No se obedecen los reemplazos de objetos de git (`refs/replace`) y se rechaza un repo con injertos: un hash siempre significa su propio contenido y su propia historia.
- La aprobación vale para el par exacto de hashes base y PR. Si alguna rama avanza, se revisa de nuevo.

Límite: el `.git` local lo aporta quien opera Contraprueba. No se defiende contra una base de objetos manipulada a mano; en producción se revisa un clon limpio del remoto.

## Marcas de integridad

`integrity.scan` revisa el diff del PR. La revisión estática da evidencia (archivo y línea), no garantía: se puede esquivar ofuscando. La garantía es el evaluador en otro contenedor.

| Cambio | Efecto |
|---|---|
| `conftest.py`, `sitecustomize.py`, `usercustomize.py`, `*.pth` nuevos, modificados o borrados | bloquea |
| `pytest.ini`, `.contraprueba.json`, o la sección de pytest de `pyproject.toml`, `setup.cfg` o `tox.ini` | bloquea |
| Tests existentes modificados, borrados o movidos | bloquea: aunque no se usan para evaluar este PR, al integrarse debilitarían la base de los siguientes |
| Código de la app que nombra pytest en líneas nuevas (imports, nombres y textos; no comentarios ni docstrings) | bloquea, con archivo y línea |
| Tests nuevos | nota: no se usan para evaluar |
| Dependencias, o `pyproject.toml` fuera de la sección de pytest | nota |

La política es restrictiva a propósito: arreglar un test existente o un fixture también bloquea y pide revisión humana. Al medir utilidad, esos casos cuentan en el denominador como falsas alarmas.

## Modos

| Modo | Tests y configuración | Dónde corre pytest | Qué decide |
|---|---|---|---|
| `ci` | los del PR | mismo contenedor que el código | código de salida de pytest |
| `restore` | los de la base; se borran los hooks que no están en la base | mismo contenedor que el código | código de salida de pytest |
| `bot` | solo los ataques visibles | contenedor evaluador, por HTTP | `gate.decide`; es la validación del bot de seguridad |
| `full` | los de la base, los tests reservados y los ataques reservados | contenedor evaluador, por HTTP | `gate.decide` sobre la evidencia y las marcas |

`bot` y `full` solo aplican a PR que parchean la falla (`half_patch`, `correct_patch`); `bot` corre únicamente el subconjunto visible de ataques, el mismo que vería el bot de seguridad.

## Contrato del evaluador

El evaluador imprime un JSON con `schema: 1` y sale con 0 (`ok`), 1 (`failed`: terminó y algo falló) o 2 (`incomplete`: app caída, timeout, sin tests ejecutados o ataques sin resultado). Reglas:

- Una suite pasa solo si terminó dentro del presupuesto, corrió al menos un test y no tuvo fallos, errores ni tests omitidos: las suites del evaluador son obligatorias, así que `skip` y `xfail` no cuentan como verde.
- Toda la evaluación tiene un presupuesto de tiempo. Si una app deja una respuesta abierta, pytest se corta y el contenedor se mata desde afuera: resultado `incomplete`, no una espera sin fin.
- La falta de evidencia nunca es éxito: `gate.decide` rechaza cualquier evaluación incompleta.
- El orquestador solo aprueba si el evaluador dijo `ok`, salió con 0 y cada suite obligatoria (la de la base y, si se pidieron, los reservados) está presente, completa y aprobada. Una salida mal formada, un estado que no coincide con el código de salida o una suite que no aprobó sin decir por qué quedan como evaluación incompleta.

Comprobación de punta a punta: `contraprueba case cases/facturas` y los tests de integración con `CP_DOCKER=1 pytest tests/test_sandbox_docker.py`.

## Contenedores

| | App | Evaluador |
|---|---|---|
| Imagen | `docker/app.Dockerfile` con el código del PR | `docker/evaluator.Dockerfile`, fija |
| Confianza | no confiable | confiable |
| Red | red interna de la corrida, sin internet | la misma red interna |
| Límites | `--cap-drop ALL`, `--security-opt no-new-privileges`, memoria y procesos acotados | igual |
| Entrada | código de la rama del PR | tests de la base y ataques, en solo lectura |
| Salida | puerto HTTP | JSON con resultados de tests y ataques |

Límite conocido: la imagen de la app se construye con acceso a internet para instalar dependencias. Un PR que cambie `requirements.txt` corre código al construir; se marca, pero no se aísla en esta versión.

Los casos son fixtures locales confiables. Aun así, `materialize` valida las capas: no acepta rutas fuera del repo, metadatos de git ni enlaces simbólicos. No es un sandbox.

## Fases

| Fase | Qué sale | Criterio de hecho |
|---|---|---|
| **0. Contratos** | Formato de `case.json`, `Attack` con predicado estructurado, app de demo en la base, imágenes de app y evaluador | Ambas imágenes construyen; los tests de la base pasan contra la app desde el evaluador |
| **1. Hito 1: aislamiento** | `sandbox.py` (red interna, levantar y bajar), caso con trampa (el paquete parcha los reportes de pytest), modos `ci`, `restore` y `full` | `ci` y `restore` dan verde; `full` rechaza con archivo y línea; un caso honesto se aprueba |
| **2. Hito 2: reservados** | `holdout.run_attacks`, `redteam.py` por plantillas (otro id, otra ruta, otro método, codificación del path), ramas de parche a medias y parche correcto | El parche a medias pasa su propia validación y cae con un ataque reservado; el correcto se aprueba |
| **3. Medición** | `bench.py` sobre casos etiquetados: detección, falsas alarmas y utilidad con intervalos de Wilson; `fixer.py` en vivo | Tabla de Contraprueba contra las tres líneas base; al menos un parche generado en vivo pasa por la revisión |
| **4. Entrega** | Página de resultados para `deploy-url`, guion de la demo, respuestas a las preguntas del jurado | Demo de punta a punta en menos de 3 minutos; `platanus-hack-project.jsonc` completo |

La ruta crítica es 0 → 1 → 2. La medición y la página avanzan en paralelo desde que existen los contratos de la fase 0.

## Reparto sugerido (5 personas)

- Sandbox y orquestador.
- App de demo y casos.
- Ataques, equipo rojo y reservados.
- Bench, métricas y página de resultados.
- Pitch, demo y parchador.
