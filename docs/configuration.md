# Configuration

Two kinds of YAML file configure the gateway:

- `gateway.yaml`: one per gateway, selected with `--config` (default `./gateway.yaml`).
- `backend.yaml`: one per backend, in its own directory under `backends.directory`.

Unknown keys are rejected, and every problem found is reported with its file and field. A key
written twice in the same mapping, at any depth (for example two `exclude:` blocks), is a
configuration error naming the key and its line: YAML would otherwise keep only the last one and
silently drop the first. A YAML syntax error is reported with the parser's message, line and
column only; the offending text is never echoed, because a malformed file may contain a pasted
secret. Relative paths are resolved against the file that contains them: `backends.directory`
against `gateway.yaml`, `schema` against its `backend.yaml`.

## gateway.yaml

```yaml
version: 1

server:
  name: Company API Gateway
  transport: stdio

backends:
  directory: ./backends
  discovery: auto

defaults:
  exposure:
    mode: read_only
  http:
    timeout_seconds: 30
    follow_redirects: false
    max_response_bytes: 1048576
    max_connections: 10

logging:
  level: INFO
  format: text
```

| Key | Required | Default | Description |
| --- | --- | --- | --- |
| `version` | yes | none | Must be `1`. |
| `server.name` | no | `OpenAPI MCP Gateway` | Server name announced to MCP clients. |
| `server.transport` | no | `stdio` | Only `stdio` is supported in v1.0. |
| `backends.directory` | no | `./backends` | Directory scanned for backends. Must exist. |
| `backends.discovery` | no | `auto` | Only `auto` is supported: every immediate subdirectory containing a `backend.yaml` is a backend, in lexicographic order. Directories without one are ignored. |
| `defaults.exposure.mode` | no | `read_only` | `read_only` or `allowlist`. `all` is rejected here; see below. |
| `defaults.http.*` | no | see the `http` table | Defaults for the `http` keys of every backend. |
| `logging.level` | no | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |
| `logging.format` | no | `text` | `text` or `json`. |

The section keys `server`, `backends`, `defaults` and `logging` are all optional.

`all` cannot be set as a default because exposing every operation, including state-changing ones,
must be a deliberate choice written in the `backend.yaml` of the backend concerned.

## backend.yaml

```yaml
name: catalog
schema: ./openapi.yaml
base_url: https://catalog.example.com
auth:
  type: bearer
  token_env: CATALOG_API_TOKEN
exposure:
  mode: read_only
  exclude:
    operations:
      - products_export
    paths:
      - /api/internal/*
http:
  timeout_seconds: 20
```

| Key | Required | Default | Description |
| --- | --- | --- | --- |
| `name` | yes | none | Backend identifier and tool-name prefix. Must match `^[a-z][a-z0-9_-]*$` and be unique. |
| `schema` | yes | none | Local OpenAPI 3.0.x or 3.1.x file, YAML or JSON (detected from the content, not the extension). |
| `base_url` | yes | none | Origin of the real API, `http://` or `https://`, with a host. May have a path prefix. No credentials, query string, fragment or whitespace. The host must be ASCII: write an internationalised domain name in its punycode form (`xn--...`). |
| `auth` | no | `type: none` | Authentication; see below and [Authentication](authentication.md). |
| `exposure` | no | defaults | Which operations become tools; see [Exposure modes](exposure-modes.md). |
| `http` | no | defaults | Per-backend HTTP settings; see below. |

`base_url` is authoritative. `servers` entries in the OpenAPI file, at document, path or operation
level, never change where requests go.

### auth

