# Cheesemonger server image.
#
# Built and pushed by CI to:
#   us.gcr.io/cds-docker-containers/cheesemonger
#
# Runtime config is via env vars (see config.py); data lives on a mounted
# volume (DATA_DIR, default /mnt/data) — nothing is baked into the image.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# UV_PYTHON_DOWNLOADS=never -> use the image's Python so the venv interpreter
# path is valid at runtime. Compile bytecode for faster cold starts.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# 1) Dependency layer — cached unless pyproject.toml / uv.lock change.
#    --no-dev: skip ruff/pytest/pyright. taigapy resolves from the public
#    CDS Artifact Registry index pinned in uv.lock (anonymous read).
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# 2) Application code, then install the project itself.
COPY cheesemonger ./cheesemonger
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    DATA_DIR=/mnt/data

EXPOSE 8000

# gunicorn manages uvicorn workers (one per vCPU on the target VM).
CMD ["gunicorn", "cheesemonger.main:app", \
     "-k", "uvicorn.workers.UvicornWorker", \
     "-w", "4", "-b", "0.0.0.0:8000", "--timeout", "120"]
