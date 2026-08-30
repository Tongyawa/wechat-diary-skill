from __future__ import annotations

import json
import os
import shutil
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence, TypeVar


_ErrorT = TypeVar("_ErrorT", bound=Exception)


def canonical_message_fragment(message: Mapping[str, Any], *, newline: str) -> str:
    dumped = json.dumps(dict(message), ensure_ascii=False, indent=2).replace("\n", newline)
    return newline.join(f"    {line}" for line in dumped.split(newline))


def assert_only_message_fields_changed(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    path: Path,
    *,
    allowed_fields: Sequence[str],
    error_type: type[_ErrorT],
) -> None:
    normalized_after = deepcopy(after)
    before_messages = before.get("messages")
    after_messages = normalized_after.get("messages")
    if not isinstance(before_messages, list) or not isinstance(after_messages, list):
        raise error_type(f"canonical messages 结构不可比：{path}")
    if len(before_messages) != len(after_messages):
        raise error_type(f"名字修复改变了消息条数：{path}")
    for old_message, new_message in zip(before_messages, after_messages):
        if not isinstance(old_message, Mapping) or not isinstance(new_message, dict):
            raise error_type(f"canonical message 结构不可比：{path}")
        for field_name in allowed_fields:
            if field_name in old_message:
                new_message[field_name] = old_message[field_name]
            else:
                new_message.pop(field_name, None)
    if normalized_after != before:
        raise error_type(f"名字修复试图修改允许字段之外的内容：{path}")


def atomic_replace_bytes(path: Path, content: bytes) -> None:
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            shutil.copymode(path, temporary)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
