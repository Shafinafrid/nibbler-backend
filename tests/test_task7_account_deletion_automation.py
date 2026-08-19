"""
Task 7 (Aug 2026) — account-deletion automation additions on top of the
already-existing durable erasure state machine (Task2 Closeout: Blocker 8):

  A — EmailAccountHistory guard: seeds trial_anchor_at/successful_sources_total
      on a fresh signup for an email that has been through account deletion
      before; leaves a genuinely fresh email untouched.
  B — User.effective_premium respects trial_anchor_at (not just created_at).
  C — _plan_label correctness across is_premium / active sub / lapsed / trial / free.
  D — _capture_erasure_snapshot: growth-profile count (multi-profile is real),
      library/streak/engagement stats, privacy-safe (no note/chat TEXT content).
  E — deletion_sheets_service row-builders: correct column positions;
      email/uid are kept permanently (founder decision, Aug 2026) even
      once personal_data_redacted_at is set.
  F — RevenueCat/Mixpanel erasure calls: blank credentials -> False, no
      network attempt (hermetic-safe), never raise.
  G — Full erasure success (all artifact classes mocked True): guard is
      written atomically with user deletion; a SUBSEQUENT signup with the
      same email is seeded from the guard, not left at fresh defaults.
  H — Partial failure: retry_count increments; the "stuck" operational
      alert fires once retry_count crosses the threshold, not every tick.

Network/provider clients stay mocked or hermetically blank throughout.
"""
import os
import sys
import tempfile
import datetime
import unittest.mock as mock

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ.update(DATABASE_URL=f"sqlite:///{TMP}/task7.db", FIREBASE_PROJECT_ID="t")
sys.path.insert(0, BACKEND)
import hermetic  # noqa: F401 — must precede `app.` imports

failures = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def section(t):
    print(f"\n=== {t} ===")


from app.database import create_tables, SessionLocal
from app.models.user import User, EmailAccountHistory, normalize_email
from app.models.profile import Profile
from app.models.streak import Streak
from app.models.user_data import Note, Highlight, ChatMessage, Completion
from app.models.library import LibraryItem, AccountErasure
from app.middleware.auth import get_or_create_user
from app.routers import auth as auth_router
from app.services import deletion_sheets_service, mixpanel_service
from app.config import get_settings

create_tables()
db = SessionLocal()
settings = get_settings()

NOW = datetime.datetime.utcnow()


def mkuser(uid, **kw):
    kw.setdefault("email", f"{uid}@example.com")
    kw.setdefault("created_at", NOW)
    u = User(id=uid, **kw)
    db.add(u)
    db.commit()
    return u


# ═══════════════════════════════════════════════════════════════════════
section("A — EmailAccountHistory guard seeds a fresh signup")
# ═══════════════════════════════════════════════════════════════════════

OLD_ANCHOR = NOW - datetime.timedelta(days=100)
db.add(EmailAccountHistory(
    email=normalize_email("returning@example.com"),
    trial_anchor_at=OLD_ANCHOR, lifetime_successful_sources_total=3,
    first_seen_user_id="old-uid",
))
db.commit()

returning_user = get_or_create_user(
    {"uid": "new-uid-1", "email": "Returning@Example.com "}, db)  # case/whitespace variant
check("returning email (case/whitespace-insensitive) seeds trial_anchor_at from the guard",
      returning_user.trial_anchor_at == OLD_ANCHOR, returning_user.trial_anchor_at)
check("returning email seeds successful_sources_total from the guard (not 0)",
      returning_user.successful_sources_total == 3, returning_user.successful_sources_total)

fresh_user = get_or_create_user({"uid": "fresh-uid-1", "email": "brandnew@example.com"}, db)
check("a genuinely fresh email is NOT seeded (trial_anchor_at stays None)",
      fresh_user.trial_anchor_at is None, fresh_user.trial_anchor_at)
check("a genuinely fresh email starts at 0 uploads",
      fresh_user.successful_sources_total == 0, fresh_user.successful_sources_total)


# ═══════════════════════════════════════════════════════════════════════
section("B — effective_premium respects trial_anchor_at over created_at")
# ═══════════════════════════════════════════════════════════════════════

