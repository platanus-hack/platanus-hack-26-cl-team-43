# Ejemplo: app con Postgres

API de notas que necesita una base de datos. `.cachai.json` apunta al compose del repo y
Cachai levanta `db` y `api` en una red interna sin salida, construye `api` con el árbol
del PR y corre los tests de la base desde otro contenedor. El compose se lee de la rama
base; un PR que lo cambia se bloquea.
