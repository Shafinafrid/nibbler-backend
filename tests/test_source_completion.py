"""Regression: completion is proven by chunk identity, then deactivates."""
import hermetic  # noqa: F401
from datetime import date, datetime, timedelta
from fastapi.testclient import TestClient

from app.database import create_tables, SessionLocal, get_db
from app.middleware.auth import get_current_user, get_current_verified_user
from app.models.user import User
from app.models.library import LibraryItem
from app.models.bite import DailyBite
from app.services.session_service import (
    SessionGenerationError, generate_session_for_item, wisdom_source_complete,
)
import main


create_tables()
db = SessionLocal()
user = User(id="completion-user", email="completion@example.test", is_premium=True)
item = LibraryItem(
    id="completion-book", user_id=user.id, title="Finite source", type="pdf",
    processed=True, mode="wisdom", chunk_count=4, is_active=True, content="fallback",
)
db.add_all([user, item])

# Five historical reads with no chunk provenance must not fabricate progress.
for offset in range(5):
    db.add(DailyBite(
        id=f"legacy-{offset}", user_id=user.id, library_item_id=item.id,
        date=date.today() - timedelta(days=10 + offset), title="legacy",
        insight="", reflection="", action="", cards=[{"kind": "summary"}],
        read_at=datetime.utcnow(), chunk_ids=[], mode="wisdom",
    ))
first = DailyBite(
    id="known-first", user_id=user.id, library_item_id=item.id,
    date=date.today() - timedelta(days=1), title="known", insight="",
    reflection="", action="", cards=[{"kind": "summary"}],
    read_at=datetime.utcnow(), chunk_ids=[0, 1], mode="wisdom",
)
last = DailyBite(
    id="known-last", user_id=user.id, library_item_id=item.id,
    date=date.today(), title="last", insight="", reflection="", action="",
    cards=[{"kind": "summary"}], read_at=None, chunk_ids=[2, 3], mode="wisdom",
)
db.add_all([first, last])
db.commit()

main.app.dependency_overrides[get_db] = lambda: db
main.app.dependency_overrides[get_current_user] = lambda: user
main.app.dependency_overrides[get_current_verified_user] = lambda: user
client = TestClient(main.app)

stats = client.get(f"/connect/stats/{item.id}")
assert stats.status_code == 200, stats.text
assert stats.json()["explored_pct"] == 50, stats.json()
# The source is exhausted (all chunks are assigned), but only half is proven
# read until the user completes the held final nibble below.
assert wisdom_source_complete(db, user.id, item)

done = client.post("/sync/session-complete", json={
    "id": "completion-op", "book_id": item.id, "daily_bite_id": last.id,
    "completed_date": date.today().isoformat(), "read_length": 5,
})
assert done.status_code == 200, done.text
assert done.json()["source_completed"] is True
assert done.json()["source_deactivated"] is True
db.refresh(item)
assert item.is_active is False
assert wisdom_source_complete(db, user.id, item)
assert client.get(f"/connect/stats/{item.id}").json()["explored_pct"] == 100

# Even if an old client/user flips the row back on, generation fails before an
# embedding or LLM call and restores the inactive state.
item.is_active = True
db.commit()
try:
    generate_session_for_item(
        db, user=user, item=item, read_length=5, profile={},
        today=date.today() + timedelta(days=1),
    )
    raise AssertionError("exhausted source generated a repeated nibble")
except SessionGenerationError as exc:
    assert exc.code == "source_complete" and exc.status_code == 409
db.refresh(item)
assert item.is_active is False

print("PASS: exact exploration, final-chunk deactivation, exhausted-source guard")
