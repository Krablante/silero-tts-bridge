#!/bin/sh
set -eu
umask 077

python /app/scripts/ensure_model.py
exec "$@"
