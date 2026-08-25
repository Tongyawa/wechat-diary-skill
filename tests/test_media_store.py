from __future__ import annotations

import hashlib
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

from wechat_diary_core.media_store import store_media_file


class MediaStoreTests(unittest.TestCase):
    def test_publish_is_content_addressed_sharded_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "中文名.JPG"
            second = root / "另一个名字.jpg"
            first.write_bytes(b"real-byte-shape-placeholder")
            second.write_bytes(first.read_bytes())
            store = root / "_media"

            created = store_media_file(first, store, "images")
            reused = store_media_file(second, store, "images")

            digest = hashlib.md5(first.read_bytes()).hexdigest()
            self.assertEqual(created.relative_path.as_posix(), f"images/{digest[:2]}/{digest}.jpg")
            self.assertTrue(created.created)
            self.assertFalse(reused.created)
            self.assertEqual(created.path, reused.path)
            self.assertEqual(created.path.read_bytes(), first.read_bytes())

    def test_atomic_replace_failure_never_exposes_a_final_hash_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            source.write_bytes(b"complete-png-placeholder")
            store = root / "_media"
            with mock.patch(
                "wechat_diary_core.media_store.os.replace",
                side_effect=OSError("injected replace failure"),
            ):
                with self.assertRaisesRegex(OSError, "injected replace failure"):
                    store_media_file(source, store, "images")

            digest = hashlib.md5(source.read_bytes()).hexdigest()
            shard = store / "images" / digest[:2]
            self.assertEqual(list(shard.glob(f"{digest}.*")), [])
            self.assertEqual(list(shard.glob(".*.tmp")), [])

    def test_existing_hash_with_wrong_size_fails_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.gif"
            source.write_bytes(b"GIF89a-complete")
            digest = hashlib.md5(source.read_bytes()).hexdigest()
            destination = root / "_media" / "emojis" / digest[:2] / f"{digest}.gif"
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"cut")

            with self.assertRaisesRegex(RuntimeError, "大小不符"):
                store_media_file(source, root / "_media", "emojis")

    def test_same_hash_different_extension_reuses_one_file_and_warns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jpg = root / "source.jpg"
            png = root / "source.png"
            jpg.write_bytes(b"same-content-placeholder")
            png.write_bytes(jpg.read_bytes())
            store = root / "_media"
            first = store_media_file(jpg, store, "images")

            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                second = store_media_file(png, store, "images")

            self.assertEqual(first.path, second.path)
            self.assertTrue(second.extension_conflict)
            self.assertEqual(len(caught), 1)
            digest = hashlib.md5(jpg.read_bytes()).hexdigest()
            self.assertEqual(len(list((store / "images" / digest[:2]).glob(f"{digest}.*"))), 1)


if __name__ == "__main__":
    unittest.main()
