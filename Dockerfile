FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright DATA_DIR=/tmp/coinglass-data PORT=10000
WORKDIR /app
COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 \
    && pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 tracker \
    && mkdir -p /tmp/coinglass-data \
    && chown -R tracker:tracker /app /opt/playwright /tmp/coinglass-data
COPY --chown=tracker:tracker *.py ./
COPY --chown=tracker:tracker tests ./tests
USER tracker
EXPOSE 10000
CMD ["python", "app.py"]
