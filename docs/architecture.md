# Architecture

## Startup benchmark

NFR-001 target: five backends with 200 operations in total start in under 3 seconds.

- Measured: about 0.13 s (config load + `prepare_all` + `build_server`; Python import time excluded).
- Machine: Intel Core i7-12700H (20 logical CPUs), Linux 7.0, Python 3.13.2 (project venv).
- Reproduce: `uv run pytest -m perf -v -s`. The test builds 5 backends x 40 operations in a temporary
  directory and fails above 3 s. The default `pytest` run excludes it (`-m "not perf"`).

## Known FastMCP limitations

- Read-only properties stay required. drf-spectacular lists read-only fields such as `id` in a request
  schema's `required`. FastMCP drops `readOnly` but keeps the property required, so tools like
  `drf_orders_create` advertise `id` as a required argument. FastMCP does not validate arguments, so calls
  without `id` succeed and the body is forwarded as given. Pinned by
  `test_read_only_property_still_advertised_as_required` in `tests/compat/test_drf_spectacular.py`.

- Enums are advertised but not enforced. FastMCP keeps an `enum` in a tool's input schema but does not
  validate arguments against it, so a call with a value outside the enum reaches the backend, which
  must reject it. The gateway does not add its own validation.
- FastMCP's logger has its own Rich handlers and does not propagate. Importing FastMCP gives the
  `fastmcp` logger two Rich handlers and `propagate=False`, so its records (for example
  `Error calling tool ...`, with a traceback) would bypass `logging.format: json`. The gateway
  removes those handlers, lets the records propagate to its own handler, and sets
  `fastmcp.settings.log_enabled = False` so FastMCP cannot reinstall them; it never passes
  `log_level` to `run_stdio_async`, the only call that would.
- Header parameters are merged over the client's headers. FastMCP builds each request through the
  client with the directed headers taking priority, so a header parameter named like the credential
  header would win. `GatewayTransport` therefore re-applies the configured credential and `Host`
  on every hop.
- Incoming HTTP headers could be forwarded to backends under an HTTP transport. FastMCP's OpenAPI
  tools forward the headers of the incoming MCP HTTP request to the backend. This is irrelevant for
  stdio, the only v1.0 transport, because there are no incoming HTTP headers. It must be disabled
  before v1.1 adds Streamable HTTP, otherwise a client's `Authorization` header could reach a backend.

## How a call flows

1. `serve` loads `gateway.yaml`, discovers `backends/*/backend.yaml` and validates the
   configuration.
2. For each backend: load the schema, validate it (3.0.x or 3.1.x, internal `$ref` only), extract
   operations, apply the exposure mode and exclusions, compute tool names, and write a filtered
   copy of the document that contains only the selected operations and no `servers`. Errors from all
   backends are collected and reported together.
3. Credentials are read from the environment. Any that is missing or unusable stops startup.
4. One FastMCP OpenAPI provider and one HTTP client per backend are registered under the backend's
   namespace. The tool names FastMCP lists are compared with the computed ones.
5. The server runs on stdio. A middleware refuses a call whose path parameter is missing, empty,
   not a scalar, or would turn the request into another path of the schema
   (`invalid_argument`). Otherwise the call becomes an HTTP request through that backend's client,
   whose transport checks the origin, re-applies the configured credential and `Host` headers,
   bounds the response and turns every failure into a JSON error.
6. On shutdown (stdin closed, SIGTERM or SIGINT) the HTTP clients are closed.
   After a stop signal the gateway closes the clients, logs `gateway.stopped` and exits at once
   with code 0: tool calls still in flight are abandoned. It does not wait for the client's next
   message, because the stdio transport's blocking read of stdin cannot be interrupted.

Nothing in the OpenAPI document is parsed per call; tools are generated once at startup. The offline
commands (`validate`, `list-backends`, `list-tools`, `version`) go through the same preparation code
(`pipeline.py`) without importing FastMCP.

## Decisions made while planning

These were settled before implementation, partly by a throwaway proof of concept against
FastMCP 4.1.8. They go beyond, or differ from, the specification.

1. **httpx2, not httpx.** FastMCP 4 takes `httpx2.AsyncClient`; httpx clients are deprecated there.
   The gateway uses httpx2.
