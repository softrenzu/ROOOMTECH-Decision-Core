from __future__ import annotations

import hashlib
import os
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse


@dataclass(frozen=True)
class ObjectRef:
    uri: str
    size_bytes: int
    sha256: str
    etag: str | None = None


def file_sha256(path: str | Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _safe_key(key: str) -> str:
    normalized = str(PurePosixPath(key.strip().lstrip("/")))
    if not normalized or normalized == "." or normalized.startswith("../") or "/../" in f"/{normalized}/":
        raise ValueError("invalid shared object key")
    return normalized


class SharedObjectStore:
    """Small object-store abstraction for immutable RTDC artifacts.

    The default backend is a local directory. Setting RTDC_SHARED_STORAGE_URL to
    s3://bucket/prefix switches to an S3-compatible backend. boto3 is imported
    only when the S3 backend is selected.
    """

    def put_file(self, local_path: str | Path, key: str, content_type: str | None = None) -> ObjectRef:
        raise NotImplementedError

    def materialize(self, uri: str, expected_sha256: str | None = None) -> Path:
        raise NotImplementedError

    def exists(self, uri: str) -> bool:
        raise NotImplementedError

    def delete(self, uri: str) -> None:
        raise NotImplementedError

    @classmethod
    def from_env(cls) -> "SharedObjectStore":
        location = os.getenv("RTDC_SHARED_STORAGE_URL", "data/shared-storage").strip()
        cache_dir = os.getenv("RTDC_SHARED_CACHE_DIR", "data/shared-cache").strip()
        if location.startswith("s3://"):
            parsed = urlparse(location)
            if not parsed.netloc:
                raise ValueError("RTDC_SHARED_STORAGE_URL s3 URI requires a bucket")
            return S3ObjectStore(
                bucket=parsed.netloc,
                prefix=parsed.path.strip("/"),
                cache_dir=cache_dir,
                endpoint_url=os.getenv("RTDC_S3_ENDPOINT_URL") or None,
                region_name=os.getenv("RTDC_S3_REGION") or None,
                sse=os.getenv("RTDC_S3_SSE") or None,
                kms_key_id=os.getenv("RTDC_S3_KMS_KEY_ID") or None,
            )
        if location.startswith("file://"):
            parsed = urlparse(location)
            if parsed.netloc not in {"", "localhost"}:
                raise ValueError("file shared-storage URI must be local")
            location = unquote(parsed.path)
        return LocalObjectStore(location)


class LocalObjectStore(SharedObjectStore):
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _target(self, key: str) -> Path:
        target = (self.root / _safe_key(key)).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("shared object path escapes configured root")
        return target

    def _path_from_uri(self, uri: str) -> Path:
        parsed = urlparse(uri)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise ValueError("object URI is not a local file URI")
        path = Path(unquote(parsed.path)).resolve()
        if not path.is_relative_to(self.root):
            raise PermissionError("object URI is outside configured local storage root")
        return path

    def put_file(self, local_path: str | Path, key: str, content_type: str | None = None) -> ObjectRef:
        del content_type
        source = Path(local_path)
        if not source.is_file():
            raise FileNotFoundError(str(source))
        target = self._target(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = file_sha256(source)
        temp = target.with_name(target.name + ".tmp")
        shutil.copyfile(source, temp)
        os.replace(temp, target)
        return ObjectRef(uri=target.as_uri(), size_bytes=target.stat().st_size, sha256=digest)

    def materialize(self, uri: str, expected_sha256: str | None = None) -> Path:
        path = self._path_from_uri(uri)
        if not path.is_file():
            raise FileNotFoundError(uri)
        if expected_sha256 and file_sha256(path) != expected_sha256:
            raise IOError("shared object SHA-256 mismatch")
        return path

    def exists(self, uri: str) -> bool:
        try:
            return self._path_from_uri(uri).is_file()
        except (ValueError, PermissionError):
            return False

    def delete(self, uri: str) -> None:
        self._path_from_uri(uri).unlink(missing_ok=True)


class S3ObjectStore(SharedObjectStore):
    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        cache_dir: str | Path = "data/shared-cache",
        endpoint_url: str | None = None,
        region_name: str | None = None,
        sse: str | None = None,
        kms_key_id: str | None = None,
    ):
        try:
            import boto3  # type: ignore
        except ImportError as exc:  # pragma: no cover - exercised only with s3 extra absent
            raise RuntimeError("S3 shared storage requires: pip install -e '.[s3]'") from exc
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.cache_dir = Path(cache_dir).expanduser().resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.sse = sse
        self.kms_key_id = kms_key_id
        self.client = boto3.client("s3", endpoint_url=endpoint_url, region_name=region_name)
        self._lock = threading.RLock()

    def _key(self, key: str) -> str:
        suffix = _safe_key(key)
        return f"{self.prefix}/{suffix}" if self.prefix else suffix

    def _parse(self, uri: str) -> str:
        parsed = urlparse(uri)
        if parsed.scheme != "s3" or parsed.netloc != self.bucket:
            raise ValueError("object URI does not belong to configured S3 bucket")
        key = parsed.path.lstrip("/")
        if self.prefix and not (key == self.prefix or key.startswith(self.prefix + "/")):
            raise PermissionError("object URI is outside configured S3 prefix")
        return key

    def put_file(self, local_path: str | Path, key: str, content_type: str | None = None) -> ObjectRef:
        source = Path(local_path)
        if not source.is_file():
            raise FileNotFoundError(str(source))
        object_key = self._key(key)
        extra: dict[str, str] = {}
        if content_type:
            extra["ContentType"] = content_type
        if self.sse:
            extra["ServerSideEncryption"] = self.sse
        if self.kms_key_id:
            extra["SSEKMSKeyId"] = self.kms_key_id
        kwargs = {"ExtraArgs": extra} if extra else {}
        self.client.upload_file(str(source), self.bucket, object_key, **kwargs)
        head = self.client.head_object(Bucket=self.bucket, Key=object_key)
        etag = str(head.get("ETag", "")).strip('"') or None
        return ObjectRef(
            uri=f"s3://{self.bucket}/{object_key}",
            size_bytes=int(head.get("ContentLength", source.stat().st_size)),
            sha256=file_sha256(source),
            etag=etag,
        )

    def materialize(self, uri: str, expected_sha256: str | None = None) -> Path:
        object_key = self._parse(uri)
        cache_key = hashlib.sha256((uri + "|" + (expected_sha256 or "")).encode("utf-8")).hexdigest()
        target = self.cache_dir / cache_key
        with self._lock:
            if target.is_file():
                if not expected_sha256 or file_sha256(target) == expected_sha256:
                    return target
                target.unlink(missing_ok=True)
            temp = target.with_name(target.name + ".tmp")
            self.client.download_file(self.bucket, object_key, str(temp))
            if expected_sha256 and file_sha256(temp) != expected_sha256:
                temp.unlink(missing_ok=True)
                raise IOError("downloaded S3 object SHA-256 mismatch")
            os.replace(temp, target)
            return target

    def exists(self, uri: str) -> bool:
        key = self._parse(uri)
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception as exc:  # botocore is optional; inspect response defensively
            response = getattr(exc, "response", {}) or {}
            code = str((response.get("Error") or {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    def delete(self, uri: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._parse(uri))
