FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    EDDN_ROOT=/app

WORKDIR /app

# Dependencies first so code changes don't reinstall them.
COPY pyproject.toml README.md ./
RUN mkdir -p src/eddn_validator && touch src/eddn_validator/__init__.py \
 && pip install . && pip uninstall -y eddn-validator

COPY src ./src
RUN pip install --no-deps .

COPY migrations ./migrations
COPY config ./config

RUN useradd --system --uid 10001 --no-create-home eddn
USER eddn

ENTRYPOINT ["eddn-validator"]
