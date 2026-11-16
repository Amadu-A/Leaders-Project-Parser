# Dockerfile
# Один образ API/worker; Chromium нужен только worker при клиентском рендеринге.
FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
WORKDIR /app
COPY requirements.lock ./
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.lock
# Нативные chromium-зависимости из Playwright Debian 12; Xvfb для DOM-сбора не нужен.
RUN apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 update \
    && apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 install -y --no-install-recommends \
       libatomic1 libasound2 libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 \
       libcairo2 libcups2 libdbus-1-3 libdrm2 libgbm1 libglib2.0-0 libnspr4 libnss3 \
       libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 \
       libxkbcommon0 libxrandr2 libfontconfig1 libfreetype6 fonts-liberation \
    && useradd --uid 10001 --create-home appuser \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
ARG PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=120000
ARG PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST
RUN PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=$PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT \
    PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST=$PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST \
    python -m playwright install --only-shell chromium \
    && chmod -R a+rX /opt/browsers
COPY pyproject.toml ./
COPY app ./app
RUN --mount=type=cache,target=/root/.cache/pip pip install --no-deps .
COPY alembic.ini .env.example ./
ENV HOME=/tmp XDG_CACHE_HOME=/tmp/.cache XDG_CONFIG_HOME=/tmp/.config
USER appuser
EXPOSE 8000
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