# created_at is old (would fail the trial window alone), but trial_anchor_at
# is None so it falls back to created_at — still expired, correctly free.
u_b1 = mkuser("eff_b1", created_at=NOW - datetime.timedelta(days=30))
check("no trial_anchor_at override -> falls back to created_at (expired -> free)",
      u_b1.effective_premium is False)

# created_at is RECENT (would look like an active trial), but trial_anchor_at
# is set far in the past by the guard seed -> trial correctly NOT re-granted.
u_b2 = mkuser("eff_b2", created_at=NOW, trial_anchor_at=NOW - datetime.timedelta(days=30))
check("trial_anchor_at overrides created_at -> a re-signup's fresh created_at "
      "cannot resurrect an already-used trial", u_b2.effective_premium is False)

u_b3 = mkuser("eff_b3", created_at=NOW, trial_anchor_at=NOW - datetime.timedelta(days=1))
check("a recent trial_anchor_at still grants the trial normally",
      u_b3.effective_premium is True)


# ═══════════════════════════════════════════════════════════════════════
section("C — _plan_label")
# ═══════════════════════════════════════════════════════════════════════

check("is_premium (comp) labelled correctly",
      auth_router._plan_label(mkuser("plan_c1", is_premium=True)) == "premium (comp)")
check("active subscription labelled correctly",
      auth_router._plan_label(mkuser("plan_c2", premium_until=NOW + datetime.timedelta(days=10))) == "premium (subscription)")
check("lapsed subscriber labelled correctly (not trial, not plain free-worded the same)",
      auth_router._plan_label(mkuser("plan_c3", premium_until=NOW - datetime.timedelta(days=10))) == "free (lapsed subscriber)")
check("in-trial labelled correctly",
      auth_router._plan_label(mkuser("plan_c4", created_at=NOW)) == "trial")
check("plain free labelled correctly",
      auth_router._plan_label(mkuser("plan_c5", created_at=NOW - datetime.timedelta(days=30))) == "free")


# ═══════════════════════════════════════════════════════════════════════
section("D — _capture_erasure_snapshot")
# ═══════════════════════════════════════════════════════════════════════

u_d = mkuser("snap_d", platform="ios", app_version="1.2.3", device_model="iPhone 15 Pro",
             os_version="17.5", timezone="Europe/Stockholm", locale="en-GB")
db.add(Profile(id="profile_snap_d", user_id="snap_d", name="snap_d", growth_state={
    "profiles": [
        {"lifeArea": "career", "contentMode": "analytical"},
        {"lifeArea": "health", "contentMode": "practical"},
    ]
}))
db.add(LibraryItem(id="snap_lib1", user_id="snap_d", title="Book 1", type="pdf",
                    file_size=2_000_000, chunk_count=40))
db.add(LibraryItem(id="snap_lib2", user_id="snap_d", title="Book 2", type="pdf",
                    file_size=3_000_000, chunk_count=25))
db.add(Streak(id="streak_snap_d", user_id="snap_d", current_streak=5, longest_streak=12, total_bites_read=40))
db.add(Note(id="n1", user_id="snap_d", book_id="snap_lib1", card_index=0, text="secret thoughts"))
db.add(Note(id="n2", user_id="snap_d", book_id="snap_lib1", card_index=1, text="more thoughts"))
db.add(Highlight(id="h1", user_id="snap_d", book_id="snap_lib1", card_index=0))
db.add(ChatMessage(id="c1", user_id="snap_d", book_id="snap_lib1", role="user", content="hi"))
db.add(Completion(id="comp1", user_id="snap_d", book_id="snap_lib1", completed_date=NOW.date()))
db.commit()

snap = auth_router._capture_erasure_snapshot(db, u_d)
check("growth_profile_count reflects the real multi-profile array (2, not always 1)",
      snap["growth_profile_count"] == 2, snap["growth_profile_count"])
check("primary_life_area is the FIRST profile's, not a merge",
      snap["primary_life_area"] == "career", snap["primary_life_area"])
check("library_item_count correct", snap["library_item_count"] == 2, snap["library_item_count"])
check("total_vectors sums chunk_count across items (65, free from existing data)",
      snap["total_vectors"] == 65, snap["total_vectors"])
