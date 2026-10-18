from __future__ import annotations
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image
from wechat_diary_core.config import load_config
from wechat_diary_core.preprocessing.image_vision import build_chat_context, build_moment_context
from wechat_diary_core.preprocessing.vision_prompts import SYSTEM_PROMPT, LABEL_PROMPT, LABEL_SCHEMA
from wechat_diary_core.preprocessing.vision_records import (
    VisionError, parse_records, finish_record, render_record, shift_suspect, context_bleed,
)
from wechat_diary_core.preprocessing.vision_units import image_item, group_chat, make_packs, pack_text, ImageGroup


def record(slot=1, **values):
    return dict(dict(slot=slot, orientation="portrait", kind="照片", gist="一张合影", text=[], people=[],
                     detail="", context_link="无语境", privacy=[], unread=[]), **values)


def response(count=1, **values):
    return json.dumps(dict(group_summary="", images=[record(i + 1, **values) for i in range(count)]), ensure_ascii=False)


class ContractTests(unittest.TestCase):
    def test_versioned_prompt_and_label_hashes_have_mutation_controls(self):
        for value, expected in ((SYSTEM_PROMPT, "fe8eadbfaa77"), (LABEL_PROMPT, "00400b363161")):
            self.assertEqual(hashlib.sha256(value.encode()).hexdigest()[:12], expected)
            self.assertNotEqual(hashlib.sha256((value + "错").encode()).hexdigest()[:12], expected)
        self.assertEqual(hashlib.sha256(json.dumps(LABEL_SCHEMA, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12], "78699661e5d8")

    def test_json_schema_and_N_slot_accept_reordered_results_and_reject_bad_shapes(self):
        good = response(3)
        for text in (good, "```json\n" + good + "\n```", "\n\n" + good):
            self.assertEqual(len(parse_records(text, 3)), 3)
        body = json.loads(good)
        body["images"].reverse()
        self.assertEqual([r["slot"] for r in parse_records(json.dumps(body), 3)], [1, 2, 3])
        bad = ["{'images':[]}", response(2), response(4)]
        for mutation in (lambda d: d.pop("group_summary"), lambda d: d.update(extra=0),
                         lambda d: d["images"][0].pop("text"),
                         lambda d: d["images"][0].update(extra="x"),
                         lambda d: d["images"][0].update(slot=True),
                         lambda d: d["images"][0].update(slot=2),
                         lambda d: d["images"][2].update(slot=4),
                         lambda d: d["images"][0].update(orientation="横"),
                         lambda d: d["images"][0].update(privacy=[dict(type="x", severity="urgent", evidence="x")])):
            d = json.loads(good)
            mutation(d)
            bad.append(json.dumps(d))
        for text in bad:
            with self.subTest(text=text[:80]), self.assertRaises(VisionError):
                parse_records(text, 3)
        with self.assertRaises(VisionError):
            parse_records(response(2), 1)

    def test_gist_rewrite_is_text_only_one_attempt_with_positive_and_negative_phrases(self):
        for gist in ("像是在展示自己准备的物品", "正对应发图人的吐槽"):
            calls = []
            result, checks = finish_record(record(gist=gist), (1000, 1000), 1000,
                lambda a, b: calls.append((a, b)) or "照片展示一组周边")
            self.assertEqual(result["gist"], "照片展示一组周边")
            self.assertTrue(checks["gist_rewritten"])
            self.assertEqual(len(calls), 1)
            _, checks = finish_record(record(gist=gist), (1000, 1000), 1000, lambda *args: gist)
            self.assertTrue(checks["gist_motive"])
        for gist in ("照片展示一组周边", "旁边写着不赖，表达认可"):
            _, checks = finish_record(record(gist=gist), (1000, 1000), 1000,
                lambda *args: self.fail("neutral gist must not be rewritten"))
            self.assertFalse(checks["gist_rewritten"])

    def test_uncertainty_never_deletes_text_and_ignores_unread_placeholders(self):
        for lines, flag in ((["文字?"] * 6 + ["清楚"] * 4, True), (["999分?"], False),
                            (["[小字?]"] * 8 + ["清楚"] * 2, False)):
            result, checks = finish_record(record(text=lines), (1000, 1000), 1000)
            self.assertEqual(checks["uncertain_heavy"], flag)
            self.assertEqual(result["text"], lines)

    def test_budget_removes_whole_detail_sentences_preserves_facts(self):
        original = record(detail="景色非常好。" * 80, text=["事实文字"], people=["红衣人物"])
        result, checks = finish_record(original, (100, 100), 1000)
        self.assertLess(len(result["detail"]), len(original["detail"]))
        self.assertTrue(not result["detail"] or result["detail"].endswith("。"))
        self.assertEqual(result["text"], original["text"])
        self.assertEqual(result["people"], original["people"])
        self.assertEqual(original["detail"], "景色非常好。" * 80)
        result, checks = finish_record(record(detail="景色。", text=["字" * 300]), (30, 30), 1000)
        self.assertEqual(result["detail"], "")
        self.assertTrue(checks["over_budget"])
        self.assertEqual(result["text"], ["字" * 300])
        _, checks = finish_record(record(detail="蓝天。"), (2000, 2000), 1000)
        self.assertEqual(checks["detail_cut_chars"], 0)

    def test_render_snapshot_no_audit_fields_and_field_separator_sanitized(self):
        r = record(kind="照片", gist="天空｜白云\n蓝色", text=["A\nB", "C"], people=["红衣", "蓝衣"],
                   detail="", context_link="不应出现", privacy=[dict(type="private", severity="high", evidence="secret")])
        self.assertEqual(render_record(r), "照片｜天空|白云 蓝色｜文字：A B / C｜人物：红衣；蓝衣")
        self.assertEqual(render_record(record(gist="空画面", kind="其他")), "其他｜空画面")

    def test_shift_has_independent_negative_controls_and_missing_labels(self):
        kinds = ["照片", "聊天截图", "文档图", "插画二次元", "视频截帧", "应用网页截图", "表情包梗图", "拼图对照图"]
        labels = [dict(kind=k) for k in kinds]
        self.assertTrue(shift_suspect([record(kind=k) for k in kinds[-1:] + kinds[:-1]], labels))
        one = [record(kind=k) for k in kinds]
        one[0]["kind"] = "聊天截图"
        self.assertFalse(shift_suspect(one, labels))
        one[0]["kind"], one[1]["kind"] = "文档图", "视频截帧"
        self.assertFalse(shift_suspect(one, labels))
        self.assertFalse(shift_suspect(one, [None] * 8))

    def test_bleed_harness_fragments_ignore_tags_and_same_group_terms(self):
        r = record(gist="豆腐脑铺子", context_link="ALPHA99")
        self.assertIn("豆腐脑铺子", context_bleed(r, 0, ["10:00 [他人1] 本组标题", "豆腐脑铺子 ALPHA99"]))
        self.assertIn("alpha99", context_bleed(r, 0, ["本组标题", "豆腐脑铺子 ALPHA99"]))
        self.assertEqual(context_bleed(r, 0, ["豆腐脑铺子 ALPHA99", "豆腐脑铺子 ALPHA99"]), [])
        self.assertEqual(context_bleed(record(gist="他人1"), 0, ["10:00 [他人1] A", "10:01 [他人2] B"]), [])


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.settings = load_config(Path("missing.toml")).preprocessing.image_vision

    def test_context_role_tags_system_redaction_and_body_names_preserved(self):
        messages = [dict(formattedTime="2026-10-18 10:00:00", senderUsername="alice", senderDisplayName="Alice",
                         type="系统消息", content='"Alice" 撤回了一条消息'),
                    dict(formattedTime="2026-10-18 10:01:00", senderUsername="owner", senderDisplayName="Owner",
                         type="图片消息", content="media/images/a.jpg"),
                    dict(formattedTime="2026-10-18 10:02:00", senderUsername="bob", senderDisplayName="Bob",
                         type="其他消息", content='"Bob" 拍了拍 "Carol"的肩膀｜原样'),
                    dict(formattedTime="2026-10-18 10:03:00", senderUsername="alice", type="系统消息", content="撤回了未知变体")]
        context = build_chat_context(messages, 1, self.settings)
        self.assertIn("10:00 [他人1] [他人1] 撤回了一条消息", context)
        self.assertIn("[他人2] 拍了拍 [他人3]的肩膀｜原样", context)
        self.assertIn("[系统提示]", context)
        for value in ("Alice", "Bob", "Carol", "Owner", "media/"):
            self.assertNotIn(value, context)
        messages[0].update(type="文本消息", content="Alice 说听我说：｜原文", quotedContent="引用｜原文")
        self.assertIn("Alice 说听我说：｜原文", build_chat_context(messages, 1, self.settings))
        self.assertIn("（引用：引用｜原文）", build_chat_context(messages, 1, self.settings))

    def test_group_context_hash_independent_of_pack_slot_and_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            messages, items = [], []
            for i in range(3):
                path = root / f"{i}.png"
                Image.new("RGB", (30 + i, 50), (i * 30, 0, 0)).save(path)
                m = dict(formattedTime=f"2026-10-18 10:0{i}:00", senderUsername="alice" if i < 2 else "bob", type="图片消息", content=str(path))
                messages.append(m)
                items.append(image_item(path, m, i, 0, m))
            groups = group_chat(items, messages, self.settings)
            self.assertEqual([len(g.items) for g in groups], [2, 1])
            original = groups[0].context_sha256
            self.assertIn("【语境·组2】", pack_text([groups[1], groups[0]]))
            self.assertEqual(groups[0].context_sha256, original)
            self.assertIn("组2 的本组图1…2 = 图2…图3", pack_text([groups[1], groups[0]]))
            self.assertNotEqual(hashlib.sha256(groups[0].context(2).encode()).hexdigest(), original)
            self.assertEqual([len(p) for p in make_packs(groups, 1)], [1, 1])
            self.assertEqual(len(group_chat(items, messages, replace(self.settings, force_single=True))), 3)
            items[1].day = "2026-10-19"
            self.assertEqual(len(group_chat(items, messages, self.settings)), 3)

    def test_zero_context_moments_full_body_comments_default_off(self):
        settings = replace(self.settings, context_messages=0)
        a = [dict(senderUsername="a", formattedTime="2026-10-18 10:00:00", content="secret")]
        b = [dict(senderUsername="b", formattedTime="2026-10-19 10:00:00", content="different")]
        self.assertEqual(build_chat_context(a, 0, settings), "【语境·组1】无语境。")
        self.assertEqual(build_chat_context(a, 0, settings), build_chat_context(b, 0, settings))
        post = dict(contentDesc="全文\n第二行", comments=[dict(nickname="Carol", content="评论")])
        self.assertEqual(build_moment_context(post, settings), "【语境·组1】本条动态正文（[发图人] 发布）：\n全文\n第二行")
        self.assertIn("[他人1]：评论", build_moment_context(post, replace(settings, include_moment_comments=True)))