2. **No respx.** respx patches httpx, not httpx2. Tests mock backends with `httpx2.MockTransport`.
3. **`base_url` is already authoritative.** With an explicit client, FastMCP ignores root and
   operation-level `servers`. The filter still strips every `servers` key as defence in depth.
4. **Error mapping lives in the transport.** FastMCP wraps tool exceptions before middleware sees
   them, but a `ToolError` subclass raised inside the transport reaches the client with its message
   intact. So `GatewayTransport` raises `GatewayToolError` with a JSON message.
5. **Tool names are computed by the gateway** and handed to FastMCP through `mcp_names`. FastMCP
   truncates names at 56 characters and silently de-duplicates with suffixes, so the gateway rejects
   anything that would trigger either.
6. **FastMCP does not enforce enums** on tool arguments; enums are preserved in the input schema and
   the backend validates.

Choices on points the specification leaves open:

- Tool name is `<backend>_<slug>`; the slug replaces every character outside `[A-Za-z0-9_]` with `_`,
  collapses runs of `_` and strips leading and trailing `_`. A slug over 56 characters, a full name
  over 64, an empty slug or a collision is a schema error naming the operation.
- `defaults.exposure.mode` accepts `read_only` or `allowlist`; `all` there is a configuration error.
- An allowlisted operationId absent from the schema is a configuration error. An excluded one
  absent from the schema is a warning.
- Path patterns: split on `/`; each pattern segment matches exactly one path segment; `*` is the only
  wildcard; a final `*` segment matches the prefix itself and everything below; patterns match the
  OpenAPI path template text. `**` and a missing leading `/` are errors; a pattern matching nothing is a
  warning.
- `follow_redirects: true` is allowed; every hop passes through the origin check, so only
  same-origin redirects succeed (at most 20 hops).
- Accepted response types are `application/json`, any `+json`, `text/*`, or an empty body. Anything
  else is `unsupported_content_type`.
- The upstream body is forwarded (at most 1000 characters) for 4xx only, never for 5xx.
- New configuration keys: `http.max_response_bytes` (default 1048576), `http.max_connections`
  (default 10), `http.follow_redirects` (default false).
- When several error categories occur, all are printed and the exit code is the lowest of them.
- Backend HTTP clients use `trust_env=False`: environment proxies and `.netrc` are ignored. The
  inner transport keeps its default and still reads `SSL_CERT_FILE` / `SSL_CERT_DIR` for TLS trust,
  so private certificate authorities work (pinned by
  `test_default_inner_transport_keeps_tls_trust_from_environment`).
- `timeout_seconds` is httpx2's per-phase timeout (connect, write, each read, pool), not a total.
- Added `src/openapi_mcp_gateway/pipeline.py` (not in the specification's tree) so offline commands
  never import FastMCP.

## Module map

All paths are under `src/openapi_mcp_gateway/`.

| File | Responsibility |
| --- | --- |
| `exceptions.py` | `GatewayError` hierarchy carrying exit code, backend, file, field |
| `logging.py` | stderr logging setup, `key=value` and JSON formatters, redaction |
| `config/models.py` | Pydantic file models and resolved frozen dataclasses |
| `config/loader.py` | Read `gateway.yaml` (rejecting duplicate keys), discover backends, merge defaults, resolve paths |
| `config/validator.py` | Cross-backend rules, `base_url` rules, credential presence |
| `openapi/loader.py` | Read YAML/JSON document, normalise keys |
| `openapi/validator.py` | Version gate, spec validation, `$ref` rules |
| `openapi/operations.py` | `Operation`, extraction, duplicate IDs, tool naming, path parameter names |
| `openapi/filter.py` | Path glob, selection algorithm, filtered document |
| `pipeline.py` | `prepare_all`: everything up to tool names, no FastMCP import |
| `http/authentication.py` | Auth configuration and environment to headers |
| `http/client.py` | `GatewayTransport`, `GatewayToolError`, `build_client` |
| `mcp/factory.py` | One `OpenAPIProvider` per prepared backend |
| `mcp/gateway.py` | `build_server`, logging and path-argument middleware, library logger routing, lifespan, `serve` |
| `cli.py`, `__main__.py` | Typer commands and exit-code mapping |
