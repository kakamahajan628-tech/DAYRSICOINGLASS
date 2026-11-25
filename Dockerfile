FROM python:3.12-slim-bookworm
ARG INSTALL_BROWSER=false
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright DATA_DIR=/tmp/coinglass-data PORT=10000 \
    NODE_OPTIONS=--max-old-space-size=128 MALLOC_ARENA_MAX=2 COINGLASS_ENABLED=false \
    MPLCONFIGDIR=/tmp/matplotlib MPLBACKEND=Agg SCAN_BUDGET_SECONDS=900 \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
WORKDIR /app
COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libatomic1 libstdc++6 \
    && pip install --no-cache-dir -r requirements.txt \
    && if [ "$INSTALL_BROWSER" = "true" ]; then python -m playwright install --with-deps --only-shell chromium; fi \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 tracker \
    && mkdir -p /tmp/coinglass-data /opt/playwright /tmp/matplotlib \
    && chown -R tracker:tracker /app /opt/playwright /tmp/coinglass-data /tmp/matplotlib
COPY --chown=tracker:tracker *.py ./
COPY --chown=tracker:tracker tests ./tests
COPY --chown=tracker:tracker web ./web
USER tracker
EXPOSE 10000
CMD ["python", "serve.py"]
