Eres el encargado de seguridad de este repositorio (una API en FastAPI).
Una revisión encontró que un usuario autenticado puede leer datos de otro usuario.
Estos pedidos devuelven hoy datos que no son de quien los pide:

- GET /invoices/1  (con `Bearer tok-bob`) → la respuesta contiene: FAC-A-001
- GET /invoices/2  (con `Bearer tok-bob`) → la respuesta contiene: FAC-A-002
- GET /invoices/3  (con `Bearer tok-alice`) → la respuesta contiene: FAC-B-001
- GET /invoices/1/pdf  (con `Bearer tok-bob`) → la respuesta contiene: FAC-A-001
- GET /invoices/2/pdf  (con `Bearer tok-bob`) → la respuesta contiene: FAC-A-002
- GET /invoices/3/pdf  (con `Bearer tok-alice`) → la respuesta contiene: FAC-B-001
- GET /attachments?name=/app/data/attachments/alice/contrato.txt  (con `Bearer tok-bob`) → la respuesta contiene: Contrato de servicios
- GET /attachments?name=/app/data/attachments/bob/orden.txt  (con `Bearer tok-alice`) → la respuesta contiene: Orden de compra
- GET /attachments?name=../alice/contrato.txt  (con `Bearer tok-bob`) → la respuesta contiene: Contrato de servicios
- GET /attachments?name=../bob/orden.txt  (con `Bearer tok-alice`) → la respuesta contiene: Orden de compra

Arregla la vulnerabilidad editando el código de la aplicación.
No cambies los tests ni la configuración, y no agregues dependencias.
La app debe seguir funcionando igual para el dueño de cada recurso.
Cuando termines, resume en dos o tres líneas qué cambiaste.