| Key | Applies to | Description |
| --- | --- | --- |
| `type` | all | `none` (default), `bearer` or `api_key`. An empty `auth: {}` means `none`. |
| `token_env` | `bearer` | Name of the environment variable holding the token. Sent as `Authorization: Bearer <token>`. |
| `location` | `api_key` | Must be `header`. API keys in query parameters are not supported. |
| `name` | `api_key` | Name of the header that carries the key, for example `X-API-Key`. Must be a valid HTTP header name (an RFC 9110 token: ASCII letters, digits and a few symbols such as `-`, `_` and `.`; no spaces or colons) and not `Host`, `Content-Length`, `Transfer-Encoding` or `Connection`. A header parameter of the same name in the schema can never replace it. |
| `value_env` | `api_key` | Name of the environment variable holding the header value. |

### exposure

| Key | Default | Description |
| --- | --- | --- |
| `mode` | inherited from `defaults.exposure.mode`, else `read_only` | `read_only`, `allowlist` or `all`. |
| `operations` | `[]` | Operation IDs to expose. Only valid in `allowlist` mode: a non-empty list with any other effective mode is a configuration error. |
| `exclude.operations` | `[]` | Operation IDs to remove after the mode is applied. |
| `exclude.paths` | `[]` | Path patterns to remove after the mode is applied. |

Rules, the exposure matrix and pattern syntax are in [Exposure modes](exposure-modes.md).

### http

These keys can be set under `defaults.http` (all backends) and `http` (one backend); the backend
value wins.

| Key | Default | Description |
| --- | --- | --- |
| `timeout_seconds` | `30` | Applied separately to each phase of a request: connecting, sending, each wait for response data, and waiting for a free pooled connection. It is not a limit on the total duration of a request. Must be a finite number greater than 0. |
| `follow_redirects` | `false` | If `true`, same-origin redirects are followed, up to 20 hops. A redirect to any other origin fails. |
| `max_response_bytes` | `1048576` | Largest response body accepted (1 MiB). Counted both as received on the wire and after decompression. |
| `max_connections` | `10` | Maximum simultaneous connections to this backend. |

The gateway never retries a request. Each backend has its own HTTP client with a connection pool.
The clients ignore the `HTTP_PROXY` / `HTTPS_PROXY` environment variables and `.netrc` files, so
proxies are not honoured in v1.0. TLS trust does follow the environment: if `SSL_CERT_FILE` or
`SSL_CERT_DIR` is set, the certificates there are trusted instead of the system store, so a backend
behind a private certificate authority works.

## Tool names

Each exposed operation becomes the tool `<backend>_<slug>`, where `slug` is the `operationId`
with:

1. every character outside `[A-Za-z0-9_]` replaced by `_`,
2. runs of `_` collapsed into one,
3. leading and trailing `_` removed.

Case is kept. For example, backend `shop` and operationId `products.list-all` give
`shop_products_list_all`. Names are stable between restarts.

The following are schema errors (exit code 3) that name the operation. Fix them by excluding the
operation or renaming its `operationId` in the schema:

- the slug is empty (the ID has no letters, digits or inner underscores),
- the slug is longer than 56 characters, or the full name is longer than 64 characters,
- two operations produce the same name, in one backend or across backends,
- an exposed operation has no `operationId`, or an `operationId` is duplicated.

## OpenAPI documents

- OpenAPI 3.0.x and 3.1.x only; Swagger 2.0 and other versions are rejected with the version found.
- Only internal references (`$ref: "#/..."`) are supported. An external reference is rejected;
  bundle the document first.
- A path item that is itself a `$ref` contributes no operations.
- A YAML document that expands past 500,000 nodes through aliases is rejected.
- Operation descriptions, parameters, request bodies and enums are carried into the tool
  definition. FastMCP advertises enums but does not enforce them; see
  [Architecture](architecture.md).

## Tool results and errors

On success the tool returns the backend's response: JSON, any `+json` type, `text/*`, or an empty
body (for example `204`). Any other content type with a body fails with `unsupported_content_type`;
binary data is never passed to the client as text.

A failure is a tool error whose message is a JSON object:

```json
{"error": "rate_limited", "backend": "shop", "status": 429, "message": "the backend's rate limit was exceeded", "detail": "...", "retry_after": "7"}
```