check("library_total_mb correct (~5.0)", abs(snap["library_total_mb"] - 5.0) < 0.01, snap["library_total_mb"])
check("streak stats captured", snap["current_streak"] == 5 and snap["longest_streak"] == 12
      and snap["total_bites_read"] == 40)
check("notes_count is a COUNT, not the text itself", snap["notes_count"] == 2)
check("chat_messages_count is a COUNT", snap["chat_messages_count"] == 1)
check("completions_count correct", snap["completions_count"] == 1)
check("device_model/os_version captured", snap["device_model"] == "iPhone 15 Pro" and snap["os_version"] == "17.5")
check("snapshot is PRIVACY-SAFE: no raw note/chat text content anywhere in it",
      not any("secret thoughts" in str(v) or "more thoughts" in str(v) for v in snap.values()),
      snap)


# ═══════════════════════════════════════════════════════════════════════
section("E — deletion_sheets_service row builders")
# ═══════════════════════════════════════════════════════════════════════

fake_erasure = AccountErasure(
    id="erasure-e1", user_id="snap_d", state="pending",
    identity={}, progress={"vectors": True, "s3_files": False}, retry_count=2,
    requested_at=NOW,
)
row = deletion_sheets_service.deletion_log_row_values(fake_erasure, {"email": "e@x.com", "plan": "free"})
check("Deletion Log row has exactly 20 columns (matches the live Sheet header)",
      len(row) == len(deletion_sheets_service.DELETION_LOG_COLUMNS) == 20, len(row))
check("email is in the User Email column (index 2)",
      row[2] == "e@x.com", row[2])
check("retry_count lands in the Retry Count column (index 16)",
      row[16] == 2, row[16])

fake_erasure.personal_data_redacted_at = NOW
still_row = deletion_sheets_service.deletion_log_row_values(fake_erasure, {"email": "e@x.com", "plan": "free"})
check("founder decision (Aug 2026): email/uid are KEPT even after "
      "personal_data_redacted_at is set — that field only records the "
      "cleanup-completed timestamp now, it no longer blanks anything",
      still_row[2] == "e@x.com", still_row[:4])

snap_row = deletion_sheets_service.snapshot_row_values(fake_erasure, snap)
check("User Snapshot row column count matches its own header exactly",
      len(snap_row) == len(deletion_sheets_service.SNAPSHOT_COLUMNS), len(snap_row))
check("Snapshot tab's last-column letter actually spans every declared column "
      "(catches an off-by-one that would silently truncate real Sheet writes)",
      deletion_sheets_service._col_letter(len(deletion_sheets_service.SNAPSHOT_COLUMNS) - 1)
      == deletion_sheets_service._SNAPSHOT_LAST_COL,
      (deletion_sheets_service._col_letter(len(deletion_sheets_service.SNAPSHOT_COLUMNS) - 1),
       deletion_sheets_service._SNAPSHOT_LAST_COL))
check("Deletion Log tab's last-column letter actually spans every declared column",
      deletion_sheets_service._col_letter(len(deletion_sheets_service.DELETION_LOG_COLUMNS) - 1)
      == deletion_sheets_service._DELETION_LOG_LAST_COL,
      (deletion_sheets_service._col_letter(len(deletion_sheets_service.DELETION_LOG_COLUMNS) - 1),
       deletion_sheets_service._DELETION_LOG_LAST_COL))
check("User Snapshot row contains no raw note/chat text anywhere",
      not any("secret thoughts" in str(v) for v in snap_row), snap_row)


# ═══════════════════════════════════════════════════════════════════════
section("F — RevenueCat/Mixpanel erasure calls: blank creds -> False, no crash")
# ═══════════════════════════════════════════════════════════════════════

check("RevenueCat delete with blank key returns False without raising",
      auth_router._delete_revenuecat_subscriber("some-uid") is False)
check("Mixpanel delete_profile with blank token returns False without raising",
      auth_router._delete_mixpanel_profile_sync("some-uid") is False)
check("connection_test with blank Firebase creds reports not-configured, doesn't raise",
      deletion_sheets_service.connection_test() == (False, "service account not configured"))


