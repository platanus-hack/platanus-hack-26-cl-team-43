#!/bin/bash
# Ollama con los tres modelos cargados y una petición a la vez por modelo (a temperatura 0,
# el batching entre peticiones puede cambiar la salida y ensuciar el piso de ruido).
export OLLAMA_MAX_LOADED_MODELS=3 OLLAMA_KEEP_ALIVE=-1 OLLAMA_NUM_PARALLEL=1
ollama serve > /tmp/ollama.log 2>&1 &
until curl -s 127.0.0.1:11434/api/version > /dev/null; do sleep 1; done
# Hilos por modelo. Sin tope, cada modelo abre un hilo por núcleo del host y no por el cupo de
# CPU del contenedor: con dos modelos respondiendo a la vez el contenedor pasa estrangulado y
# un pedido de 2 segundos tarda minutos. El tope va en un modelo con nombre propio: recrear el
# modelo con su mismo nombre no aplica el parámetro.
HILOS="${CACHAI_THREADS:-8}"
crear() {  # crear NOMBRE BASE
  printf 'FROM %s\nPARAMETER num_thread %s\n' "$2" "$HILOS" > /tmp/Modelfile
  ollama create "$1" -f /tmp/Modelfile
}
crear cachai-ref qwen2.5:1.5b-instruct-q8_0
crear cachai-q4 qwen2.5:1.5b-instruct-q4_K_M
crear cachai-chico qwen2.5:0.5b-instruct-q8_0
export CACHAI_REF_MODEL=cachai-ref CACHAI_Q4_MODEL=cachai-q4 CACHAI_SMALL_MODEL=cachai-chico
# precarga: la primera auditoría no paga el tiempo de cargar los modelos
for m in cachai-ref cachai-q4 cachai-chico; do
  curl -s 127.0.0.1:11434/api/generate -d "{\"model\":\"$m\",\"prompt\":\"hi\",\"stream\":false,\"options\":{\"num_predict\":1}}" > /dev/null
done
ps -eo args | grep -o "llama-server.*--threads [0-9]*" | sed 's/.*--threads/hilos por modelo:/' || true
exec uv run --no-sync cachai serve --host 0.0.0.0 --port "${PORT:-8000}"
