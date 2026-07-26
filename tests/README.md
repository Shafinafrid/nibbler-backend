# Backend persistence tests

Hand-rolled suites — no pytest, no CI, matching this repo's tooling reality.
Each file is a script that prints PASS/FAIL lines and exits non-zero on failure.

```bash
for t in tests/test_*.py; do .venv/bin/python "$t"; done
```

**They never touch production.** Each creates a throwaway SQLite database in a
temp dir and supplies its own settings via env vars, so the real `.env` (which
holds production secrets) is never loaded. They run FastAPI's `TestClient`
against the *real* routers with `get_db` / `get_current_user` overridden.

Two things to know if one fails oddly:

- **SQLite does not enforce foreign keys by default.** A cascade that works on
  Postgres will not fire here. That is deliberate — `DELETE /library/{id}`
  deletes child rows explicitly rather than relying on the database, precisely
  so the behaviour is provable in these tests.
- **`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` is Postgres-only**, so the
  migration runner prints a wall of expected failures on startup. The
  `CREATE INDEX` statements do apply, which is what the schema tests check.

| Suite | Covers |
|---|---|
| `test_backend_primary.py` | Scheduler stall from orphaned bites, model imports, unique indexes |
| `test_batch_a_backend.py` | `/sync` rate limits + payload validation, highlight update, generation cap |
| `test_batch_b.py` | Note/highlight session identity, legacy rows, delete-by-natural-key |
| `test_batch_c.py` | `session-complete` idempotency, `/bites/sessions`, growth LWW, erasure reporting |
| `test_v2round_backend.py` | Active-source cap on upload, timezone cap window, book-delete cascade |
