"""Read + judge the rolling bundle backup's last-run state.

Why this exists as its own module: the same judgement is surfaced in two places
(``doctor.py`` when diagnosing, and the daily export's tail where a human
actually looks every day). Duplicating the staleness rule in both would let them
drift apart, and a backup check that disagrees with itself is worse than none.

The backup job that writes ``last-run.json`` is orchestrated by
``scripts/Invoke-BundleBackup.ps1``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import BackupConfig


# Ordered worst-first: callers that surface a single line report the first hit.
STATUS_DISABLED = "disabled"
STATUS_NEVER_RAN = "never_ran"
STATUS_UNREADABLE = "unreadable"
STATUS_FAILED = "failed"
STATUS_STALE = "stale"
STATUS_OK = "ok"


@dataclass(frozen=True)
class BackupStatus:
    status: str
    message: str
    #: Absolute age of the last *successful* run, in days. None when unknown.
    age_days: float | None = None

    @property
    def needs_attention(self) -> bool:
        """True when a human should act. ``disabled`` deliberately excluded.

        An unconfigured optional feature is not a problem to report -- warning
        about it would train users to ignore this channel, which is exactly how
        the previous backup failure went unnoticed for a month.
        """
        return self.status in {
            STATUS_NEVER_RAN,
            STATUS_UNREADABLE,
            STATUS_FAILED,
            STATUS_STALE,
        }


def _rerun_hint(skill_root: Path | None) -> str:
    script = "Invoke-BundleBackup.ps1"
    location = f'"{skill_root / "scripts" / script}"' if skill_root else f"<skill根>\\scripts\\{script}"
    return f"手动补跑：powershell -NoProfile -ExecutionPolicy Bypass -File {location}"


#: PowerShell's round-trip format emits 7 fractional digits
#: (``2026-08-08T20:54:22.8305510+08:00``). Current CPython parses that, but the
#: producer's precision is not ours to control, so normalise to 6 defensively.
_FRACTION_RE = re.compile(r"(\.\d{6})\d+")


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(_FRACTION_RE.sub(r"\1", text))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def evaluate_backup_state(
    backup: BackupConfig,
    *,
    now: datetime | None = None,
    skill_root: Path | None = None,
) -> BackupStatus:
    """Judge whether the bundle cold backup is actually running."""
    if not backup.enabled:
        return BackupStatus(STATUS_DISABLED, "bundle 冷备未配置（[backup] 未启用），跳过检查。")

    hint = _rerun_hint(skill_root)
    state_file = backup.state_file
    assert state_file is not None  # guaranteed by ``enabled``

    if not state_file.exists():
        return BackupStatus(
            STATUS_NEVER_RAN,
            f"bundle 冷备从未成功运行过（找不到 {state_file}）。{hint}",
        )

    try:
        # utf-8-sig, not utf-8: the producer is PowerShell's ``Set-Content
        # -Encoding UTF8``, which emits a BOM. Plain utf-8 raises on it, which
        # would make this check report "unreadable" forever. Verified against a
        # real run -- a Python-written fixture has no BOM and would hide this.
        payload = json.loads(state_file.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return BackupStatus(
            STATUS_UNREADABLE,
            f"bundle 冷备状态文件读不出（{state_file}）：{exc}。{hint}",
        )

    finished = _parse_timestamp(payload.get("finishedAt"))
    now = now or datetime.now(timezone.utc)
    age_days = None if finished is None else (now - finished).total_seconds() / 86400

    failed = [
        str(item.get("name") or "?")
        for item in payload.get("repos") or []
        if isinstance(item, dict) and item.get("result") != "ok"
    ]
    if payload.get("overall") != "ok" or failed:
        which = "、".join(failed) if failed else "未知仓"
        return BackupStatus(
            STATUS_FAILED,
            f"bundle 冷备上次运行有失败：{which}。详情见 {state_file}。{hint}",
            age_days,
        )

    if age_days is None:
        return BackupStatus(
            STATUS_UNREADABLE,
            f"bundle 冷备状态文件缺少可解析的 finishedAt（{state_file}）。{hint}",
        )

    if age_days > backup.stale_warn_days:
        return BackupStatus(
            STATUS_STALE,
            (
                f"bundle 冷备已 {age_days:.1f} 天没有成功运行"
                f"（阈值 {backup.stale_warn_days} 天，最后一次 {payload.get('finishedAt')}）。"
                f"计划任务可能已失效。{hint}"
            ),
            age_days,
        )

    return BackupStatus(
        STATUS_OK,
        f"bundle 冷备正常，最后一次成功 {age_days:.1f} 天前（{payload.get('finishedAt')}）。",
        age_days,
    )
