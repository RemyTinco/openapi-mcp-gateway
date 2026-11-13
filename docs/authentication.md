# Authentication

Each backend has its own credential and its own HTTP client. A credential is sent only to the
origin (scheme, host and port) of the backend it is configured for.

Version 1.0 uses one static credential per backend. There is no user impersonation, no OAuth flow
and no per-user authorization: every agent using the gateway acts with the same credential. The
backend remains responsible for authenticating and authorizing each request.

## Modes

No authentication (the default):

```yaml
auth:
  type: none
```

Bearer token, sent as `Authorization: Bearer <token>`:

```yaml
auth:
  type: bearer
  token_env: CATALOG_API_TOKEN
```

API key in a header:

```yaml
auth:
  type: api_key
  location: header
  name: X-API-Key
  value_env: MESSAGING_API_KEY
```

`location` must be `header`; query-parameter keys are not supported. `name` must be a valid HTTP
header name and cannot be `Host`, `Content-Length`, `Transfer-Encoding` or `Connection`. The
environment variable holds the complete header value.

The configured credential header always wins. If the OpenAPI document declares a header parameter
with the same name (`Authorization` for bearer tokens, or the API-key header), the gateway replaces
whatever value the agent passes with the configured one before the request leaves, on every
redirect hop too. The `Host` header is likewise always reset to the backend's host. For Django REST Framework token authentication the header is
`Authorization` and the value is `Token <your token>`, so the example in
[`examples/django-drf-spectacular`](../examples/django-drf-spectacular/) uses
`name: Authorization` with `value_env: DRF_AUTHORIZATION`.

## Where secrets live

Configuration files hold the **name** of an environment variable, never its value. Set the
variables in the environment of the process that starts the gateway:

```bash
export CATALOG_API_TOKEN=...
openapi-mcp-gateway serve --config ./gateway.yaml
```

Each example directory has a `.env.example` listing the variable names with empty values. The
gateway does not read `.env` files itself; if you keep secrets in one, load it with your shell or
a secrets manager, and keep it out of version control (this repository's `.gitignore` already
ignores `.env`).

A variable counts as **missing** when it is not set, is empty, contains only whitespace, or contains
a line break (which would allow header injection). It is **unusable** unless every character is
printable ASCII (spaces are allowed between other characters, but not at the start or end): a
non-ASCII character such as `é` could not be sent in an HTTP header. Missing or unusable credentials make `validate` and `serve`
fail with exit code 4 and an error naming the backend and the variable, never its value. `list-backends`
and `list-tools` do not need the variables:

```
error: [shop] .../backends/shop/backend.yaml: environment variable SHOP_API_TOKEN is not set
```

The secrets are read once at startup. After changing one, restart the gateway.

## What the gateway does with a secret

- It is held in memory, in the headers of that backend's HTTP client.
- It does not appear in tool definitions, logs (at any level), error payloads, exceptions or
  diagnostics.
- It is not sent to any other host, including on redirects; see [Security](security.md).
- `validate` checks that the variables exist and are usable. It does not contact the backend, so a
  wrong or expired token is only noticed on the first tool call, as an `authentication_failed`
  error.

Use credentials with the least privilege the backend can give, ideally a dedicated read-only
account or token for the agent.
