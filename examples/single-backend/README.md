# Single backend

One read-only backend, `shop`, with bearer authentication and an excluded path.

```bash
export SHOP_API_TOKEN=any-value-for-this-check    # see .env.example for the variable name
openapi-mcp-gateway validate --config gateway.yaml
openapi-mcp-gateway list-tools --config gateway.yaml
```

Tools: `shop_products_list`, `shop_products_retrieve`, `shop_orders_list`. `products_create` and
`products_destroy` are not exposed (read-only mode) and `/internal/*` is excluded.

The schema is written for this repository and `shop.example.com` does not exist: `validate` and
`list-tools` work offline, but calling a tool fails with `backend_unavailable`. Replace `base_url`
and the schema to use your own API.
