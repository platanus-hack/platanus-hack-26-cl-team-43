# Proveedores reales: GLM-5.3 en OpenRouter

`cachai scan z-ai/glm-5.3` sobre OpenRouter, el 22-11-2026: los cinco endpoints más baratos de GLM-5.3 (de cuatro proveedores: Wafer tiene dos) contra el del autor (Z.AI), con el juego ciego y la semilla `1410046408`. Hay dos corridas:

| Corrida | Juego | Ficha | Estado |
|---|---|---|---|
| 07:31 UTC | 52 pruebas, con `--extra-tokens 512` | [`ficha-z-ai-glm-5-3-2026-11-22t07-31-56z.md`](ficha-z-ai-glm-5-3-2026-11-22t07-31-56z.md) | **Completa**: 364 respuestas, ningún error. USD 0,16 |
| 06:15 UTC | 48 pruebas | [`ficha-z-ai-glm-5-3-2026-11-22t06-15-11z.md`](ficha-z-ai-glm-5-3-2026-11-22t06-15-11z.md) | Preliminar: la referencia quedó a medias. USD 0,05 |

Cada ficha tiene al lado sus respuestas crudas (`.evidencia.jsonl`) y el SHA-256 de ese archivo. Las fichas se publican tal como salieron y hay que leerlas junto con las respuestas.

## Corrida completa: lo que se puede afirmar

- **Ningún endpoint agrega un prompt oculto.** Los cinco cuentan los mismos tokens de prompt que la referencia en los seis mensajes mínimos.
- **Ninguno se distingue de la referencia** con esta muestra. En las tareas, cuatro aciertan 20 de 20 y SiliconFlow 19 de 20.
- **Ninguno reveló el secreto del system prompt** (0 de 8). En cinco de las siete filas, entre una y tres de esas respuestas salieron vacías porque el razonamiento gastó el límite; una respuesta vacía cuenta como «no reveló».
- **Las tool calls con un objeto anidado se dividen en dos grupos.** La herramienta `emit_alert` declara `target` como un objeto (con `$ref`). Los endpoints de Wafer y Decart lo devuelven como objeto y cumplen 8 de 8. El endpoint del autor (Z.AI), SiliconFlow y Novita devuelven `target` como un texto con JSON adentro, en las tres llamadas: 5 de 8. Como la referencia es el endpoint del autor, la ficha no marca a nadie: los que cumplen el esquema son los que se apartan de ella, y con 8 llamadas la diferencia no llega al umbral (p = 0,2). Un agente que valide el esquema fallaría esas llamadas en los tres endpoints declarados como fp8, incluido el oficial.
- **SiliconFlow sigue cortando en la mitad del límite pedido** (4 de 8 y 384 de 768 tokens). Con margen de tokens ya no trunca las respuestas y pasa de 9 a 19 de 20, lo que confirma que el 9 de 20 de la corrida preliminar era truncamiento y no otro modelo. No verificamos si el corte lo hace el proveedor o el adaptador de OpenRouter.

## Corrida completa: lo que no se puede afirmar

- **El texto libre no sirve con este modelo.** La referencia consultada dos veces no repitió ninguna de sus 10 frases (piso de ruido: 0 de 10). GLM-5.3 razona siempre y no es determinista a temperatura 0, así que ningún endpoint coincide y eso no significa nada. Sin el piso de ruido, los cinco habrían salido marcados.
- **No detectar no es descartar.** Tres de los endpoints declaran cuantización fp4 y con esta muestra no se distinguen del fp8 del autor en las tareas. El control que delata una cuantización es justamente el de texto libre, que aquí no tiene potencia.

## Corrida preliminar: qué pasó

- Z.AI limita las cuentas nuevas de OpenRouter a 20 pedidos por minuto en este modelo. La referencia falló 18 de 48 pruebas y su segunda pasada 44 de 48, todas con error 429. Por eso «texto libre igual» dice «sin datos» y el piso aparece marcado: es un artefacto del límite.
- SiliconFlow salió «DISTINGUIBLE» con 9 de 20 tareas: todas sus respuestas cortadas terminan en la mitad exacta de `max_tokens` (128 de 256, 32 de 64, 24 de 48, 4 de 8) y el razonamiento gastó ese límite: «138» donde la referencia responde «138364».
- Después de esa corrida el auditor espera y repite ante un 429 en vez de contarlo como falla, y `--extra-tokens N` da margen de salida a los modelos que razonan siempre.

## Cómo repetirlas

La semilla genera las mismas pruebas solo con la misma versión del juego. La corrida completa se hizo con el commit `532804d` y la preliminar con una versión anterior, de 48 pruebas.

```bash
uvx --from git+https://github.com/platanus-hack/platanus-hack-26-cl-team-43@532804d \
  cachai scan z-ai/glm-5.3 --seed 1410046408 --extra-tokens 512
```

Tarda unos 7 minutos con una cuenta nueva, por el límite de pedidos de la referencia.
