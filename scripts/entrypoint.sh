#!/bin/sh
set -eu

python /app/scripts/ensure_model.py
exec "$@"
