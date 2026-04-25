# Versioning Scheme

This project uses [Semantic Versioning](https://semver.org/) — `MAJOR.MINOR.PATCH`.

## Rules

| Increment | When |
|---|---|
| `PATCH` | Any code change, no matter how small |
| `MINOR` | New user-visible feature or capability |
| `MAJOR` | Breaking change or major architectural shift |

## Source of Truth

- `/VERSION` — plain text file containing the current version (e.g. `0.1.0`)
- `CHANGELOG.md` — human-readable log of every version and its changes
- `src/core/version.py` — in-code accessor; reads `/VERSION` at import time

## Workflow (required on every code change)

1. Make code changes.
2. Determine increment level (PATCH at minimum).
3. Edit `/VERSION` with the new version string.
4. Add a new entry to `CHANGELOG.md`.
5. Commit both files along with code changes.
6. Push to `main`.

## Pre-release / Development

- Pre-release versions use the suffix `-dev` (e.g. `0.2.0-dev`) during active
  feature work on a branch; the suffix is removed on merge to `main`.

## Current Version

See `/VERSION`.
