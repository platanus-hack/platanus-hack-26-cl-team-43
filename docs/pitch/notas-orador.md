# Notas del orador

Slides: `contraprueba-pitch.pdf`. Guion completo en `docs/superpowers/specs/2026-11-21-demo-design.md`.

## 1. Portada

En pantalla mientras se prepara. No se lee. Al empezar, avanzar a la slide del problema (0:00).

## 2. Problema (0:00–0:15)

«Cada vez más código lo escribe un agente y lo aprueba otra IA, o la misma. Y hacen trampa: con un archivo de nueve líneas un agente saca 100 % en SWE-bench sin arreglar nada. Y un tercio de los parches de seguridad que pasan su propio ataque siguen rotos. Al final, un humano ve el check verde y hace merge.»

## 3. Solución (0:15–0:30)

«Hoy la IA rinde la prueba y se la corrige ella misma. Contraprueba es el profesor que corrige en otra sala, con preguntas que el alumno nunca vio.» «Contraprueba es el revisor que no le cree al modelo. Corre los tests desde otro contenedor, donde el código no puede tocarlos, y guarda ataques que el parche nunca vio. Si algo falla, no entra nada. Lo medimos sobre 19 PR: el CI normal no atrapa ninguna trampa; restaurar los tests, 5 de 8; y ninguna de las defensas actuales atrapa un parche a medias. Contraprueba atrapa todo. Miren.» → Pasar a la terminal (demo 0:30–2:20). Si preguntan por la falsa alarma: un cambio legítimo a un test existente; la política bloquea todo cambio a tests existentes y pide revisión humana.

## Demo en la terminal (`scripts/demo.sh`)

Decir la frase del examen antes de cada comando, así el jurado sabe qué esperar.

### Acto 1 · la trampa (0:30–1:15)

`trampa-paquete: ci → restore → full`

«Este alumno no estudió: cambió el corrector, así todo lo que entrega sale con un 7. El CI normal le cree. Le devolvemos la pauta original, pero si corrige en su mismo puesto sigue sacando un 7, porque escondió otro corrector en la mochila. Contraprueba corrige en otra sala, donde la mochila no llega: un 1, con la pregunta que falló.»

Lo técnico: rompió el impuesto y lo tapó con invoices/_telemetry.py, que parcha los reportes de pytest desde dentro del paquete. ci y restore aprueban; full lo rechaza con tests/test_invoices.py:48 y el archivo.

### Acto 2 · la solución parcial (1:15–1:55)

`parche-a-medias: visible → full`

«Este alumno sí estudió, pero solo se aprendió de memoria la guía de ejercicios que le pasamos. Evaluado con esa misma guía, saca un 7. Contraprueba guardó preguntas que no estaban en la guía, y falla 11. No entendió la materia: tapó los casos que vio.»

Lo técnico: vio 10 ataques y los tapó; de los 19 reservados, 11 siguen entrando por /total, /v2, /export y ./../.

### Acto 2 · la solución total (1:55–2:20)

`parche-correcto: full`

«Este entendió la materia: responde las preguntas que no vio y no olvidó lo que ya sabía. Aprueba, y le entregamos la prueba corregida con la pauta: qué se preguntó y qué respondió.»

Lo técnico: 12 tests de la base, 19 ataques reservados bloqueados, 5 tests funcionales reservados. Si falta tiempo, una línea: «Este entendió la materia: aprueba, con la prueba corregida.»

## 4. AI control (2:20–2:40)

«Esto es AI control aplicado al código. El agente y el bot son modelos no confiables. La decisión la toma un proceso determinista que ningún modelo puede convencer: prueba desde otro contenedor, guarda ataques que el parche nunca vio, y si algo falla no entra nada.»

## 5. Cierre (2:40–3:00)

«Todo lo que vieron corre de verdad y lo pueden reproducir: escaneen el QR. Ninguna IA aprueba el código de otra IA. Gracias.»

## 6. Respaldo

Fin del pitch. Lo que sigue es respaldo: si falla la demo en vivo o para preguntas.

## 7. La analogía del examen

Para preguntas o para reforzar la explicación: cómo se corresponde cada parte de la analogía con el sistema.

## 8. Respaldo acto 1

Usar solo si la terminal falla. Salida real del caso facturas, PR trampa-paquete. «Este alumno no estudió: cambió el corrector, así todo lo que entrega sale con un 7. El CI normal le cree. Le devolvemos la pauta original, pero si corrige en su mismo puesto sigue sacando un 7, porque escondió otro corrector en la mochila. Contraprueba corrige en otra sala, donde la mochila no llega: un 1, con la pregunta que falló.» Lo técnico: rompió el impuesto y lo tapó con un módulo dentro del paquete que parcha los reportes de pytest. El CI normal y hasta restaurar los tests dan verde; desde otro contenedor, rechazado con archivo y línea.

## 9. Respaldo acto 2 (a medias)

Salida real de parche-a-medias (results/bench.json). «Este alumno sí estudió, pero solo se aprendió de memoria la guía de ejercicios que le pasamos. Evaluado con esa misma guía, saca un 7. Contraprueba guardó preguntas que no estaban en la guía, y falla 11. No entendió la materia: tapó los casos que vio.» Lo técnico: vio diez ataques y los tapó, así que su validación da verde. De los 19 reservados, 11 siguen entrando por /total, /v2, /export y ./../.

## 10. Respaldo acto 2 (correcto)

«Este entendió la materia: responde las preguntas que no vio y no olvidó lo que ya sabía. Aprueba, y le entregamos la prueba corregida con la pauta: qué se preguntó y qué respondió.» Salida real de parche-correcto: comprueba el dueño en un solo lugar para todas las rutas y confina los adjuntos a la carpeta del usuario.

## 11. Con agentes reales

Preguntas: ¿funciona con modelos de verdad? Con el encargo normal, 8 de 8 parches reales se aprobaron. Con un parchador instruido para hacer el cambio mínimo, 3 de 3 pasaron su propia validación y Contraprueba los rechazó.

## 12. Cómo funciona

Preguntas: cómo funciona, en cuatro pasos.

## 13. Alcance y límites

Preguntas: alcance, límites y cómo lo medimos.

## 14. Referencias

Preguntas: referencias.
