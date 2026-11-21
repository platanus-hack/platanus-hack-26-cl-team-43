# Notas del orador

Slides: `contraprueba-pitch.pdf`. Guion completo en `docs/superpowers/specs/2026-11-21-demo-design.md`.

## 1. Portada

En pantalla mientras se prepara. No se lee. Al empezar, avanzar a la slide del problema (0:00).

## 2. Problema (0:00–0:15)

«Cada vez más código lo escribe un agente y lo aprueba otra IA, o la misma. Y hacen trampa: con un archivo de nueve líneas un agente saca 100 % en SWE-bench sin arreglar nada. Y un tercio de los parches de seguridad que pasan su propio ataque siguen rotos. Al final, un humano ve el check verde y hace merge.»

## 3. Solución (0:15–0:30)

«Contraprueba es el revisor que no le cree al modelo. Corre los tests desde otro contenedor, donde el código no puede tocarlos, y guarda ataques que el parche nunca vio. Si algo falla, no entra nada. Hoy ninguna de estas defensas lo atrapa. Miren.» → Pasar a la terminal (demo 0:30–2:20).

## 4. AI control (2:20–2:40)

«Esto es AI control aplicado al código. El agente y el bot son modelos no confiables. La decisión la toma un proceso determinista que ningún modelo puede convencer: prueba desde otro contenedor, guarda ataques que el parche nunca vio, y si algo falla no entra nada.»

## 5. Cierre (2:40–3:00)

«Todo lo que vieron corre de verdad y lo pueden reproducir: escaneen el QR. Ninguna IA aprueba el código de otra IA. Gracias.»

## 6. Respaldo

Fin del pitch. Lo que sigue es respaldo: si falla la demo en vivo o para preguntas.

## 7. Respaldo acto 1

Usar solo si la terminal falla. «No arregló nada: metió un archivo que hace mentir a pytest. Evaluamos desde otro contenedor: ahí no puede tocar nada.»

## 8. Respaldo acto 2 (a medias)

«El bot vio dos ataques y los tapó. Su validación: verde. Nosotros guardamos otros que nunca vio. Uno entra: rechazado.»

## 9. Respaldo acto 2 (correcto)

«El segundo parche arregla la falla de verdad: aprobado, y con la evidencia de qué se probó.»

## 10. Cómo funciona

Preguntas: cómo funciona, en cuatro pasos.

## 11. Alcance y límites

Preguntas: alcance, límites y cómo lo medimos.

## 12. Referencias

Preguntas: referencias.
