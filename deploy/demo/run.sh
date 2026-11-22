#!/bin/bash
# Ollama con los tres modelos cargados y una petición a la vez por modelo (a temperatura 0,
# el batching entre peticiones puede cambiar la salida y ensuciar el piso de ruido).
export OLLAMA_MAX_LOADED_MODELS=3 OLLAMA_KEEP_ALIVE=-1 OLLAMA_NUM_PARALLEL=1
ollama serve > /tmp/ollama.log 2>&1 &
until curl -s 127.0.0.1:11434/api/version > /dev/null; do sleep 1; done
MODELOS="qwen2.5:1.5b-instruct-q8_0 qwen2.5:1.5b-instruct-q4_K_M qwen2.5:0.5b-instruct-q8_0"
# Hilos por modelo. Sin tope, cada modelo abre un hilo por núcleo del host y no por el cupo de
# CPU del contenedor: con dos modelos respondiendo a la vez el contenedor pasa estrangulado y
# un pedido de 2 segundos tarda minutos. Con 8 hilos caben tres modelos en un cupo de 32 vCPU.
for m in $MODELOS; do
  printf 'FROM %s\nPARAMETER num_thread %s\n' "$m" "${CACHAI_THREADS:-8}" > /tmp/Modelfile
  ollama create "$m" -f /tmp/Modelfile > /dev/null
done
# precarga: la primera auditoría no paga el tiempo de cargar los modelos
for m in $MODELOS; do
  curl -s 127.0.0.1:11434/api/generate -d "{\"model\":\"$m\",\"prompt\":\"hi\",\"stream\":false,\"options\":{\"num_predict\":1}}" > /dev/null
done
exec uv run --no-sync cachai serve --host 0.0.0.0 --port "${PORT:-8000}"
