# Multi-stage build: one source of truth for both services.
#   docker build --target api -t give-exit-api .
#   docker build --target frontend -t give-exit-frontend .

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./

# ---------------------------------------------------------------- api
FROM base AS api
# Tesseract (por) enables OCR for consumer evidence inside the container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-por \
    && rm -rf /var/lib/apt/lists/*
COPY app ./app
ARG API_EXTRAS=ocr
RUN pip install ".[${API_EXTRAS}]"
# The API parses hostile PDFs and images, so it must not run as root. /app/data
# is a mount point for ChromaDB, uploads and run history.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s \
    CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8000/health')"
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ---------------------------------------------------------------- frontend
FROM base AS frontend
COPY app ./app
RUN pip install ".[frontend]"
COPY frontend ./frontend
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser
EXPOSE 8501
CMD ["streamlit", "run", "frontend/streamlit_app.py", \
     "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true"]
