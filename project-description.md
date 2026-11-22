# Cachai

**¿Cachai qué modelo te están sirviendo?**

Cachai es el auditor de modelos servidos como *token-as-a-service*. Mide los endpoints que sirven un mismo modelo abierto y le dice a quien compra tokens cuáles se apartan de la referencia.

## El problema

Los modelos abiertos (GLM, DeepSeek, Kimi, Qwen) los sirven decenas de proveedores con el mismo nombre y precios muy distintos. Consultado el 21-nov-2026 en la API pública de OpenRouter, GLM-5.3 tiene 47 endpoints: 14 no declaran con qué cuantización lo sirven y el precio de salida varía 4,7 veces.

Quien compra no puede comprobar qué le están sirviendo: si el modelo es el declarado, si está degradado o si el proveedor agrega instrucciones propias delante de su pedido. Las certificaciones que existen, como SOC 2 o ISO 27001, revisan procesos de la empresa y no cubren nada de eso.

Esto es un problema de seguridad de IA: las evaluaciones de seguridad de un modelo valen para unos pesos y una configuración. Si no se puede verificar qué se sirve, la evaluación no cubre lo que está desplegado.

## Para quién

Equipos que ya gastan en tokens de modelos abiertos a través de varios proveedores y tienen agentes en producción. Lo usa quien elige proveedor, para decidir a cuál mandarle tráfico y a cuál sacarlo.

Lo paga quien compra los tokens, no quien los vende. Así el auditado no financia su propia auditoría.

## Qué hace

`cachai scan` le manda el mismo juego de pruebas a cada endpoint y a una referencia, y escribe una ficha por corrida con:

- **Tokens de prompt de sobra.** Un excedente constante es la firma de un prompt oculto.
- **Exactitud** en tareas con respuesta calculable, comparada con la referencia con un test estadístico.
- **Tool calls válidas** contra un esquema estricto.
- **Secreto del system prompt:** si el endpoint lo filtra cuando se lo piden.
- **Confiabilidad:** errores y latencia.
- **Lo declarado:** cuantización y precio, tal como los publica cada proveedor.

Las pruebas se generan con una semilla nueva en cada auditoría, así un proveedor no puede reconocerlas de antemano. La semilla queda en la ficha junto con el hash de las respuestas crudas, para que cualquiera pueda volver a correr la misma auditoría.

## Qué muestra la demo

El villano es un proveedor tramposo propio: un proxy que agrega un prompt oculto, desvía parte del tráfico a un modelo más barato y se porta bien cuando reconoce los prompts de una auditoría pública. La auditoría de prompts fijos no lo marca; la auditoría ciega de Cachai sí.

## Qué no promete

- Cada dato de la ficha dice si está declarado por el proveedor o medido desde afuera. "Sin desviación detectada" no significa "es el mismo modelo".
- No certifica privacidad: desde afuera no se puede probar que un proveedor no mire ni guarde los datos.
- "Distinguible de la referencia" no es una acusación. Puede deberse a la plantilla, al parser o a la cuantización.

## Qué hay en el repo

- **Cachai auditor** (`src/radar/audit/`): el comando `cachai`, los controles y el proveedor tramposo.
- **Jailbreak Radar** (`src/radar/`): recolecta prompts de jailbreak públicos y los prueba contra modelos. El auditor reutiliza su cliente y su prueba de canario.
- **Blog de reportes** (`cachai/`): plataforma donde se publican las fichas.
