#!/usr/bin/env bash
set -e
gunicorn -w 1 -b 0.0.0.0:${PORT:-10000} web:app
