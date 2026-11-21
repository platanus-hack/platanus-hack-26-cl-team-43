# Imagen de la app que se revisa: la de dependencias (docker/deps.Dockerfile)
# más el código. El contexto es el checkout de la rama del PR (no confiable); el
# comando de arranque lo pone Cachai al correrla.
#
# El build corre con `docker build --network none`: la imagen base ya trae las
# dependencias instaladas desde wheels y acá solo se copia el código.
ARG DEPS_IMAGE
FROM ${DEPS_IMAGE}
WORKDIR /app
COPY . .
USER app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
