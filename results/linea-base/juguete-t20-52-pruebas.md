# Línea base: benchmark fijo contra juego ciego (modelo de juguete)

2026-11-22T07:36:14+00:00 · modelo `toy-large`, desvío a `toy-small` · 30 repeticiones por celda, 20 tareas por juego · gasto 0.0 USD

Celda: proveedores marcados «distinguible de la referencia» / repeticiones.

| Proveedor | Benchmark fijo | Cachai ciego | Cachai ciego, tramposo que conoce las plantillas |
|---|---|---|---|
| honesto (control: debería dar 0) | 0/30 (0 %, IC95 0-11 %) | 0/30 (0 %, IC95 0-11 %) | 0/30 (0 %, IC95 0-11 %) |
| prompt-oculto | 0/30 (0 %, IC95 0-11 %) | 30/30 (100 %, IC95 89-100 %) | 0/30 (0 %, IC95 0-11 %) |
| desvia-30 | 0/30 (0 %, IC95 0-11 %) | 12/30 (40 %, IC95 25-58 %) | 0/30 (0 %, IC95 0-11 %) |
| desvia-100 | 0/30 (0 %, IC95 0-11 %) | 30/30 (100 %, IC95 89-100 %) | 0/30 (0 %, IC95 0-11 %) |
| oculto+desvia-30 | 0/30 (0 %, IC95 0-11 %) | 30/30 (100 %, IC95 89-100 %) | 0/30 (0 %, IC95 0-11 %) |

Controles que dispararon (veces):

- prompt-oculto · ciego · reconoce el juego fijo: filtra 30, tokens 30
- desvia-30 · ciego · reconoce el juego fijo: falla 12
- desvia-100 · ciego · reconoce el juego fijo: coincide 30, exactitud 30, filtra 30, tool 30
- oculto+desvia-30 · ciego · reconoce el juego fijo: coincide 1, exactitud 2, filtra 30, tokens 30

Piso de ruido (el mismo modelo que la referencia, en otra llamada): marcado 0/90 veces; coincidencia por corrida: 30/30.
