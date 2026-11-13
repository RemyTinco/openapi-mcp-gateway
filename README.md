# OpenAPI MCP Gateway

A command-line tool that turns **local OpenAPI files** into **MCP tools** for AI agents such as
Claude Code. Point it at the OpenAPI schema of an existing REST API, say which operations may be
used, and the agent can call them. The backend application does not change and you write no
Python.

- Several REST APIs behind one MCP server, each with its own credentials. A credential is only
  ever sent to the backend it belongs to.
- Safe by default: only `GET` and `HEAD` operations are exposed unless you say otherwise.
- Works with any OpenAPI 3.0.x or 3.1.x document in YAML or JSON (Django REST Framework with
  drf-spectacular, FastAPI, Flask, and others). The gateway runs on your machine over stdio.

## Quickstart (about 60 seconds)

You need Python 3.14 or newer and [uv](https://docs.astral.sh/uv/). The package is not on PyPI
yet; until the first release, install from a clone:

```bash
git clone https://github.com/RemyTinco/openapi-mcp-gateway
cd openapi-mcp-gateway
uv tool install .        # installs the `openapi-mcp-gateway` command
```

Once released it will be `uv tool install openapi-mcp-gateway`.

Check the bundled example, which needs no network access:

```bash
export SHOP_API_TOKEN=any-value-for-this-check
openapi-mcp-gateway validate --config examples/single-backend/gateway.yaml
openapi-mcp-gateway list-tools --config examples/single-backend/gateway.yaml
```

Expected tools: `shop_products_list`, `shop_products_retrieve` and `shop_orders_list`. Register the
gateway with Claude Code, using an absolute path:

```bash
claude mcp add --transport stdio openapi-gateway \
  -- openapi-mcp-gateway serve --config "$PWD/examples/single-backend/gateway.yaml"
```

The example points at `shop.example.com`, which does not exist, so tool calls fail with a
`backend_unavailable` error. To use your own API, follow [Getting started](docs/getting-started.md).

## Documentation

| Page | What is in it |
| --- | --- |
| [Getting started](docs/getting-started.md) | Install, export a schema, configure a backend, connect Claude Code |
| [Configuration](docs/configuration.md) | Every configuration key, tool names, error payloads, logging |
| [Exposure modes](docs/exposure-modes.md) | `read_only`, `allowlist`, `all`, exclusions and path patterns |
| [Authentication](docs/authentication.md) | Bearer tokens, API keys, how secrets are handled |
| [Security](docs/security.md) | What the gateway protects, and what it does not |
| [Architecture](docs/architecture.md) | How it works, design decisions, FastMCP limitations, benchmark |
| [Acceptance](docs/acceptance.md) | Which test or document proves each v1.0 acceptance criterion |
| [Examples](examples/) | Three runnable configurations |
| [Contributing](CONTRIBUTING.md) | Development setup and checks |

## Security note

The gateway lets an AI agent act on your APIs. Expose only operations that are appropriate for the
agent, give it credentials with the least privilege the backend allows, and remember that the
gateway does **not** redact upstream responses: whatever an exposed operation returns, the agent
sees. `GET` and `HEAD` are only nominally safe. Read [Security](docs/security.md) before pointing
the gateway at a production API. To report a vulnerability see [SECURITY.md](SECURITY.md).

## Licence

Apache License 2.0. See [LICENSE](LICENSE).
