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
| `test_llm_routing.py` | Config validation, single vs fallback mode, bounded retries, refusal-stops-chain, circuit breaker, HTTP-status classification |
| `test_llm_providers.py` | Per-adapter request shaping (Luna reasoning + strict schema, Haiku cache markers + forced tool-use, Qwen `/no_think`), usage normalization, cost arithmetic |
| `test_llm_workflows.py` | The four workflows end to end, every semantic deck rule, telemetry hygiene, proof AI image generation stays dark |
| `test_llm_hardening.py` | Regressions for the twelve findings in Hermes's 2026-08-02 audit — breaker lifetime, SDK retry defaults, refusal classification, billed-failure telemetry, local schema enforcement, quote grounding, context budget, config strictness |
| `test_book_images.py` | Extraction from real in-memory PDFs/EPUBs, rejection filters, visual dedup, EPUB spine/caption/alt, relevance shortlisting, server-side validation, Story spoiler guard, proof AI image generation stays dark |
| `test_book_image_access.py` | The image endpoint through the real HTTP stack — owner 200, stranger 404, unauthenticated refused, arbitrary keys refused, JSON not a redirect, refresh, book/account deletion cleanup |

### Isolation: `hermetic.py`

**Every suite imports `hermetic` before anything from `app`.** `app/config.py`
declares `env_file = ".env"` — a *relative* path — so a suite run from the repo
root loads the real `.env`, which holds production AWS, Pinecone, Voyage and
Firebase credentials.

Setting env vars beforehand is not enough: it covers the keys you thought of,
and every key you did not think of still comes from `.env`. That is how the
Batch A suite once made a **live S3 request with production credentials**.

`hermetic` closes three separate leaks:

1. **`.env`** — moves the process to an empty temp dir, so the relative path
   cannot resolve.
2. **Exported variables** — OVERWRITES every credential-shaped variable rather
   than `setdefault`-ing it. An inherited `DATABASE_URL` or `AWS_ACCESS_KEY_ID`
   is exactly the thing being defended against, so "only if absent" was the
   wrong verb. An inherited Postgres URL is replaced with a sandbox SQLite file;
   a suite that already chose its own SQLite path keeps it.
3. **The network** — blocks every non-loopback socket connection, and registers
   an `atexit` handler that **exits the run non-zero** if one was attempted.
   That last part is the point: blank AWS credentials do not stop boto3 opening
   a real TLS connection, and a test that accepts the resulting 502 will pass
   over it silently. "All suites exited 0" and "no suite touched the network"
   are different claims, and only the guard checks the second one.

### The LLM suites

Same rules, one addition: they use **fake provider clients** (`llm_fakes.py`) and
build settings with `Settings(_env_file=None, …)`, so there is no credential, no
network call and no paid API call anywhere in a normal run.

One trap worth knowing: `LLMService` finishes a Wisdom deck **in place** — it
strips schema null placeholders and shuffles quiz options on the object the
adapter returned. Give each fake response its own deep copy, or the second
scenario silently tests whatever the first one left behind.

Two scripts DO make real calls and are therefore named `smoke_*`, which keeps
them outside the `test_*` glob. Both refuse to run without an explicit opt-in:

```bash
# Your own Mac, after starting llama-server — see docs/QWEN_LOCAL_SETUP.md
RUN_QWEN_CONTRACT_TEST=1 .venv/bin/python tests/smoke_qwen_contract.py

# ⚠️ BILLED — one real call to the named provider
RUN_LIVE_LLM_TESTS=1 .venv/bin/python tests/smoke_live_llm.py luna
```
| `test_v2round_backend.py` | Active-source cap on upload, timezone cap window, book-delete cascade |
