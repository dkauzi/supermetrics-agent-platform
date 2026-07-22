# Multi-stage so the runtime image carries no build toolchain.
FROM python:3.12-slim AS build

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim

# Non-root: the container writes only to /app/data, which is a mounted volume in
# compose and an ephemeral disk on Cloud Run.
RUN useradd --create-home --uid 1001 agent
WORKDIR /app

COPY --from=build /install /usr/local
COPY --chown=agent:agent . .

USER agent
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# Cloud Run injects PORT; default matches local compose.
ENV PORT=8000
EXPOSE 8000

# Hits the real readiness endpoint, which reports warehouse and registry state
# rather than just proving the process is alive.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen(f'http://localhost:{os.environ[\"PORT\"]}/healthz').status==200 else 1)"

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
