"""Closed processed truth table and deliberate decision mutations."""
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import archive_exports, process_existing_raw
from test_raw_archive import payload, save
import test_processed_retention as retention_fixture
import test_processed_many_to_one as multi_fixture
from wechat_diary_core import workspace
from wechat_diary_core.config import load_config


class CurrentRotationTests(unittest.TestCase):
    def test_empty_touch_set_breaks_both_e2_and_2a(self):
        fixtures = [
            (retention_fixture.ProcessedRetentionTests, "test_e2_existing_processed_cannot_merge_withheld_chat"),
            (multi_fixture.ManyToOneRetentionTests, "test_2a_self_moments_archive_bytes_stay_and_live_file_is_not_protected"),
        ]
        for fixture, method in fixtures:
            for broken in (False, True):
                with self.subTest(fixture=method, broken=broken):
                    case = fixture(method)
                    case.setUp()
                    try:
                        if broken:
                            with patch.object(workspace, "retained_chat_targets", return_value=set()), \
                                 patch.object(workspace, "retained_multi_targets", return_value={}):
                                with self.assertRaises(AssertionError):
                                    getattr(case, method)()
                        else:
                            getattr(case, method)()
                    finally:
                        case.doCleanups()

    def test_batch_copy_and_move_merge_every_ready_processed_file(self):
        for move in (False, True):
            with self.subTest(move=move), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = root / "config.toml"
                config.write_text('[paths]\nraw="live"\nprocessed="processed"\narchived="archived"\n', encoding="utf-8")
                incoming = save(root / "incoming/New_20300102/New_20300102.json", payload(wxid="wxid_new"))
                processed = root / "supplied"
                (processed / "New").mkdir(parents=True)
                (processed / "New/2030-01-02.md").write_bytes(b"NEW-MD\n")
                side = processed / "_targets/chats/2030-01-02.md"
                side.parent.mkdir(parents=True)
                side.write_bytes(b"SIDECAR-NEW")
                old = root / "archived/processed/_targets/chats/2030-01-02.md"
                old.parent.mkdir(parents=True)
                old.write_bytes(b"SIDECAR-OLD")
                args = ["--config", str(config), "--raw-root", str(root / "incoming"),
                        "--processed-root", str(processed), "--force-overwrite"]
                if move:
                    args.append("--move-source")
                with redirect_stdout(io.StringIO()):
                    code = archive_exports.main(args)
                self.assertEqual(code, 0)
                self.assertEqual((root / "archived/processed/New/2030-01-02.md").read_bytes(), b"NEW-MD\n")
                self.assertEqual(old.read_bytes(), b"SIDECAR-NEW")
                self.assertEqual(incoming.exists(), not move)

    def test_nonrotation_does_not_consume_even_a_corrupt_raw_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text('[paths]\nraw="live"\nprocessed="processed"\narchived="archived"\n', encoding="utf-8")
            cfg = load_config(config)
            save(cfg.paths.raw / "New_20300102/New_20300102.json", payload())
            cfg.paths.processed.mkdir()
            (cfg.paths.processed / "previous.md").write_bytes(b"PREVIOUS")
            cfg.paths.archived.mkdir()
            ledger = cfg.paths.archived / "raw-fold-report.json"
            ledger.write_bytes(b"{broken ledger")
            with redirect_stdout(io.StringIO()):
                process_existing_raw.process_existing_raw(cfg, day="2030-01-02", skip_voice_fallback=True)
            self.assertEqual(ledger.read_bytes(), b"{broken ledger")
            self.assertEqual((cfg.paths.archived / "processed/previous.md").read_bytes(), b"PREVIOUS")
            self.assertTrue(list(cfg.paths.processed.rglob("*.md")))
