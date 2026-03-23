FROM python:3.12-slim AS base
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM base AS tailwind
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*
RUN curl -sLO https://github.com/tailwindlabs/tailwindcss/releases/download/v4.1.3/tailwindcss-linux-x64 \
    && chmod +x tailwindcss-linux-x64
COPY app/static/ ./app/static/
RUN ./tailwindcss-linux-x64 -i app/static/input.css -o app/static/style.css --minify

FROM base
RUN groupadd -r app && useradd -r -g app -d /app -s /sbin/nologin app \
    && mkdir -p /app/data && chown -R app:app /app
COPY --chown=app:app app/ ./app/
COPY --from=tailwind --chown=app:app /app/app/static/style.css ./app/static/style.css
USER app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
