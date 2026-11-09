# Teal Deal as a self-contained public demo.
#
# The data is built into the image (./run.sh --build-only), so every fresh
# container starts from the seeded state: recreating the containers
# (`docker compose up -d --force-recreate`, or `make up RESET=1`) is the "reset the
# demo" button. The extensions are baked in too, so the container needs no network
# at runtime.
FROM python:3.13-slim

ARG DUCKDB_VERSION=2.0.0
ARG TARGETARCH

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && apt-get clean

# DuckDB CLI (run.sh drives the .sql files with it), pinned to the same version as
# the Python package in pyproject.toml.
RUN curl -fsSL "https://install.duckdb.org/v${DUCKDB_VERSION}/duckdb-cli-linux-${TARGETARCH}.tar.gz" \
  | tar -xz -C /usr/local/bin duckdb

RUN pip install --no-cache-dir uv==0.11.19

RUN useradd --create-home app
USER app
WORKDIR /home/app/teal-deal
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy

# Python deps first: this layer only rebuilds when the lockfile changes.
COPY --chown=app pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen

# Extensions live in ~/.duckdb and are shared by the CLI and the Python package.
# httpfs is not used directly: quack's server needs its crypto module, which
# DuckDB autoloads from httpfs, and the container has no network at runtime.
RUN duckdb -c "INSTALL ducklake; INSTALL quack; INSTALL httpfs; INSTALL fts; INSTALL vss; INSTALL spatial; INSTALL duckpgq FROM community"

COPY --chown=app . .
RUN ./run.sh --build-only

# Public mode: the lake's quack endpoint is read-only (see collector.py). Quack
# listens on loopback inside the container; the TLS proxy shares its network
# namespace (compose.yaml), so only the proxy's ports are reachable.
ENV TEAL_DEAL_PUBLIC=1
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8787/config', timeout=3)"
CMD ["uv", "run", "--no-sync", "collector.py"]
