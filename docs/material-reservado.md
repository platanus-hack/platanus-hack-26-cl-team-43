# Material reservado: dónde vive y quién lo ve

El material reservado son los ataques y los tests funcionales que el autor del PR (una persona o un agente) nunca ve. Si los ve, dejan de medir algo: un agente puede tapar justo esos casos y nada más. Este documento dice cómo se guarda, cómo llega al evaluador y qué amenazas cubre y cuáles no.

## El paquete

`cachai hidden seal` arma un tar.gz con los ataques, el reparto visible/reservado y la carpeta de tests reservados, y lo cifra con AES-256-CBC (openssl, PBKDF2 de 600.000 iteraciones). Un HMAC-SHA256 sobre todo lo cifrado se comprueba antes de descifrar: un paquete alterado o una clave que no corresponde no se abren.

```bash
cachai hidden keygen                       # una vez; va a un secreto del CI, p. ej. CACHAI_HIDDEN_KEY
CACHAI_HIDDEN_KEY=... cachai hidden seal --attacks ataques.json --holdout-tests reservados/ \
  --out .cachai/reservado.cachai
```

El paquete cifrado puede vivir en el repo. La configuración de la rama base lo declara:

```json
{
  "version": 2,
  "app": {"module": "invoices.main:app"},
  "hidden": {"package": ".cachai/reservado.cachai", "key_env": "CACHAI_HIDDEN_KEY"}
}
```

## Cómo llega al evaluador

1. `cachai check` lee `.cachai.json` y el paquete **del commit de la base**, por hash. El árbol del PR no se usa para eso: un PR que reemplaza el paquete no cambia nada, y además la revisión de integridad lo marca.
2. La clave sale de la variable que nombra `key_env`, en el proceso de Cachai.
3. El paquete se descifra en una carpeta temporal privada (0700) del runner.
4. Esa carpeta se monta de solo lectura **solo** en el contenedor del evaluador. El contenedor de la app del PR no recibe la carpeta, ni el archivo cifrado, ni ninguna variable de entorno del job.
5. Si falta la clave o el paquete no abre, la revisión queda **incompleta** y no se aprueba. Sin evidencia no hay aprobado.

`tests/test_sandbox_docker.py::test_app_container_cannot_see_hidden_key_or_material` lo comprueba: con el paquete abierto en el host, desde dentro del contenedor de la app se listan las variables de entorno, los montajes y todo el sistema de archivos buscando la clave y un texto canario de los tests reservados. No aparecen.

## `pull_request` y no `pull_request_target`

El workflow corre con `on: pull_request`.

- `pull_request_target` corre en el contexto de la base, con los secretos y un token con permisos de escritura. Hacer checkout del código del PR y ejecutarlo ahí (instalar dependencias, correr un script o un test) le entrega los secretos al autor del PR. Es la vulnerabilidad clásica de las Actions; no se usa.
- Con `pull_request`, GitHub no entrega secretos a PR que vienen de forks. Para esos PR la revisión queda incompleta (no aprobada) hasta que alguien con permisos la corra. Es lo correcto: la alternativa sería aprobar sin la prueba reservada.
- El código del PR nunca corre fuera de un contenedor sin red: Cachai lee el PR como datos (blobs de git por hash), construye su imagen y la levanta con `--cap-drop ALL`, sin salida a internet y sin variables del job.

## Lo que no cubre (y cómo se mitiga)

- **Un PR que edita el workflow.** Con `pull_request` el archivo del workflow sale del commit del PR, así que un PR de una rama del mismo repo podría editarlo para imprimir el secreto. Mitigaciones: el token del agente no debe tener el permiso `workflows` (GitHub rechaza el push de cambios a `.github/workflows/` sin él); la revisión de integridad bloquea cualquier cambio en `.github/workflows/` y `.github/actions/`; y el check de Cachai debe ser obligatorio por regla de la rama, para que un workflow editado que no lo corre no deje mergear.
- **Quien tiene la clave ve el material.** Quien mantiene el repo lo abre con `cachai hidden open`. Si el agente corre con las credenciales de esa persona, la reserva no sirve: el agente debe correr con su propia identidad, sin acceso a los secretos del CI.
- **Un reservado que se revela deja de medir.** Cuando el detalle de un ataque reservado se muestra (por ejemplo, para explicar un rechazo), hay que quemarlo y rotarlo (#10). El reporte público ya lo evita: de un reservado solo lleva familia y ruta, y borra cualquier texto del paquete que se cuele.
