# Tests de la base por HTTP

Cachai corre los tests de la rama base en un contenedor distinto del de la app: así un hook
del PR no puede falsear sus resultados. Para eso los tests le hablan a la app por HTTP, en
`BASE_URL`.

## Sin cambiar nada

La mayoría de los repos FastAPI y Starlette prueban con `TestClient(app)` dentro del mismo
proceso. El evaluador carga un plugin de pytest (`cachai.pytest_http`) que:

- reemplaza `TestClient` por un cliente httpx contra `BASE_URL`;
- cuando un test o un `conftest.py` importa la app u otro módulo que en el evaluador no
  existe (la app, SQLAlchemy, pandas, FastAPI mismo), le entrega un sustituto. El sustituto
  acepta que lo llamen, que entren a un `with` con él o que le asignen cosas
  (`app.dependency_overrides[...] = ...`), y anota que el test usó código de la app. Lo que
  no acepta es que se lea su valor (compararlo, pasarlo a texto, iterarlo, usarlo en un
  `if`): ahí el test se corta. Así ningún test aprueba gracias a un sustituto;
- `fastapi.status` y `starlette.status` se entregan de verdad (son solo números).

Un test que solo usa la API HTTP corre igual que antes, también con la app armada en una
fixture (`with TestClient(create_app()) as c`): lo que solo se le pasa a `TestClient` no
cuenta como uso de la app. Queda como **no evaluado desde afuera** (no aprueba ni falla, y
el reporte lo lista con el motivo) el test que:

- lee un valor del código de la app (`assert calcular_total(2) == 4`, `settings.api_password`);
- falla después de usar código de la app, él o una de sus fixtures (un override, una base
  de prueba que se llena o se vacía): la falla puede venir de esa preparación, que afuera
  no ocurre;
- aprueba sin hacer ningún pedido HTTP pero usó código de la app: no probó nada de ella.

Si un test con override aprueba, aprobó contra la app real por HTTP y cuenta como aprobado.

## Para que un test sí se evalúe

| El test hace | Cámbialo por |
| --- | --- |
| `app.dependency_overrides[get_db] = ...` | Datos de prueba que la app carga al arrancar (variables de entorno de `app.env` en `.cachai.json`) |
| Llama una función interna (`calcular_total(...)`) | Un pedido a la ruta que la usa, comprobando la respuesta |
| Lee o escribe la base de datos directo | Crear y leer por la API |
| Lee credenciales o rutas de los settings de la app (`get_settings().api_username`) | Leerlas de variables de entorno en el test, con las mismas que recibe la app |
| Da por hecho que la base queda vacía entre tests (la fixture la borra) | Datos únicos por test (un correo con `uuid4()`), o no depender de lo que crearon otros tests |
| Espera que una `BackgroundTasks` haya terminado cuando vuelve la respuesta | Sondear el estado con una pausa (`time.sleep`) y un límite: por HTTP la tarea corre después de la respuesta |
| Fixture `client` con `TestClient(app)` o `TestClient(create_app())` | Nada: ya funciona |

Un patrón que funciona en los dos mundos (CI normal y Cachai) es el `conftest.py` del caso
de ejemplo `cases/facturas/base/tests/conftest.py`: con `BASE_URL` usa httpx, sin ella
importa la app.

## Medición

Medido el 2026-11-21 contra repos FastAPI públicos, sin cambiar sus tests. Cada app corrió
con su propio venv (`uvicorn`, en local). Al evaluador se le copió solo la carpeta de tests
(y el `conftest.py`), sin el código de la app, y corrió `run_suite` con un venv como el de
`docker/evaluator.Dockerfile`: pytest 8.3.4 y httpx, sin FastAPI. La línea base es la suite
del repo corrida en el proceso, con su venv: en los cuatro aprueban todos los tests de API.

Test de API = test que usa el cliente (`TestClient` o la fixture `client`).

