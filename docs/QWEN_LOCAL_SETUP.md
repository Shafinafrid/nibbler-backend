# Running Qwen locally as Nibbler's third provider

Qwen is the last stop in Nibbler's fallback chain: it answers when both Luna and
Haiku cannot. The weights run on the owner's M4 Pro MacBook (24 GB unified
memory) behind `llama-server`, and Railway reaches them over an authenticated
HTTPS tunnel.

Nothing in the backend loads a model. From Railway's point of view Qwen is an
HTTP provider like any other — see `app/services/llm/qwen.py`.

**Every step below is manual and owner-run.** `scripts/run_qwen_local.sh`
deliberately installs nothing, downloads nothing, opens no port and starts no
tunnel; it checks the prerequisites and starts the server.

---

## Which model, and why not Qwen 3.5

**Qwen3.5 has no 14B variant.** Verified 2026-08-02: the family shipped as
397B-A17B, then 122B-A10B / 35B-A3B / 27B, then 9B / 4B / 2B / 0.8B. There is
no 14B at any point in the line.

So this uses the latest official Qwen 14B:

| | |
|---|---|
| Repository | `Qwen/Qwen3-14B-GGUF` (official Qwen org) |
| File | `Qwen3-14B-Q4_K_M.gguf` |
| License | Apache 2.0 |
| Size on disk | ~9 GB |
| Native context | 32,768 tokens (131,072 with YaRN) |
| Non-thinking | `/no_think`, sent automatically by the adapter |
| Server | `llama-server` (llama.cpp), officially supported |

Q4_K_M at ~9 GB leaves comfortable headroom inside 24 GB alongside macOS.
`QWEN_MODEL` and `QWEN_BASE_URL` are configuration, so moving to a bigger model
later — Qwen3.5-27B at Q4_K_M is ~16 GB and would also fit — is an
environment-variable change, not a code change.

### Context size: 32K, not 8K

An 8K window looks generous and **cannot serve Nibbler's largest request**:

| | tokens |
|---|---|
| System prompt (`SESSION_SYSTEM`) | ~700 |
| Retrieved excerpts (15-min read: 14 chunks × 500) | 7,000 |
| Growth profile, targets, interaction instruction | ~300 |
| Reserved output (15-min deck: 1500 + 12×450 + 9×120, capped at 8,000) | 8,000 |
| **Total** | **~16,000** |

At `--ctx-size 8192` the prompt alone overflows, and llama.cpp does not error —
it drops the **start** of the prompt, which is exactly where the instructions
are. The result is a model improvising a card deck with no format rules, which
then fails validation and falls back, having burned the time anyway.

Because being wrong here is silent, the minimum carries a **1.5× margin**:
`QWEN_MIN_CONTEXT_TOKENS = 24576`, derived in `router.py` from the same
constants as the table above rather than picked by hand — a hand-picked number
drifts the moment a card target or chunk size changes.

The launch script defaults to **32768** (Qwen3-14B's native window, no YaRN
needed). The backend declares the same number in `QWEN_CONTEXT_SIZE` and
**refuses to boot below 24576**, because it cannot ask the server what window it
was started with. Keep the two in sync.

---

## 1. Install the server (once)

```bash
brew install llama.cpp
```

## 2. Download the weights (once, ~9 GB)

```bash
mkdir -p "$HOME/models"
huggingface-cli download Qwen/Qwen3-14B-GGUF Qwen3-14B-Q4_K_M.gguf --local-dir "$HOME/models"
```

## 3. Pick a shared secret

One long random token, used in two places: the `--api-key` llama-server enforces,
and `QWEN_API_KEY` in the backend's environment. They must match.

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Keep it out of git. It goes in your shell profile or `.env`, never in a
committed file.

## 4. Start the server

```bash
export QWEN_API_KEY='the-token-from-step-3'
./scripts/run_qwen_local.sh
```