| Field | Meaning |
| --- | --- |
| `error` | One of the codes below. |
| `backend` | The backend name. |
| `status` | The upstream HTTP status, or `null` when no response was received. |
| `message` | A fixed, sanitised sentence. |
| `detail` | 4xx responses only: the upstream body as text, truncated to 1000 characters. Never included for 5xx. |
| `retry_after` | 429 responses only, and only if the backend sent `Retry-After`: its value. |

| Code | When |
| --- | --- |
| `validation_error` | Upstream status 400. |
| `authentication_failed` | Upstream status 401. |
| `authorization_failed` | Upstream status 403. |
| `not_found` | Upstream status 404. |
| `rate_limited` | Upstream status 429. |
| `upstream_client_error` | Any other 4xx status. |
| `upstream_error` | A 5xx status, or a response body that cannot be decoded (for example corrupt gzip). |
| `redirect_not_followed` | A 3xx response while `follow_redirects` is `false`, a 3xx without a `Location` header, or a `304 Not Modified` (conditional requests are not supported). The message says which. |
| `timeout` | A phase of the request exceeded `timeout_seconds`. |
| `backend_unavailable` | The connection failed, a redirect chain exceeded 20 hops, or a followed redirect had a `Location` header that is not a valid URL. |
| `origin_rejected` | A request or redirect target outside the backend's scheme, host and port. Nothing was sent. |
| `response_too_large` | A 2xx body over `max_response_bytes`, or an over-size body on a followed redirect. For other non-2xx responses the status mapping above wins. |
| `unsupported_content_type` | A 2xx response with a body whose type is not JSON or text, or with no `Content-Type`. |
| `invalid_argument` | A path parameter argument is missing, `null`, empty, only whitespace, a list or an object, or its value would make the request match a different path of the backend's schema (`id="export"` turning `/items/{id}` into `/items/export`). `status` is `null` and nothing was sent; the message names the parameter. Any of these would otherwise produce a URL that the backend may serve as a different, possibly excluded, endpoint. See [security](security.md). |

A 2xx response that claims `application/json` but is not valid JSON is passed on as text.

## Logging

Logs go to stderr, never stdout. `text` prints `LEVEL event key=value ...`; `json` prints one
JSON object per line with the fields plus `level` and `event`. Events:

| Event | Meaning |
| --- | --- |
| `gateway.starting` | Startup begins. |
| `backend.loaded` | A backend's tools were registered (`name`). |
| `backend.warning` | A warning from preparing a backend (`name`, `detail`), for example an exclusion that matches nothing. |
| `tools.registered` | All tools registered (`count`). |
| `gateway.ready` | Serving (`transport`). |
| `tool.executed` | One tool call finished: `backend`, `operation`, `method`, `status`, `duration_ms`, plus `error=<code>` when it failed. |
| `gateway.stopping` | A stop signal arrived (`signal`). |
| `gateway.stopped` | HTTP clients closed; the process is exiting. |

Request and response bodies are never logged. Values of fields whose names contain
`authorization`, `token`, `api_key`, `password`, `secret` or `body` are replaced by `***`. Loggers of
third-party libraries (httpx2, FastMCP, MCP) are capped at `WARNING`, so URLs and headers are not
logged even at `DEBUG`. Their warnings and errors, such as FastMCP's `Error calling tool '<name>'`
after a failed call, go through the gateway's own handler and format (in `json`, the message is the
`event`), so stderr stays one line per record. A record's traceback is printed only when
`logging.level` is `DEBUG`; in `json` it is the `traceback` field of the same one-line object.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success. |
| 1 | General runtime error. |
| 2 | Configuration error. |
| 3 | OpenAPI schema error. |
| 4 | Missing or unusable credentials. |
| 5 | MCP initialisation error. |

When several categories of error occur together, all are printed and the lowest code is the exit
code. Add `--debug` before the command (`openapi-mcp-gateway --debug serve ...`) to print
tracebacks.
