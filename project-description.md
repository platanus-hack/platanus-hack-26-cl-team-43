# Cachai

**¿Cachai qué modelo te están sirviendo?**

Cachai audita desde afuera los modelos abiertos servidos como *token-as-a-service*. Detecta cuándo un endpoint se aparta de la referencia: un prompt oculto, un modelo sustituido o degradado, o un proveedor que se porta bien solo cuando reconoce la auditoría.

## El problema

Las evaluaciones de seguridad de un modelo (red teaming, evaluaciones de riesgo, salvaguardas) se hacen sobre ese modelo y esa configuración. Pero un modelo abierto (GLM, DeepSeek, Kimi, Qwen) llega a producción a través de proveedores de inferencia, y decenas lo sirven con el mismo nombre. Consultado el 21-nov-2026 en la API pública de OpenRouter, GLM-5.3 tiene 47 endpoints y 14 no declaran con qué cuantización lo sirven.

Quien usa el modelo no puede comprobar qué le responde: si es el modelo declarado, si está degradado o si el proveedor agrega instrucciones propias delante de su pedido. Si el endpoint sirve otra cosa, las evaluaciones del modelo original ya no cubren lo que está en producción. Y un proveedor que agrega instrucciones ocultas es un ataque a la cadena de suministro. Las certificaciones que existen, como SOC 2 o ISO 27001, revisan procesos de la empresa y no cubren nada de eso.

Además, el auditado puede reconocer la auditoría. Los benchmarks públicos usan pruebas fijas y conocidas, y un proveedor que las reconoce puede portarse bien solo con ellas.

## Para quién

Equipos con agentes en producción sobre modelos abiertos servidos por terceros. Lo usa quien responde por lo que hacen esos agentes, para saber si un endpoint se aparta del modelo que evaluó y sacarlo del tráfico.

La auditoría corre del lado de quien usa el modelo y no la financia quien lo sirve. Así el auditado no paga su propia auditoría.

## Qué hace

`cachai scan` le manda el mismo juego de pruebas a cada endpoint y a una referencia, y escribe una ficha por corrida con:

- **Tokens de prompt de sobra.** Un excedente constante es la firma de un prompt oculto.
- **Exactitud** en tareas con respuesta calculable, comparada con la referencia con un test estadístico.
- **Tool calls válidas** contra un esquema estricto.
- **Secreto del system prompt:** si el endpoint lo filtra cuando se lo piden.
- **Confiabilidad:** errores y latencia.
- **Lo declarado:** cuantización y precio, tal como los publica cada proveedor.

Los controles son deterministas, sin juez LLM: la respuesta auditada no puede inyectar al auditor. Las pruebas se generan con una semilla nueva en cada auditoría, así un proveedor no puede reconocerlas de antemano. La semilla queda en la ficha junto con el hash de las respuestas crudas, para que cualquiera pueda volver a correr la misma auditoría.

## Qué muestra la demo

El villano es un proveedor tramposo propio: un proxy que agrega un prompt oculto, desvía parte del tráfico a un modelo más barato y se porta bien cuando reconoce los prompts de una auditoría pública. La auditoría de prompts fijos no lo marca; la auditoría ciega de Cachai sí. En el experimento de línea base, el benchmark fijo no lo detecta en ninguna de 120 auditorías con trampa; el juego ciego lo marca en todas, salvo el desvío parcial cuando las tareas son pocas.

## Qué no promete

- Cada dato de la ficha dice si está declarado por el proveedor o medido desde afuera. "Sin desviación detectada" no significa "es el mismo modelo".
- No certifica privacidad: desde afuera no se puede probar que un proveedor no mire ni guarde los datos.
- "Distinguible de la referencia" no es una acusación. Puede deberse a la plantilla, al parser o a la cuantización.

## Qué hay en el repo

- **Auditor** (`src/cachai/`): el comando `cachai`, los controles, la ficha y el proveedor tramposo de la demo.
- **Experimento de línea base** (`results/linea-base/`): benchmark de prompts fijos contra el juego ciego.
- **Diseño** (`docs/arquitectura.md`): modelo de amenaza, decisiones, medición y riesgos.
