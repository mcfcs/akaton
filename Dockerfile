FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY migrations ./migrations
COPY alembic.ini .
RUN pip install --no-cache-dir .

ARG INSTALL_BROWSER=false
RUN if [ "$INSTALL_BROWSER" = "true" ]; then \
      pip install --no-cache-dir ".[browser]" && patchright install --with-deps chromium; \
    fi

RUN useradd --create-home --uid 10001 akaton \
    && mkdir -p /app/data \
    && chown -R akaton:akaton /app
USER akaton

CMD ["akaton", "run"]
