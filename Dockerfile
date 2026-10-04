FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    TIDDL_PATH=/app \
    HOME=/app

WORKDIR /app

# Instalar FFmpeg (necesario para remuxar FLAC y preescuchas DASH de Tidal) y utilidades
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Crear carpeta temporal y dar permisos completos para usuarios no-root (Hugging Face Spaces uid 1000 y Render)
RUN mkdir -p /app/temp_downloads && \
    cp /app/tiddl.json /root/tiddl.json 2>/dev/null || true && \
    chmod -R 777 /app

EXPOSE 7860

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-7860}"]

