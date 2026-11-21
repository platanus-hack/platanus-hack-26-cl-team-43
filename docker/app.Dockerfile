# Imagen de la app que se revisa. El contexto es el checkout de la rama del PR
# (no confiable); el comando de arranque lo pone Cachai al correrla.
#
# Se construye con `docker build --network none`: las dependencias salen de
# .cachai-wheels, que Cachai descargó antes como wheels (binarios, sin correr
# ningún setup.py). Un paquete local del PR se construye acá, sin red.
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir --no-index --find-links /app/.cachai-wheels -r requirements.txt \
    && rm -rf /app/.cachai-wheels && useradd --create-home app
USER app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
