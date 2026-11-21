# Contraprueba

**Ninguna IA aprueba el código de otra IA.**

Cada vez más código lo escriben agentes y bots, y cada vez más lo aprueban ellos mismos. Un agente al que le piden «que pasen los tests» puede alterar cómo corren los tests en vez de arreglar el bug. Un bot de seguridad valida su parche con el mismo ataque que encontró, y muchas veces solo tapa ese caso. El humano ve el check verde y hace merge.

Contraprueba es un revisor determinista que se pone antes del merge y no le cree al modelo:

- **Prueba desde afuera:** la app corre en un contenedor y el evaluador en otro, con los tests de la rama base. El código no puede tocar a quien lo califica.
- **Marca lo que altera la evaluación:** hooks y configuraciones nuevas de pytest.
- **Guarda ataques y tests en secreto:** el parchador nunca los ve, así que un parche que solo tapa el caso conocido se rechaza.
- **Todo o nada:** si algo falla, no entra nada.

Es AI control aplicado al código: los modelos que escriben y parchan son no confiables, y la decisión final la toma un proceso que ningún modelo puede convencer.
