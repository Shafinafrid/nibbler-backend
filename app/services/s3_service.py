import boto3
from botocore.exceptions import ClientError
import logging
from app.config import get_settings

settings = get_settings()


logger = logging.getLogger(__name__)


class S3Service:
    def __init__(self):
        self.client = boto3.client(
            "s3",
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
            region_name=settings.aws_region,
        )
        self.bucket = settings.s3_bucket_name

    def _key_from(self, ref: str) -> str:
        """Accept either a bare object key (current rows) or a full public URL
        (rows written before July 2026, when the bucket was public)."""
        return ref.split(f"{self.bucket}.s3.{settings.aws_region}.amazonaws.com/")[-1]

    def upload_file(self, file_content: bytes, filename: str, content_type: str) -> str:
        """Upload a file to S3 and return its object KEY — not a URL. The
        bucket is private; use generate_presigned_url() for temporary access."""
        self.client.put_object(
            Bucket=self.bucket,
            Key=filename,
            Body=file_content,
            ContentType=content_type,
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
