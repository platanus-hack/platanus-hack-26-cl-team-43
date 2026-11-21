# El inventario: usuarios, recursos y marcas

El inventario es la parte de `.cachai.json` que le dice a Cachai **quién es
quién** en tu app: qué usuarios de prueba existen, qué recursos tienen dueño y
cómo reconocer un dato ajeno cuando aparece en la respuesta equivocada. De ahí
salen los ataques; sin un buen inventario, el equipo rojo ataca el aire.

`cachai init` propone un borrador leyendo los listados de la app. Esta guía es
para entenderlo y corregirlo.

## Qué es una marca

Una **marca** es un texto que aparece en la respuesta de un recurso y que
**solo su dueño debería ver**. Cachai la busca en la respuesta del atacante:
si aparece, hubo fuga.

```json
{
  "id": "1",
  "owner": "alice",
  "markers": ["FAC-A-001", "1428.0"]
}
```

Buenas marcas:

- Un identificador de negocio único: número de factura, slug, nombre de
  archivo, email.
- Un valor numérico distintivo (un total, un saldo) si el id no aparece.
- Texto estable entre corridas: **no** uses timestamps, ids autoincrementales
  que cambian con la semilla, ni el nombre de usuario solo.

Malas marcas:

- Algo que aparece en la respuesta de todos (un nombre de empresa, un
  `status: "ok"`). Si el dueño y el atacante lo ven, no distingue nada.
- Texto de menos de 4 caracteres: se descarta.
- Un dato que la app devuelve a cualquiera por otra ruta. Entonces el recurso
  no es privado y el ataque va a fallar con razón.

La regla que aplica `init`: el objeto aparece en el listado de **un solo**
usuario, y la marca no aparece en lo que ven los demás usuarios ni en otro
registro.

### Adjuntos y archivos

Para archivos, el inventario usa una carpeta por usuario:

```
data/attachments/
  alice/contrato.txt
  bob/orden.txt
```

```json
{
  "files": [
    {"path": "alice/contrato.txt", "owner": "alice", "markers": ["Contrato de servicios"]},
    {"path": "bob/orden.txt", "owner": "bob", "markers": ["Orden de compra"]}
  ],
  "files_root": "/app/data/attachments"
}
```

`files_root` es la ruta **dentro del contenedor**. Con la imagen por defecto,
el repo se copia a `/app`, así que una carpeta `data/attachments/` del repo es
`/app/data/attachments`. La marca es la primera línea no vacía del archivo.

## Cómo elegir los usuarios

- **Al menos dos**, con datos que no se solapen. Dos usuarios del mismo tenant
  con recursos compartidos no sirven para probar autorización por recurso.
- Un header completo por usuario: `Authorization: Bearer …`, `X-API-Key: …`,
  lo que use tu app.
- **Tokens de solo lectura y de prueba.** Cachai no necesita permisos de
  administrador. Si la app permite crear datos, el equipo rojo de escritura va
  a intentar modificar recursos: usá una base de prueba.
- Si el login es por formulario, `--login 'alice=./scripts/login.sh alice'`:
  el comando corre en tu máquina y su última línea es un header
  `Nombre: valor` o un token suelto (va como `Bearer`).
- Si la app no tiene autenticación, no hay inventario que proponer y el equipo
  rojo no tiene con qué atacar. Cachai sirve igual para tests y archivos, pero
  no para autorización.

## Cómo armar el inventario a mano

`init` escribe un borrador. Para corregirlo, editá `.cachai.json` en la rama
base:

```json
{
  "version": 2,
  "app": {"module": "invoices.main:app", "port": 8000, "health": "/health"},
  "tests_dir": "tests",
  "inventory": {
    "users": {
      "alice": {"Authorization": "Bearer tok-alice"},
      "bob": {"Authorization": "Bearer tok-bob"}
    },
    "records": [
      {"id": "1", "owner": "alice", "markers": ["FAC-A-001"]},
      {"id": "2", "owner": "alice", "markers": ["FAC-A-002"]},
      {"id": "3", "owner": "bob", "markers": ["FAC-B-001"]}
    ]
  },
  "attacks": {"families": ["authz", "authn", "sqli"]},
  "hidden": {"package": ".cachai/reservado.cachai", "key_env": "CACHAI_HIDDEN_KEY"}
}
```

