from pydantic import BaseModel, Field, field_validator
from datetime import datetime
from typing import Optional, List


class ProfileCreate(BaseModel):
    name: str
    goals: Optional[List[str]] = None
    struggles: Optional[str] = None
    reading_habits: Optional[str] = None
    daily_time: Optional[str] = None
    tone_preference: Optional[str] = None
    background_summary: Optional[str] = None


class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    goals: Optional[List[str]] = None
    struggles: Optional[str] = None
    reading_habits: Optional[str] = None
    daily_time: Optional[str] = None
    tone_preference: Optional[str] = None
    background_summary: Optional[str] = None


class ProfileResponse(BaseModel):
    id: str
    user_id: str
    name: str
    goals: Optional[List[str]]
    struggles: Optional[str]
    reading_habits: Optional[str]
    daily_time: Optional[str]
    tone_preference: Optional[str]
    background_summary: Optional[str]
    growth_state: Optional[dict] = None
    # Deletion tombstones (finding #7, Sep 2026): the server's full, never-
    # shrinking union of profile ids any device has recorded as deleted. The
    # client should filter/reconcile its own local profiles[] against this on
    # read so a stale local copy doesn't keep showing (or re-push) a profile
    # that was deleted elsewhere. camelCase to match the app's GrowthProfile
    # blob field-naming convention (see AspirationResult below).
    # validation_alias (not `alias`) on purpose: this must READ the ORM
    # object's snake_case `deleted_profile_ids` attribute (from_attributes
    # mode) but SERIALIZE to JSON under the camelCase field name itself —
    # `alias` would apply to both directions and put `deleted_profile_ids`
    # on the wire, breaking the app-side camelCase convention this mirrors
    # (see AspirationResult below).
    deletedProfileIds: List[str] = Field(default_factory=list, validation_alias="deleted_profile_ids")
    created_at: datetime
    updated_at: datetime

    @field_validator("deletedProfileIds", mode="before")
    @classmethod
    def _default_empty_tombstones(cls, v):
        # The DB column is nullable JSON — every profile row created before
        # this fix (and every fresh row with no deletions yet) has it as
        # NULL, not []. default_factory only fills in an ABSENT field, not an
        # explicit None from ORM attribute access, so without this a plain
        # GET /profile/ on any pre-existing row would 500.
        return v if v is not None else []

    class Config:
        from_attributes = True
        populate_by_name = True


class GrowthStateUpdate(BaseModel):
    # The app's full nibbler_growth_state_v1 blob: {person, profiles[], activeProfileId}
    growth_state: dict
    # Deletion tombstones (finding #7, Sep 2026): profile ids THIS device
    # knows should be deleted, as of this push. Optional/defaults to empty so
    # older app builds that don't send it keep working unchanged — the server
    # only ever UNIONS these into its stored tombstone set, never removes.
    deletedProfileIds: List[str] = Field(default_factory=list)


class OnboardingMessage(BaseModel):
    message: str
    conversation_history: List[dict] = []


class AspirationRequest(BaseModel):
    # Hard length cap: this endpoint is unauthenticated (onboarding runs before
    # account creation), so the input must stay small and cheap.
    answer: str = Field(..., min_length=1, max_length=500)


class AspirationResult(BaseModel):
    # Field names are camelCase on purpose — they mirror the app's GrowthProfile
    # seed shape (see nibbler/src/data/ProfileRepository.js). Defaults make a
    # slightly-off model response still validate instead of 500ing onboarding.
    needsClarification: bool = False
    clarifyPrompt: Optional[str] = None
    lifeArea: str = "Personal Growth"
    contentMode: str = "practical"
    motivation: str = "curiosity"
    motivationType: str = "intrinsic"
    goalOrientation: str = "summary"
    interests: List[str] = []
    profileName: str = "Growing Every Day"
    confirmation: str = ""
    understanding: str = ""


class OnboardingResponse(BaseModel):
    reply: str
    profile: Optional[ProfileCreate] = None
    is_complete: bool = False
