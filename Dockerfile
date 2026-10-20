FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 ENV=production
WORKDIR /app
COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt
COPY src ./src
COPY static ./static
RUN useradd --system --uid 10001 --no-create-home app
USER app
EXPOSE 8080
# Secrets (QLOO_API_KEY etc.) are injected at runtime by the host, never baked into the image.
CMD ["sh", "-c", "exec uvicorn --app-dir src app:app --host 0.0.0.0 --port ${PORT} --no-server-header --proxy-headers --forwarded-allow-ips=${FORWARDED_ALLOW_IPS:-127.0.0.1} --limit-concurrency 50 --timeout-keep-alive 5"]
