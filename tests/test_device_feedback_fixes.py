"""Real HTTP/DB regressions for September 6 device feedback; no external calls."""
import hermetic  # noqa: F401 -- before every app import
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

from app.database import create_tables, SessionLocal, get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.models.library import LibraryItem
from app.models.profile import Profile
from app.models.bite import DailyBite
from app.models.personalization import PersonalizationQuestion
from app.services.personalization_history import (
    question_memory, available_tags, is_novel_question, persist_novel_question,
)
from app.services.llm.prompts import build_personalization_user_message
from app.services.llm.schemas import wisdom_schema, story_schema
from app.services.session_service import generate_session_for_item
from app.routers.bites import _bite_to_session
import main

create_tables()
db = SessionLocal()
user = User(id="free", email="free@example.test", is_premium=False,
            created_at=datetime.utcnow() - timedelta(days=40),
            trial_anchor_at=datetime.utcnow() - timedelta(days=40))
db.add(user)
db.add(User(id="other", email="other@example.test"))
gp = {"id": "goal", "profileName": "Financial confidence", "lifeArea": "Personal Finance"}
db.add(Profile(id="profile-row", user_id="free", name="Reader", growth_state={"profiles": [gp], "activeProfileId": "goal"}))
db.add(LibraryItem(id="book", user_id="free", title="Habits", type="pdf", processed=True,
                   growth_profile_id="goal", chunk_count=10, is_unlocked_selection=True))
db.add(LibraryItem(id="theirs", user_id="other", title="Private", type="pdf", processed=True))
db.commit()
assert not user.effective_premium

def bite(bid, day, cards=None):
    row = DailyBite(id=bid, user_id="free", library_item_id="book", date=day,
                    title="Nibble", insight="text", reflection="", action="", source="Habits",
                    read_length=5, growth_profile_id="goal", chunk_ids=[0, 1],
                    read_at=datetime.utcnow(), goal_passage="a real goal passage",
                    cards=cards or [{"kind": "insight", "title": "Text", "body": "Only text"}])
    db.add(row); db.commit()
    return row

old = bite("old", date.today() - timedelta(days=2))
q = {"question": "For growing your finances, would you rather automate saving or manage it yourself?",
     "options": [{"id": "a", "text": "Automate monthly saving", "tag": "prefers_automation"},
                 {"id": "b", "text": "Manage it myself", "tag": "prefers_manual_control"}]}
db.add(PersonalizationQuestion(user_id="free", profile_id="goal", daily_bite_id="old",
                               question=q["question"], options=q["options"], status="answered",
                               answer_option_id="a", applied_tags=["prefers_automation"]))
# Another user/profile must not leak into this goal's context.
db.add(PersonalizationQuestion(user_id="other", profile_id="goal", daily_bite_id="other-bite",
                               question="Private question", options=[], status="pending"))
db.add(PersonalizationQuestion(user_id="free", profile_id="another-goal", daily_bite_id="another-bite",
                               question="Another goal question", options=[], status="pending"))
db.commit()
history = question_memory(db, "free", "goal")
assert len(history) == 1 and history[0]["answer"] == "Automate monthly saving"
assert "prefers_automation" not in available_tags(history)
assert "prefers_manual_control" not in available_tags(history)
repeat = {**q, "question": "Would hands-off investment contributions fit your financial goal better?"}
assert not is_novel_question(repeat, history), "paraphrase of known dimension must be rejected"
fresh = {"question": "How confident do you feel about starting this week's first small step?",
         "options": [{"id": "a", "text": "Ready to start", "tag": "increase_confidence"},
                     {"id": "b", "text": "I need support", "tag": "decrease_confidence"}]}
assert is_novel_question(fresh, history)
prompt = build_personalization_user_message(book_title="Habits", author=None, profile=gp,
                                           context_chunks=["source"], question_history=history)
assert "Automate monthly saving" in prompt and "Private question" not in prompt
assert "prefers_automation" not in prompt.split("ALLOWED TAGS", 1)[1]

