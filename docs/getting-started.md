# Getting started

This walks from nothing to Claude Code calling one of your REST APIs. You do not write any Python.
Commands that print something show real output from the gateway.

## 1. Install

You need Python 3.14 or newer and [uv](https://docs.astral.sh/uv/).

The package is not on PyPI yet. Install from a clone of the repository:

```bash
git clone https://github.com/RemyTinco/openapi-mcp-gateway
uv tool install ./openapi-mcp-gateway
openapi-mcp-gateway version
```

Once the first release is published, `uv tool install openapi-mcp-gateway` will do the same without
a clone.

## 2. Get the OpenAPI file

The gateway reads **local files only**; it never downloads a schema. Save your API's OpenAPI 3.0.x
or 3.1.x document (YAML or JSON) next to the configuration.

For a Django application using drf-spectacular, export it with:

```bash
python manage.py spectacular \
  --file openapi.yaml \
  --format openapi
```

Nothing in the Django application needs to change. The gateway does not support references to
other files or URLs (`$ref: other.yaml#/...`); bundle them into one document first.

## 3. Create the files

```
my-gateway/
├── gateway.yaml
└── backends/
    └── catalog/
        ├── backend.yaml
        └── openapi.yaml        <- the file from step 2
```

`gateway.yaml`:

```yaml
version: 1

server:
  name: My Gateway

backends:
  directory: ./backends
```

`backends/catalog/backend.yaml`:

```yaml
name: catalog
schema: ./openapi.yaml
base_url: https://catalog.example.com
auth:
  type: bearer
  token_env: CATALOG_API_TOKEN
exposure:
  mode: read_only
```

`base_url` is the origin of the real API and is authoritative: any `servers` entry in the OpenAPI
file is ignored. Every immediate subdirectory of `backends/` that contains a `backend.yaml` is one
backend. See [Configuration](configuration.md) for every key.

## 4. Provide the secret

The configuration holds the **name** of an environment variable, never the secret:

```bash
export CATALOG_API_TOKEN=...   # your real token
```

Do not commit it. See [Authentication](authentication.md).

## 5. Validate

```bash
openapi-mcp-gateway validate --config ./gateway.yaml
```

Output for the bundled `examples/single-backend` (a read-only backend with an excluded path):

```
OpenAPI MCP Gateway — Validation

✓ Gateway configuration valid
✓ 1 backend discovered
✓ shop: OpenAPI 3.0.3 valid
✓ No duplicate tool names
✓ 3 operations selected

Validation successful.
```

`validate` reads your files and checks that the environment variables exist. It contacts no
backend and does not test whether the credentials work. Problems are reported with the backend,
file and field. With the variable unset:

```
error: [shop] .../examples/single-backend/backends/shop/backend.yaml: environment variable SHOP_API_TOKEN is not set
```

(path shortened) and the exit code is 4. Exit codes: 0 success, 1 runtime error, 2 configuration,
3 OpenAPI schema, 4 missing credentials, 5 MCP initialisation. Warnings start with `!` and do not
fail validation, for example an exclusion that matches nothing.

## 6. See the tools

```bash
openapi-mcp-gateway list-tools --config ./gateway.yaml
```

Real output for `examples/multi-backend`:

```
BACKEND     TOOL
inventory   inventory_stock_list
inventory   inventory_stock_retrieve
inventory   inventory_stock_update
messaging     messaging_messages_list
messaging     messaging_messages_retrieve
shop        shop_products_list
shop        shop_products_retrieve
```

`list-backends` shows each backend's origin, mode and tool count:

```
BACKEND     BASE_URL                        MODE        TOOLS
inventory   https://inventory.example.com   all         3
messaging     https://messaging.example.com     allowlist   2
shop        https://shop.example.com        read_only   2
```

## 7. Connect Claude Code

```bash
claude mcp add --transport stdio \
  openapi-gateway \
  -- openapi-mcp-gateway serve \
     --config /absolute/path/to/gateway.yaml
```

Claude Code starts the gateway as a local subprocess. The gateway needs the environment variables
from step 4: export them in the shell that launches Claude Code, or pass them with the `--env`
option of `claude mcp add` (see `claude mcp add --help`). Use `/mcp` inside Claude Code to check
the connection, then ask for something in plain language, such as "list the products".

While running, the gateway writes log lines to **stderr** (stdout carries only the MCP protocol).
Starting the example and closing its input prints:

```
INFO gateway.starting
INFO backend.loaded name=shop
INFO tools.registered count=3
INFO gateway.ready transport=stdio
INFO gateway.stopped
```

## Adding a backend

1. Create `backends/<name>/`.
2. Put the OpenAPI file in it.
3. Add a `backend.yaml` with `name`, `schema`, `base_url`, authentication and exposure.
4. Export any environment variable it needs.
5. Run `validate`, then restart the gateway.

Backend names must match `^[a-z][a-z0-9_-]*$` and be unique. No code changes are needed.

## Updating a backend

1. Export the latest OpenAPI file and replace the one in the backend directory.
2. Run `openapi-mcp-gateway validate --config ./gateway.yaml`.
3. Restart the MCP server (in Claude Code, reconnect it from `/mcp` or restart the session).

Schemas are read once at startup; there is no hot reload. Operations keep their tool names as long
as their `operationId` is unchanged. In `read_only` mode, a newly added `GET` or `HEAD` operation
becomes a tool on restart unless you exclude it.

## Where next

- [Examples](../examples/): single backend, several backends, a Django REST Framework schema.
- [Exposure modes](exposure-modes.md): decide what the agent may call.
- [Security](security.md): read before using a production API.
