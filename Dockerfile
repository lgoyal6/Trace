# Credential-free Trace backend: the `fake` (or `local_vector`) profile only.
# No .env, keys or Snowflake config are copied in; live profiles are not
# runnable from this image by design.
FROM python:3.12.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NEULIT_PROFILE=fake \
    NEULIT_LOCAL_VECTOR_DB=/tmp/neulit/vectors.sqlite3

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY config ./config
COPY backend ./backend
COPY frontend/.openapi.json ./frontend/.openapi.json

RUN useradd --create-home --uid 10001 trace && chown -R trace /app
USER trace

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"
CMD ["python", "-m", "uvicorn", "backend.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
