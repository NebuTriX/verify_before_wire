"""File storage for uploaded inputs, on whichever cloud the customer runs.
BLOB_BACKEND = gcs | s3 | azure | file | memory. Credentials come from the platform
(workload identity / instance role / managed identity); no keys are stored in config."""
from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from pathlib import Path

from .config import Settings

SAFE_KEY = re.compile(r"^[A-Za-z0-9_\-./]{1,512}$")


def _check(key: str) -> str:
    if not SAFE_KEY.match(key) or ".." in key or key.startswith("/"):
        raise ValueError("unsafe storage key")
    return key


class Blobs(ABC):
    scheme: str

    @abstractmethod
    async def put(self, key: str, data: bytes, mime: str) -> str: ...
    @abstractmethod
    async def get(self, uri: str) -> bytes: ...

    def _key(self, uri: str) -> str:
        prefix = f"{self.scheme}://"
        if not uri.startswith(prefix):
            raise ValueError(f"expected {prefix} uri")
        rest = uri[len(prefix):]
        return _check(rest.split("/", 1)[1] if self.scheme in ("gs", "s3", "az") else rest)


class MemoryBlobs(Blobs):
    scheme = "mem"

    def __init__(self):
        self.data: dict[str, bytes] = {}

    async def put(self, key, data, mime):
        self.data[_check(key)] = data
        return f"mem://{key}"

    async def get(self, uri):
        return self.data[self._key(uri)]


class FileBlobs(Blobs):
    """Local disk or a mounted volume (on-premises installs)."""
    scheme = "file"

    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / _check(key)).resolve()
        if self.root not in p.parents:
            raise ValueError("path escapes storage root")
        return p

    async def put(self, key, data, mime):
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(p.write_bytes, data)
        return f"file://{key}"

    async def get(self, uri):
        return await asyncio.to_thread(self._path(self._key(uri)).read_bytes)


class GcsBlobs(Blobs):
    scheme = "gs"

    def __init__(self, bucket: str):
        from google.cloud import storage
        self.bucket_name, self.b = bucket, storage.Client().bucket(bucket)

    async def put(self, key, data, mime):
        await asyncio.to_thread(self.b.blob(_check(key)).upload_from_string, data, content_type=mime)
        return f"gs://{self.bucket_name}/{key}"

    async def get(self, uri):
        return await asyncio.to_thread(self.b.blob(self._key(uri)).download_as_bytes)


class S3Blobs(Blobs):
    scheme = "s3"

    def __init__(self, bucket: str, kms_key_id: str = ""):
        import boto3
        self.bucket, self.kms, self.c = bucket, kms_key_id, boto3.client("s3")

    async def put(self, key, data, mime):
        extra = {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": self.kms} if self.kms else \
                {"ServerSideEncryption": "AES256"}
        await asyncio.to_thread(self.c.put_object, Bucket=self.bucket, Key=_check(key), Body=data,
                                ContentType=mime, **extra)
        return f"s3://{self.bucket}/{key}"

    async def get(self, uri):
        r = await asyncio.to_thread(self.c.get_object, Bucket=self.bucket, Key=self._key(uri))
        return await asyncio.to_thread(r["Body"].read)


class AzureBlobs(Blobs):
    scheme = "az"

    def __init__(self, account_url: str, container: str):
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient
        self.container = container
        self.c = BlobServiceClient(account_url, credential=DefaultAzureCredential()).get_container_client(container)

    async def put(self, key, data, mime):
        from azure.storage.blob import ContentSettings
        await asyncio.to_thread(self.c.upload_blob, _check(key), data, overwrite=True,
                                content_settings=ContentSettings(content_type=mime))
        return f"az://{self.container}/{key}"

    async def get(self, uri):
        return await asyncio.to_thread(lambda: self.c.download_blob(self._key(uri)).readall())


def build_blobs(s: Settings) -> Blobs:
    match s.blob_backend:
        case "gcs": return GcsBlobs(s.uploads_bucket)
        case "s3": return S3Blobs(s.uploads_bucket, s.s3_kms_key_id)
        case "azure": return AzureBlobs(s.azure_blob_account_url, s.uploads_bucket)
        case "file": return FileBlobs(s.blob_dir)
        case "memory": return MemoryBlobs()
    raise ValueError(f"unknown BLOB_BACKEND {s.blob_backend!r}")
