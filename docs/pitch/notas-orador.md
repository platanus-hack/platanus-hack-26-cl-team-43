# Notas del orador

Slides: `contraprueba-pitch.pdf`. Guion completo en `docs/superpowers/specs/2026-11-21-demo-design.md`.

## 1. Portada

En pantalla mientras se prepara. No se lee. Al empezar, avanzar a la slide del problema (0:00).

## 2. Problema (0:00–0:15)

«Cada vez más código lo escribe un agente y lo aprueba otra IA, o la misma. Y hacen trampa: con un archivo de nueve líneas un agente saca 100 % en SWE-bench sin arreglar nada. Y un tercio de los parches de seguridad que pasan su propio ataque siguen rotos. Al final, un humano ve el check verde y hace merge.»

## 3. Solución (0:15–0:30)

«Contraprueba es el revisor que no le cree al modelo. Corre los tests desde otro contenedor, donde el código no puede tocarlos, y guarda ataques que el parche nunca vio. Si algo falla, no entra nada. Lo medimos sobre 19 PR: el CI normal no atrapa ninguna trampa; restaurar los tests, 5 de 8; y ninguna de las defensas actuales atrapa un parche a medias. Contraprueba atrapa todo. Miren.» → Pasar a la terminal (demo 0:30–2:20). Si preguntan por la falsa alarma: un cambio legítimo a un test existente; la política bloquea todo cambio a tests existentes y pide revisión humana.

## 4. AI control (2:20–2:40)

«Esto es AI control aplicado al código. El agente y el bot son modelos no confiables. La decisión la toma un proceso determinista que ningún modelo puede convencer: prueba desde otro contenedor, guarda ataques que el parche nunca vio, y si algo falla no entra nada.»

## 5. Cierre (2:40–3:00)

«Todo lo que vieron corre de verdad y lo pueden reproducir: escaneen el QR. Ninguna IA aprueba el código de otra IA. Gracias.»

## 6. Respaldo

Fin del pitch. Lo que sigue es respaldo: si falla la demo en vivo o para preguntas.

## 7. Respaldo acto 1

Usar solo si la terminal falla. Salida real del caso facturas, PR trampa-paquete. «Le pedimos agregar el descuento por pronto pago. Rompió el impuesto y lo tapó con un módulo dentro del paquete que parcha los reportes de pytest. El CI normal y hasta restaurar los tests dan verde. Desde otro contenedor no puede tocar nada: rechazado, con archivo y línea.»

## 8. Respaldo acto 2 (a medias)

Salida real de parche-a-medias (results/bench.json). «El bot vio diez ataques y los tapó: su validación da verde. Arregló dos rutas y una forma de ruta de archivo. Nosotros guardamos otros 19 que nunca vio; 11 siguen entrando por /total, /v2, /export y ./../. Rechazado.»

## 9. Respaldo acto 2 (correcto)

«El segundo parche arregla la falla de verdad: aprobado, y con la evidencia de qué se probó.» Salida real de parche-correcto: comprueba el dueño en un solo lugar para todas las rutas y confina los adjuntos a la carpeta del usuario.

## 10. Con agentes reales

Preguntas: ¿funciona con modelos de verdad? Con el encargo normal, 8 de 8 parches reales se aprobaron. Con un parchador instruido para hacer el cambio mínimo, 3 de 3 pasaron su propia validación y Contraprueba los rechazó.

## 11. Cómo funciona

Preguntas: cómo funciona, en cuatro pasos.

## 12. Alcance y límites

Preguntas: alcance, límites y cómo lo medimos.

## 13. Referencias

Preguntas: referencias.
