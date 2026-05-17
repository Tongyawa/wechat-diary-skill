from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from wechat_diary_core.archiving import archive_chats_for, promote_day_to_archive
from wechat_diary_core.config import load_config


def _write_chat(folder: Path, *, target_wxid: str, day_iso: str, ts: int, image_ref: str | None = None) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    messages = [
        {
            "localId": 1,
            "createTime": ts,
            "formattedTime": f"{day_iso} 18:00:00",
            "type": "文本消息",
            "content": "你好",
            "isSend": 1,
            "senderUsername": "me",
            "platformMessageId": "p1",
        },
        {
            "localId": 2,
            "createTime": ts + 60,
            "formattedTime": f"{day_iso} 18:01:00",
            "type": "图片消息" if image_ref else "文本消息",
            "content": image_ref if image_ref else "回声",
            "isSend": 0,
            "senderUsername": target_wxid,
            "senderDisplayName": "Target",
            "platformMessageId": "p2",
        },
    ]
    payload = {
        "session": {"type": "私聊", "username": target_wxid, "displayName": "Target"},
        "messages": messages,
    }
    (folder / f"{folder.name}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _config(root: Path) -> object:
    config_path = root / "config.toml"
    config_path.write_text(
        f"""
[paths]
raw = "{(root / 'raw').as_posix()}"
processed = "{(root / 'processed').as_posix()}"
archived = "{(root / 'archived').as_posix()}"
""".strip(),
        encoding="utf-8",
    )
    return load_config(config_path)


class ArchiveChatsForTests(unittest.TestCase):
    def test_filters_to_target_usernames_and_skips_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root)
            target_folder = cfg.paths.raw / "私聊_Target_20260515"
            (target_folder / "media" / "images").mkdir(parents=True)
            (target_folder / "media" / "images" / "a.jpg").write_bytes(b"fake")
            _write_chat(
                target_folder,
                target_wxid="wxid_target",
                day_iso="2026-05-15",
                ts=1778839200,
                image_ref="media/images/a.jpg",
            )
            other_folder = cfg.paths.raw / "私聊_Other_20260515"
            _write_chat(other_folder, target_wxid="wxid_other", day_iso="2026-05-15", ts=1778839200)

            written = archive_chats_for(["wxid_target"], config=cfg, subroot="_targets/chats")

            self.assertEqual({p.name for p in written}, {"2026-05-15.md"})
            body = written[0].read_text(encoding="utf-8")
            self.assertIn("Target：[图片：media/images/a.jpg]", body)
            self.assertNotIn("[OCR]", body)
            self.assertEqual(written[0].parent.name, "chats")
            self.assertEqual(written[0].parent.parent.name, "_targets")

    def test_empty_usernames_returns_empty_without_clearing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root)
            keep = cfg.paths.processed / "session_a" / "2026-05-14.md"
            keep.parent.mkdir(parents=True)
            keep.write_text("hi", encoding="utf-8")

            written = archive_chats_for([], config=cfg)

            self.assertEqual(written, [])
            self.assertTrue(keep.exists())


class PromoteDayToArchiveTests(unittest.TestCase):
    def test_copies_matching_day_files_into_archived(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root)
            a = cfg.paths.processed / "私聊_X" / "2026-05-15.md"
            b = cfg.paths.processed / "私聊_Y" / "2026-05-15.md"
            c = cfg.paths.processed / "私聊_X" / "2026-05-14.md"  # different day, should be ignored
            for path, text in ((a, "alpha"), (b, "beta"), (c, "stale")):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")

            promoted = promote_day_to_archive("2026-05-15", config=cfg)

            names = {p.relative_to(cfg.paths.archived).as_posix() for p in promoted}
            self.assertEqual(names, {"私聊_X/2026-05-15.md", "私聊_Y/2026-05-15.md"})
            self.assertTrue(a.exists())
            self.assertTrue(b.exists())
            self.assertFalse((cfg.paths.archived / "私聊_X" / "2026-05-14.md").exists())

    def test_move_mode_removes_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root)
            source = cfg.paths.processed / "私聊_X" / "2026-05-15.md"
            source.parent.mkdir(parents=True)
            source.write_text("hi", encoding="utf-8")

            promote_day_to_archive("2026-05-15", config=cfg, move=True)

            self.assertFalse(source.exists())
            self.assertTrue((cfg.paths.archived / "私聊_X" / "2026-05-15.md").exists())


if __name__ == "__main__":
    unittest.main()