# ═══════════════════════════════════════════════════════════════════════
section("G — full erasure success writes the guard atomically with user deletion")
# ═══════════════════════════════════════════════════════════════════════

u_g = mkuser("erase_g", email="willdelete@example.com",
             created_at=NOW - datetime.timedelta(days=10), successful_sources_total=2)
db.add(AccountErasure(
    id="erasure-g1", user_id="erase_g", state="pending",
    identity={
        "source_keys": [], "image_keys": [], "avatar_key": None,
        "pinecone_namespace": "erase_g", "cleanup_ledger_ids": [],
        "firebase_uid": "erase_g",
        "snapshot": auth_router._capture_erasure_snapshot(db, u_g),
    },
))
db.commit()
erasure_g = db.query(AccountErasure).filter(AccountErasure.id == "erasure-g1").first()

with mock.patch("app.routers.auth.EmbeddingService") as MockEmbed, \
     mock.patch("app.routers.auth.S3Service") as MockS3, \
     mock.patch("firebase_admin.auth.delete_user") as MockFirebase, \
     mock.patch("app.routers.auth._delete_revenuecat_subscriber", return_value=True), \
     mock.patch("app.routers.auth._delete_mixpanel_profile_sync", return_value=True), \
     mock.patch("app.routers.auth._send_email_sync", return_value=True):
    MockEmbed.return_value.delete_user_namespace.return_value = True
    MockS3.return_value.delete_file.return_value = True
    MockFirebase.return_value = None
    complete_g = auth_router._attempt_account_erasure_cleanup(db, erasure_g)

check("full success (all artifact classes True, including RC+Mixpanel) returns True",
      complete_g is True)

db.expire_all()
check("the User row is genuinely gone", db.query(User).filter(User.id == "erase_g").first() is None)
check("the AccountErasure row is genuinely gone", db.query(AccountErasure).filter(AccountErasure.id == "erasure-g1").first() is None)

guard_g = db.query(EmailAccountHistory).filter(EmailAccountHistory.email == "willdelete@example.com").first()
check("EmailAccountHistory guard was created atomically with the deletion",
      guard_g is not None)
check("guard's trial_anchor_at matches the deleted account's own anchor",
      guard_g.trial_anchor_at == u_g.created_at, (guard_g.trial_anchor_at, u_g.created_at) if guard_g else None)
check("guard's lifetime_successful_sources_total carries the deleted account's count forward",
      guard_g.lifetime_successful_sources_total == 2, guard_g.lifetime_successful_sources_total if guard_g else None)

# The whole point: prove a NEW signup with the SAME email does NOT get a fresh trial/uploads.
resignup_g = get_or_create_user({"uid": "erase_g_v2", "email": "willdelete@example.com"}, db)
check("re-signup with the same email is seeded from the guard, not fresh defaults",
      resignup_g.trial_anchor_at == u_g.created_at and resignup_g.successful_sources_total == 2,
      (resignup_g.trial_anchor_at, resignup_g.successful_sources_total))
check("re-signup's effective_premium correctly reflects an already-used trial (False)",
      resignup_g.effective_premium is False)


# ═══════════════════════════════════════════════════════════════════════
section("H — partial failure: retry_count increments, alert fires once past threshold")
# ═══════════════════════════════════════════════════════════════════════

u_h = mkuser("erase_h", email="stuckcase@example.com")
db.add(AccountErasure(
    id="erasure-h1", user_id="erase_h", state="pending", retry_count=4,
    identity={
        "source_keys": [], "image_keys": [], "avatar_key": None,
        "pinecone_namespace": "erase_h", "cleanup_ledger_ids": [],
        "firebase_uid": "erase_h", "snapshot": {"email": "stuckcase@example.com"},
    },
))
db.commit()
erasure_h = db.query(AccountErasure).filter(AccountErasure.id == "erasure-h1").first()

alert_calls = []


def _fake_send_email(*a, **kw):
    alert_calls.append(kw.get("subject", ""))
    return True


