# VERA — Agent Imperatives

These rules apply to **every** interaction, code change, and commit in this project.
The agent must follow them unconditionally.

---

## 1. Versioning — Mandatory on Every Code Change

- The project uses **Semantic Versioning**: `MAJOR.MINOR.PATCH`
  - `PATCH` — every code change, no matter how small
  - `MINOR` — new user-visible feature or capability added
  - `MAJOR` — breaking change or major architecture shift
- The single source of truth is `/VERSION` at the repo root.
- **Before committing any code change**, update `/VERSION`.
- **After updating `/VERSION`**, immediately update `CHANGELOG.md`.
- Commit message must include the new version: `chore: bump to vX.Y.Z` or inline.

## 2. Changelog — Mandatory on Every Version Bump

- Every version bump **must** produce a new entry in `CHANGELOG.md`.
- Format (Keep a Changelog style):

```
## [X.Y.Z] - YYYY-MM-DD
### Added / Changed / Fixed / Removed
- Description of what changed and why.
```

- Do not skip entries. Do not batch multiple version bumps into one entry.

## 3. Spoken Version Requirement

- The assistant **must** be able to speak the current version number:
  - **At startup** — announce version during boot sequence via TTS.
  - **On verbal request** — respond to any phrase like "what version are you?"
    or "tell me your version number" with a spoken version string.
- `src/core/version.py` is the canonical in-code version source; it reads `/VERSION`.
- All services that perform TTS must be able to call `version.get_version()`.

## 4. Code Quality Imperatives

- Run existing tests after every non-trivial code change.
- Do not leave debugging print statements in committed code.
- Every new module in `src/` must have a corresponding stub in `tests/`.
- Configuration values go in `config/`, never hardcoded.

## 5. Hardware Safety Imperatives

- **Servo**: Never command the DS3218 outside its 270° mechanical range.
  When crossing the logical 360°→1° wrap, always traverse the long way
  (backward through the mechanical range). Enforce this in code, not just docs.
- **Fan**: Always fail-safe to 100% duty if TMP117 read fails.
- **Hailo-8**: Gracefully degrade to CPU inference if accelerator is unavailable.

## 6. Commit Hygiene

- Every commit must include the Co-authored-by trailer:
  `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`
- Push to `main` after every logical unit of work.
- Never force-push to `main`.

## 7. Architecture Diagram — Mandatory on Every Architectural Change

- The system architecture diagram lives at `docs/architecture/architecture.pdf`.
- The Graphviz source is `docs/architecture/architecture.dot` (the source of
  truth — never hand-edit the rendered outputs).
- Regenerate **all three** rendered outputs (`.pdf`, `.svg`, `.png`) via
  `bash docs/architecture/build.sh` and commit them alongside the `.dot`
  edit in the same commit.
- The diagram **must** be updated whenever any of these is added, removed,
  or renamed:
  1. A service (anything under `src/services/`)
  2. A driver subsystem (e.g., a new `src/<subsystem>/`)
  3. A hardware component
  4. A systemd unit (under `services/systemd/`)
  5. A bus topic that has a cross-service consumer
  6. An external interface (CLI, ZMQ socket, HTTP, MQTT, …)
  7. A process boundary (splitting/merging units)
- The diagram's title block contains the version string. Bump it together
  with `/VERSION` per imperative #1.
- If a PR/commit changes architecture without updating the diagram, the
  change is incomplete — fix it before merging.

## 8. CLI Consistency — Mandatory on Every CLI Change

The CLI entry point is `scripts/desktop-assistant` (symlinked to `/usr/local/bin/vera` and `/usr/local/bin/da`).

**Any time a CLI command is added, removed, renamed, or its arguments change:**

1. **Update `cmd_help()`** — the `commands` dict inside `cmd_help()` must exactly match
   every top-level command registered with `sub.add_parser()`. No stale entries, no
   missing entries.
   - For commands with subcommands (e.g. `servo`), list all sub-commands in the
     description string under the parent key.
   - The `help=` string on `add_parser()` and the `commands` dict entry must be
     consistent.

2. **Remove dead code** — if a handler function or parser registration is removed, delete
   the corresponding function definition too. Unreachable code after a `return` statement
   must be removed immediately.

3. **Verify `vera help` renders cleanly** — run `python3 scripts/desktop-assistant help`
   and confirm every command appears with an accurate one-line summary before committing.

4. **`vera help <command>`** — every entry in the `commands` dict must support the
   single-command lookup path (`vera help servo`, `vera help meet`, etc.).

Failure to keep `cmd_help()` in sync with the registered subparsers is a bug, not a
style issue. Fix it in the same commit as the CLI change.

## 9. Commit After Every Prompt

- After completing the work requested by each user prompt, commit all resulting
  changes to the current branch in use.
- Do not leave completed work uncommitted between prompts. Include the required
  version bump, changelog entry, and Co-authored-by trailer for each commit.
- Keep unrelated pre-existing user changes intact and do not commit them unless
  they are part of the requested work or are explicitly requested.
