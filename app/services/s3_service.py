import boto3
from botocore.exceptions import ClientError
from app.config import get_settings

settings = get_settings()


class S3Service:
    def __init__(self):
        self.client = boto3.client(
            "s3",
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
            region_name=settings.aws_region,
        )
        self.bucket = settings.s3_bucket_name

    async def upload_file(self, file_content: bytes, filename: str, content_type: str) -> str:
        """Upload a file to S3 and return its public URL."""
        self.client.put_object(
            Bucket=self.bucket,
            Key=filename,
            Body=file_content,
            ContentType=content_type,
        )
        return f"https://{self.bucket}.s3.{settings.aws_region}.amazonaws.com/{filename}"

    async def download_file(self, file_url: str) -> bytes:
        """Download a file from S3 by URL."""
        key = file_url.split(f"{self.bucket}.s3.{settings.aws_region}.amazonaws.com/")[-1]
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        return response["Body"].read()

    async def delete_file(self, file_url: str):
        """Delete a file from S3 by URL."""
        try:
            key = file_url.split(f"{self.bucket}.s3.{settings.aws_region}.amazonaws.com/")[-1]
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except ClientError:
            pass

    def generate_presigned_url(self, key: str, expiry: int = 3600) -> str:
        """Generate a temporary presigned URL for private file access."""
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expiry,
        )
