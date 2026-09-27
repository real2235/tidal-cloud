FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    TIDDL_PATH=/app

WORKDIR /app

# Instalar FFmpeg (necesario para remuxar FLAC de Tidal) y utilidades
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Asegurar que la configuración esté disponible tanto en /app como en /root
RUN cp /app/tiddl.json /root/tiddl.json 2>/dev/null || true

EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
