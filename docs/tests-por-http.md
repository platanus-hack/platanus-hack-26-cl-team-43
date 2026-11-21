# Tests de la base por HTTP

Cachai corre los tests de la rama base en un contenedor distinto del de la app: así un hook
del PR no puede falsear sus resultados. Para eso los tests le hablan a la app por HTTP, en
`BASE_URL`.

## Sin cambiar nada

La mayoría de los repos FastAPI y Starlette prueban con `TestClient(app)` dentro del mismo
proceso. El evaluador carga un plugin de pytest (`cachai.pytest_http`) que:

- reemplaza `TestClient` por un cliente httpx contra `BASE_URL`;
- cuando un test importa la app u otro módulo que en el evaluador no existe, le entrega un
  sustituto que solo sirve para pasárselo a `TestClient`.

Un test que solo usa la API HTTP corre igual que antes. Uno que usa el código de la app
(llama una función interna, toca `app.dependency_overrides`, abre la base de datos) queda
como **no evaluado desde afuera**: no aprueba ni falla, y el reporte lo lista en una nota.

## Para que un test sí se evalúe

| El test hace | Cámbialo por |
| --- | --- |
| `app.dependency_overrides[get_db] = ...` | Datos de prueba que la app carga al arrancar (variables de entorno de `app.env` en `.cachai.json`) |
| Llama una función interna (`calcular_total(...)`) | Un pedido a la ruta que la usa, comprobando la respuesta |
| Lee o escribe la base de datos directo | Crear y leer por la API |
| Fixture `client` con `TestClient(app)` | Nada: ya funciona |

Un patrón que funciona en los dos mundos (CI normal y Cachai) es el `conftest.py` del caso
de ejemplo `cases/facturas/base/tests/conftest.py`: con `BASE_URL` usa httpx, sin ella
importa la app.
