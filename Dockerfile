FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend ./
RUN npm run build

FROM python:3.13-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . ./
COPY --from=frontend /core/static ./core/static
RUN python manage.py collectstatic --noinput && useradd --system --uid 10001 stockbot && mkdir -p /data && chown -R stockbot:stockbot /app /data
USER stockbot
EXPOSE 8000
CMD ["python", "manage.py", "run_service"]