with mock.patch("app.routers.auth.EmbeddingService") as MockEmbedH, \
     mock.patch("app.routers.auth.S3Service") as MockS3H, \
     mock.patch("firebase_admin.auth.delete_user", side_effect=Exception("still down")), \
     mock.patch("app.routers.auth._delete_revenuecat_subscriber", return_value=True), \
     mock.patch("app.routers.auth._delete_mixpanel_profile_sync", return_value=True), \
     mock.patch("app.routers.auth._send_email_sync", side_effect=_fake_send_email):
    MockEmbedH.return_value.delete_user_namespace.return_value = True
    MockS3H.return_value.delete_file.return_value = True
    complete_h = auth_router._attempt_account_erasure_cleanup(db, erasure_h)

check("partial failure (Firebase still down) returns False", complete_h is False)
db.expire_all()
erasure_h_after = db.query(AccountErasure).filter(AccountErasure.id == "erasure-h1").first()
check("retry_count incremented (4 -> 5)", erasure_h_after.retry_count == 5, erasure_h_after.retry_count)
check("crossing the alert threshold (5) fired exactly one operational alert",
      len([c for c in alert_calls if "stuck" in c]) == 1, alert_calls)
check("the user row SURVIVES a partial failure (never deleted until all_ok)",
      db.query(User).filter(User.id == "erase_h").first() is not None)

# A second consecutive failure at retry_count=6 must NOT re-fire the alert.
alert_calls.clear()
with mock.patch("app.routers.auth.EmbeddingService") as MockEmbedH2, \
     mock.patch("app.routers.auth.S3Service") as MockS3H2, \
     mock.patch("firebase_admin.auth.delete_user", side_effect=Exception("still down")), \
     mock.patch("app.routers.auth._delete_revenuecat_subscriber", return_value=True), \
     mock.patch("app.routers.auth._delete_mixpanel_profile_sync", return_value=True), \
     mock.patch("app.routers.auth._send_email_sync", side_effect=_fake_send_email):
    MockEmbedH2.return_value.delete_user_namespace.return_value = True
    MockS3H2.return_value.delete_file.return_value = True
    auth_router._attempt_account_erasure_cleanup(db, erasure_h_after)

check("a SECOND consecutive failure past the threshold does NOT re-send the alert",
      len(alert_calls) == 0, alert_calls)


# ═══════════════════════════════════════════════════════════════════════
section("I — audit-fix verification (independent subagent review, Aug 2026)")
# ═══════════════════════════════════════════════════════════════════════

# I1 — CONFIRMED BUG fix: Firebase already-deleted (UserNotFoundError) on a
# RETRY must count as success, not permanently strand the erasure. Without
# this fix, "Firebase succeeded once, something else failed that attempt,
# retry re-calls delete_user on the now-gone uid" would loop forever.
import firebase_admin.auth as _firebase_auth_real

u_i1 = mkuser("erase_i1", email="stranding-repro@example.com")
db.add(AccountErasure(
    id="erasure-i1", user_id="erase_i1", state="failed", retry_count=1,
    progress={"vectors": True, "s3_files": True, "s3_images": True, "avatar": True,
              "ledger_artifacts": True, "revenuecat": True, "mixpanel": True, "firebase": True},
    identity={
        "source_keys": [], "image_keys": [], "avatar_key": None,
        "pinecone_namespace": "erase_i1", "cleanup_ledger_ids": [],
        "firebase_uid": "erase_i1", "snapshot": {"email": "stranding-repro@example.com"},
    },
))
db.commit()
erasure_i1 = db.query(AccountErasure).filter(AccountErasure.id == "erasure-i1").first()

not_found = _firebase_auth_real.UserNotFoundError("no such user")
with mock.patch("app.routers.auth.EmbeddingService") as MockEmbedI1, \
     mock.patch("app.routers.auth.S3Service") as MockS3I1, \
     mock.patch("firebase_admin.auth.delete_user", side_effect=not_found), \
     mock.patch("app.routers.auth._delete_revenuecat_subscriber", return_value=True), \
     mock.patch("app.routers.auth._delete_mixpanel_profile_sync", return_value=True), \
     mock.patch("app.routers.auth._send_email_sync", return_value=True):
    MockEmbedI1.return_value.delete_user_namespace.return_value = True
    MockS3I1.return_value.delete_file.return_value = True
    complete_i1 = auth_router._attempt_account_erasure_cleanup(db, erasure_i1)

