# Teal Deal as a self-contained public demo.
#
# The data is built into the image (./run.sh --build-only), so every fresh
# container starts from the seeded state: recreating the containers
# (`docker compose up -d --force-recreate`, or `make up RESET=1`) is the "reset the
# demo" button. The extensions are baked in too, so the container needs no network
# at runtime.
# The base image is pinned by digest, so a rebuild can't change under us; bump it
# deliberately to pick up security updates (`docker buildx imagetools inspect python:3.13-slim`).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# DUCKDB_VERSION must match the duckdb pin in pyproject.toml (test_pages.py checks).
ARG DUCKDB_VERSION=2.0.1
# SHA-256 of that release's CLI tarball for each architecture. DuckDB publishes no
# checksums, so these were taken from the downloads when the version was pinned; they
# make later downloads of the same file verifiable. Update them with DUCKDB_VERSION.
ARG DUCKDB_SHA256_AMD64=b676268be4d39e4e41a904442c9796c9914e645cc085c7505f048f2c992b5eca
ARG DUCKDB_SHA256_ARM64=84e37891fcc45c887e30220f51a4914995cd8174139e87f8efd311ac83c956a7
ARG TARGETARCH

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# DuckDB CLI (run.sh drives the .sql files with it), pinned to the same version as
# the Python package in pyproject.toml.
RUN expected="$([ "$TARGETARCH" = arm64 ] && echo "$DUCKDB_SHA256_ARM64" || echo "$DUCKDB_SHA256_AMD64")" \
 && curl -fsSL -o /tmp/duckdb-cli.tar.gz "https://install.duckdb.org/v${DUCKDB_VERSION}/duckdb-cli-linux-${TARGETARCH}.tar.gz" \
 && echo "${expected}  /tmp/duckdb-cli.tar.gz" | sha256sum -c - \
 && tar -xz -C /usr/local/bin -f /tmp/duckdb-cli.tar.gz duckdb \
 && rm /tmp/duckdb-cli.tar.gz

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

# The data is built from the SQL and run.sh alone, so editing the pages or the collector
# does not re-run the build.
COPY --chown=app 01_seed.sql 02_oltp.sql 03_lake.sql 04_marts.sql 05_extras.sql 06_ducklake.sql run.sh ./
RUN ./run.sh --build-only

COPY --chown=app . .

# Public mode: the lake's quack endpoint is read-only (see collector.py). Quack
# listens on loopback inside the container; the TLS proxy shares its network
# namespace (compose.yaml), so only the proxy's ports are reachable.
ENV TEAL_DEAL_PUBLIC=1
EXPOSE 8787
# Healthy = the site answers AND the lake's quack endpoint (8789) accepts connections;
# checking only the site would miss quack dying while the dashboard goes blank.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import socket, urllib.request as u; u.urlopen('http://127.0.0.1:8787/config', timeout=3); socket.create_connection(('127.0.0.1', 8789), timeout=3).close()"
CMD ["uv", "run", "--no-sync", "collector.py"]
