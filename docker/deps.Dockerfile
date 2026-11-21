# Imagen de dependencias de la app por defecto. El contexto trae el
# requirements.txt y sus wheels (o el árbol completo si hay dependencias
# locales que construir); nunca se reutiliza si el PR cambió las dependencias.
#
# Se construye con `docker build --network none`: los wheels los descargó antes
# Cachai, sin correr ningún setup.py del paquete.
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir --no-index --find-links /app/.cachai-wheels -r requirements.txt \
    && rm -rf /app/.cachai-wheels && useradd --create-home app
USER app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