| Repo (commit) | Tests de API | Aprueban desde afuera | No evaluados desde afuera | Falsos fallos |
| --- | --- | --- | --- | --- |
| [A-Nuru/Deploying-a-ML-Model-on-Heroku-with-FastAPI](https://github.com/A-Nuru/Deploying-a-ML-Model-on-Heroku-with-FastAPI) (`9a552b7`) | 4 | **4 (100 %)** | 0 | 0 |
| [LeticiaPaesano/Churn_Hackathon](https://github.com/LeticiaPaesano/Churn_Hackathon) (`f76325e`) | 5 | **4 (80 %)** | 0 | 1 |
| [platzi/curso-fastapi](https://github.com/platzi/curso-fastapi) (`34ed444`) | 3 | 1 (33 %) | 2 | 0 |
| [rednafi/fastapi-nano](https://github.com/rednafi/fastapi-nano) (`5f86e32`) | 9 | 3 (33 %) | 6 | 0 |

Los dos primeros cumplen el criterio de la issue #22 (al menos la mitad). Los otros dos
muestran los límites:

- **Deploying-a-ML-Model** (modelo de scikit-learn detrás de `/inference`): los 4 tests de
  API aprueban, incluido el `conftest.py` que importa `pandas`, `main` y `starter.ml`, que
  en el evaluador no están. Sus 6 tests de entrenamiento e inferencia, que no son de API,
  quedan listados como no evaluados desde afuera.
- **Churn_Hackathon**: aprueban `/health`, `/`, `/previsao` y el 422 por payload incompleto.
  El falso fallo es `test_previsao_lote_fluxo_completo`: sube un CSV a `/previsao-lote`, que
  lo procesa con `BackgroundTasks`, y consulta el estado 10 veces sin pausa. `TestClient`
  corre las tareas de fondo antes de devolver la respuesta; por HTTP corren después, así
  que la descarga da 404. No hay forma de saber desde afuera cuándo terminó la tarea; el
  arreglo va en el test (fila de `BackgroundTasks` de la tabla de arriba).
- **curso-fastapi** (el patrón del tutorial de SQLModel: `dependency_overrides` con una base
  SQLite que se crea y se borra en cada test): `test_create_customer` aprueba.
  `test_read_customer` crea el mismo correo y la app lo rechaza porque ya existe (en el
  proceso, la fixture borraba la base entre tests): falla después del override, queda no
  evaluado. `test_client` solo compara el tipo del cliente, sin pedidos HTTP, y depende de
  la fixture de la base: no evaluado.
- **fastapi-nano**: los 3 tests sin autenticación aprueban. Los otros 6 piden el token con
  el usuario y la contraseña que leen de `get_settings()`, el código de la app: no
  evaluados. Sus 10 tests de funciones internas y del logger, que no son de API, también.

Con el plugin anterior (commit `b45f30e`) ningún test de API aprobaba en los tres repos que
medí con él (Deploying-a-ML-Model, curso-fastapi y fastapi-nano): los `conftest.py` raíz se
cargaban antes de que el plugin instalara los sustitutos, así que la suite entera caía por
`ModuleNotFoundError` (`pandas`, `sqlalchemy`), y en fastapi-nano llamar `create_app()` en
la fixture del cliente dejaba los 9 como no evaluados. El plugin ahora instala los
sustitutos antes de los `conftest.py` y los hace perezosos, como se describe arriba.

## Node (jest)

Con `"tests_runner": "jest"` en `.cachai.json`, los tests de la base corren con jest en un
contenedor construido desde la rama base (su Dockerfile, sus dependencias de desarrollo, su
configuración de jest y sus tests), en la misma red que la app del PR y con `BASE_URL`. Nada
del PR entra en esa imagen. Necesita `app.dockerfile` o `app.compose`, y que la base tenga
`jest-junit` entre sus dependencias de desarrollo (Cachai lee el resultado de ahí).

Los tests usan supertest contra la URL cuando existe:

```js
const request = require('supertest');
const client = () => request(process.env.BASE_URL || require('../src/app'));
```

Un PR que cambia `jest.config.*`, `vitest.config.*`, `.mocharc*` o la sección `jest`, `mocha`
o el script `test` de `package.json` se bloquea: ahí viven `setupFiles` y los reporters. Ver
el caso `cases/notas-express`.
