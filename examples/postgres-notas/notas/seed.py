"""Semilla de datos de prueba. Cachai la corre con la versión de la rama base, antes de
levantar la app, y le pasa marcas canario nuevas en cada corrida (CACHAI_CANARY_*)."""

import os

import psycopg

NOTES = [
    (1, "ana", "Nota privada de Ana: " + os.environ.get("CACHAI_CANARY_NOTA_ANA", "NOTA-ANA-LOCAL")),
    (2, "beto", "Nota privada de Beto: " + os.environ.get("CACHAI_CANARY_NOTA_BETO", "NOTA-BETO-LOCAL")),
]

with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as db:
    db.execute("CREATE TABLE IF NOT EXISTS notes (id int PRIMARY KEY, owner text NOT NULL, body text NOT NULL)")
    db.execute("DELETE FROM notes")
    db.cursor().executemany("INSERT INTO notes VALUES (%s, %s, %s)", NOTES)
print(f"{len(NOTES)} notas sembradas")
