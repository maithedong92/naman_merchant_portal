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
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

# Copy application source code
COPY app ./app
COPY pyproject.toml .

# Expose FastAPI custom internal port (2223)
EXPOSE 2223

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:2223/api/v1/health || exit 1

# Start FastAPI application via Uvicorn on custom port 2223
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "2223", "--workers", "2"]