check("UserNotFoundError on retry (Firebase already gone) completes the erasure "
      "instead of stranding it forever", complete_i1 is True)
db.expire_all()
check("the erasure row is genuinely gone after the fix (not stuck retrying UserNotFoundError forever)",
      db.query(AccountErasure).filter(AccountErasure.id == "erasure-i1").first() is None)

# I2 — founder decision (Aug 2026, superseding the earlier redact-on-completion
# audit fix): email/uid are kept PERMANENTLY in both Sheet tabs for support
# lookups, regardless of personal_data_redacted_at, in both tabs consistently.
fake_erasure_i2 = AccountErasure(id="erasure-i2", user_id="x", state="resolved",
                                  identity={}, progress={}, requested_at=NOW)
snap_row_before = deletion_sheets_service.snapshot_row_values(
    fake_erasure_i2, {"email": "kept@example.com", "firebase_uid": "uid-x"})
check("BEFORE personal_data_redacted_at is set, snapshot row carries email (sanity check)",
      snap_row_before[2] == "kept@example.com")

fake_erasure_i2.personal_data_redacted_at = NOW
snap_row_after = deletion_sheets_service.snapshot_row_values(
    fake_erasure_i2, {"email": "kept@example.com", "firebase_uid": "uid-x"})
check("User Snapshot tab still carries email/uid AFTER personal_data_redacted_at is set "
      "(founder decision: keep permanently for support lookups)",
      snap_row_after[2] == "kept@example.com" and snap_row_after[3] == "uid-x", snap_row_after[:4])

# I3 — length cap on client-controlled growth_state free text landing in the Sheet.
long_area = "x" * 500
snap_row_long = deletion_sheets_service.snapshot_row_values(
    fake_erasure_i2, {"primary_life_area": long_area, "primary_content_mode": "y" * 500})
check("primary_life_area is truncated to a bounded length even if growth_state "
      "held arbitrary long text", len(snap_row_long[14]) <= 62, len(snap_row_long[14]))

# I4 — "PostgreSQL Deleted" column must reflect erasure.state == 'resolved'
# exactly, not a partial AND of unrelated artifact classes (the old mapping
# could read True while Firebase/RevenueCat/Mixpanel/vectors were still failing).
fake_erasure_i4 = AccountErasure(
    id="erasure-i4", user_id="x", state="failed", requested_at=NOW,
    progress={"s3_files": True, "s3_images": True, "avatar": True, "ledger_artifacts": True,
              "vectors": False, "firebase": False, "revenuecat": False, "mixpanel": False},
)
row_i4 = deletion_sheets_service.deletion_log_row_values(fake_erasure_i4, {})
check("'PostgreSQL Deleted' is False while state is still 'failed', even though the "
      "old (buggy) s3/avatar/ledger-only AND would have read True here",
      row_i4[8] is False, row_i4[8])
fake_erasure_i4.state = "resolved"
row_i4b = deletion_sheets_service.deletion_log_row_values(fake_erasure_i4, {})
check("'PostgreSQL Deleted' is True once state is genuinely 'resolved'", row_i4b[8] is True)

# I5 — commit-before-sheet-write ordering: prepare_success_write() must FREEZE
# values at call time — a later mutation of the erasure object (e.g. by the
# subsequent db.delete()/commit() dance) must not change what actually gets
# written, and write_prepared_rows() must not need the erasure object at all
# (proving it can safely run strictly AFTER the object may be stale/expired).
with mock.patch("app.services.deletion_sheets_service._get_access_token_sync", return_value=None):
    # Unconfigured creds -> prepare returns None, write is a safe no-op. This
    # also proves neither function raises when Sheets isn't configured yet.
    prep_none = deletion_sheets_service.prepare_success_write(fake_erasure_i4, {})
    check("prepare_success_write returns None (not raise) when unconfigured", prep_none is None)
    check("write_prepared_rows(None) is a safe no-op", deletion_sheets_service.write_prepared_rows(None) is False)


# ═══════════════════════════════════════════════════════════════════════
print(f"\n{'='*60}")
if failures:
    print(f"FAILED: {len(failures)} check(s) — {failures}")
    sys.exit(1)
print("All Task 7 account-deletion-automation checks passed.")
sys.exit(0)
