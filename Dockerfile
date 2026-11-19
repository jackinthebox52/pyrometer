# Stage 1: build the dashboard
FROM node:22-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Stage 2: the server. torch is deliberately absent — only the SDK (in your
# training environment) needs it, and it would add several GB to this image.
# Keep this dependency list in sync with the server imports in pyrometer/server/.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app HOME=/tmp
WORKDIR /app
RUN pip install --no-cache-dir "fastapi>=0.115" "uvicorn[standard]>=0.32" "duckdb>=1.1" "httpx>=0.28" "aiofiles>=24.1"
COPY pyrometer/ pyrometer/
COPY --from=frontend /build/dist frontend/dist
USER 1000:1000
EXPOSE 8321
CMD ["python", "-m", "pyrometer", "serve", "--host", "0.0.0.0", "--port", "8321"]
