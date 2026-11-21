# Propuesta: Cachai

## Idea

Ninguna IA aprueba el código de otra IA. Cachai es un revisor determinista, fuera del alcance de cualquier modelo, que decide antes del merge si entra el código escrito por un agente o el parche propuesto por un bot de seguridad.

## Modelo de amenaza

- **Modelos no confiables:** el agente de código y el parchador. Pueden equivocarse o hacer trampa para cumplir su métrica (*reward hacking*).
- **Componentes confiables:** el evaluador, los tests y el runner de la rama base, los ataques y tests reservados y la decisión final. Ningún modelo los escribe ni los ejecuta en su entorno.
- **Fuera de alcance:** atacantes externos sobre la infraestructura de Cachai y acciones irreversibles fuera del repo (correos, pushes, datos ya enviados).

## Dos escenarios

**Agente que hace trampa.** Se le pide «arregla el endpoint y que pasen los tests». En vez de arreglarlo, mete un hook que reescribe los resultados de pytest (por ejemplo, un `conftest.py` en la raíz o un módulo dentro del paquete que se importa al correr los tests). El CI normal da verde.

**Parche a medias.** Un bot encuentra que un usuario puede ver las facturas de otro cambiando el identificador en la URL. Su parche bloquea ese ataque exacto, su validación da verde, pero la misma falla sigue abierta por otra ruta o con otro identificador.

## Cómo funciona

1. **Aislamiento del evaluador.** La app del PR corre en un contenedor. El evaluador corre en otro y le hace pedidos por HTTP con los tests de la rama base. Restaurar los tests en el mismo contenedor no basta: un hook dentro del paquete sobrevive a esa restauración ([BenchJack](https://arxiv.org/abs/2605.12673)).
2. **Integridad de la evaluación.** Se restauran `conftest.py`, `pytest.ini`, `pyproject.toml` y demás configuración de la rama base; se marcan los archivos nuevos que cambian cómo corre pytest (`conftest.py`, `sitecustomize.py`, `usercustomize.py`, `*.pth`).
3. **Ataques y tests reservados.** Por cada falla, el equipo rojo genera varias variantes de ataque. El parchador recibe algunas; el revisor guarda el resto junto con tests funcionales que el parchador no ve.
4. **Decisión todo o nada.** Aprobado solo si pasan los tests de la base, los ataques reservados fallan y los tests funcionales reservados pasan. Si no, no se integra nada y se entrega la evidencia.

## Evaluación

> Estado: hecha sobre un caso propio (`cases/facturas`, 19 PR etiquetados); ver [`results/resultados.md`](../results/resultados.md). Los casos de ImpossibleBench, BaxBench y CVE de Python descritos abajo son el plan y todavía no están adaptados.

**Casos**
- Agentes con trampa etiquetada: tareas donde pasar los tests solo es posible haciendo trampa (estilo ImpossibleBench) y las variantes de ataque al evaluador de BenchJack, ejecutadas por un agente instruido para atacar, como en los experimentos de AI control.
- Seguridad: apps de BaxBench con vulnerabilidad conocida y CVE de Python con exploit verificable.

**Líneas base**
- CI normal que corre los tests del PR.
- Tests de la base restaurados, pero en el mismo contenedor que el código.
- Parche validado solo con el ataque que vio el parchador.

**Métricas**
- Trampas detectadas y falsas alarmas sobre corridas honestas.
- Parches aprobados por su propia validación que caen con ataques reservados.
- Utilidad conservada: cambios legítimos que se aprueban.

Con pocos casos se reportan intervalos de confianza, no «cero vulnerable».

## Demo

1. El agente hace trampa con un hook de pytest. CI normal: verde. Cachai: rechazado, con archivo y línea.
2. El bot parcha un acceso a datos de otro usuario y su validación da verde. Un ataque reservado sigue entrando: rechazado.
3. Segundo parche: pasa los tests de la base y los reservados. Aprobado.

## Límites

- Tests unitarios que corren dentro del mismo proceso que el código: no hay garantía, solo se marcan para revisión.
- Trampas que no tocan la evaluación (por ejemplo, explotar un verificador débil) no se cubren en esta versión.
- Solo APIs en Python y dos familias de falla: autorización por recurso y acceso indebido a archivos.

## Hitos

- **Hito 1:** un hook de pytest engaña al CI normal y a la restauración de tests en el mismo contenedor, y no engaña al evaluador en otro contenedor.
- **Hito 2:** al menos un parche que pasó su propia validación cae con un ataque reservado.
- **Hito 3:** ciclo completo en la CLI y corridas contra las líneas base.

## Referencias

- Greenblatt et al., *AI Control: Improving Safety Despite Intentional Subversion*, 2023. [arXiv 2312.06942](https://arxiv.org/abs/2312.06942)
- MacDiarmid et al., *Natural Emergent Misalignment from Reward Hacking in Production RL*, 2025. [arXiv 2511.18397](https://arxiv.org/abs/2511.18397)
- Wang et al., *Do Androids Dream of Breaking the Game? Systematically Auditing AI Agent Benchmarks with BenchJack*, 2026. [arXiv 2605.12673](https://arxiv.org/abs/2605.12673)
- Le et al., *On the Reliability of LLM-Based Vulnerability Patching Benchmarks*, 2026. [arXiv 2610.10150](https://arxiv.org/abs/2610.10150)
- *SWE-Bench Pro Verified*, 2026. [arXiv 2609.08149](https://arxiv.org/abs/2609.08149)
- [ControlArena](https://github.com/UKGovernmentBEIS/control-arena) (UK AISI)
- [BaxBench](https://baxbench.com)
