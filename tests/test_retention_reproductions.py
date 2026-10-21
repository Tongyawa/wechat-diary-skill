"""Copied independent reproductions run only against synthetic system-temp data."""
from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch

from wechat_diary_core import workspace

FIXTURES = Path(__file__).parent / 'fixtures/retention'


class RetentionReproductions(unittest.TestCase):
    def probe(self, filename, args=()):
        with tempfile.TemporaryDirectory(prefix='retention-probe-') as temp:
            spec = importlib.util.spec_from_file_location('common', FIXTURES / 'common.py')
            common = importlib.util.module_from_spec(spec)
            # Fixture imports modify sys.path, so restore it after the probe.
            old_path = list(sys.path)
            output = io.StringIO()
            try:
                spec.loader.exec_module(common)
                common.HERE = Path(temp)
                with patch.dict(sys.modules, {'common':common}), patch.object(sys, 'argv', [filename, *args]), redirect_stdout(output):
                    result = runpy.run_path(str(FIXTURES / filename))
                # Assertions on paths must happen while temp files still exist.
                return output.getvalue(), result.get('elapsed')
            finally:
                sys.path[:] = old_path

    def test_b1_ready_reexport_refreshes_sidecar_and_merges_previous_sidecar(self):
        output, _ = self.probe('b1_rerender_skip_stale_sidecar.py')
        self.assertIn('run2 Target/D.md has NEW-MSG: True', output)
        self.assertIn('run2 sidecar D.md has NEW-MSG: True', output)
        self.assertIn('archived sidecar exists: True', output)

    def test_b2_same_day_transcription_refreshes_both_outputs(self):
        output, _ = self.probe('b2_daily_rerun_sidecar_stale.py')
        self.assertIn('Target/D.md has transcription: True', output)
        self.assertIn('sidecar D.md has transcription: True', output)

    def test_c1_archive_subdirectory_rerender_merges_previous_live_output(self):
        output, _ = self.probe('c1_existing_raw_from_archive_root.py')
        self.assertIn('Gamma render merged to archive: True', output)
        self.assertIn("processed files after: ['Beta/2029-05-05.md']", output)

    def test_a1_schema_failure_matches_main_in_plain_force_and_move(self):
        for variant in ([], ['force'], ['move']):
            with self.subTest(variant=variant):
                output, _ = self.probe('a1_bulk_schema_invalid_render_merged.py', variant)
                self.assertIn('rc = 1', output)
                self.assertIn('incoming raw still unarchived in raw root: True', output)
                if variant == ['force']:
                    self.assertIn("archived md = b'OTHER W2 RENDER (from unarchived raw)\\n'", output)
                else:
                    self.assertIn("archived md = b'VICTIM W1 RENDER\\n'", output)
                    self.assertNotIn('VIOLATION', output)

    def test_s1_isolation_prevents_next_day_partial_render_and_legacy_state_control_fails(self):
        output, _ = self.probe('s1_retained_rerenders_partial_sidecar.py', ['isolate'])
        self.assertIn('live sidecar N re-rendered on day N+1: False', output)
        self.assertIn('archive sidecar N still has private msg after N+2 merge: True', output)
        with self.assertRaisesRegex(AssertionError, 'S1:'):
            self.probe('s1_retained_rerenders_partial_sidecar.py', ['legacy-state'])

    def test_e1_original_existing_moments_render_does_not_replace_archive(self):
        output, _ = self.probe('e1_moments_retained_processed_merged.py')
        self.assertIn('archived moments raw unchanged: True', output)
        self.assertIn('incoming moments retained in live raw: False', output)
        self.assertIn('its media still in live raw: False', output)
        self.assertIn("archived processed md = b'ARCHIVED: p1 p2 p3\\n'", output)
        self.assertIn('live moments md still exists: True', output)
        self.assertIn('report lists path: True', output)
        self.assertNotIn('VIOLATION', output)
