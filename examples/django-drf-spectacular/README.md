# Django REST Framework with drf-spectacular

A backend described by a schema that drf-spectacular generated: `backends/orders/openapi.yaml` is a
copy of `tests/fixtures/drf/openapi-3.0.yaml`, produced by a small throwaway DRF project (versions
in `tests/fixtures/drf/README.md`).

To do the same with your own Django project, run this in the project and copy the file into the
backend directory:

```bash
python manage.py spectacular \
  --file openapi.yaml \
  --format openapi
cp openapi.yaml /path/to/gateway/backends/orders/openapi.yaml
```

The Django application does not change. Then set `base_url` in `backends/orders/backend.yaml`.

DRF token authentication expects the header `Authorization: Token <token>`, so the backend uses
`auth.type: api_key` with `name: Authorization`, and the environment variable holds the whole value:

```bash
export DRF_AUTHORIZATION="Token <your DRF token>"
openapi-mcp-gateway validate --config gateway.yaml
openapi-mcp-gateway list-tools --config gateway.yaml
```

Read-only mode exposes `orders_health_retrieve`, `orders_orders_list`, `orders_orders_retrieve` and
`orders_reports_list`. See "Known FastMCP limitations" in [`docs/architecture.md`](../../docs/architecture.md)
about read-only properties in generated request schemas.
