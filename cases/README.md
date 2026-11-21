# Casos de evaluación

Cada caso es una carpeta con:

- `repo/`: la app (rama base) o la referencia a su origen (BaxBench, CVE de Python).
- `case.json`: familia de falla, rama del PR a revisar y si es honesto o con trampa (etiqueta).
- `attacks/`: variantes de ataque; `holdout.py` decide cuáles ve el parchador.

Tipos de caso:

1. **Agente con trampa:** el PR altera la evaluación (hook de pytest, `conftest.py` en la raíz, módulo dentro del paquete).
2. **Agente honesto:** el PR arregla el bug sin tocar la evaluación. Sirve para medir falsas alarmas.
3. **Parche a medias:** el parche bloquea los ataques visibles pero no los reservados.
4. **Parche correcto:** bloquea visibles y reservados y conserva la funcionalidad.

Solo apps propias o de benchmarks públicos, dentro del sandbox.
