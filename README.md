# Contraprueba

<img src="./project-logo.png" alt="Logo" width="160" />

**Ninguna IA aprueba el código de otra IA.**

Track: 🛡️ AI Security · Platanus Hack 26: Santiago ft. Buk · team-43

- Mauricio Genta ([@5y5F4il](https://github.com/5y5F4il))
- Jaime Aza ([@Jjat00](https://github.com/Jjat00))
- Tomás Degese ([@tomileonel](https://github.com/tomileonel))
- Christian Rodrigo Rojas Rodriguez ([@Christian-Rojas-Rodriguez](https://github.com/Christian-Rojas-Rodriguez))
- Federico Hörl ([@fede-h](https://github.com/fede-h))

## El problema

El código que escribe una IA hoy lo aprueba otra IA, o la misma.

- **Los agentes de código hacen trampa para cumplir la métrica.** Si la meta es «que pasen los tests», un agente puede alterar cómo corre pytest en vez de arreglar el bug. Un `conftest.py` de nueve líneas basta para sacar 100 % en SWE-bench ([BenchJack](https://arxiv.org/abs/2605.12673)), y restaurar los tests originales no lo impide si el evaluador corre junto al código.
- **Los bots que parchan vulnerabilidades se validan a sí mismos.** Prueban su parche con el mismo ataque que encontraron. Incluso con modelos de frontera, cerca de un tercio de los parches que pasan su propio exploit fallan los tests del desarrollador ([Le et al.](https://arxiv.org/abs/2610.10150)): tapan el caso que vieron, no la falla.
- **Es un problema de AI safety, no solo de calidad.** El adversario es el propio modelo. Anthropic mostró que un modelo que aprende a hacer trampa en entornos de programación generaliza a engaño y sabotaje ([MacDiarmid et al.](https://arxiv.org/abs/2511.18397)).

Al final aprueba un humano que no alcanza a leer todo y confía en el check verde.

## La solución

Un revisor determinista, fuera del alcance de cualquier modelo, que decide antes del merge si entra el código escrito por un agente o el parche propuesto por un bot de seguridad.

1. **Prueba desde afuera.** La app corre en un contenedor y el evaluador en otro; los tests, tomados de la rama base, le hacen pedidos por HTTP. El código del PR nunca comparte proceso con quien lo califica.
2. **Marca lo que altera la evaluación.** Restaura `conftest.py` y la configuración de pytest de la rama base y señala hooks nuevos (`conftest.py`, `sitecustomize.py`, `*.pth`).
3. **Guarda ataques y tests en secreto.** Un equipo rojo genera varios ataques por falla; el parchador ve algunos y el revisor guarda el resto, junto con tests funcionales. Un parche que solo tapa el caso que vio, o que rompe la app, se rechaza.
4. **Todo o nada.** Si algo falla, no entra nada y el repo queda como estaba. Si todo pasa, entra con la evidencia de qué se probó.

En términos de AI control: el agente y el parchador son modelos no confiables, y Contraprueba es el monitor confiable que no depende de que cooperen.

## Alcance de esta versión

- APIs web en Python.
- Dos familias de falla: autorización por recurso (ver datos de otro usuario) y acceso indebido a archivos.
- Casos de [BaxBench](https://baxbench.com) y CVE de Python con exploit verificable.

No promete «cero vulnerabilidades»: reporta qué se probó y con qué resultado. En tests unitarios que corren dentro del mismo proceso que el código no hay garantía y solo se marca para revisión.

## Estructura

```
src/contraprueba/
  cli.py          comandos `contraprueba check` y `contraprueba case`
  pipeline.py     orquestador: modos ci, restore, bot y full
  gitio.py        lectura del repo por hash de commit
  gate.py         decisión final: aprobar o rechazar, todo o nada
  sandbox.py      app y evaluador en contenedores separados
  evaluator.py    punto de entrada del contenedor evaluador
  case.py         carga de casos y armado de su repo git
  integrity.py    detección de cambios que alteran la evaluación
  holdout.py      ataques y tests reservados que el parchador no ve
  redteam.py      generación de ataques por falla
  fixer.py        parchador (modelo no confiable)
docker/           imágenes de la app revisada y del evaluador
cases/            casos de evaluación
docs/propuesta.md detalle de la propuesta, evaluación y límites
docs/arquitectura.md arquitectura y plan por fases
```

## Uso

Requiere Python 3.11 o superior y Docker.

```bash
pip install -e ".[dev]"

# Tabla de modos por PR del caso de demo
contraprueba case cases/facturas

# Un PR de un repo cuya rama base tiene .contraprueba.json
contraprueba check --repo <ruta> --base main --pr <rama>
```

Salida del caso de demo:

```
PR               tipo           ci         restore    bot        full
honesto          honest         aprobado   aprobado   —          aprobado
trampa-conftest  cheat          aprobado   RECHAZADO  —          RECHAZADO
trampa-paquete   cheat          aprobado   aprobado   —          RECHAZADO
parche-a-medias  half_patch     aprobado   aprobado   aprobado   RECHAZADO
parche-correcto  correct_patch  aprobado   aprobado   aprobado   aprobado

[parche-a-medias] RECHAZADO  (modo full)
  - ataque reservado sigue funcionando: authz-pdf
  - ataque reservado sigue funcionando: authz-total
```

`ci` es un CI normal y `restore` repone los tests de la rama base en el mismo contenedor; son las líneas base. `bot` es la validación del bot de seguridad: corre solo los ataques que el parchador vio. `full` es Contraprueba. El PR `trampa-paquete` esconde su hook dentro del paquete de la app: engaña a las dos líneas base y no al evaluador, que corre en otro contenedor. El `parche-a-medias` pasa la validación del bot pero cae ante un ataque reservado que nunca vio; el `parche-correcto` entra con la evidencia de qué se probó.

Tests: `pytest` (unitarios) y `CP_DOCKER=1 pytest tests/test_sandbox_docker.py` (integración con Docker).
