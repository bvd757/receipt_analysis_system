#!/bin/sh
set -e

echo "[entrypoint] Running migrations..."
alembic upgrade head

echo "[entrypoint] Starting: $@"
exec "$@"
