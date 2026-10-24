# FinBridge pre-publication checklist

Use these steps **inside `~/FinBridge`**, not `~/quickstart`. The audit script is an aid, not an exhaustive security review.

## Before first Git commit

- [ ] Confirm this is the standalone `FinBridge/` directory (not the old Plaid Quickstart repository).
- [ ] Rotate any Plaid Sandbox secret/access token previously disclosed or committed.
- [ ] Ensure `.env` exists only locally and contains no values copied into `.env.example`.
- [ ] Confirm `finbridge.db`, `.env`, `.venv`, `__pycache__`, and screenshot exports containing tokens are not staged.
- [ ] Inspect every `.py`, `.js`, `.html`, `.md`, and config file for hardcoded credentials, personally identifying details, raw API payloads, and account data.
- [ ] Run `python scripts/release_preflight.py`; review warnings without copying sensitive content into chat.
- [ ] Run `python -m pytest -q tests/` using the project's own virtual environment.
- [ ] Start `python repair_item.py` locally and verify `/console` and an incident case workflow.
- [ ] Add a **sanitized** screenshot to `docs/images/support-console.png` and uncomment the README image link if desired.
- [ ] Retain Plaid Quickstart license notices for upstream code and choose a separate license for original FinBridge code if desired.
- [ ] Confirm the `README.md` matches actual behavior, including Sandbox-only limitations.

## Important hardening opportunities

1. **No public deployment:** The web console and case PATCH endpoint do not have authorization. Keep the app bound to localhost; add authentication, CSRF protections, and session management before any hosted deployment.
2. **Malformed response handling:** Verify that missing/malformed `item` or `item.error` never becomes `healthy`. Add validation tests if logic changes.
3. **Outage semantics:** Distinguish transport/API outages from customer Item errors; prevent `unavailable` and `api_request_failed` from being interpreted as successful recovery. Consider explicit incident types in a later version.
4. **Single Item:** Database schema and incident grouping are not multi-tenant or multi-Item safe.
5. **Development server:** Flask's built-in server is not production infrastructure.
6. **No secrets in logs:** Don't print access tokens, client ID/secret, complete request bodies, or credentials in error reports.

## Review Git before publishing

```bash
cd ~/FinBridge
python scripts/release_preflight.py
python -m pytest -q tests/

# If Git is initialized, inspect this before staging:
git status --short
git ls-files
```

If files are already staged, run `git diff --cached --name-only` and inspect each staged source file. **Do not use `git add .` blindly.** Prefer explicitly staging reviewed files.

Do not publish until the preflight flags are resolved and the included third-party license is retained.
