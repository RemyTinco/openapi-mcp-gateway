"""Examples, documentation and workflows must stay runnable, safe and in step with the code."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml
from typer.testing import CliRunner

from openapi_mcp_gateway.cli import app
from openapi_mcp_gateway.config import models
from openapi_mcp_gateway.http import client

if TYPE_CHECKING:
    from pydantic import BaseModel

ROOT = Path(__file__).parents[2]
EXAMPLES = ROOT / "examples"
DOCS = ROOT / "docs"
EXAMPLE_NAMES = ["single-backend", "multi-backend", "django-drf-spectacular"]
# Every environment variable any example asks for, set to a dummy value.
DUMMY_ENV = {
    "SHOP_API_TOKEN": "dummy",
    "MESSAGING_API_KEY": "dummy",
    "INVENTORY_API_TOKEN": "dummy",
    "DRF_AUTHORIZATION": "dummy",
}
_SKIPPED_DIRS = {".git", ".venv", ".superpowers", "node_modules", "dist", ".mypy_cache"}


def _repo_files(pattern: str) -> list[Path]:
    return [
        p
        for p in ROOT.rglob(pattern)
        if p.is_file() and not _SKIPPED_DIRS.intersection(p.relative_to(ROOT).parts)
    ]


@pytest.mark.parametrize("example", EXAMPLE_NAMES)
def test_example_validates(example: str) -> None:
    result = CliRunner().invoke(
        app, ["validate", "--config", str(EXAMPLES / example / "gateway.yaml")], env=DUMMY_ENV
    )
    assert result.exit_code == 0, result.stderr
    assert result.stdout.rstrip().endswith("Validation successful.")


@pytest.mark.parametrize("example", EXAMPLE_NAMES)
def test_example_without_secrets_fails_with_credentials_error(example: str) -> None:
    unset = dict.fromkeys(DUMMY_ENV)
    result = CliRunner().invoke(
        app, ["validate", "--config", str(EXAMPLES / example / "gateway.yaml")], env=unset
    )
    assert result.exit_code == 4


@pytest.mark.parametrize("example", EXAMPLE_NAMES)
def test_example_env_template_lists_exactly_the_required_variables(example: str) -> None:
    template = EXAMPLES / example / ".env.example"
    names = {line.split("=", 1)[0] for line in template.read_text().splitlines() if "=" in line}
    needed: set[str] = set()
    for backend_yaml in (EXAMPLES / example).glob("backends/*/backend.yaml"):
        auth = yaml.safe_load(backend_yaml.read_text()).get("auth", {})
        needed |= {v for k, v in auth.items() if k.endswith("_env")}
    assert names == needed


def test_no_populated_env_files() -> None:
    env_files = [*_repo_files(".env"), *_repo_files(".env.*")]
    assert any(p.name == ".env.example" for p in env_files), "expected .env.example templates"
    for path in env_files:
        assert path.name != ".env", f"{path} must not be committed or created"
        for line in path.read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                name, sep, value = line.partition("=")
                assert sep, f"{path}: {line!r}"
                assert re.fullmatch(r"[A-Z][A-Z0-9_]*", name), f"{path}: {line!r}"
                assert value == "", f"{path}: {name} has a value"


def test_no_secret_looking_strings_in_examples_and_docs() -> None:
    scanned = [
        *_repo_files("*.md"),
        *_repo_files("*.yaml"),
        *_repo_files("*.yml"),
        *_repo_files("*.json"),
    ]
    scanned = [p for p in scanned if "tests" not in p.relative_to(ROOT).parts]
    scanned = [p for p in scanned if p.name != "uv.lock"]
    assert EXAMPLES / "single-backend" / "gateway.yaml" in scanned
    patterns = [
        re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
        re.compile(r"\b(sk|pk|ghp|gho|xox[bp])[-_][A-Za-z0-9]{16,}"),
        re.compile(r"AKIA[0-9A-Z]{16}"),
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ]
    for path in scanned:
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            assert not pattern.search(text), f"{path} looks like it contains a secret"


def test_no_personal_details_in_published_files() -> None:
    files = [ROOT / "pyproject.toml", ROOT / "SECURITY.md", *_repo_files("*.yml")]
    files += [
        p for p in _repo_files("*.md") if "superpowers" not in p.parts and "tests" not in p.parts
    ]
    email = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
    pyproject = ROOT / "pyproject.toml"
    # The maintainer chose to publish a contact address: it may appear in the package
    # metadata and nowhere else.
    authors = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["authors"]
    declared = {author["email"] for author in authors if "email" in author}
    for path in files:
        text = path.read_text(encoding="utf-8")
        allowed = declared if path == pyproject else set()
        # Only the pyproject dependency pins and the example.com placeholders may look like this.
        found = [
            m for m in email.findall(text) if not m.endswith("example.com") and m not in allowed
        ]
        assert not found, f"{path} contains an email address: {found}"


def _field_names(model: type[BaseModel]) -> set[str]:
    return {info.alias or name for name, info in model.model_fields.items()}


def test_documented_config_keys_match_models() -> None:
    file_models = [
        models.GatewayFile,
        models.ServerFile,
        models.BackendsFile,
        models.DefaultsFile,
        models.DefaultExposureFile,
        models.LoggingFile,
        models.HttpFile,
        models.BackendFile,
        models.ExposureFile,
        models.ExcludeFile,
        models.AuthBearer,
        models.AuthApiKey,
        models.AuthNone,
    ]
    keys = set().union(*(_field_names(m) for m in file_models))
    assert {"max_response_bytes", "max_connections", "follow_redirects", "token_env"} <= keys
    text = (DOCS / "configuration.md").read_text(encoding="utf-8")
    # A key counts as documented when it is a component of some backticked name such as
    # `backends.directory` or `exclude.paths`.
    documented = {
        part.rstrip(":") for token in re.findall(r"`([^`\n]+)`", text) for part in token.split(".")
    }
    missing = sorted(keys - documented)
    assert not missing, f"keys missing from docs/configuration.md: {missing}"


def test_documented_error_codes_match_transport() -> None:
    source = Path(client.__file__).read_text(encoding="utf-8")
    codes = {code for code, _ in client._STATUS_ERRORS.values()}
    codes |= set(re.findall(r'_error\(\s*"(\w+)"', source))  # self._error("code", ...)
    codes |= set(re.findall(r'\(\s*"(\w+)",\s*f"', source))  # fallback ("code", f"...")
    # Errors raised before the request is built, by the gateway's middleware.
    middleware = (Path(client.__file__).parents[1] / "mcp" / "gateway.py").read_text("utf-8")
    codes |= set(re.findall(r'"error":\s*"(\w+)"', middleware))
    assert "invalid_argument" in codes
    assert len(codes) >= 14, codes  # guards against the patterns silently matching nothing
    text = (DOCS / "configuration.md").read_text(encoding="utf-8")
    assert not sorted(c for c in codes if f"`{c}`" not in text)


def _markdown_files() -> list[Path]:
    files = [ROOT / "README.md", ROOT / "CONTRIBUTING.md", ROOT / "SECURITY.md"]
    files += sorted(DOCS.glob("*.md")) + sorted(EXAMPLES.glob("*/README.md"))
    return files


@pytest.mark.parametrize("path", _markdown_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_markdown_links_resolve(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        if re.match(r"[a-z]+:", target) or target.startswith("#"):
            continue
        file_part = target.split("#", 1)[0]
        assert (path.parent / file_part).exists(), f"{path}: broken link {target}"


@pytest.mark.parametrize("name", ["ci.yml", "release.yml"])
def test_workflow_parses(name: str) -> None:
    data = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text())
    assert data["jobs"]


def test_ci_workflow_runs_the_required_checks() -> None:
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    for command in ("ruff check .", "ruff format --check .", "mypy src/", "pytest"):
        assert f"uv run {command}" in text
    assert "uv sync --locked" in text
    assert "--resolution lowest-direct" in text
    # The lowest-versions job must resolve outside the project so uv.lock is never rewritten.
    assert "uv sync --resolution" not in text
    assert "uv lock" not in text
    assert "uv pip install" in text


def test_release_workflow_uses_trusted_publishing_without_tokens() -> None:
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    data = yaml.safe_load(text)
    publish = data["jobs"]["publish"]
    assert publish["environment"] == "pypi"
    assert publish["permissions"]["id-token"] == "write"
    assert data["permissions"] == {"contents": "read"}
    assert "does not match" in text
    assert "uv build" in text
    assert "pypa/gh-action-pypi-publish" in text
    assert "secrets." not in text
    assert "password" not in text.lower()
    assert data[True]["push"]["tags"] == ["v*"]  # PyYAML reads the key `on` as True


def test_release_publishes_only_after_the_checks_pass() -> None:
    data = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text())
    jobs = data["jobs"]
    needs = jobs["publish"]["needs"]
    needs = [needs] if isinstance(needs, str) else needs
    checking = [
        name
        for name in needs
        if all(
            any(f"uv run {command}" in str(step.get("run", "")) for step in jobs[name]["steps"])
            for command in ("ruff check .", "ruff format --check .", "mypy src/", "pytest")
        )
    ]
    assert checking, f"publish needs {needs}, none of which runs all four checks"
    check = jobs[checking[0]]
    # The checks run on the tagged commit (a plain checkout of the pushed ref), locked.
    assert any("actions/checkout" in str(step.get("uses", "")) for step in check["steps"])
    assert any("uv sync --locked" in str(step.get("run", "")) for step in check["steps"])
