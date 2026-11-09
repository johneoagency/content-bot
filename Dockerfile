FROM python:3.12-slim

# ffmpeg/ffprobe for video, plus CA certs. Pillow wheels are prebuilt.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY prompts ./prompts
COPY tools ./tools

# config.json and secrets/.env are mounted at runtime, not baked in.
ENV PYTHONUNBUFFERED=1

# Healthcheck validates config/connectivity without starting a second consumer.
HEALTHCHECK --interval=60s --timeout=20s --start-period=20s --retries=3 \
    CMD python -m app --check || exit 1

CMD ["python", "-m", "app"]
