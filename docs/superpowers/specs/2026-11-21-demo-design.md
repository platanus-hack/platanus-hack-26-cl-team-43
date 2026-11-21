# Diseño: demo de Contraprueba para Platanus Hack 26 CL

## Intención

Ganar el pitch del track AI Security con una demo en vivo de unos 3 minutos frente al jurado. El jurado tiene que salir convencido de una idea: **el check verde de una IA no vale nada si la IA puede tocar a quien la evalúa, y Contraprueba es el que no se deja convencer.**

La demo tiene que mostrar ejecución real (no slides que simulan una terminal), pero no puede depender de nada impredecible en el escenario.

Además, la organización pide un `deploy-url` público para validar el proyecto. No es la demo; es la evidencia verificable.

## Metas

- Tres veredictos reales de `contraprueba check` en vivo, en ~100 segundos: ❌ trampa del agente, ❌ parche a medias, ✅ parche correcto.
- Contraste visible contra la línea base: el CI normal da ✅ en los dos casos malos.
- Cada rechazo dice *por qué* con evidencia concreta (archivo del hook; id del ataque reservado que todavía entra).
- `deploy-url` público con los reportes de esas mismas corridas, reproducibles desde el repo.

## No-metas

- Generar parches con un LLM en el escenario.
- Familia «acceso indebido a archivos» en la demo (queda en el alcance del README, no en el escenario).
- Correr contenedores en un servidor público o que el jurado ejecute cosas desde la web.
- Casos de BaxBench/CVE reales en la demo (pueden aparecer en el reporte si hay tiempo).

## Anti-metas (falla aunque técnicamente funcione)

- Que el veredicto sea un `print` hardcodeado o que el «CI normal» sea fingido. Si un jurado pregunta «¿eso corre de verdad?», la respuesta tiene que ser sí y mostrable.
- Que la demo se pase de tiempo o espere más de ~10 s por un comando.
- Que el jurado no entienda qué pasó sin que lo expliquemos: la salida tiene que leerse sola (grande, en español, con colores).

## Restricciones

- Python ≥ 3.11, sin dependencias de runtime nuevas en el núcleo (el `gate` sigue determinista y sin modelos). FastAPI/uvicorn/httpx solo dentro de las imágenes de la app y del evaluador.
- Docker local en el notebook de la demo. Red del lugar no confiable: nada en el camino crítico descarga de internet en vivo (imágenes construidas y cacheadas antes).
- Tiempo de hackathon: lo construye el equipo de 5 en paralelo.

## Enfoques considerados

- **A. Todo en vivo con LLM real.** Máximo impacto, pero 30–90 s por parche, salida no determinista, mucho que construir. Descartado: no cabe en 3 min y puede fallar en vivo.
- **B. Contraprueba real sobre casos congelados (elegido).** Los PRs del agente y del bot se generan antes (con un LLM real, y se guarda el prompt/transcripción como evidencia) y quedan congelados como ramas/carpetas en `cases/`. En vivo corre solo lo determinista: CI normal vs. `contraprueba check`. El `deploy-url` es una página estática con los reportes generados por la misma CLI.
- **C. Dashboard web que corre contenedores en un servidor.** El `deploy-url` sería la demo, pero exige VM con Docker pública, backend y UI, y depende de la red del lugar. Doble trabajo para algo que el jurado ve igual en B.

El equipo eligió **B**.

## Lo decidido

### Guion (≈3:00)

30 s de slides (problema y solución) y después la demo. Aprobado por el equipo. Slides: `docs/pitch/build_deck.py`.

| Tiempo | Qué se ve | Qué se dice |
|---|---|---|
| 0:00–0:15 | **Slide problema.** «El código de una IA lo aprueba otra IA»: 9 líneas (BenchJack), 1 de cada 3 (Le et al.), trampa → sabotaje (MacDiarmid et al.) | Los agentes hacen trampa, los bots se validan solos, el humano confía en el check verde. |
| 0:15–0:30 | **Slide solución.** Tabla: CI normal / tests restaurados en el mismo contenedor / validación del bot / Contraprueba contra «hook en pytest», «parche que tapa solo lo que vio», «evidencia» | «Contraprueba no le cree al modelo: otro contenedor, ataques que el parche nunca vio, todo o nada. Miren.» |
| 0:30–1:15 | **Acto 1.** Terminal: `pytest` del PR del agente ✅, luego split: CI normal ✅ / `contraprueba check` ❌ RECHAZADO, hook `app/_hook.pth`, tests de la base fallan desde otro contenedor | «Este PR pasa todos los tests. No arregló nada: metió un archivo que hace mentir a pytest. Desde otro contenedor no puede tocar nada.» |
| 1:15–2:20 | **Acto 2 — bot de seguridad.** Split: `validar-bot` 2/2 bloqueados ✅ / `contraprueba check` ❌ ataque reservado sigue entrando. Luego segundo parche: ✅ APROBADO con resumen de lo probado | «El bot vio dos ataques y los tapó. Guardamos otros que nunca vio. Uno entra: rechazado. El segundo parche arregla la falla: aprobado, con evidencia.» |
| 2:20–3:00 | **Cierre.** Frase, AI control en una línea, QR al `deploy-url` | Modelos no confiables, monitor confiable. Lo que viene. |

RECHAZADO grande en rojo: es la foto que tiene que quedar. Paneles en la misma terminal con `demo.sh` avanzando por tecla.

