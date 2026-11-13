# Multiple backends

Three backends with different policies and credentials.

| Backend | Authentication | Exposure |
| --- | --- | --- |
| `shop` | bearer, `SHOP_API_TOKEN` | default `read_only`, minus `orders_list` and `/internal/*` |
| `messaging` | header `X-API-Key`, `MESSAGING_API_KEY` | `allowlist` of two read operations; `openapi.json` is OpenAPI 3.1 |
| `inventory` | bearer, `INVENTORY_API_TOKEN` | `all`, so the `PUT` operation is a tool (explicit opt-in) |

```bash
export SHOP_API_TOKEN=x MESSAGING_API_KEY=x INVENTORY_API_TOKEN=x   # dummy values for the check
openapi-mcp-gateway validate --config gateway.yaml
openapi-mcp-gateway list-backends --config gateway.yaml
openapi-mcp-gateway list-tools --config gateway.yaml
```

Each credential is sent only to its own backend. `messaging` also sets a shorter `timeout_seconds`.
All hosts are `example.com` placeholders.
