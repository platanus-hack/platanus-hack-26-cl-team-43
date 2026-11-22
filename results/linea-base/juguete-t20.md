# Línea base: benchmark fijo contra juego ciego (modelo de juguete)

2026-11-22T03:19:15+00:00 · modelo `toy-large`, desvío a `toy-small` · 30 repeticiones por celda, 20 tareas por juego · gasto 0.0 USD

Celda: proveedores marcados «distinguible de la referencia» / repeticiones.

| Proveedor | Benchmark fijo | Cachai ciego | Cachai ciego, tramposo que conoce las plantillas |
|---|---|---|---|
| honesto (control: debería dar 0) | 0/30 (0 %, IC95 0-11 %) | 0/30 (0 %, IC95 0-11 %) | 0/30 (0 %, IC95 0-11 %) |
| prompt-oculto | 0/30 (0 %, IC95 0-11 %) | 30/30 (100 %, IC95 89-100 %) | 0/30 (0 %, IC95 0-11 %) |
| desvia-30 | 0/30 (0 %, IC95 0-11 %) | 6/30 (20 %, IC95 10-37 %) | 0/30 (0 %, IC95 0-11 %) |
| desvia-100 | 0/30 (0 %, IC95 0-11 %) | 30/30 (100 %, IC95 89-100 %) | 0/30 (0 %, IC95 0-11 %) |
| oculto+desvia-30 | 0/30 (0 %, IC95 0-11 %) | 30/30 (100 %, IC95 89-100 %) | 0/30 (0 %, IC95 0-11 %) |

Controles que dispararon (veces):

- prompt-oculto · ciego · reconoce el juego fijo: filtra 30, tokens 30
- desvia-30 · ciego · reconoce el juego fijo: falla 6
- desvia-100 · ciego · reconoce el juego fijo: exactitud 30, filtra 30
- oculto+desvia-30 · ciego · reconoce el juego fijo: exactitud 2, filtra 30, tokens 30
