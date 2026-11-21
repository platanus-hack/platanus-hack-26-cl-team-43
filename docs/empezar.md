# Cachai en 5 minutos

Esta guía es para quien va a poner Cachai en un repo propio con una API
FastAPI. Al final tenés: `.cachai.json`, el paquete de ataques reservados, un
workflow que corre en cada PR y el check obligatorio en la rama principal.

Si preferís ver primero qué garantiza y qué no, saltá a
[Lo que no garantiza](#lo-que-no-garantiza).

## 0. Qué necesitás

- Python 3.11 o superior y Docker corriendo en tu máquina.
- Una API FastAPI que ya corra con `uvicorn`, un `requirements.txt` en la raíz
  (aunque sea solo `fastapi` y `uvicorn`) y una carpeta de tests.
- Los headers de **dos usuarios de prueba** (por ejemplo dos tokens), o un
  comando de login para cada uno.
- Permisos para crear un secreto y marcar un check como obligatorio en GitHub.

## 1. Instalar

```bash
git clone https://github.com/platanus-hack/platanus-hack-26-cl-team-43
cd platanus-hack-26-cl-team-43
pipx install -e .
```

Para probarlo sin instalar:

```bash
pipx run --spec /ruta/al/repo cachai --help
```

> `cachai init` **lo corre quien mantiene el repo**, nunca el agente cuyos PR
> se van a revisar: el que configura ve el material reservado y recibe la clave.

## 2. Configurar el repo en un comando

Desde la rama principal de tu repo:

```bash
cachai init \
  --user 'alice=Authorization: Bearer tok-alice' \
  --user 'bob=Authorization: Bearer tok-bob' \
  --holdout-tests ../reservados
```

O, si preferís que la app te dé los tokens:

```bash
cachai init \
  --login 'alice=./scripts/login.sh alice' \
  --login 'bob=./scripts/login.sh bob'
```

`cachai init` hace todo esto:

1. **Detecta la app**: busca `FastAPI(` y `app =`, el puerto y la ruta de
   salud. Si no hay salud, propone agregar `GET /health` (o usa
   `/openapi.json` mientras tanto).
2. **Lee la app en el sandbox**: la levanta sin red, como en una revisión, y
   pide `/openapi.json` y los listados de cada usuario.
3. **Propone el inventario**: registros con dueño y sus marcas, y adjuntos
   organizados en una carpeta por usuario. Te lo muestra y te deja corregirlo.
4. **Corre el equipo rojo contra la base** y sella el paquete reservado con los
   ataques confirmados y, si se los pasaste, los tests funcionales reservados.
5. **Escribe** `.cachai.json` (validado con el esquema publicado y el cargador
   de Cachai) y `.github/workflows/cachai.yml`.

Con `--yes` no pregunta nada (para agentes y CI). Con `--no-sandbox` solo
detecta y escribe, sin inventario ni ataques.

Al terminar imprime **una sola vez** la clave del paquete. Copiala en el
secreto del repo:

```bash
gh secret set CACHAI_HIDDEN_KEY     # pegá la clave
```

Si preferís no verla en la terminal, pasá `--key-file clave.txt` (se escribe
con permisos 0600) y bórralo después de cargarla.

### Revisá lo que escribió

```json
{
  "version": 2,
  "app": {"module": "invoices.main:app", "port": 8000, "health": "/health"},
  "tests_dir": "tests",
  "inventory": {
    "users": {"alice": {"Authorization": "Bearer tok-alice"}, "bob": {"Authorization": "Bearer tok-bob"}},
    "records": [{"id": "1", "owner": "alice", "markers": ["FAC-A-001"]}]
  },
  "attacks": {"families": ["authz", "authn", "sqli"]},
  "hidden": {"package": ".cachai/reservado.cachai", "key_env": "CACHAI_HIDDEN_KEY"}
}
```

- **Commitéalo en la rama principal**, junto con `.cachai/reservado.cachai` y
  el workflow. La rama base es la fuente confiable: el PR nunca aporta esta
  configuración.
- `.cachai/reservado.cachai` está cifrado. Igual conviene que el agente no lo
  tenga a mano: solo el CI tiene la clave.
- Si `init` encontró poca cosa, mirá [inventario.md](inventario.md) antes de
  seguir.

## 3. Marcar el check como obligatorio

El workflow se llama `cachai` y publica un check con el mismo nombre. Sin la
regla de rama, un PR puede editar el workflow para no correrlo.

En GitHub: **Settings → Rules → Rulesets** (o *Branches → Branch protection*),
exigí el check `cachai` en la rama principal. Con `gh`:

```bash
gh api repos/{owner}/{repo}/rulesets -X POST --input - <<'JSON'
{"name": "cachai", "target": "branch", "enforcement": "active",
 "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
 "rules": [{"type": "required_status_checks", "parameters": {
   "strict_required_status_checks_policy": false,
   "required_status_checks": [{"context": "cachai"}]}}]}
JSON
```

## 4. Abrí un PR y leé el comentario

En cada PR, el workflow corre Cachai en modo `full`. Al terminar:

- **Verde**: un comentario con lo que se comprobó (cuántos tests de la base,
  cuántos ataques, qué tests reservados) y una línea de tiempo por fase.
- **Rojo**: el merge queda bloqueado. El comentario dice el motivo con
  evidencia: el test que falla con archivo y línea, o el ataque que sigue
  funcionando con familia y ruta. De un ataque reservado **nunca** se publica
  el pedido exacto ni las marcas: se quema y rota (ver
  [material-reservado.md](material-reservado.md)).

El comentario es uno solo y se edita en cada push: no llena el PR. Si querés
reproducir lo visible en tu máquina (sin el material reservado, que no tenés):

```bash
cachai check --repo . --base main --pr mi-rama
```

Eso corre en modo `visible` y lo avisa: no es el veredicto final, pero es lo
que el CI muestra. En CI el modo va explícito y `full` es el que decide.

## 5. Cuando algo se rechaza

| Motivo en el comentario | Qué hacer |
|---|---|
| Un test de la rama base falla desde otro contenedor | Arreglá el código, no el test. El PR no puede cambiar los tests. |
| Un ataque visible sigue funcionando | Es el mismo tipo de falla que el bot ya vio: revisá el parche. |
| Un ataque reservado sigue funcionando | El parche tapó el caso que vio y no la falla. Corregí la causa. |
| Un test funcional reservado falla | El parche bloquea de más: se rompió una función existente. |
| Cambios que tocan la evaluación | El PR modificó tests, configuración de pytest, hooks o el workflow. Revisión humana. |
| Evaluación incompleta | Faltó evidencia (imagen que no construye, app que no levanta, falta el secreto). No se aprueba: mirá el detalle. |

Si la app falla al arrancar en el sandbox, casi siempre es una dependencia que
necesita red o un servicio que no está. Mirá
[`docs/tests-por-http.md`](tests-por-http.md) y `cachai check --mode ci` para
comparar con un CI normal.

## Lo que no garantiza

Cachai es un revisor determinista, no una prueba de seguridad. Lo que **no**
cubre:

- **Los tests que escribió una IA.** Cachai garantiza que los tests de la base
  no se falseen y que un parche no bloquee de más, pero no que esos tests
  comprueben lo suficiente. Está el aviso de tests débiles con pruebas de
  mutación (#19), todavía como nota y no como rechazo.
- **Apps que cambian después de la evaluación.** Se evalúa el commit que se
  integra. Si el despliegue hace otra cosa (otro compose, otra base, variables
  distintas), eso queda fuera.
- **Familias no cubiertas.** Hoy: autorización por recurso, archivos,
  autenticación, inyección SQL, SSRF, inyección de comandos y escrituras
  (POST/PUT/PATCH/DELETE) sobre recursos ajenos. No cubre, por ejemplo, XSS,
  CSRF, deserialización ni lógica de negocio.
- **Ataques reservados que se revelan.** Si un rechazo muestra familia y ruta
  de un reservado, ese grupo se quema y hay que rotarlo. Mientras tanto mide
  menos.
- **Apps cuyo comportamiento depende de internet o de servicios externos.** El
  sandbox no tiene salida a internet.
- **Un atacante con la clave del paquete.** Quien tenga la clave ve el material
  reservado. El agente debe correr con su propia identidad, sin secretos del
  CI.

Si algo de esto te deja una falsa sensación de seguridad, es un bug de la
documentación: abrí un issue.

## Siguientes pasos

- [inventario.md](inventario.md): qué es una marca, cómo elegir usuarios y
  recursos, errores comunes.
- [material-reservado.md](material-reservado.md): cómo funciona el paquete, la
  clave, quemar y rotar.
- [comentario-pr.md](comentario-pr.md): qué dice exactamente el comentario y
  cómo se arma.
- [preguntas.md](preguntas.md): preguntas frecuentes y límites del modelo de
  amenaza.
