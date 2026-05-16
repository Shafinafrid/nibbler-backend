from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
import firebase_admin
from firebase_admin import auth as firebase_auth, credentials
from app.database import get_db
from app.models.user import User
from app.config import get_settings
import uuid

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
        db.commit()
        db.refresh(user)

    return user


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    token = credentials.credentials
    decoded = verify_firebase_token(token)
    return get_or_create_user(decoded, db)
