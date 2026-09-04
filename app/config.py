from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Database
    database_url: str

    # Image generation for nibble cards. INERT until openai_api_key is set —
    # see services/image_gen.py. Cheapest tier by default because every image
    # is a real charge (~$0.011 at gpt-image-1 low quality).
    openai_api_key: str = ""
    image_model: str = "gpt-image-1"
    image_quality: str = "low"
    # Default flipped True → False on 2026-08-02 after audit. The module was
    # already unreachable, but "unreachable" was an accident of nothing
    # importing it (and of a broken import inside it) rather than a decision.
    # A default of True meant the day someone wired it up, image generation
    # would switch itself on and start billing. Turning it on must take a
    # deliberate act.
    image_generation_enabled: bool = False

    # ── Text generation: provider-neutral routing ────────────────────────────
    # Nibbler talks to one of three interchangeable models through
    # app/services/llm/. Which one is a deployment decision, not a code change:
    # flip these variables in Railway and restart.
    #
    #   single   → call LLM_ACTIVE_PROVIDER and nothing else. This is the mode
    #              for judging output quality, because a silent fallback would
    #              mean reviewing a different model's work than you think.
    #   fallback → walk LLM_FALLBACK_ORDER, stopping at the first response that
    #              passes schema + semantic validation.
    llm_routing_mode: str = "fallback"          # single | fallback
    llm_active_provider: str = "luna"           # luna | haiku | qwen
    llm_fallback_order: str = "luna,haiku,qwen"
    llm_luna_enabled: bool = True
    llm_haiku_enabled: bool = True
    llm_qwen_enabled: bool = True
    # How long a provider stays skipped after it proves itself unwell (out of
    # credit, 401, unreachable). Process-local — see llm/circuit.py.
    llm_circuit_cooldown_seconds: int = 120

    # Luna (OpenAI GPT-5.6 Luna) — default provider.
    # DELIBERATELY SEPARATE from `openai_api_key` above, which belongs to the
    # inert image-generation module. Setting this key must never switch image
    # generation on, so the two never share a field.
    openai_llm_api_key: str = ""
    # Always the explicit Luna id: the bare `gpt-5.6` alias routes to Sol.
    openai_llm_model: str = "gpt-5.6-luna"
    # Capped at low by validate_llm_settings(); reasoning tokens bill as output.
    openai_llm_reasoning_effort: str = "low"
    openai_llm_timeout_seconds: float = 90.0

    # Haiku (Anthropic Claude Haiku 4.5) — first fallback.
    # `claude_api_key` is the pre-August-2026 name, kept working so an existing
    # Railway deploy does not break the moment this ships; the new name wins if
    # both are set. It no longer has a required marker, because Luna-only or
    # Qwen-only deployments must not need an Anthropic key at all.
    anthropic_llm_api_key: str = ""
    anthropic_llm_model: str = "claude-haiku-4-5"
    anthropic_llm_timeout_seconds: float = 120.0
    claude_api_key: str = ""

    # DEPRECATED AND UNREAD. Nothing in the codebase looks at these — model
    # choice is a routing decision now, never a subscription-tier one, and
    # Sonnet is not used at all. They are declared solely because
    # pydantic-settings rejects unknown env keys, and both are still set in
    # Railway and in local .env files: deleting the fields would turn the next
    # deploy into a boot failure. Safe to remove here once they are gone from
    # the Railway variables.
    claude_model_free: str = ""
    claude_model_paid: str = ""

    # Qwen — second fallback, an OpenAI-compatible HTTPS endpoint.
    # Railway cannot reach a MacBook's localhost or LAN, so this is a public
    # tunnel URL with its own bearer token, never http://127.0.0.1.
    qwen_base_url: str = ""
    qwen_api_key: str = ""
    qwen_model: str = "qwen3-14b"
    qwen_timeout_seconds: float = 180.0
    # The context window llama-server was STARTED with (`--ctx-size`). The
    # backend cannot ask the server what it is, so it is declared here and
    # checked at startup against the largest request Nibbler actually makes.
    # 8192 — the obvious-looking default — cannot serve a 15-minute Wisdom
    # deck: that request alone reserves 7,980 output tokens, which would leave
    # ~200 for the system prompt, profile and book excerpts. See
    # docs/QWEN_LOCAL_SETUP.md for the arithmetic.
    qwen_context_size: int = 32768

    # Firebase
    firebase_project_id: str
    firebase_private_key_id: str = ""
    firebase_private_key: str = ""
    firebase_client_email: str = ""
    firebase_client_id: str = ""

    # AWS S3
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "eu-north-1"
    s3_bucket_name: str = "nibbler-user-files"

    # Pinecone
    pinecone_api_key: str = ""
    pinecone_index_name: str = "nibbler-content"
    pinecone_environment: str = "gcp-starter"

    # Voyage AI (embeddings)
    voyage_api_key: str = ""

    # Push Notifications (Expo)
    expo_access_token: str = ""  # Optional — increases Expo push API rate limits

    # RevenueCat (subscription sync)
    revenuecat_webhook_secret: str = ""   # Authorization header value configured in the RC dashboard webhook
    revenuecat_secret_api_key: str = ""   # RC secret API key (sk_...) for GET /v1/subscribers

    # Analytics
    mixpanel_token: str = ""    # Same project token as frontend

    # Support (bug reports + email)
    resend_api_key: str = ""    # Same Resend account the website contact form uses
    support_from_email: str = "noreply@getnibbler.com"
    bug_report_email: str = "bug-report@getnibbler.com"
    # Shafin's shared Drive folder that holds the yearly bug-report folders
    bug_drive_folder_id: str = "1cGuEbgMMIzi2hCIpXIpyTw0M4PgLhkQM"

    # Account-deletion Google Sheet operational record (Task 7, Aug 2026).
    # Same credential path as bug-report sheets_service — no separate
    # Sheets/Drive credential needed. Hardcoded constants (not Railway env
    # vars), same precedent as bug_drive_folder_id above — a fixed,
    # already-shared spreadsheet needs no per-environment override.
    account_deletion_sheet_id: str = "16txTS6d_rDYk2AZWudW91DVuBZO1qvLnM10e05xA-V8"
    account_deletion_sheet_tab: str = "Deletion Log"
    account_deletion_snapshot_tab: str = "User Snapshot"
    account_deletion_alert_email: str = "support@getnibbler.com"
    # Gates ONLY the Sheet-sync side effects (append/update rows) — account
    # deletion itself is unconditional and always-on (required for App Store
    # 5.1.1(v) self-service deletion). Lets the Sheet-writer code ship inert,
    # verified via a non-destructive connection test, before being flipped on.
    account_deletion_automation_enabled: bool = False
    # Deletion grace period (Aug 2026): a scheduled deletion sits inert — account fully
    # usable, nothing locked — until this many hours have passed, giving
    # someone who taps delete on impulse a real window to cancel. Cleanup
    # itself only starts once the window elapses (see
    # entitlement_service.promote_scheduled_erasures).
    account_deletion_grace_hours: int = 24

    # App
    app_env: str = "development"
    secret_key: str = "changeme"
    free_upload_limit: int = 3
    free_bites_per_day: int = 1
    premium_bites_per_day: int = 3
    # Upload safety: one unbounded file.read() can OOM the single Railway
    # process, so uploads are hard-capped (and extracted text bounded too).
    # Raised 20 → 50 MB on 2026-07-25 (real 25 MB textbooks were being
    # rejected). The app enforces the same number BEFORE uploading — see
    # MAX_UPLOAD_MB in nibbler/src/screens/UploadScreen.js; keep them in sync.
    max_pdf_upload_mb: int = 50
    max_extracted_text_chars: int = 2_000_000

    # Dynamic growth-profile personalization (Aug 2026): chance any single
    # eligible wisdom session also carries a book-grounded "personalize" card.
    # Founder-tunable via Railway without a redeploy — e.g. set to 1.0
    # temporarily while testing on a real device, then back to 0.2 for normal
    # operation. See _roll_personalization in session_service.py.
    personalization_probability: float = 0.20

    # Growth-profile assignment enforcement rollout (Sep 2026, plan Phase 10).
    # Shipped pre-feature app builds let ANY tier tap "choose this book's
    # goal" and have it silently succeed — that is now a Premium-only choice
    # (PATCH /library/{item_id}'s explicit-assignment branch). Turning
    # rejection on the moment the backend deploys would turn an old client's
    # normal tap into a hard 403 for every free/lapsed user still running
    # that build, before any client ships that even understands the
    # structured error or reconciles a rejection.
    #
    # False (default, safe to deploy immediately): a non-entitled user's
    # explicit assignment request is tolerated the same way an unentitled
    # CREATE already is (§4.2) — the requested id/name is ignored and the
    # server's resolved default is substituted, never a 403. Every other
    # PATCH field (title, mode) and every entitlement-gated CREATE path is
    # unaffected either way; this flag narrowly gates the one endpoint that
    # used to have no gate at all.
    #
    # True: PATCH's explicit-assignment branch 403s a non-entitled request,
    # per this feature's actual product rule (§4.3). Flip only in Railway env
    # (no deploy needed) after verifying the four Phase-10 step-3 conditions
    # are live in the shipped app build — see the plan's Phase 10.
    strict_assignment_enforcement: bool = False

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    return Settings()
