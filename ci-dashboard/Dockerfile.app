FROM node:20-alpine AS web-build

WORKDIR /app/web

COPY web/package.json web/package-lock.json ./

RUN npm ci

COPY web ./

ARG VITE_BASE_PATH=/dashboard/
ENV VITE_BASE_PATH=${VITE_BASE_PATH}

RUN npm run build


FROM python:3.12-slim
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/PingCAP-QE/ee-apps"

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --upgrade pip setuptools wheel

COPY pyproject.toml README.md ./
COPY src ./src
COPY sql ./sql

RUN pip install --retries 5 .

COPY --from=web-build /app/web/dist ./web/dist

EXPOSE 8000

CMD ["uvicorn", "ci_dashboard.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
