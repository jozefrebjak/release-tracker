FROM python:3.12-slim AS base
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM base AS tailwind
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ARG TARGETARCH
RUN ARCH=$([ "$TARGETARCH" = "arm64" ] && echo "arm64" || echo "x64") \
    && curl -sLO "https://github.com/tailwindlabs/tailwindcss/releases/download/v4.1.3/tailwindcss-linux-${ARCH}" \
    && chmod +x "tailwindcss-linux-${ARCH}" \
    && mv "tailwindcss-linux-${ARCH}" tailwindcss
COPY app/static/ ./app/static/
RUN ./tailwindcss -i app/static/input.css -o app/static/style.css --minify

FROM base
RUN groupadd -r app && useradd -r -g app -d /app -s /sbin/nologin app \
    && mkdir -p /app/data && chown -R app:app /app
COPY --chown=app:app app/ ./app/
COPY --from=tailwind --chown=app:app /app/app/static/style.css ./app/static/style.css
USER app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
