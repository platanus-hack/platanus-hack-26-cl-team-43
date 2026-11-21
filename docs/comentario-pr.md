# Comentario de Cachai en el PR

Así se ve el comentario que la Action publica en el PR (issue #9). Es un solo comentario:
la Action lo encuentra por el marcador oculto `<!-- cachai-report -->` y por su autor
(`github-actions[bot]`), y lo edita en cada push en vez de sumar uno nuevo. Además crea un
check run `cachai: veredicto` cuyo título es el resumen corto (`public.summary` del reporte v2).

Los ejemplos de abajo son la salida real de `cachai check --mode full` con Docker sobre PRs de
`cases/facturas` (`src/cachai/report_md.py` arma el texto; `pipeline` lo redacta). De un ataque
reservado se muestra solo familia y ruta con los tramos variables ocultos; de un test reservado,
solo cuántos fallan. Ningún id, pedido, header, marca ni nombre de test reservado llega al
comentario (`tests/test_report_md.py`).

Para regenerarlos: `CP_DOCKER=1`, `materialize(load_case("cases/facturas"), ...)` y
`pipeline.check(repo, "main", <pr>, "full", ...)`, como en `tests/test_sandbox_docker.py`.

## PR aprobado: `parche-correcto`

Resumen del check run:

> Cachai: APROBADO. 12 tests de la rama base pasan desde otro contenedor; 9 tests funcionales reservados pasan; 29 ataques bloqueados, 19 de ellos reservados

Comentario:

---

### Cachai: APROBADO

PR `cfb6eb468d` sobre base `7802c2e732`, modo `full`.

**Cómo se evaluó:** la app del PR en un contenedor y el evaluador en otro, con los tests de la rama base, los ataques visibles y reservados, los tests funcionales reservados y la revisión de los cambios que tocan la evaluación.

**Comprobado**

- 12 tests de la rama base pasan desde otro contenedor
- 9 tests funcionales reservados pasan
- 29 ataques bloqueados, 19 de ellos reservados

<details><summary>Reproducir en local lo visible</summary>

```sh
cachai check --repo . --base 7802c2e732331f41811bd776ea0acc23f9007415 --pr cfb6eb468d150afb730ab6b029b8f83c1e1cebcd
```

Corre la app y el evaluador en contenedores separados con los tests de la rama base y los ataques visibles. Los ataques y tests reservados no se reproducen en local: por eso miden algo que el autor del PR no pudo ver.

</details>

<!-- cachai-report -->

---

## PR rechazado: `parche-a-medias` (bloquea los ataques visibles, no los reservados)

Resumen del check run:

> Cachai: RECHAZADO, 11 motivos (11 ataques reservados).

Comentario:

---

### Cachai: RECHAZADO

PR `ec66038d84` sobre base `7802c2e732`, modo `full`.

**Cómo se evaluó:** la app del PR en un contenedor y el evaluador en otro, con los tests de la rama base, los ataques visibles y reservados, los tests funcionales reservados y la revisión de los cambios que tocan la evaluación.

**Ataques reservados que siguen funcionando** (11)

- `authz` `GET /invoices/export` ×3
- `authz` `GET /invoices/{…}/total` ×3
- `authz` `GET /v2/invoices/{…}` ×3
- `files` `GET /attachments` ×2

**Notas** (no bloquean)

- `11 vulnerabilidades que ya estaban en la base siguen abiertas; no bloquean este PR (GET /attachments, GET /invoices/export, GET /invoices/{…}/total, GET /v2/invoices/{…})`

<details><summary>Reproducir en local lo visible</summary>

```sh
cachai check --repo . --base 7802c2e732331f41811bd776ea0acc23f9007415 --pr ec66038d844375420582369972bece62c00b8df9
```

Corre la app y el evaluador en contenedores separados con los tests de la rama base y los ataques visibles. Los ataques y tests reservados no se reproducen en local: por eso miden algo que el autor del PR no pudo ver.

</details>

<!-- cachai-report -->

---

## PR rechazado: `trampa-paquete` (toca la evaluación; motivos con archivo y línea)

Resumen del check run:

> Cachai: RECHAZADO, 4 motivos (1 test de la base, 2 cambios en la evaluación, 1 test reservado).

Comentario:

---

### Cachai: RECHAZADO

PR `1bf1b8a31c` sobre base `7802c2e732`, modo `full`.

**Cómo se evaluó:** la app del PR en un contenedor y el evaluador en otro, con los tests de la rama base, los ataques visibles y reservados, los tests funcionales reservados y la revisión de los cambios que tocan la evaluación.

**Tests de la rama base que fallan** (1)

- `tests/test_invoices.py:48` `test_total_includes_tax` — `assert 1200 == 1428.0`

**Cambios que tocan la evaluación** (2)

- `invoices/_telemetry.py:7` `código de la app que referencia pytest (A)` — `if "pytest" not in sys.modules:`
- `invoices/_telemetry.py:9` `código de la app que referencia pytest (A)` — `from _pytest import reports`

**Tests funcionales reservados** (1)

- 1 test funcional reservado falla; el detalle queda reservado.

**Notas** (no bloquean)

- `26 vulnerabilidades que ya estaban en la base siguen abiertas; no bloquean este PR (GET /attachments, GET /invoices/export, GET /invoices/{…}, GET /invoices/{…}/pdf y 1 más)`

<details><summary>Reproducir en local lo visible</summary>

```sh
cachai check --repo . --base 7802c2e732331f41811bd776ea0acc23f9007415 --pr 1bf1b8a31ce6a9755a9ef03ffbf4b2a19b51ee9d
```

Corre la app y el evaluador en contenedores separados con los tests de la rama base y los ataques visibles. Los ataques y tests reservados no se reproducen en local: por eso miden algo que el autor del PR no pudo ver.

</details>

<!-- cachai-report -->

---
