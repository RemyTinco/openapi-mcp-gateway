# Contributing

Thanks for helping. This is a small project; the aim is a gateway that stays easy to audit.

## Setup

You need Python 3.14+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/RemyTinco/openapi-mcp-gateway
cd openapi-mcp-gateway
uv sync --locked
```

## Checks

Every pull request must pass these four commands, which CI also runs:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src/
uv run pytest
```

`uv run ruff format .` fixes formatting. Every Ruff rule is enabled (`select = ["ALL"]`); the
few exceptions are listed with their reason in `pyproject.toml`. Prefer fixing the code to adding
an ignore, and give any `# noqa` a reason. Ruff is pinned to an exact version, because a new
release can add rules: upgrade it in its own change. The default `pytest` run skips the startup benchmark
(`-m "not perf"`); run it with `uv run pytest -m perf -v -s`. Test output should stay free of warnings
and stray output.

## Guidelines

- Write the test first and watch it fail for the right reason.
- Backends are mocked with `httpx2.MockTransport`. The HTTP library is httpx2 (FastMCP 4 requires it);
  respx is not used.
- Tests live in `tests/unit`, `tests/integration` (real FastMCP server, stdio subprocess) and
  `tests/compat` (drf-spectacular schemas). There are no `__init__.py` files; pytest runs with
  `--import-mode=importlib`.
- stdout belongs to the MCP protocol. Anything a human should read goes to stderr, except the
  output of the offline commands.
- Secrets never appear in logs, exceptions or tool definitions. Error messages name the variable,
  not its value.
- The published package must not depend on Django, DRF or drf-spectacular. They are used only to
  generate fixtures; see `tests/fixtures/drf/README.md`.
- Dependencies are bounded to tested major versions, and each lower bound is the oldest release the
  `lowest-deps` CI job passes with (`fastmcp>=4.1.7`, `typer>=0.26`, `pyyaml>=6.0.1`, and so on).
  Raise a lower bound when the code or tests need a newer release. Do not edit `uv.lock` by hand;
  use `uv lock`.
- Never commit credentials, populated `.env` files or production configuration. Examples use
  `example.com` hosts only.
- Documentation must describe what the code does. If you change behaviour, update the pages under
  `docs/`; `tests/unit/test_examples.py` fails if a configuration key is missing from
  `docs/configuration.md`.

## Releases

Maintainers publish by pushing a tag `vX.Y.Z` that matches the `version` in `pyproject.toml`. The
release workflow builds the package and publishes it to PyPI with trusted publishing; no token is
stored in the repository.

## Security issues

Do not open a public issue. See [SECURITY.md](SECURITY.md).
