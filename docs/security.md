# Security

The gateway turns API operations into actions an AI agent can invoke. This page says what it
enforces and, just as important, what it does not. The requirement numbers (SEC-001 to SEC-006)
come from the project specification.

## Read this first

- **Responses are not redacted (SEC-004).** The gateway cannot reliably recognise sensitive fields in
  arbitrary JSON, so it passes upstream responses to the agent unchanged. Whatever an exposed
  operation returns, the agent, and the model provider behind it, can see. Expose only operations
  whose responses you are willing to share, rely on backend-side permissions, and give the
  backend the least data it can return.
- **`GET` and `HEAD` are only nominally safe.** `read_only` mode exposes them because they should not
  change state, but some APIs do change state on a `GET`, and reading can itself be sensitive or
  expensive. Review what a read-only backend exposes.
- **The gateway does not replace backend authorization.** All agents share the backend's one
  credential. The filters decide which operations exist as tools; the backend decides what the
  credential may do.
- **Upstream content is untrusted input to the model.** A response can contain text that tries to
  steer the agent (prompt injection). Combining a read tool over untrusted data with a write tool
  is the risky pattern; keep write operations on an explicit allowlist.

## What the gateway enforces

### SEC-001 Safe defaults

- The exposure mode defaults to `read_only`. `all` is accepted only in a `backend.yaml`.
- Configuration that would be silently misread is rejected: a key repeated in a YAML mapping (such
  as a second `exclude:` block that would replace the first), and an `exposure.operations` list
  outside `allowlist` mode (which would otherwise be ignored while every `GET`, or every operation,
  is exposed).
- Redirects are not followed unless `follow_redirects: true`.
- Schemas are read from local files only; there is no remote schema fetching.
- Secrets come from environment variables only.

### SEC-002 Network restrictions

- Each backend's HTTP client is pinned to the scheme, host and port of its `base_url`. Every
  outgoing request, and every redirect hop, is checked against the same parsed URL fields the
  connection is opened with; a mismatch fails with `origin_rejected` before anything is sent.
- On every request and redirect hop, the configured credential header (`Authorization`, or the
  API-key header) and the `Host` header are set by the gateway after the origin check. A schema
  that declares them as header parameters cannot let the agent replace the credential or reach
  another virtual host on the same address.
- With `follow_redirects: true`, only same-origin redirects succeed, up to 20 hops. A redirect whose
  `Location` is not a valid URL fails with `backend_unavailable`.
- `servers` entries in the schema are removed and never used. `base_url` decides where requests go.
- Tool arguments for path parameters are percent-encoded into a single path segment, so a value
  such as `../admin` cannot leave the templated path or the `base_url` prefix. A path argument that
  is missing, `null`, empty or only whitespace is refused with `invalid_argument` and nothing is
  sent: `/users//profile` or `/items/` would reach the parent or collection endpoint on many
  backends, which may be one the exposure filter excluded.
- A path argument must be a string, a number or a boolean. A list or an object is refused with
  `invalid_argument`: it would be joined with commas, and an empty one is the empty case again.
  Path parameters declared as arrays or objects therefore cannot be called in v1.0.
- A path argument whose value turns the request into another path of the same schema is refused
  with `invalid_argument`. With `/items/{id}` exposed and `/items/export` excluded, `id="export"`
  would otherwise send `GET /items/export`. The check runs against every path of the unfiltered
  schema, exposed or not, so call the other path's own tool if it has one. Two paths that overlap
  for every value (`/items/{id}` and `/{collection}/{key}`) do not block each other.
- That check compares the path the way the schema writes it: case-sensitive, one value per
  segment. It cannot see routes the schema does not list, and it does not know backend-specific
  equivalences such as case-insensitive routing or `;` path parameters. Backend authorization
  remains the control for those.
- The backend HTTP clients ignore environment proxies and `.netrc` (`HTTP_PROXY` and `HTTPS_PROXY` are
  not honoured in v1.0), so credentials cannot be routed through an unexpected proxy.
- TLS trust is the one setting taken from the environment: `SSL_CERT_FILE` or `SSL_CERT_DIR`, when
  set, replace the system certificate store, so private certificate authorities work. Whoever
  controls the gateway's environment can therefore decide which certificates are trusted; run it
  in an environment you control (see the checklist below).

### SEC-003 Tool authorization

Filtering happens before tool registration. An operation that is not selected has no tool, so an MCP
client cannot invoke it. The gateway registers tools only: no resources, no prompts and no generic
"make an HTTP request" tool. At startup it also checks that the tool names FastMCP registered are
exactly the configured ones and refuses to start otherwise.

### SEC-004 Sensitive data

See "Read this first". Response bodies are bounded (1 MiB by default, `max_response_bytes`) and only
JSON, `+json`, text or empty bodies are accepted; binary data is refused rather than passed on as
text. For 4xx errors the upstream body is forwarded in `detail`, truncated to 1000 characters, so a
backend that puts sensitive text in an error body exposes it to the agent. For 5xx responses the body
is never forwarded.

### SEC-005 Logging

Logs go to stderr. Request and response bodies, authorization headers and credentials are not logged;
sensitive field names are redacted as `***`; library loggers that could print URLs or headers are
capped at `WARNING`, and their records pass through the gateway's own handler and format, with
tracebacks only at `DEBUG`. YAML syntax errors in configuration files report a line and column, never
the offending text. Tool-call logs record backend, operation ID, method, status, duration and error
code, not arguments or results. See [Configuration](configuration.md#logging).

### SEC-006 Untrusted schemas

OpenAPI files are treated as data. The gateway parses them with a safe YAML loader, rejects external
`$ref` references (bundle documents first), rejects documents that expand past 500,000 nodes through
YAML aliases, and does not execute code, import modules or fetch URLs based on schema contents.
Descriptions from the schema are copied into tool definitions that the agent reads, so use schemas
you trust.

## Credentials

See [Authentication](authentication.md). Secrets are not stored in configuration, tool definitions,
logs, error payloads or exceptions. The repository ignores `.env`, and the example `.env.example`
files contain names only. Do not commit populated environment files or production configuration.

## Checklist before connecting a real API

1. Create a credential for the agent with the least privilege the backend offers.
2. Start with `read_only` or a short `allowlist`; read the `list-tools` output.
3. Exclude paths and operations that return personal, financial or secret data.
4. Keep `all` out of production unless you accept that the agent may modify and delete data.
5. Run the gateway as an unprivileged user, in an environment that holds only the variables it needs.
6. Re-run `validate` and review `list-tools` after every schema update, because `read_only` exposes new
   `GET` operations automatically.

To report a vulnerability, see [SECURITY.md](../SECURITY.md).