first = bite("first", date.today() - timedelta(days=1))
second = bite("second", date.today(), [{"kind": "insight"}, {"kind": "personalize"}])
assert persist_novel_question(db, user_id="free", profile_id="goal", bite_id="first", item_id="book",
                              question=fresh, chunk_ids=[0])
assert not persist_novel_question(db, user_id="free", profile_id="goal", bite_id="second", item_id="book",
                                  question=fresh, chunk_ids=[0]), "late writer must recheck pending questions"
db.refresh(second)
assert all(c["kind"] != "personalize" for c in second.cards)
assert "increase_confidence" not in available_tags(question_memory(db, "free", "goal"))

main.app.dependency_overrides[get_db] = lambda: db
main.app.dependency_overrides[get_current_user] = lambda: user
client = TestClient(main.app)
with patch("app.routers.connect.EmbeddingService") as embeddings:
    embeddings.return_value.search_item_scored.return_value = [
        {"score": 0.40, "text": "source passage", "embedder": "voyage"}]
    r = client.post("/connect/insights", json={"library_item_id": "book", "scoring_version": 2})
    assert r.status_code == 200, r.text
    assert r.json()["relevance_pct"] == 78 and r.json()["resolved_profile_id"] == "goal"
    assert client.post("/connect/insights", json={"library_item_id": "theirs"}).status_code == 404
r = client.get("/connect/stats/book")
assert r.status_code == 200, r.text
assert r.json()["explored_pct"] == 20 and r.json()["sessions_read"] == 3
assert r.json()["goal_passage"] is None, "only match and stats are newly free"
assert client.get("/connect/stats/theirs").status_code == 404
assert client.post("/connect/chat", json={"library_item_id": "book", "message": "Hi"}).status_code == 403
user.is_premium = True; db.commit()
assert client.get("/connect/stats/book").json()["goal_passage"]["text"] == "a real goal passage"

old.cards = [{"kind": "insight", "title": "old", "image": {"id": "old-image"}, "imageId": "old-image"}]
db.commit()
assert "image" not in _bite_to_session(old).cards[0]
assert "imageId" not in _bite_to_session(old).cards[0]
archived = client.get("/bites/sessions")
assert archived.status_code == 200, archived.text
assert all("image" not in c and "imageId" not in c
           for s in archived.json()["sessions"] for c in s["cards"])
assert client.get("/library/book/images/old-image").status_code == 410
assert "imageId" not in str(wisdom_schema(5, 2)) and "imageIds" not in str(story_schema(3))
for filename in ("image_extract.py", "image_select.py", "image_gen.py"):
    assert not (Path(hermetic.BACKEND) / "app/services" / filename).exists()

# Drive the actual generation path, not just the pure novelty checker.
with patch("app.services.session_service.EmbeddingService") as embeddings, \
     patch("app.services.session_service.LLMService") as llm, \
     patch("app.services.session_service._roll_personalization", return_value=True):
    embeddings.return_value.search_item_fresh.return_value = [
        {"text": "A book-grounded passage about deliberate practice. " * 12, "chunk_index": i}
        for i in range(3, 6)]
    llm.return_value.generate_personalization_question.return_value = repeat
    llm.return_value.generate_wisdom_session.return_value = {
        "title": "New text-only session", "cards": [{"kind": "insight", "body": "A new insight"}]}
    generated = generate_session_for_item(db, user=user, item=db.get(LibraryItem, "book"),
                                          read_length=5, profile=gp,
                                          today=date.today() + timedelta(days=1))
    assert all(c["kind"] != "personalize" for c in generated.cards)
    sent = llm.return_value.generate_personalization_question.call_args.kwargs["question_history"]
    assert any(h["answer"] == "Automate monthly saving" for h in sent)
    assert db.query(PersonalizationQuestion).filter_by(daily_bite_id=generated.id).count() == 0
print("PASS: novelty memory + final recheck, tier-correct analytics, ownership, text-only stored decks")
