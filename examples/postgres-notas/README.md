# Ejemplo: app con Postgres

API de notas que necesita una base de datos. `.cachai.json` apunta al compose del repo y
Cachai levanta `db` y `api` en una red interna sin salida, construye `api` con el árbol
del PR y corre los tests de la base desde otro contenedor. El compose se lee de la rama
base; un PR que lo cambia se bloquea.

Los datos de prueba los pone `notas/seed.py`, que Cachai corre con la imagen de la rama
base antes de levantar la app. Las marcas de las notas (`{{canary:nota_ana}}` en el
inventario) son aleatorias en cada corrida: el PR no las puede conocer de antemano, y un
PR que cambia la semilla no cambia los datos con los que se lo evalúa.

En local, sin Cachai: `docker compose up -d`, `docker compose exec api python -m notas.seed`
y `BASE_URL=http://localhost:8000 pytest`.
