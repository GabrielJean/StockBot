FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend ./
RUN npm run build

FROM python:3.13-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/db.sqlite3 \
    DJANGO_DEBUG=false \
    ALLOWED_HOSTS=* \
    CSRF_TRUSTED_ORIGINS=https://stockbot.docker-1.gwebs.ca \
    COOKIE_SECURE=true \
    SCHEDULER_ENABLED=true
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . ./
COPY --from=frontend /core/static ./core/static
RUN python manage.py collectstatic --noinput && mkdir -p /data && chmod +x /app/docker-entrypoint.sh
EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "manage.py", "run_service"]
