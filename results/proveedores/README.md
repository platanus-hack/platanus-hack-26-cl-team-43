# Proveedores reales: primera corrida

`cachai scan z-ai/glm-5.3` sobre OpenRouter, el 22-11-2026: los cinco endpoints más baratos de GLM-5.3 contra el del autor (Z.AI), con el juego ciego de 48 pruebas y la semilla `1410046408`. Costó USD 0,05.

- Ficha: [`ficha-z-ai-glm-5-3-2026-11-22t06-15-11z.md`](ficha-z-ai-glm-5-3-2026-11-22t06-15-11z.md)
- Respuestas crudas: `ficha-z-ai-glm-5-3-2026-11-22t06-15-11z.evidencia.jsonl` (su SHA-256 está en la ficha)

Es una corrida preliminar. La ficha se publica tal como salió, y hay que leerla junto con las respuestas crudas.

## Lo que sí se puede afirmar

- **Ningún endpoint agrega un prompt oculto.** Los cinco cuentan los mismos tokens de prompt que la referencia en los seis mensajes mínimos.
- **Las tool calls cumplen el esquema** en los cinco: 4 de 4.
- **Cuatro de los cinco no se distinguen de la referencia en las tareas** (los dos de Wafer, Decart y Novita): 19 o 20 de 20 correctas.
- **SiliconFlow corta las respuestas en la mitad del límite pedido.** Todas sus respuestas cortadas terminan en la mitad exacta de `max_tokens`, en los cuatro tipos de prueba: 128 de 256, 32 de 64, 24 de 48 y 4 de 8 (35 respuestas). GLM-5.3 razona antes de responder y ese razonamiento gasta el límite, así que la respuesta queda trunca: «138» donde la referencia responde «138364». Por eso la ficha lo marca con 9 de 20 tareas correctas. Es una diferencia real del endpoint, pero no es evidencia de otro modelo. No verificamos si el corte lo hace el proveedor o el adaptador de OpenRouter.

## Lo que esta corrida no permite afirmar

- **La referencia quedó incompleta.** Z.AI limita las cuentas nuevas de OpenRouter a 20 pedidos por minuto en este modelo. La referencia falló 18 de 48 pruebas y su segunda pasada (el piso de ruido) 44 de 48, todas con error 429. Por eso «texto libre igual» dice «sin datos» y el piso aparece marcado: es un artefacto del límite, no una medición.
- **La columna del secreto no es comparable.** La referencia no llegó a responder esas pruebas. Además, GLM-5.3 no permite apagar el razonamiento, y en varios endpoints el razonamiento gastó los 64 tokens del límite y la respuesta salió vacía, lo que cuenta como «no filtró».

## Qué cambió en el auditor después de esta corrida

- Un error 429 ya no cuenta como tarea fallida: el auditor espera y repite la misma prueba.
- `--extra-tokens N` agrega margen de salida a cada prueba, para modelos que razonan siempre.

Para repetirla con esos cambios (tarda unos minutos por el límite de la referencia):

```bash
cachai scan z-ai/glm-5.3 --seed 1410046408 --extra-tokens 512
```
