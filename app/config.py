from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Database
    database_url: str

    # Claude
    claude_api_key: str
    claude_model_free: str = "claude-haiku-4-5"
    claude_model_paid: str = "claude-sonnet-4-6"

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

    # App
    app_env: str = "development"
    secret_key: str = "changeme"
    free_upload_limit: int = 3
    free_bites_per_day: int = 1
    premium_bites_per_day: int = 3
    # Upload safety: one unbounded file.read() can OOM the single Railway
    # process, so uploads are hard-capped (and extracted text bounded too).
    max_pdf_upload_mb: int = 20
    max_extracted_text_chars: int = 2_000_000

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    return Settings()
