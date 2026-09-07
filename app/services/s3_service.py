import boto3
import hashlib
from botocore.config import Config
from botocore.exceptions import ClientError
import logging
from app.config import get_settings

settings = get_settings()


logger = logging.getLogger(__name__)

# Task 2 closeout (Verified Blocker 10): bounded provider timeout — the
# image-byte proxy route now calls download_file() synchronously inside a
# request, so an unresponsive S3 endpoint must fail fast rather than hang
# the request (and the worker) indefinitely.
_S3_CLIENT_CONFIG = Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 2})


class S3Service:
    def __init__(self):
        self.client = boto3.client(
            "s3",
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
            region_name=settings.aws_region,
            config=_S3_CLIENT_CONFIG,
        )
        self.bucket = settings.s3_bucket_name

    def _key_from(self, ref: str) -> str:
        """Accept either a bare object key (current rows) or a full public URL
        (rows written before July 2026, when the bucket was public)."""
        return ref.split(f"{self.bucket}.s3.{settings.aws_region}.amazonaws.com/")[-1]

    def upload_file(self, file_content: bytes, filename: str, content_type: str) -> str:
        """Upload and verify a file, returning its object key (not a URL)."""
        digest = hashlib.sha256(file_content).hexdigest()
        self.client.put_object(
            Bucket=self.bucket,
            Key=filename,
            Body=file_content,
            ContentType=content_type,
            Metadata={"sha256": digest},
        )
        head = self.client.head_object(Bucket=self.bucket, Key=filename)
        stored_size = head.get("ContentLength")
        stored_digest = (head.get("Metadata") or {}).get("sha256")
        if stored_size != len(file_content) or stored_digest != digest:
            raise RuntimeError(
                "S3 archive verification failed: stored object metadata does not match upload"
            )
        return filename

    def download_file(self, ref: str) -> bytes:
        """Download a file from S3 by key or legacy URL."""
        response = self.client.get_object(Bucket=self.bucket, Key=self._key_from(ref))
        return response["Body"].read()

    def delete_file(self, ref: str) -> bool:
        """Delete a file from S3 by key or legacy URL. True when it succeeded.

        This used to swallow ClientError entirely, which meant the GDPR
        erasure path in DELETE /auth/me reported "permanently deleted" even
        when the objects were still sitting in the bucket. Callers that care
        must check the return value.
        """
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self._key_from(ref))
            return True
        except ClientError as e:
            logger.error("S3 delete failed for %r: %s", ref, e)
            return False

    def generate_presigned_url(self, key: str, expiry: int = 3600) -> str:
        """Generate a temporary presigned URL for private file access."""
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expiry,
        )
