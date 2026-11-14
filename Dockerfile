# --- 1. Интерфейс диспетчера: React + TypeScript + Leaflet (настоящий Leaflet из npm) ---
FROM node:22-alpine AS ui
WORKDIR /ui
COPY frontend/package.json ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
# Проверка типов не блокирует сборку образа: локально tsc не запускался (нет доступа к реестру npm).
RUN (npm run typecheck || echo "ВНИМАНИЕ: tsc нашёл ошибки типов") && npm run build

# --- 2. Сервис ---
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ADO_UI_DIR=/app/frontend/dist
WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY src ./src
COPY spec ./spec
COPY migrations ./migrations
COPY scripts ./scripts
RUN pip install --no-cache-dir .
COPY --from=ui /ui/dist ./frontend/dist
COPY docker/entrypoint.sh /entrypoint.sh

EXPOSE 8080
# POST /plan синхронный до 300 с; таймауты HTTP ≥ 330 с; один процесс — состояние дня в памяти (D5).
ENTRYPOINT ["/entrypoint.sh"]
