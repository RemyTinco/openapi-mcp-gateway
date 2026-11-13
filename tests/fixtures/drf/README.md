# drf-spectacular fixtures

`project/` is a minimal Django REST Framework project (in-memory SQLite, no migrations) used only to
generate `openapi-3.0.yaml` and `openapi-3.1.yaml`. The generated files are committed unmodified; tests
read only the YAML and never import Django. Django is not a dependency of this repository.

Covered: `APIView` (`/health/`), `ModelViewSet` (`/orders/`) with a nested serializer, page-number
pagination (`page`, `page_size`), enum `choices`, nullable fields, a `@extend_schema` query filter
(`status`), a plain `ViewSet` with only `list` (`/reports/`), and token authentication.

Regenerate with `tests/fixtures/drf/generate.sh` (needs `uv` and network access).

Versions used for the committed schemas (pinned in `generate.sh`):

| Package | Version |
| --- | --- |
| Django | 6.1.3 |
| djangorestframework | 3.18.3 |
| drf-spectacular | 0.30.0 |
| Python | 3.12 |

`backend.yaml` is the backend definition the compatibility tests start from; the tests override
`schema` and `exposure.mode`.
