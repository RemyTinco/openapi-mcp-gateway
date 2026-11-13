#!/usr/bin/env bash
# Regenerates the committed schemas (openapi-3.0.yaml, openapi-3.1.yaml).
# Django, DRF and drf-spectacular are NOT project dependencies; uv fetches them on the fly.
# Pinned versions used for the committed output are also recorded in README.md.
set -euo pipefail

DJANGO="django==6.1.3"
DRF="djangorestframework==3.18.3"
SPECTACULAR="drf-spectacular==0.30.0"

cd "$(dirname "$0")/project"
generate() {
  OAS_VERSION="$1" uv run --no-project --python 3.12 \
    --with "$DJANGO" --with "$DRF" --with "$SPECTACULAR" \
    python manage.py spectacular --file "../$2" --format openapi
}
generate 3.0.3 openapi-3.0.yaml
generate 3.1.0 openapi-3.1.yaml
