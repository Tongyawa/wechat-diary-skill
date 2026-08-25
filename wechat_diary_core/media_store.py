"""Content-addressed media storage shared by canonical raw exports."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


MEDIA_STORE_NAME = "_media"
MEDIA_SCHEME_KEY = "mediaScheme"
MEDIA_SCHEME_VALUE = "cas-v1"
MEDIA_CLASSES = frozenset({"images", "voices", "emojis", "videos"})
_EXTENSION_RE = re.compile(r"^\.[a-z0-9]{1,16}$")
_BUFFER_SIZE = 1024 * 1024


@dataclass(frozen=True)
class StoredMedia:
    path: Path
    relative_path: PurePosixPath
    created: bool
    extension_conflict: bool


def store_media_file(source: Path, store_root: Path, media_class: str) -> StoredMedia:
    """Publish one complete file under ``<class>/<hh>/<md5>.<ext>``.

    The source is hashed before a final name is chosen, then copied to a
    same-directory temporary file, flushed, fsynced, and atomically renamed.
    Existing content is reused only after its size is checked.
    """

    if media_class not in MEDIA_CLASSES:
        raise ValueError(f"不支持的 canonical media class：{media_class}")
    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(source)

    digest, source_size = _hash_file(source)
    extension = _normalized_extension(source)
    shard = Path(store_root) / media_class / digest[:2]
    shard.mkdir(parents=True, exist_ok=True)

    existing = _existing_hash_files(shard, digest)
    if existing:
        return _reuse_existing(existing, source_size, extension, media_class, digest)

    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{digest}.",
        suffix=".tmp",
        dir=shard,
    )
    temporary = Path(temporary_name)
    try:
        copied_digest, copied_size = _copy_and_hash(source, handle)
        if copied_digest != digest or copied_size != source_size:
            raise RuntimeError(f"媒体源文件在写入 CAS 期间发生变化：{source}")

        # Another writer may have published the same content while this copy
        # was being prepared. Recheck before the final atomic rename.
        existing = _existing_hash_files(shard, digest)
        if existing:
            return _reuse_existing(existing, source_size, extension, media_class, digest)

        destination = shard / f"{digest}{extension}"
        os.replace(temporary, destination)
        if destination.stat().st_size != source_size:
            raise RuntimeError(f"CAS 文件发布后大小不符：{destination}")
        relative = PurePosixPath(media_class, digest[:2], destination.name)
        return StoredMedia(destination, relative, True, False)
    finally:
        if temporary.exists():
            temporary.unlink()


def chat_media_reference(relative_path: PurePosixPath) -> str:
    return (PurePosixPath("..", MEDIA_STORE_NAME) / relative_path).as_posix()


def root_media_reference(relative_path: PurePosixPath) -> str:
    return (PurePosixPath(MEDIA_STORE_NAME) / relative_path).as_posix()


def is_media_reference(value: object, media_class: str | None = None) -> bool:
    """Recognize legacy ``media`` and canonical ``_media`` path references."""

    if not isinstance(value, str):
        return False
    normalized = value.strip().replace("\\", "/")
    if not normalized or "\n" in normalized or "\r" in normalized:
        return False
    parts = PurePosixPath(normalized).parts
    if parts and parts[0] == "..":
        parts = parts[1:]
    if len(parts) < 3 or parts[0] not in {"media", MEDIA_STORE_NAME}:
        return False
    if any(part in {"", ".", ".."} for part in parts[1:]):
        return False
    candidate_class = parts[1]
    return candidate_class in MEDIA_CLASSES and (media_class is None or candidate_class == media_class)


def _reuse_existing(
    existing: list[Path],
    source_size: int,
    incoming_extension: str,
    media_class: str,
    digest: str,
) -> StoredMedia:
    if len(existing) != 1:
        names = ", ".join(path.name for path in existing)
        raise RuntimeError(
            f"CAS 违反唯一文件约束：class={media_class}, md5={digest}, files={names}"
        )
    destination = existing[0]
    stored_size = destination.stat().st_size
    if stored_size != source_size:
        raise RuntimeError(
            "CAS 命中同 md5 但大小不符，拒绝复用："
            f"class={media_class}, md5={digest}, incoming={source_size}, stored={stored_size}"
        )
    extension_conflict = destination.suffix.lower() != incoming_extension
    if extension_conflict:
        warnings.warn(
            "CAS 命中同 md5 不同扩展名，复用既有文件："
            f"class={media_class}, md5={digest}, existing={destination.suffix.lower()}, "
            f"incoming={incoming_extension}",
            RuntimeWarning,
            stacklevel=3,
        )
    relative = PurePosixPath(media_class, digest[:2], destination.name)
    return StoredMedia(destination, relative, False, extension_conflict)


def _existing_hash_files(shard: Path, digest: str) -> list[Path]:
    return sorted(path for path in shard.glob(f"{digest}.*") if path.is_file())


def _normalized_extension(source: Path) -> str:
    extension = source.suffix.lower()
    return extension if _EXTENSION_RE.fullmatch(extension) else ".bin"


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.md5()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(_BUFFER_SIZE):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _copy_and_hash(source: Path, handle: int) -> tuple[str, int]:
    digest = hashlib.md5()
    size = 0
    with os.fdopen(handle, "wb") as destination_stream:
        with source.open("rb") as source_stream:
            while chunk := source_stream.read(_BUFFER_SIZE):
                destination_stream.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        destination_stream.flush()
        os.fsync(destination_stream.fileno())
    return digest.hexdigest(), size


__all__ = [
    "MEDIA_CLASSES",
    "MEDIA_SCHEME_KEY",
    "MEDIA_SCHEME_VALUE",
    "MEDIA_STORE_NAME",
    "StoredMedia",
    "chat_media_reference",
    "is_media_reference",
    "root_media_reference",
    "store_media_file",
]
