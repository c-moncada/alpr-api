# Imagen para Render (o cualquier host con Docker).
#
# Los modelos ONNX se descargan durante el build y quedan dentro de la
# imagen: el contenedor arranca sin bajar nada y sin disco persistente.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Usuario sin privilegios. Las librerías guardan los modelos en ~/.cache,
# así que la precarga tiene que correr como este mismo usuario.
RUN useradd --create-home --uid 1000 app
WORKDIR /home/app/api

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY scripts ./scripts

USER app
# Falla el build si los modelos no quedaron en caché
RUN python scripts/precargar_modelos.py

# Render define PORT; en local cae a 8000
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