It binds to `127.0.0.1` only. A laptop that joins café Wi-Fi with an open
`0.0.0.0` bind is offering a free GPU to everyone on that network, so the tunnel
is the only route in.

## 5. Expose it over HTTPS (Cloudflare Tunnel)

Railway cannot reach `127.0.0.1` — inside a Railway container that address *is*
the container. A tunnel is not optional.

```bash
brew install cloudflared
cloudflared tunnel login                      # opens a browser; owner-only step
cloudflared tunnel create nibbler-qwen
cloudflared tunnel route dns nibbler-qwen qwen.getnibbler.com
cloudflared tunnel run --url http://127.0.0.1:8080 nibbler-qwen
```

That gives `https://qwen.getnibbler.com` → your local server, with TLS
terminated by Cloudflare.

## 6. Point the backend at it

In Railway:

```
QWEN_BASE_URL=https://qwen.getnibbler.com/v1
QWEN_API_KEY=the-token-from-step-3
QWEN_MODEL=qwen3-14b
QWEN_TIMEOUT_SECONDS=180
```

`QWEN_MODEL` must match the `--alias` the server advertises (the launch script
sets it from the same variable).

## 7. Check it end to end

With the server running, from the backend directory:

```bash
RUN_QWEN_CONTRACT_TEST=1 .venv/bin/python tests/smoke_qwen_contract.py
```

It refuses to run without the opt-in variable, uses text written for the test,
and prints sanitized usage — no credentials, no book content. Its one network
call goes to your own machine.

There is a second gated script, `tests/smoke_live_llm.py`, which makes **real,
billed** calls to a named provider (`luna`, `haiku` or `qwen`). Neither runs in
a normal test pass: both are named `smoke_*`, which keeps them outside the
`tests/test_*.py` glob the suite runner uses, and both refuse to start without
their own opt-in variable.

```bash
# ⚠️ BILLED — one real call to the named provider
RUN_LIVE_LLM_TESTS=1 .venv/bin/python tests/smoke_live_llm.py luna
```

---

## Security requirements

The endpoint is on the public internet the moment the tunnel is up.

- **HTTPS only** — Cloudflare terminates TLS; never publish plain HTTP.
- **Authenticated** — `--api-key` is enforced by llama-server on every request.
- **No management surface** — `--no-webui` means a leaked URL exposes the chat
  endpoint behind the key, not a model console.
- **Bounded concurrency** — `--parallel 1` means one generation at a time, so a
  flood of requests queues rather than thrashing the Mac's memory. The backend
  enforces its own timeout on top (`QWEN_TIMEOUT_SECONDS`).
- **Bounded prompt length** — `--ctx-size` is what the model can hold, not an
  HTTP request-size limit; a prompt beyond it is truncated, not rejected. It is
  therefore a correctness setting first and a safety one second, which is why
  it is **32768** here and why the backend refuses to boot below 24576. See
  "Context size" above for the arithmetic.
- **No prompt logging** — llama-server does not log request bodies by default.
  Do not add `--log-verbose`, which would write users' book excerpts to disk.
- **Loopback bind** — the server itself never listens on a public interface.

## When the Mac is asleep

Nothing breaks. A closed lid, a dropped tunnel, a quit `llama-server` and a
restart all surface to the backend as a connection failure, which
`app/services/llm/qwen.py` classifies as `TRANSPORT`:

- eligible for fallback, so the request continues to whichever provider is next;
- opens Qwen's circuit breaker, so the following requests skip it for the
  cooldown instead of each paying the same timeout.

Qwen being offline is an expected state, not an incident. It is the third
provider precisely because it is the least available one.

## Cost

`app/services/llm/usage.py` records Qwen's **API** cost as `$0.00`, because there
is no per-token bill. That is not the same as free: the Mac, its electricity,
its wear and the tunnel are real costs this system does not measure. Any cost
comparison that puts Qwen at zero should say "API spend" explicitly.
