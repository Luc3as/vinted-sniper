# CLAUDE.md — vinted-sniper

Standalone fork (no upstream merges). Live at vinted.veduci.sk, Portainer stack 31.
Baseline rules imported from `/opt/AI_projects/_Default_project/project_harness/`
(AI-DEV-PRACTICES.md, CLAUDE.md) — tailored to this project.

## Token economy

- Grep/Glob before Read; read only the line ranges you need from big files.
- Delegate broad multi-file searches to a subagent; keep the main context for synthesis.
- Scripts should print findings (counts, matches), never raw dumps.
- Verify external data (API shapes, DB columns, n8n payloads) with one cheap probe
  before writing mapping code — a wrong assumption costs a full rerun.
- Don't compress task specs to save tokens: state DONE-WHEN before starting;
  a misunderstanding-triggered rerun costs far more than the spec did.

## Coding discipline (A²D)

- **Verification files are protected**: never modify existing tests, lint/type config,
  thresholds, or mocks to get green. If one blocks you, stop and write why.
- **Bug fixes are test-first**: the test must fail before the fix; show the failing output.
- **On failure, never bypass**: no try/catch, mocks, or longer timeouts to route around
  a problem. Diagnose; bounded recovery = one hypothesis, one scoped change, then stop
  and report.
- **Surgical changes**: no incidental cleanup of adjacent code. Observations become
  written suggestions, never auto-fixes.
- **Brownfield**: do contract discovery ("who calls this and what do they expect back?")
  before touching an existing path.
- New dependencies only with explicit approval.
- What must actually hold belongs in a test or hook (sensor), not in prose here.

## Project rules

- Tests: plain `.venv/bin/python -m pytest` — addopts already has `-q`; adding another
  hides the summary line.
- Gates before commit: pytest, `ruff format --check`, `ruff check`, `mypy`.
- User-facing verdict/alert/UI text: plain language, no stats jargon (percentile,
  median). Slovak strings must be proper Slovak (not Czech) with correct diacritics.
- Sweep results never touch the `items` table; attach triage via `Repo.record_triage()`.
- Commit messages: English, conventional commits.

## Deploy (Portainer endpoint 2, stack 31)

- Build local image `vinted-sniper:impersonate` → load + redeploy via `/tmp/vs-deploy.py`.
- **ALWAYS fetch the live stack env fresh before the PUT** — never reuse a saved env
  snapshot (WEB_TOKEN overwrite incident, 2026-09-08).
- Never redeploy mid-sweep — the restart kills the running sweep.
- Never print secrets; collect new ones with `secure_env_collect`.

## n8n (n8n.lukasporubcan.sk)

- Always `validate_workflow` before publish; import/update deactivates a flow → republish.
- Haiku 4.5 flows need an explicit output-language field and sk≠cs word examples.
- Credential bindings survive `update_workflow` even when the response says
  `autoAssignedCredentials: []` — but verify after structural edits.