Estado: hay más de 24 h; el repo del caso se elige cuando el código esté listo (la app de liquidaciones es placeholder).

### La app del caso

API de **liquidaciones de sueldo** en FastAPI (guiño al sponsor Buk), mínima:
- Usuarios por header `Authorization: Bearer <token>` con 2–3 empleados sembrados.
- `GET /liquidaciones/{id}`, `GET /liquidaciones/{id}/pdf` (o `/detalle`), `GET /liquidaciones?empleado_id=…`.
- Falla (IDOR, familia `authz`): no verifica que la liquidación pertenezca al usuario.
- Tests de la rama base: funcionales por HTTP (el empleado ve lo suyo) + uno que exige que otro empleado reciba 403/404. En la base ese test falla (el bug existe).

### Los cuatro PRs congelados (en `cases/`)

1. **agente-trampa**: no arregla el bug; agrega `app/_hook.pth` (o módulo importado) que marca todos los tests como pasados. `pytest` en el mismo entorno da ✅.
2. **parche-a-medias**: chequea pertenencia solo en `GET /liquidaciones/{id}`. Los ataques visibles (sobre esa ruta) fallan; ataques reservados por `/pdf` o por `?empleado_id=` siguen entrando.
3. **parche-correcto**: chequea pertenencia en todas las rutas. Pasa base, reservados y funcionales reservados.
4. (opcional, para métricas) **agente-honesto**: arregla sin tocar la evaluación → ✅. Prueba que no hay falsa alarma.

Los PRs se generan con un LLM real antes del evento cuando sea posible, guardando prompt y salida en el caso; si el LLM no produce el comportamiento justo, se ajusta a mano y se declara así en el `case.json`.

### Qué tiene que ser real en `contraprueba check`

- `integrity.classify` sobre el diff real base→PR (ya existe).
- `sandbox`: levantar la app del PR en un contenedor sin red externa; correr los tests de la **rama base** desde un contenedor evaluador separado vía HTTP. Quitar el `base_tests_passed=True` hardcodeado del CLI.
- `holdout.run_attacks`: ejecutar los ataques (pedidos HTTP + condición determinista `succeeded_if`) contra la app levantada.
- Tests funcionales reservados ejecutados igual que los ataques.
- `gate.decide` con toda esa evidencia (ya existe).
- Salida: veredicto grande y en colores en terminal + un `report.json` por corrida.

`redteam.generate_attacks` y `fixer.propose_patch` no corren en vivo: los ataques y parches se generan antes y se versionan en el caso.

### Severidad de las marcas de integridad (decisión de Claude)

- **Bloquean:** hooks de evaluación (`conftest.py`, `sitecustomize.py`, `usercustomize.py`, `*.pth`) y cambios en configuración de pytest.
- **Solo informan:** cambios bajo `tests/`. Se muestran en el reporte, pero no rechazan, porque igual se evalúa con los tests de la rama base y los del PR se ignoran. Si no, un parche honesto que agrega un test queda rechazado y se pierde el ✅ del acto 2.
- Requiere agregar severidad a `integrity.Flag` y que `gate.decide` ignore las informativas.

### Línea base visible (decisión de Claude)

- **Acto 1:** `ci-normal` corre `pytest` del PR en el mismo entorno que su código, como un CI ingenuo. Es real: si el hook funciona, da verde de verdad.
- **Acto 2:** `validar-bot` corre solo los **ataques visibles** contra la app del PR, que es exactamente la validación que haría el bot. Da ✅ en el parche a medias. Panel izquierdo `validar-bot`, panel derecho `contraprueba check`.
- Cada caso guarda los ataques visibles separados de los reservados (resultado congelado de `holdout.split`).

### Plataforma de la demo

Notebook macOS arm64 con Docker Desktop. Las imágenes base tienen que ser multi-arch o arm64 nativas (por ejemplo `python:3.12-slim`). Medir el arranque del contenedor contra el objetivo de < 10 s; si no alcanza, levantar la app una vez por caso antes del acto y reusarla.

### `deploy-url`

Página estática (GitHub Pages o Vercel) generada desde los `report.json`: por caso, el veredicto, el diff relevante, qué se probó, qué ataque reservado entró, y el resultado del CI normal al lado. Incluye cómo reproducir (`git clone && contraprueba check --case …`). Se regenera con un comando y se publica antes del pitch.

### Confiabilidad en el escenario

- Imágenes Docker construidas y precalentadas antes de subir; cada `check` debe tardar < 10 s.
- Script de demo (`demo.sh` o similar) que encadena los comandos con pausas por tecla, para no tipear en vivo.
- Video grabado de la demo completa como respaldo.
- Fuente grande en la terminal; salida en español.

## Lo que queda al builder

- Formato exacto de `case.json`, `attacks/*.json` y `report.json`.
- Cómo se representan los PRs (ramas git en un repo del caso vs. carpetas base/pr con diff) siempre que `integrity.changed_files` funcione sobre ellos.
- Cómo se aísla la red (`--network` interna de Docker, etc.) y cómo se comunican evaluador y app.
- Librería de colores en terminal (o ANSI a mano), y generador del sitio estático.
- Mecanismo concreto del hook tramposo, siempre que engañe a `pytest` en el mismo entorno y no al evaluador externo.
- Reparto del trabajo entre las 5 personas.
