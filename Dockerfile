# anvil-data-qc -- automated QC of AnVIL data releases, run non-interactively.
#
# The image is CODE-ONLY -- it contains no config and no secrets. Everything sensitive is provided at
# run time, never baked in:
#   - Terra/WDL: ADC = the ambient runtime service account (no file, no code change); the config and
#                any Azul token arrive as workflow inputs.
#   - Local:     mount config / ADC / Azul token via -v and point --config and
#                GOOGLE_APPLICATION_CREDENTIALS at them.
#
# Build:  docker build -t anvil-data-qc:dev .
# Run:    docker run --rm anvil-data-qc:dev --help
FROM python:3.13-slim-bookworm

LABEL org.opencontainers.image.title="anvil-data-qc" \
      org.opencontainers.image.description="Automated QC of AnVIL data releases" \
      org.opencontainers.image.source="https://github.com/mbaumann-broad/anvil-data-qc"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 1) Pinned runtime dependencies first, so the large dependency layer is cached across code changes.
COPY requirements.txt ./
RUN pip install --requirement requirements.txt

# 2) The package itself. Its dependencies are already pinned and installed above, so --no-deps avoids
#    re-resolving them (the build reads pyproject.toml + README.md + LICENSE).
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-deps .

# 3) Run as a non-root user.
RUN useradd --create-home --uid 1000 appuser
USER appuser
WORKDIR /home/appuser

ENTRYPOINT ["anvil-data-qc"]
CMD ["--help"]
