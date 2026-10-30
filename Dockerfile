# ttdwn Dockerfile
# Multi-stage build for smaller final image

# Build stage
FROM python:3.12-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY pyproject.toml ./
RUN pip install --no-cache-dir --prefix=/install ".[dev]"

# Runtime stage
FROM python:3.12-slim

# Install runtime dependencies (ffmpeg for audio/remux)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd --create-home --shell /bin/bash --uid 1000 appuser

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY --chown=appuser:appuser ttdwn/ ./ttdwn/
COPY --chown=appuser:appuser webui/ ./webui/
COPY --chown=appuser:appuser ttdwn.py ./
COPY --chown=appuser:appuser pyproject.toml ./
COPY --chown=appuser:appuser README.md ./

# Create download directory with correct permissions
RUN mkdir -p /downloads && chown appuser:appuser /downloads

# Switch to non-root user
USER appuser

# Environment variables
ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    DOWNLOAD_DIR=/downloads \
    MAX_CONCURRENT=2 \
    LOG_LEVEL=INFO

# Expose port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz', timeout=3)" || exit 1

# Default command: run web UI
CMD ["python", "-m", "webui.main"]