- `records`: cada recurso con `id`, `owner` y `markers`. Poné **varios**
  recursos por dueño y al menos uno de cada usuario.
- El `id` se usa para reemplazar el tramo variable de la ruta
  (`/invoices/{id}`) y el parámetro de consulta (`?ids=`).
- `attacks.families`: qué familias corre el equipo rojo. Si no la ponés, usa
  las de los ataques del paquete. Para un paquete sellado por `init` vienen
  de las carpetas con dueño:

  | Familia | Qué prueba |
  |---|---|
  | `authz` | pedir un recurso ajeno con sesión propia |
  | `authn` | pedir sin sesión, con la credencial vacía o inválida |
  | `files` | salir de la carpeta propia con `..`, codificado o doble |
  | `sqli` | inyectar en el id para traer datos de otro |
  | `ssrf` | meter una URL interna en un parámetro con nombre de URL |
  | `cmdi` | inyectar un comando que crea un archivo testigo |
  | `writes` | POST/PUT/PATCH/DELETE sobre el recurso de otro |

- Después de editar, volvé a correr `init` para validar y sellar de nuevo:

  ```bash
  cachai init --force --yes --user 'alice=…' --user 'bob=…'
  ```

  `init` corre el equipo rojo contra la base y solo conserva los ataques que
  **funcionan**. Un ataque que no funciona no se guarda: nunca cuenta como
  detección. Si preferís revisar los ataques primero, `cachai redteam
  cases/<caso> --write` hace lo mismo sobre un caso de `cases/`.

## Errores comunes

| Síntoma | Causa | Arreglo |
|---|---|---|
| «no encontré recursos con dueño en los listados» | Los endpoints de listado devuelven `{items: [...]}`? `init` lo entiende; si devuelven una lista sin `id` o todos ven lo mismo, no. | Agregá `records` a mano con marcas que solo vea el dueño. |
| El equipo rojo no confirma ningún ataque | Las marcas aparecen en la respuesta del atacante porque el recurso no es privado, o el `owner` del inventario no coincide con el usuario que pide. | Revisá qué devuelve cada usuario con su token. |
| Todos los ataques fallan con 401 | El header del inventario está mal escrito o el token venció. | Probá `curl` con el mismo header. |
| `files` ataca rutas que no existen | `files_root` no coincide con la ruta dentro del contenedor. | Con la imagen por defecto es `/app/<carpeta>`; con `dockerfile` o `compose`, la que use tu imagen. |
| «falta requirements.txt» | La imagen por defecto instala desde ese archivo. | Agregá `requirements.txt` en la raíz. |
| La app no arranca en el sandbox | Falta una dependencia que necesita red o un servicio externo. | Usá `app.compose` (ver abajo) o revisá las dependencias. |
| Mucho ruido de escrituras | La app cambia datos entre ataques. | Usá otra base para las pruebas; `init` ya corre las escrituras después de los tests. |

## Apps con base de datos o servicios

Si tu app necesita Postgres, Redis o un servicio propio, `cachai init` lo
detecta y te avisa: agregá el compose a mano.

```json
{
  "app": {
    "compose": {"file": "docker-compose.yml", "service": "api"},
    "port": 8000,
    "health": "/health"
  },
  "seed": ["python", "-m", "app.seed"]
}
```

- Cachai interpreta el compose de la **rama base** (no corre `docker compose`):
  levanta los servicios en una red interna sin salida a internet y después la
  app.
- `seed` corre una vez, con la imagen de la base, antes de la app. Es el lugar
  para crear los usuarios y recursos del inventario.
- Ver [`examples/postgres-notas`](../examples/postgres-notas) y
  [tests-por-http.md](tests-por-http.md).

## Seguridad del inventario

- El inventario, los ataques y los tests reservados viven en la **rama base** o
  en el paquete cifrado. Un PR no los puede cambiar: si lo intenta, la revisión
  de integridad lo bloquea.
- Los tokens del inventario son de prueba y de solo lectura. No pongas un token
  de producción en `.cachai.json`.
- El paquete reservado va cifrado y la clave solo está en el secreto del CI.
  Ver [material-reservado.md](material-reservado.md).
