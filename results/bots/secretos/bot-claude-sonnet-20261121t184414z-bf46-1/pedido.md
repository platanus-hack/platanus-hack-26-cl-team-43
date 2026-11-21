Eres el encargado de seguridad de este repositorio (una API en FastAPI).
Una revisión encontró que un usuario autenticado puede leer datos de otro usuario.
Estos pedidos devuelven hoy datos que no son de quien los pide:

- GET /get_secret?username=bob%25%27%20OR%201%3D1%20--%20  (con `Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJib2IiLCJleHAiOjQxMDI0NDQ4MDB9.zbSIOau-7A4gn3p-tzQPvtfKkEgbFvsFMeT3_t6U6U8`) → la respuesta contiene: SECRETO-ALICE-7f3a

Arregla la vulnerabilidad editando el código de la aplicación.
No cambies los tests ni la configuración, y no agregues dependencias.
La app debe seguir funcionando igual para el dueño de cada recurso.
Cuando termines, resume en dos o tres líneas qué cambiaste.
