# Multi-stage Dockerfile for Nam An Merchant Portal
FROM python:3.12-slim as builder

WORKDIR /app

# Install build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Final runtime image
FROM python:3.12-slim

WORKDIR /app

# Install curl for health checks
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy python packages from builder
COPY --from=builder /root/.local /usr/local
ENV PATH=/usr/local/bin:$PATH

# Copy application source code & migrations
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini .
COPY pyproject.toml .

# Keep bytecode out of read-only app directories and flush logs immediately
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Run as non-root user; keep application code read-only and only /app/data writable
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser \
    && mkdir -p /app/data \
    && chown -R root:appgroup /app \
    && chmod -R a-w /app/app /app/alembic /app/alembic.ini /app/pyproject.toml \
    && chown -R appuser:appgroup /app/data \
    && chmod 750 /app/data

USER appuser

# Expose FastAPI custom internal port (3002)
EXPOSE 3002

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:3002/api/v1/health/db || exit 1

# Start FastAPI application via Uvicorn on custom port 3002 behind trusted reverse proxies
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3002", "--workers", "2", "--proxy-headers", "--forwarded-allow-ips=127.0.0.1,172.16.0.0/12"]
