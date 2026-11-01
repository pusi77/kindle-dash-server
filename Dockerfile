ARG PYTHON_IMAGE=python:3.12-slim
# Published as ghcr.io/pusi77/kindle-dash-server (see .github/workflows/publish.yml).

# --- build: resolve the locked dependencies into /app/.venv -------------------------------
FROM ${PYTHON_IMAGE} AS build
RUN pip install --no-cache-dir "uv>=0.11,<0.12"
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# --- runtime -------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE}
ENV PYTHONUNBUFFERED=1 KDS_DATA_DIR=/data PATH=/app/.venv/bin:$PATH
COPY --from=build /app/.venv /app/.venv
COPY --chmod=755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN groupadd --system --gid 1000 kds \
    && useradd --system --uid 1000 --gid 1000 --home-dir /data kds \
    && mkdir -p /data && chown 1000:1000 /data
# Starts as root only to fix the ownership of /data, then runs as kds (or PUID/PGID).
ENV HOME=/tmp
WORKDIR /data
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"]
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["kindle-dash-server", "--host", "0.0.0.0", "--port", "8080"]
