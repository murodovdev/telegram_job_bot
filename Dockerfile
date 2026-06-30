# syntax=docker/dockerfile:1
# ──────────────────────────────────────────────────────────────────
#  Korea Ish E'lonlari — production image
#  No ARG/ENV for secrets: all sensitive values are injected at
#  runtime by Railway's environment variable system, never baked
#  into the image layers.
# ──────────────────────────────────────────────────────────────────

FROM python:3.11-slim

# Keeps Python from buffering stdout/stderr so Railway sees logs instantly
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies first — cached unless requirements.txt changes
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

# Copy source (generate_session.py and local dev files excluded via .dockerignore)
COPY . .

CMD ["python", "main.py"]
