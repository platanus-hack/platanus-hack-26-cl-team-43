# Imagen fija del evaluador. Se construye solo con el código de Cachai,
# nunca con archivos del repo que se está revisando.
FROM python:3.12-slim
RUN pip install --no-cache-dir pytest==8.3.4 httpx==0.28.1
COPY pyproject.toml README.md /opt/cachai/
COPY src /opt/cachai/src
RUN pip install --no-cache-dir /opt/cachai && useradd --create-home evaluator
USER evaluator
ENV PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python", "-m", "cachai.evaluator"]
