# Desktop Assistant — Agent Imperatives

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
