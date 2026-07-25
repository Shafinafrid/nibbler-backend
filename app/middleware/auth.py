from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
import firebase_admin
from firebase_admin import auth as firebase_auth, credentials
from app.database import get_db
from app.models.user import User
from app.config import get_settings
import logging
import uuid

logger = logging.getLogger(__name__)

settings = get_settings()
security = HTTPBearer()

# Initialise Firebase Admin SDK once
_firebase_initialized = False

def init_firebase():
    global _firebase_initialized
    if not _firebase_initialized and not firebase_admin._apps:
        cred_dict = {
            "type": "service_account",
            "project_id": settings.firebase_project_id,
            "private_key_id": settings.firebase_private_key_id,
            "private_key": settings.firebase_private_key.replace("\\n", "\n"),
            "client_email": settings.firebase_client_email,
            "client_id": settings.firebase_client_id,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        _firebase_initialized = True


def verify_firebase_token(token: str) -> dict:
    """Verify a Firebase ID token and return the decoded claims."""
    init_firebase()
    try:
        decoded = firebase_auth.verify_id_token(token)
        return decoded
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid authentication token: {str(e)}",
        )


def get_or_create_user(decoded_token: dict, db: Session) -> User:
    """Get existing user or create a new one from Firebase token claims."""
    firebase_uid = decoded_token["uid"]
    user = db.query(User).filter(User.id == firebase_uid).first()

    if not user:
        user = User(
            id=firebase_uid,
            email=decoded_token.get("email", ""),
            display_name=decoded_token.get("name"),
        )
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            # This check-then-insert isn't atomic, and AppContext.init() fires
            # several authenticated calls in parallel right after sign-in —
            # each one independently lands here on a brand-new account, so
            # more than one can race to create the same row. Previously this
            # surfaced as a bare, undiagnosable 500 on every endpoint (found
            # 2026-07-25 chasing what looked like an unrelated upload bug: a
            # real user had 7 of these racing in two bursts, ~20s apart).
            db.rollback()
            existing = db.query(User).filter(User.id == firebase_uid).first()
            if existing:
                # A concurrent request for this SAME uid won the race — fine,
                # use its row.
                return existing
            # Not a self-race: a DIFFERENT Firebase uid already owns this
            # email (e.g. an orphaned row left behind by a Firebase-side
            # account deletion/recreation that bypassed the app's own
            # DELETE /auth/me, which cleans up both sides together).
            logger.error(
                "get_or_create_user: uid %s can't register — email %r already "
                "belongs to a different account", firebase_uid, decoded_token.get("email"),
            )
            raise HTTPException(
                status_code=409,
                detail="This email is already linked to a different Nibbler account. Contact support to sort this out.",
            )
        db.refresh(user)

    return user


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    token = credentials.credentials
    decoded = verify_firebase_token(token)
    user = get_or_create_user(decoded, db)
    # Rate limits key on the uid so one user can't dodge them by rotating IPs
    # (see app/rate_limit.py).
    request.state.user_id = user.id
    return user
