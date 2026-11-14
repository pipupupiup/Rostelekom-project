#!/bin/sh
# Запуск сервиса ADO: миграции БД (если задана) → uvicorn.
# Сервис стартует и без БД: оценка от PostgreSQL не зависит, /health должен ответить ≤ 120 с.
set -u
if [ -n "${ADO_DATABASE_URL:-}" ]; then
  i=0
  until alembic -c /app/alembic.ini upgrade head; do
    i=$((i + 1))
    if [ "$i" -ge 20 ]; then
      echo "ВНИМАНИЕ: миграции не применены (БД недоступна) — сервис стартует без истории версий"
      break
    fi
    echo "БД недоступна, повтор через 2 с ($i/20)"
    sleep 2
  done
fi
exec uvicorn ado.api.app:app --host 0.0.0.0 --port "${ADO_PORT:-8080}" \
  --timeout-keep-alive 400 --workers 1 --log-level "${ADO_LOG_LEVEL:-info}"
