# La demo en vivo (cachai serve): Ollama con Qwen2.5 dentro de la imagen y la página.
# Railway: variable RAILWAY_DOCKERFILE_PATH=deploy/demo/Dockerfile, build desde la raíz.
FROM ollama/ollama:latest
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
# los modelos quedan en la imagen: el servicio arranca sin bajar nada
RUN (ollama serve > /tmp/serve.log 2>&1 &) && sleep 5 \
 && ollama pull qwen2.5:1.5b-instruct-q8_0 \
 && ollama pull qwen2.5:1.5b-instruct-q4_K_M \
 && ollama pull qwen2.5:0.5b-instruct-q8_0
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --python 3.12
COPY deploy/demo/run.sh ./
ENTRYPOINT ["/bin/bash", "/app/run.sh"]
