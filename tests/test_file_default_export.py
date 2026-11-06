"""File export consequences, using anonymized API XML shapes and synthetic bytes."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from datetime import date, datetime
from dataclasses import replace
from contextlib import redirect_stdout
import io

from wechat_diary_core.on_demand_files import FileResolver, _metadata, export_files, file_extension
from wechat_diary_core.backends.weflow_api.mapper import map_session_json
from wechat_diary_core.config import load_config, FileAttachmentsConfig
from wechat_diary_core.chat_identity import preserve_file_sources
from scripts import export_on_demand


def api_file(data=b"synthetic document", *, name="report.PDF", size=None, md5=None, number=1):
    # appmsg/appattach shape observed by the live 2026-11-06 preflight;
    # values and identities are synthetic, including XML without MD5 control.
    import xml.etree.ElementTree as ET
    root = ET.Element("msg")
    app = ET.SubElement(root, "appmsg")
    ET.SubElement(app, "title").text = name
    ET.SubElement(app, "type").text = "6"
    attach = ET.SubElement(app, "appattach")
    ET.SubElement(attach, "totallen").text = str(len(data) if size is None else size)
    ET.SubElement(attach, "md5").text = hashlib.md5(data).hexdigest() if md5 is None else md5
    return {"localId": number, "serverId": f"synthetic_{number}", "localType": (6 << 32) | 49,
            "createTime": int(datetime(2026, 11, 5, 8).timestamp())+number,
            "content": "[文件]", "rawContent": ET.tostring(root, encoding="unicode"), "isSend": 0}


class Client:
    session = {"username": "synthetic_identity", "displayName": "synthetic"}
    def __init__(self, messages):
        self.messages = messages
    def fetch_sessions(self, **kwargs):
        return [self.session]
    def fetch_contacts(self, **kwargs):
        return [self.session]
    def fetch_messages(self, *args, **kwargs):
        return self.messages


def config(root, enabled=True):
    path = put(root / "config.toml", (f'[paths]\nraw="live"\narchived="archived"\nprocessed="processed"\n'
               f'[file_attachments]\nenabled={str(enabled).lower()}\nsources=["source"]\n'
               '[asr]\nengine=""\n').encode())
    return load_config(path)

from wechat_diary_core.raw_archive import plan_raw_ingest, apply_raw_ingest_plan
from wechat_diary_core.preprocessing.cleaner import discover_chat_exports
from wechat_diary_core.preprocessing.moments import discover_moments_exports


def put(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
    return path


def chat(source=""):
    return {"session": {"wxid": "synthetic_identity", "displayName": "synthetic", "nickname": "synthetic", "remark": "", "type": "私聊"},
            "weflow": {"source": "http_api", "mediaScheme": "cas-v1"},
            "messages": [{"localId": 1, "platformMessageId": "synthetic_message",
                          "createTime": 1793836800, "formattedTime": "2026-11-05 08:00:00",
                          "type": "其他消息", "source": source, "content": "[文件：synthetic.txt（18 B）]",
                          "isSend": 0, "senderUsername": "synthetic_sender", "senderDisplayName": "synthetic"}]}


class FileConsequencesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_discovery_never_opens_media_json(self):
        root = self.root
        normal = put(root / "synthetic" / "synthetic_20261105.json", chat())
        put(root / "_media/files/aa/chat.json", chat())
        put(root / "_media/files/aa/moments.json", {"posts": [], "totalPosts": 0})
        put(root / "_media/files/aa/binary.json", b"\xffsynthetic")
        self.assertEqual(discover_chat_exports(root), [normal])
        self.assertEqual(discover_moments_exports(root), [])
        from scripts.backfill_group_names import backfill_group_names
        backfill_group_names(root, apply=True)
        self.assertEqual((root/'_media/files/aa/binary.json').read_bytes(), b'\xffsynthetic')

    def test_daily_bulk_rename_preserve_file_source_after_publication(self):
        for policy in ("daily", "bulk", "rename"):
            with self.subTest(policy=policy):
                root = self.root / policy
                archive, incoming = root / "archived/raw", root / "incoming"
                ref = "../_media/files/aa/" + "a" * 32 + ".txt"
                old = put(archive / "synthetic/synthetic_20261105.json", chat(ref))
                if policy == "rename":
                    incoming = archive
                    source = put(archive / "old/synthetic_20261105.json", chat())
                    kwargs = {"rename_targets": {"old": "synthetic"}, "only_paths": {source}}
                else:
                    put(incoming / "synthetic/synthetic_20261105.json", chat())
                    kwargs = {}
                plan = plan_raw_ingest(archive, [incoming], policy=policy, **kwargs)
                self.assertFalse(plan.schema_failures)
                result = apply_raw_ingest_plan(plan)
                self.assertIn(old, result.published)
                self.assertEqual(json.loads(old.read_bytes())["messages"][0]["source"], ref)

    def test_rejected_daily_file_is_self_contained(self):
        archive, incoming = self.root / "archived/raw", self.root / "incoming"
        data = b"synthetic dependency payload"
        digest = hashlib.md5(data).hexdigest()
        ref = f"../_media/files/{digest[:2]}/{digest}.bin"
        old = chat()
        second = copy.deepcopy(old["messages"][0])
        second.update(localId=2, createTime=1793836801, platformMessageId="synthetic_second")
        old["messages"].append(second)
        put(archive / "synthetic/synthetic_20261105.json", old)
        source = put(incoming / "synthetic_20261105/synthetic_20261105.json", chat(ref))
        put((source.parent / ref).resolve(), data)
        plan = plan_raw_ingest(archive, [incoming], policy="daily")
        result = apply_raw_ingest_plan(plan, move_source=True)
        isolated = result.isolation_dir / source.relative_to(incoming)
        self.assertEqual((isolated.parent / ref).resolve().read_bytes(), data)

    def test_exact_identity_cas_name_dedup_and_readonly_sources(self):
        cfg = config(self.root)
        data = b"synthetic document"
        put(self.root / "source/opaque.cache", data)
        put(self.root / "source/report.PDF", b"same name wrong bytes")
        before = {p.name: p.read_bytes() for p in (self.root/"source").iterdir()}
        messages = [api_file(), api_file(name="copy.pdf", number=2), api_file(md5="", number=3),
                    api_file(b"missing", number=4), api_file(size=1, number=5)]
        resolver = FileResolver.from_config(cfg)
        result = map_session_json(Client.session, messages, contacts=[], media_dir=self.root/"out/_media", file_resolver=resolver)
        refs = [m["source"] for m in result["messages"]]
        digest = hashlib.md5(data).hexdigest()
        self.assertEqual(refs, [f"../_media/files/{digest[:2]}/{digest}.pdf"]*2+["", "", ""])
        self.assertEqual(len(list((self.root/"out/_media").rglob("*.*"))), 1)
        self.assertEqual((resolver.total, resolver.acquired, resolver.missing, resolver.no_md5), (5,2,2,1))
        self.assertEqual(before, {p.name: p.read_bytes() for p in (self.root/"source").iterdir()})
        without_files = map_session_json(Client.session, messages, contacts=[])
        self.assertEqual(result["messages"][0]["content"], without_files["messages"][0]["content"])

    def test_extensions_archive_fallback_and_corrupt_candidate(self):
        cfg = config(self.root)
        data = b"synthetic document"
        digest = hashlib.md5(data).hexdigest()
        archived = put(cfg.paths.archived / f"raw/_media/files/{digest[:2]}/{digest}.docx", data)
        for name in ("file", "file.123", "file.bad-ext", "folder/name.123", "a."+"a"*17):
            self.assertEqual(file_extension(name), "bin")
        self.assertEqual(file_extension("folder\\NAME.PDF "), "pdf")
        resolver = FileResolver.from_config(cfg)
        refs = resolver.localize([api_file(name="other.pdf")], self.root/"out/_media")
        self.assertTrue(refs[0].endswith(digest+".docx"))
        self.assertEqual((self.root/"out/_media/files"/digest[:2]/(digest+".docx")).read_bytes(), data)
        archived.write_bytes(b"corrupt same length")
        with mock.patch.object(FileResolver, '_build_index', return_value=None):
            resolver = FileResolver((), cas_roots=[archived.parents[1]])
            resolver.index = {}
            self.assertIsNone(resolver.find(_metadata(api_file())))

    def test_missing_size_is_unverifiable_and_no_filename_guess(self):
        cfg = config(self.root)
        put(self.root/"source/report.PDF", b"wrong")
        msg = api_file()
        msg["rawContent"] = msg["rawContent"].replace("<totallen>18</totallen>", "")
        self.assertIsNone(_metadata(msg)["size"])
        report = export_files([msg, api_file()], self.root/"source", self.root/"out")
        self.assertEqual(report["exported"], 0)
        self.assertEqual(report["entries"][0]["status"], "unverifiable")

    def test_daily_bulk_and_on_demand_default_and_disabled(self):
        from wechat_diary_core.backends.weflow_api.backend import WeflowApiBackend
        for enabled in (True, False):
            for channel in ("daily", "bulk", "on_demand"):
                with self.subTest(enabled=enabled, channel=channel):
                    root = self.root/f"{enabled}-{channel}"
                    cfg = config(root, enabled)
                    put(root/"source/document", b"synthetic document")
                    client = Client([api_file()])
                    reads = []
                    original = FileResolver.find
                    def find(resolver, entry):
                        if resolver.enabled:
                            reads.append(entry)
                        return original(resolver, entry)
                    with mock.patch.object(FileResolver, 'find', find), redirect_stdout(io.StringIO()):
                        if channel == "on_demand":
                            result = export_on_demand.export_on_demand(cfg, sessions=[client.session], client=client,
                                session_query="synthetic_identity", start=date(2026,11,5), end=date(2026,11,5), out_root=root/"out", enable_asr=False)
                            raw = result.raw_session_dir
                            self.assertEqual((result.output_session_dir/"files").exists(), enabled)
                        else:
                            backend = WeflowApiBackend(cfg)
                            backend._client = client
                            raw = cfg.paths.raw if channel == "daily" else root/"bulk"
                            if channel == "daily":
                                backend.export_chats(date(2026,11,5))
                            else:
                                outcome = backend.export_chats_range(date(2026,11,5), date(2026,11,5), raw_root=raw, state_path=root/"state.json")
                                self.assertFalse(outcome.failures)
                        path = next(p for p in raw.rglob("*.json") if "_media" not in p.parts)
                        payload = json.loads(path.read_bytes())
                        ref = payload["messages"][0]["source"]
                        self.assertEqual(bool(ref), enabled)
                        if enabled:
                            self.assertEqual((path.parent/ref).resolve().read_bytes(), b"synthetic document")
                        self.assertEqual(bool(reads), enabled)

    def test_disable_never_reads_source_and_cli_default_missing_exit_zero(self):
        cfg = config(self.root)
        client = Client([api_file(b"missing")])
        common = ['--config', str(cfg.config_path), '--session', 'synthetic_identity', '--no-asr']
        for flag, expected in (([],0), (['--include-files'],3), (['--no-files'],0)):
            output = self.root/("out-"+str(len(flag))+str(expected))
            with mock.patch.object(export_on_demand, '_make_client', return_value=client), redirect_stdout(io.StringIO()):
                if flag == ['--no-files']:
                    with mock.patch.object(FileResolver, '_build_index', side_effect=AssertionError('source scan')), mock.patch.object(FileResolver, '_hash', side_effect=AssertionError('source read')):
                        rc = export_on_demand.main(common+flag+['--out', str(output)])
                else:
                    rc = export_on_demand.main(common+flag+['--out', str(output)])
            self.assertEqual(rc, expected)

    def test_preservation_does_not_expand_to_other_fields_or_ambiguous_identity(self):
        old, new = chat('../_media/files/aa/'+'a'*32+'.txt'), chat()
        old['messages'][0]['content'] = '[文件：older content]'
        result = preserve_file_sources(new, old)
        self.assertEqual(result['messages'][0]['content'], new['messages'][0]['content'])
        self.assertEqual(result['messages'][0]['source'], old['messages'][0]['source'])
        new['messages'][0]['source'] = 'different source'
        self.assertIs(preserve_file_sources(new, old), new)
        new['messages'][0]['source'] = ''
        old['messages'].append(copy.deepcopy(old['messages'][0]))
        self.assertIs(preserve_file_sources(new, old), new)

    def test_plan_binds_original_and_merged_bytes(self):
        archive, incoming = self.root/'archived/raw', self.root/'incoming'
        ref = '../_media/files/aa/'+'a'*32+'.txt'
        old = put(archive/'synthetic/synthetic_20261105.json', chat(ref))
        source = put(incoming/'synthetic/synthetic_20261105.json', chat())
        plan = plan_raw_ingest(archive, [incoming], policy='daily')
        original = old.read_bytes()
        source.write_bytes(source.read_bytes()+b' ')
        with self.assertRaisesRegex(ValueError, 'incoming changed'):
            apply_raw_ingest_plan(plan)
        self.assertEqual(old.read_bytes(), original)

    def test_merge_into_preserves_source_with_lookup_off(self):
        cfg = config(self.root)
        put(self.root/'source/document', b'synthetic document')
        client = Client([api_file()])
        kwargs = dict(sessions=[client.session], client=client, session_query='synthetic_identity',
                      start=date(2026,11,5), end=date(2026,11,5), out_root=self.root/'out', enable_asr=False)
        with redirect_stdout(io.StringIO()):
            first = export_on_demand.export_on_demand(cfg, **kwargs)
        first_json = next(first.raw_session_dir.glob('*.json'))
        ref = json.loads(first_json.read_bytes())['messages'][0]['source']
        self.assertTrue(ref)
        with mock.patch.object(FileResolver, '_build_index', side_effect=AssertionError('source scan')), redirect_stdout(io.StringIO()):
            second = export_on_demand.export_on_demand(cfg, merge_into=first.output_session_dir,
                                                       files_enabled=False, **kwargs)
        final_json = next(second.raw_session_dir.glob('*.json'))
        self.assertEqual(json.loads(final_json.read_bytes())['messages'][0]['source'], ref)
        self.assertEqual((final_json.parent/ref).resolve().read_bytes(), b'synthetic document')

    def test_file_predicate_legacy_and_other_shapes_but_not_text(self):
        ref = '../_media/files/aa/'+'a'*32+'.txt'
        for kind in ('文件消息', '文件', '其他消息'):
            old, new = chat(ref), chat()
            old['messages'][0]['type'] = new['messages'][0]['type'] = kind
            self.assertEqual(preserve_file_sources(new, old)['messages'][0]['source'], ref)
        old, new = chat(ref), chat()
        old['messages'][0]['type'] = new['messages'][0]['type'] = '文本消息'
        self.assertIs(preserve_file_sources(new, old), new)
