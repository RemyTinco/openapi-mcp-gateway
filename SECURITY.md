# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately, using GitHub's private vulnerability reporting for
this repository (the "Report a vulnerability" button under the Security tab). Do not open a public
issue or pull request for a vulnerability.

Include the version, a description of the impact, and the smallest configuration and OpenAPI file that
reproduces it. Never include real credentials.

## Scope

In scope: ways to make the gateway call an operation that is not exposed, send a credential to a
different origin, leak a credential into logs, errors or tool definitions, read files or fetch URLs
through a crafted OpenAPI document, or crash it with a document of modest size.

Out of scope: what an exposed operation returns (the gateway does not redact responses by design),
and weaknesses of the backend APIs themselves. Read [docs/security.md](docs/security.md) for the
security model and its limits.

## Supported versions

The latest released version receives security fixes.
