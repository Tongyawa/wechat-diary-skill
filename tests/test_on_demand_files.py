from __future__ import annotations
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from contextlib import redirect_stdout
from datetime import date

from scripts import export_on_demand as runner
from wechat_diary_core.on_demand_files import export_files, resolve_files_root
from wechat_diary_core.backends.weflow_api.client import WeflowApiClient
from test_export_on_demand import _config, FixtureClient, _message_at


def message(data=b"document", name="report.pdf", size=None, digest=None):
    md5 = digest if digest is not None else hashlib.md5(data).hexdigest()
    return {"localId": 1, "serverId": "file-placeholder", "localType": 49,
            "createTime": 1785921697,
            "rawContent": f"<msg><appmsg><title>{name}</title><type>6</type><appattach><md5>{md5}</md5><totallen>{len(data) if size is None else size}</totallen></appattach></appmsg></msg>"}


class FileTests(unittest.TestCase):
    def test_original_names_collisions_numbered_names_and_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            source.mkdir()
            for index, data in enumerate([b'first', b'second', b'third']):
                (source / str(index)).write_bytes(data)
            entries = [message(b'first', '报告.pdf'), message(b'second', '报告.pdf'),
                       message(b'third', '报告(1).pdf'), message(b'first', '报告.pdf')]
            report = export_files(entries, source, root / 'out')
            self.assertEqual([e['path'] for e in report['entries']],
                             ['报告.pdf','报告(2).pdf','报告(1).pdf','报告.pdf'])
            before = {p.name:p.read_bytes() for p in (root/'out').iterdir()}
            again = export_files(entries, source, root/'out')
            self.assertEqual(report, again)
            self.assertEqual(before, {p.name:p.read_bytes() for p in (root/'out').iterdir()})

    def test_existing_file_case_conflicts_and_index_names_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, out = root/'source', root/'out'
            source.mkdir()
            out.mkdir()
            (source/'actual').write_bytes(b'document')
            (out/'Report.PDF').write_bytes(b'older')
            report = export_files([message(name='report.pdf'), message(name='README.md'),
                                   message(name='manifest.json'), message(name='NUL.pdf')], source, out)
            self.assertEqual([e['path'] for e in report['entries']],
                             ['report(1).pdf','README(1).md','manifest(1).json','_NUL.pdf'])
            self.assertEqual((out/'Report.PDF').read_bytes(), b'older')
            self.assertEqual((out/'README(1).md').read_bytes(), b'document')

    def test_same_name_wrong_bytes_missing_identity_size_and_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "report.pdf").write_bytes(b"wrong!!!")
            (source / "renamed.bin").write_bytes(b"document")
            messages = [message(), message(name="../../evil.pdf"), message(b"missing!"),
                        message(digest=""), message(size=9)]
            report = export_files(messages, source, root / "out")
            self.assertEqual([e['status'] for e in report['entries']],
                             ['exported', 'exported', 'not_found', 'unverifiable', 'not_found'])
            for entry in report['entries'][:2]:
                target = root / "out" / entry['path']
                self.assertTrue(target.resolve().is_relative_to(root / "out"))
                self.assertEqual(target.read_bytes(), b"document")
            # Real bad input: a same-name candidate alone must not pass.
            (source / "renamed.bin").unlink()
            self.assertEqual(export_files([message()], source, root / "red")['exported'], 0)

    def test_source_changes_during_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "a").write_bytes(b"document")
            with mock.patch('wechat_diary_core.on_demand_files.shutil.copyfile', side_effect=lambda a,b: Path(b).write_bytes(b"corrupt!")):
                result = export_files([message()], source, root / "out")
            self.assertEqual(result['entries'][0]['status'], 'verification_failed')
            self.assertEqual(list((root / "out").glob('.copy-*')), [])

    def test_no_file_messages_does_not_scan(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch('os.walk', side_effect=AssertionError('must not scan')):
            self.assertEqual(export_files([], Path(tmp) / 'source', Path(tmp) / 'out')['total'], 0)

    def test_malformed_file_message_is_reported_and_nested_output_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            msg = message()
            msg['rawContent'] = '<msg><appmsg><type>6</type><broken'
            result = export_files([msg], root / 'source', root / 'out')
            self.assertEqual(result['entries'][0]['status'], 'unverifiable')
            with self.assertRaisesRegex(ValueError, '必须分离'):
                export_files([msg], root, root / 'nested')

    def test_root_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / 'config.toml'
            config.write_text('[on_demand]\nfiles_root="files"', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '不存在'):
                resolve_files_root(config)
            (root / 'files').mkdir()
            self.assertEqual(resolve_files_root(config), root / 'files')

    def test_api_omits_unbounded_dates(self):
        client = WeflowApiClient('http://localhost', 'placeholder')
        for start, end in [(None,None), (date(2026,1,1),None), (None,date(2026,1,2))]:
            with mock.patch.object(client, '_request', return_value={'messages': []}) as request:
                client.get_message_page('placeholder', start=start, end=end, media=False)
            params = request.call_args.args[1]
            self.assertEqual('start' in params, start is not None)
            self.assertEqual('end' in params, end is not None)

    def test_cli_full_history_and_file_integration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root)
            source = root / 'source'
            source.mkdir()
            (source / 'document').write_bytes(b'document')
            session = {'username': 'wxid_contact_placeholder', 'displayName': 'Contact'}
            msg = _message_at(date(2023,1,30), 10, 0, 'history', 20)
            client = FixtureClient([session], {session['username']: [msg, message()]})
            with mock.patch.object(runner, '_make_client', return_value=client), redirect_stdout(io.StringIO()):
                rc = runner.main(['--config', str(cfg.config_path), '--session', session['username'], '--out', str(root/'out'), '--no-asr', '--include-files', '--files-root', str(source)])
            self.assertEqual(rc, 0)
            call = next(c for c in client.calls if c[0]=='messages')
            self.assertIsNone(call[2]['start'])
            self.assertIsNone(call[2]['end'])
            self.assertEqual(len(list((root/'out').rglob('manifest.json'))), 1)
            self.assertIn('20230130', next((root/'out'/'_raw').glob('私聊*')).name)
            (source / 'document').unlink()
            with mock.patch.object(runner, '_make_client', return_value=client), redirect_stdout(io.StringIO()):
                rc = runner.main(['--config', str(cfg.config_path), '--session', session['username'], '--out', str(root/'partial'), '--no-asr', '--include-files', '--files-root', str(source)])
            self.assertEqual(rc, 3)

    def test_cli_single_boundaries_empty_results_and_no_files_zero_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root)
            session = {'username': 'wxid_contact_placeholder', 'displayName': 'Contact'}
            for index, (extra, start, end) in enumerate([
                ([], None, None),
                (['--start', '2020-01-01'], date(2020,1,1), None),
                (['--end', '2020-01-01'], None, date(2020,1,1)),
            ]):
                client = FixtureClient([session], {})
                with mock.patch.object(runner, '_make_client', return_value=client), mock.patch.object(runner, 'export_files', side_effect=AssertionError('must not scan')), redirect_stdout(io.StringIO()):
                    rc = runner.main(['--config', str(cfg.config_path), '--session', session['username'], '--out', str(root/f'out{index}'), '--no-asr', '--no-files', *extra])
                self.assertEqual(rc, 0)
                call = next(c for c in client.calls if c[0]=='messages')
                self.assertEqual((call[2]['start'],call[2]['end']), (start,end))
