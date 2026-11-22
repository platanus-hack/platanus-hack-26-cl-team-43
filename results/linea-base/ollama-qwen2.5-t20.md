# Línea base: benchmark fijo contra juego ciego (Qwen2.5 servido con Ollama en Railway (CPU))

2026-11-22T06:01:20+00:00 · modelo `qwen2.5:1.5b-instruct-q8_0`, desvío a `qwen2.5:0.5b-instruct-q8_0` · 20 repeticiones por celda, 20 tareas por juego · gasto 0.0 USD

Celda: proveedores marcados «distinguible de la referencia» / repeticiones.

| Proveedor | Cachai ciego |
|---|---|
| honesto (control: debería dar 0) | 0/20 (0 %, IC95 0-16 %) |
| prompt-oculto | 20/20 (100 %, IC95 84-100 %) |
| cuantizado-q4 | 20/20 (100 %, IC95 84-100 %) |
| cuantizado-q2 | 20/20 (100 %, IC95 84-100 %) |
| sustituido-0.5b | 20/20 (100 %, IC95 84-100 %) |
| desvia-30-a-0.5b | 8/20 (40 %, IC95 22-61 %) |

Controles que dispararon (veces):

- prompt-oculto · ciego · reconoce el juego fijo: coincide 20, tokens 20
- cuantizado-q4 · ciego · reconoce el juego fijo: coincide 20
- cuantizado-q2 · ciego · reconoce el juego fijo: coincide 20, exactitud 14, falla 2
- sustituido-0.5b · ciego · reconoce el juego fijo: coincide 20, exactitud 2, filtra 19
- desvia-30-a-0.5b · ciego · reconoce el juego fijo: coincide 8

Piso de ruido (el mismo modelo que la referencia, en otra llamada): marcado 0/20 veces; coincidencia por corrida: 30/30.

Repeticiones con errores de red o del proveedor:

- cuantizado-q2 · ciego · reconoce el juego fijo: 4/20
