from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from wechat_diary_core.config import load_config
from wechat_diary_core.preprocessing.moments import (
    archive_moments_for,
    discover_moments_exports,
    load_moments_export,
    render_moments_flow,
)


def _moments_payload(posts: list[dict]) -> dict:
    return {
        "exportTime": "2026-05-15T00:00:00",
        "totalPosts": len(posts),
        "filters": {"usernames": ["wxid_target"], "keyword": ""},
        "posts": posts,
    }


def _write_moments_file(root: Path, name: str, posts: list[dict]) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_moments_payload(posts), ensure_ascii=False), encoding="utf-8")
    return path


def _post(
    *,
    pid: str,
    username: str = "wxid_target",
    nickname: str = "Target",
    create_time_str: str = "2026/5/15 10:00:00",
    create_time: int = 1778839200,
    content: str = "",
    media: list[dict] | None = None,
    likes: list[str] | None = None,
    comments: list[dict] | None = None,
    location: dict | None = None,
) -> dict:
    return {
        "id": pid,
        "username": username,
        "nickname": nickname,
        "createTime": create_time,
        "createTimeStr": create_time_str,
        "contentDesc": content,
        "type": 1,
        "media": media or [],
        "likes": likes or [],
        "comments": comments or [],
        "location": location or {"latitude": 0, "longitude": 0},
    }


class MomentsTests(unittest.TestCase):
    def test_load_moments_export_returns_filter_and_posts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = _write_moments_file(root, "朋友圈导出_2026-05-15.json", [_post(pid="p1", content="hi")])

            export = load_moments_export(path)

        self.assertEqual(export.filters["usernames"], ["wxid_target"])
        self.assertEqual(len(export.posts), 1)
        self.assertEqual(export.posts[0]["id"], "p1")

    def test_render_includes_images_comments_and_location_but_not_likes(self) -> None:
        post = _post(
            pid="p1",
            content="今天天气不错",
            media=[
                {"localPath": "media/p1_0.jpg"},
                {"localPath": "media/p1_1.mp4"},
            ],
            likes=["Alice", "Bob"],
            comments=[
                {"nickname": "Carol", "content": "好看", "refNickname": ""},
                {"nickname": "Target", "content": "谢谢", "refNickname": "Carol"},
                {"nickname": "Wenyao", "content": "", "refNickname": "", "emojis": [{"md5": "abc"}]},
            ],
            location={"latitude": 1, "longitude": 1, "poiName": "武汉大学"},
        )

        text = render_moments_flow([post])

        self.assertIn("2026-05-15 10:00:00", text)
        self.assertIn("Target：今天天气不错", text)
        self.assertIn("[图片：media/p1_0.jpg]", text)
        self.assertIn("[视频：media/p1_1.mp4]", text)
        self.assertNotIn("❤", text)
        self.assertNotIn("Alice、Bob", text)
        self.assertIn("💬 Carol：好看", text)
        self.assertIn("💬 Target 回复 Carol：谢谢", text)
        self.assertIn("💬 Wenyao：[表情]", text)
        self.assertIn("📍 武汉大学", text)

    def test_archive_moments_for_filters_by_username_and_writes_per_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw" / "朋友圈导出_2026-05-15"
            _write_moments_file(
                raw,
                "朋友圈.json",
                [
                    _post(pid="a", username="wxid_target", create_time_str="2026/5/14 09:00:00", content="day1"),
                    _post(pid="b", username="wxid_target", create_time_str="2026/5/15 18:00:00", content="day2"),
                    _post(pid="c", username="wxid_other", create_time_str="2026/5/15 19:00:00", content="other"),
                ],
            )
            config_path = root / "config.toml"
            config_path.write_text(
                f"""
[paths]
raw = "{(root / 'raw').as_posix()}"
processed = "{(root / 'processed').as_posix()}"
""".strip(),
                encoding="utf-8",
            )
            cfg = load_config(config_path)

            written = archive_moments_for(["wxid_target"], config=cfg, subroot="_targets/moments")

            self.assertEqual({p.name for p in written}, {"2026-05-14.md", "2026-05-15.md"})
            day2 = next(p for p in written if p.name == "2026-05-15.md")
            body = day2.read_text(encoding="utf-8")
            self.assertIn("day2", body)
            self.assertNotIn("other", body)

    def test_discover_moments_exports_skips_non_moments_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            moments = _write_moments_file(root, "ok.json", [_post(pid="a")])
            (root / "notmoments.json").write_text(json.dumps({"random": "object"}), encoding="utf-8")
            (root / "chat.json").write_text(
                json.dumps({"session": {"type": "私聊"}, "messages": []}, ensure_ascii=False),
                encoding="utf-8",
            )

            discovered = discover_moments_exports(root)

        self.assertEqual(discovered, [moments])


if __name__ == "__main__":
    unittest.main()
