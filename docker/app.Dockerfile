# Imagen de la app que se revisa. El contexto es el checkout de la rama del PR
# (no confiable); el comando de arranque lo pone Contraprueba al correrla.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home app
COPY . .
USER app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
