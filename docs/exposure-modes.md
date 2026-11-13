# Exposure modes

The exposure policy decides which OpenAPI operations become MCP tools. An operation that is not
selected has no tool and cannot be called, whatever the agent asks for.

The default is `read_only`. `defaults.exposure.mode` in `gateway.yaml` accepts only `read_only` or
`allowlist`; `all` must be written in a `backend.yaml`, so it is always a visible choice.

## The three modes

```yaml
exposure:
  mode: read_only     # GET and HEAD only (the default)
```

```yaml
exposure:
  mode: allowlist     # exactly these operationIds
  operations:
    - products_list
    - products_retrieve
```

```yaml
exposure:
  mode: all           # every operation, including POST, PUT, PATCH, DELETE
```

| Method | `read_only` | `allowlist` | `all` |
| --- | --- | --- | --- |
| GET | Yes | If selected | Yes |
| HEAD | Yes | If selected | Yes |
| POST | No | If selected | Yes |
| PUT | No | If selected | Yes |
| PATCH | No | If selected | Yes |
| DELETE | No | If selected | Yes |
| OPTIONS | No | If selected | Yes |
| TRACE | No | If selected | Yes |

`GET` and `HEAD` are treated as nominally safe, not guaranteed free of side effects: some APIs
change state on a `GET`. Backend authorization always remains necessary. See [Security](security.md).

In `allowlist` mode, every operationId listed must exist in the schema. A missing one is a
configuration error (exit code 2), so a typo cannot silently hide a tool. `operations` is only
valid in `allowlist` mode: a non-empty list with `read_only`, `all` or no `mode` at all (when the
inherited mode is not `allowlist`) is a configuration error, because the list would otherwise be
silently ignored and every `GET`, or every operation, exposed instead:

```
error: [shop] .../backends/shop/backend.yaml: exposure.operations: operations are only used in allowlist mode, but the mode is read_only; set mode: allowlist or remove the list
```

`validate`, `list-backends`, `list-tools` and `serve` all report it.

## Exclusions

Exclusions apply after the mode, in every mode. Write each key once: a second `exclude:` (or any
other repeated key) in the same mapping is a configuration error, not a merge. Real output for the
single-backend example with a second `exclude:` block added:

```
error: [shop] .../backends/shop/backend.yaml: duplicate key 'exclude' (line 12)
```

1. All operations are discovered.
2. The mode selects some of them.
3. Operations named in `exclude.operations` are removed.
4. Operations whose path matches a pattern in `exclude.paths` are removed.
5. The remaining operations get tool names, which are checked for uniqueness.

```yaml
exposure:
  mode: read_only
  exclude:
    operations:
      - products_export
    paths:
      - /api/internal/*
      - /api/admin/*
```

An excluded operationId that is not in the schema produces a warning, not an error: the schema may
simply have dropped the operation. A path pattern that matches no operation also produces a warning.
Warnings appear in `validate` output (lines starting with `!`) and as `backend.warning` log events.

Real `validate` output for a backend that excludes `products_export` and `/admin/*`, neither of which
exists in its schema:

```
! [shop] exposure.exclude.operations: 'products_export' matches no operation in the schema
! [shop] exposure.exclude.paths: '/admin/*' matches no operation in the schema
```

## Path patterns

Patterns are matched against the path template text in the OpenAPI document, for example
`/orders/{id}/`, not against a concrete URL.

- A pattern must start with `/`.
- `**` is rejected as a configuration error. Use a trailing `/*` for a prefix and everything below.
- The path and the pattern are split on `/`. Each pattern segment matches exactly one path
  segment, case-sensitively.
- `*` is the only wildcard and matches any characters within one segment (`/api/v*/users` matches
  `/api/v2/users`).
- `?`, `[...]` and `{id}` are literal characters. `/users/{id}` matches only a template written
  exactly that way, and `/files/[abc]` matches only that text.
- A final `*` segment matches the prefix itself and everything below it. `/api/admin/*` matches
  `/api/admin`, `/api/admin/users` and `/api/admin/users/{id}`, but not `/api/administrators`.
- Repeated and trailing slashes are ignored: `/api/admin/`, `//api//admin` and `/api/admin` are
  the same.

| Pattern | Path template | Match |
| --- | --- | --- |
| `/api/admin/*` | `/api/admin/users/{id}` | yes |
| `/api/admin/*` | `/api/admin` | yes |
| `/api/admin/*` | `/api/administrators` | no |
| `/api/*/users` | `/api/v2/users` | yes |
| `/api/*/users` | `/api/v2/users/{id}` | no |
| `/users/{id}` | `/users/{id}` | yes |
| `/users/{id}` | `/users/42` | no |
| `/Admin/*` | `/admin/users` | no |

A pattern that is not valid is reported when you run `validate`:

```
error: [shop] .../backends/shop/backend.yaml: exposure.exclude.paths: invalid exclude path pattern '/internal/**': '**' is not supported; use a trailing '/*' to match a prefix and below
```

(path shortened; exit code 2.)

## Empty exposure

A backend that ends up with no tools produces a warning, and the gateway still starts if at least
one other backend has tools. If no backend exposes any tool, `validate`, `list-tools` and `serve`
fail with `no tools exposed by any backend` (exit code 2). `list-backends` still lists every
discovered backend, with `0` in the `TOOLS` column, and exits 0, so you can see what was found.
Real output for the single-backend example with `exclude.paths: [/*]`:

```
BACKEND   BASE_URL                   MODE        TOOLS
shop      https://shop.example.com   read_only   0
```
