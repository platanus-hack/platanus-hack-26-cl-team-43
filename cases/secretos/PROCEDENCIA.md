# Procedencia: caso `secretos`

Caso de terceros para #17: no lo escribimos nosotros. Se adaptó una app pública
de BaxBench/Honeyval al formato de Cachai.

## Material de terceros

- **Base: `base/secret_storage/app.py`**, sin cambios: implementación
  vulnerable del escenario SecretStorage publicada por Honeyval (Google LLC),
  Apache-2.0. Copia en `LICENSE-honeyval`.
  Origen: <https://github.com/google-research/honeyval/blob/86272d8d1fb4b3090037561cc31b28dc8a8350f8/data/baxbench-webapps/SecretStorage/vul/app.py>
- **Especificación del escenario** (OpenAPI y tests funcionales y de seguridad
  de BaxBench, que los tests de la base siguen): BaxBench (SRI Lab,
  LogicStar.ai), MIT. Copia en `LICENSE-baxbench`.
  Origen: <https://github.com/logic-star-ai/baxbench/blob/de885cd93d561682e203a5a0d57c33b08aac6f5e/src/scenarios/secret_storage.py>

## Lo que agregó Cachai

- `base/secret_storage/main.py`: adaptador. La base SQLite va a `/tmp`, fija
  `APP_SECRET` de prueba, siembra `alice` y `bob` con un secreto cada uno y
  publica `/health`.
- `base/tests/`, `reservados/`, `attacks.json` e inventario: tests por HTTP,
  marcas canario y ataques de las familias `authz`, `authn` y `sqli`.
- Los PR: `parche-agente` es el diff de un agente real (Claude Sonnet con
  `cachai fix`), guardado en `results/bots/secretos/`; `parche-a-medias` es
  nuestro, para medir el parche que solo tapa lo que vio.

## Cobertura y huecos

Cubierto:

- CWE-284 en `GET /get_secret` (sin sesión y sin control de dueño): familias
  `authn` y `authz`.
- CWE-89 en `GET /get_secret` (SQL armado con el parámetro): familia `sqli`.

No cubierto (también es un resultado):

- CWE-863 en `POST /set_secret`: el equipo rojo solo arma pedidos GET.
- CWE-522: contraseñas en texto plano.
- CWE-798: secreto JWT por defecto escrito en el código.

Los dos últimos no son fallas de autorización y quedan fuera del alcance de
esta versión.
