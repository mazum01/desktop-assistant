---
name: security-scan
description: >
  Scan the entire VERA codebase for privacy and security concerns using a
  sub-agent. Findings are appended as new items to docs/TODO.md under a
  "Security & Privacy" section. The skill ONLY reports and logs — it never
  modifies source code. Use for "security scan", "check for secrets",
  "privacy audit", "find vulnerabilities", "scan for hardcoded credentials",
  "security review", or any request to audit the codebase for risks.
---

# Security Scan Skill

Runs a thorough privacy and security audit of the VERA codebase as a
sub-agent. Findings are appended to `docs/TODO.md`. **No source code is
modified.**

## When to use

- "Run a security scan"
- "Check for hardcoded secrets or API keys"
- "Privacy audit" / "Security review"
- "Are there any vulnerabilities in the codebase?"
- After any significant new feature lands (new service, new endpoint, new
  hardware driver)

## How to invoke

Launch a `general-purpose` sub-agent with the full prompt below. Wait for
the agent to finish, then summarise its findings for the user.

---

## Sub-agent prompt

```
You are a security and privacy auditor for the VERA (Vision-Enabled
Reasoning Agent) robotics project located at:
  /home/starter/Code/Desktop Assistant

Your ONLY job is to:
1. Scan the codebase for security and privacy concerns.
2. Deduplicate against what is already in docs/TODO.md.
3. Append NEW findings to docs/TODO.md under the "## Security & Privacy"
   section (create that section at the end of the file if it does not exist).
4. Report a summary back to the user — count of new items added, count of
   already-known items skipped, and a brief description of each NEW finding.

You MUST NOT modify any source file, config file, or test file. You MUST NOT
fix anything. You MUST NOT commit anything.

---

### What to scan for

Check ALL files under src/, scripts/, config/, services/, tests/, and
.github/ for the following categories:

**Secrets & Credentials**
- Hardcoded API keys, tokens, passwords, or secrets in any .py, .sh, .yml,
  .yaml, .json, .env, or .conf file.
- Credentials committed to git history (check recent git log --diff-filter=A
  for new files containing 'key', 'token', 'secret', 'password', 'api_key').
- Config files that should be in .gitignore but are not.

**Injection & Execution**
- Use of eval(), exec(), os.system(), subprocess with shell=True without
  input sanitisation.
- f-string or %-format SQL queries (if any database calls exist).
- Unsanitised user input passed to shell commands (e.g. in CLI handlers,
  web endpoints, or skill handlers).

**Network & Transport**
- HTTP (non-TLS) URLs in source for external API calls.
- Missing certificate verification (verify=False in requests/httpx calls).
- Open bind addresses (0.0.0.0) without authentication on web/API endpoints.
- ZMQ sockets without authentication or encryption (CURVE/PLAIN).

**Privacy & Data Retention**
- Logging or writing of face images, biometric embeddings, or PII to disk
  without user consent mechanism.
- Indefinite retention of recordings, face databases, or conversation logs
  without a documented purge policy.
- Face/person identity data transmitted over the network in plaintext.
- Camera streams exposed without authentication.

**Authentication & Authorisation**
- Web API endpoints (FastAPI routes) that perform sensitive actions without
  any auth check (e.g. controlling servos, triggering TTS, accessing files).
- Default credentials or empty passwords anywhere.

**File & System Safety**
- World-writable config or data directories.
- Symlinks followed insecurely (path traversal).
- Temporary files created in /tmp with predictable names (race condition).
- Overly broad file permissions in installed service units or scripts.

**Dependency & Supply Chain**
- Requirements pinned without hashes (pip-audit friendliness).
- Known-insecure package version ranges (check requirements.txt for packages
  with known CVEs if you can infer them).

---

### Output format for docs/TODO.md

For each NEW finding, append under `## Security & Privacy` using this format:

- [ ] **[CATEGORY]** `file:line` — One-line description of the risk and why
  it matters.

Categories: SECRETS, INJECTION, NETWORK, PRIVACY, AUTH, FILESYSTEM, DEPS

Example:
- [ ] **[NETWORK]** `src/services/web_service.py:45` — FastAPI `/record`
  endpoint accepts POST with no authentication; any LAN client can trigger
  recording.

If the section already exists, append only items not already present
(match on file path + description keyword to detect duplicates).

---

### Final report to user

After updating docs/TODO.md, output:

```
Security Scan Complete
======================
New findings added : N
Already in TODO    : M
Total concerns     : N+M

New findings:
  [CATEGORY] file:line — description
  ...

No source code was modified.
```
```

---

## Notes

- The sub-agent must be `general-purpose` (needs full tool access to grep,
  glob, view files, and edit docs/TODO.md).
- Do not run this skill if another security-scan agent is already in progress.
- After the sub-agent completes, relay its final report verbatim to the user.
- Do NOT summarise away findings — report every item.
