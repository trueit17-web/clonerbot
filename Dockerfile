FROM python:3.12-slim

WORKDIR /app

# System deps kept minimal; ccxt/telethon are pure-python wheels.
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip && pip install .

# Telethon session lives here (mounted volume in compose).
RUN mkdir -p /app/sessions
ENV CLONERBOT_TG_SESSION=/app/sessions/clonerbot_user

CMD ["clonerbot", "run"]
