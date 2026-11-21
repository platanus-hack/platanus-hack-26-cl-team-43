# Preguntas que esperamos

Respuestas cortas, con lo que está probado y lo que no. Los números salen de [`results/resultados.md`](../results/resultados.md).

## ¿Esto no lo resuelve ya una herramienta existente, o las salvaguardas del propio laboratorio?

Medimos las tres defensas habituales sobre los mismos 19 cambios:

- Un CI normal aprobó las 8 trampas.
- Restaurar los tests de la rama base, que es lo que hacen los benchmarks, atrapó 5 de 8. Las otras 3 no tocan los archivos de test: viven dentro del paquete de la app o en su comportamiento.
- La validación que un bot parchador se hace a sí mismo aprobó los 3 parches malos.

Las salvaguardas de un laboratorio actúan sobre lo que el modelo dice o hace mientras trabaja. Contraprueba actúa después, sobre el resultado, y no depende de que el modelo coopere ni de quién lo entrenó.

No conocemos una herramienta que combine evaluador fuera del alcance del código, ataques que el parchador no vio y decisión sin ningún modelo. Si existe, se puede sumar como una fila más del mismo banco.

## ¿Su métrica se puede engañar con el mismo truco que atrapan?

Es la pregunta que más trabajamos, y la respuesta tiene dos partes.

Lo que cerramos: el evaluador no comparte proceso, contenedor ni archivos con el código que evalúa. La configuración, los tests y los ataques vienen de la rama base o de fuera del repo. Y la regla es que sin evidencia no hay aprobado: si algo no se pudo ejecutar, se rechaza y además no se cuenta como detección.

Lo que probamos a propósito: parches que intentan parecer bloqueados sin arreglar nada. Responder con otro código HTTP, redirigir la lectura a otra ruta, negarle el servicio al cliente del evaluador, esconder el dato con escapes de JSON. Los cuatro se rechazan y quedaron como tests.

Lo que no cerramos está en la última pregunta.

## ¿Qué pasa si el atacante sabe que el sistema existe?

Suponemos que lo sabe: el adversario es el modelo y el código es público. Saberlo no le alcanza, porque lo que necesita no está a su alcance:

- No ve los ataques ni los tests reservados. Viven fuera del repo, y al agente parchador lo corremos aislado, sin acceso a ellos ni a las soluciones.
- No puede tocar al evaluador: corre en otro contenedor con una imagen que no se construye con nada del PR.
- No puede cambiar las reglas: un PR que toca la configuración de la evaluación se rechaza.

Lo que sí puede hacer un modelo que sabe: escribir una app que se porte bien durante la evaluación y cambie después. Eso no lo detectamos.

## ¿Quién lo adopta o lo paga primero?

Equipos que ya dejan que un agente abra PR o que un bot proponga parches de seguridad, y que hoy los integran mirando el check verde. Para ellos es un check obligatorio más antes del merge, que no pide confiar en ningún proveedor de modelos.

El segundo uso es para quienes construyen benchmarks y entornos de entrenamiento: el mismo evaluador separado evita que un agente saque puntaje alterando la evaluación.

## ¿Por qué es AI safety y no solo seguridad o un producto?

Porque el adversario es el modelo. Es AI control aplicado al código: se asume que el agente y el parchador pueden hacer trampa a propósito, y se pone un monitor confiable que funciona aunque no cooperen (Greenblatt et al., 2023).

Y porque la trampa con los tests no es un problema menor de calidad. Hay evidencia de que un modelo que aprende a hacer trampa en entornos de programación generaliza a engaño y sabotaje (MacDiarmid et al., 2025). Un check que no se deja engañar quita la recompensa por hacer trampa.

## ¿Qué se rompe a escala o con otro modelo?

- **Otro modelo:** la decisión no usa ningún modelo, así que no cambia. Probamos parches de Claude Sonnet, Claude Haiku y Codex con el mismo revisor.
- **Otras apps:** hoy cubre APIs web en Python que se prueban por HTTP y dos familias de falla. El generador de ataques solo conoce rutas GET con un id en la ruta o en la consulta. Tests unitarios que corren en el mismo proceso que el código quedan fuera.
- **Costo:** unos 6 segundos por revisión en la app de demo, casi todo en levantar contenedores. No medimos apps más grandes: ahí crecen tanto la construcción de la imagen como la evaluación, con más tests, más ataques y respuestas más lentas.
- **Falsas alarmas:** rechazó 1 de 8 cambios legítimos, porque bloquea todo cambio a un test existente. A escala, esa política pide una vía de revisión humana.

## ¿Qué no probaron?

- Casos de terceros. Los 19 cambios están escritos a mano sobre una sola app; no adaptamos BaxBench ni CVE reales.
- Con qué frecuencia un modelo deja un parche a medias por su cuenta. Con el encargo normal, los 8 parches de modelos salieron bien. Los 3 parches a medias de un modelo real vienen de un parchador instruido para hacer lo mínimo, y así está declarado.
- Fugas ofuscadas. La fuga se detecta buscando en claro textos que solo el dueño debería ver; un parche que codifique lo que filtra no se detecta.
- Comportamiento que cambia después de la evaluación, por tiempo o por cantidad de pedidos.
- La instalación de dependencias del PR, que corre con red al construir la imagen: se informa, no se aísla.
- Un repositorio git manipulado a mano. Ignoramos reemplazos e injertos, pero en producción hay que revisar un clon limpio del remoto.
