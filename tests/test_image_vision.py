from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch

from scripts.export_on_demand import export_on_demand
from scripts.process_existing_raw import ProcessExistingRawDeps, process_existing_raw
from scripts.run_daily_export import DailyExportDeps, run_daily_export
from wechat_diary_core.archiving import archive
from wechat_diary_core.chat_flow import render_message_content
from wechat_diary_core.config import load_config
from wechat_diary_core.preprocessing.image_vision import (
    CheapApiVisionDescriber,
    MAX_IMAGE_BYTES,
    VisionError,
    annotate_vision_descriptions,
    build_chat_context,
    normalize_description,
)


class FakeDescriber:
    def __init__(self, value: str = "视觉描述") -> None:
        self.value = value
        self.calls: list[tuple[Path, str]] = []

    def describe(self, image_path: Path, context_text: str) -> str:
        self.calls.append((image_path, context_text))
        return self.value


def _image_paths(message: dict, base_dir: Path) -> list[Path]:
    value = str(message.get("content") or "")
    return [base_dir / value] if "media/images/" in value else []


class ImageVisionTests(unittest.TestCase):
    def test_three_entrypoints_emit_byte_identical_descriptions_from_same_raw(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            session_dir = raw / "私聊_入口一致_20260813"
            image = session_dir / "media" / "images" / "a.jpg"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"same-image")
            (session_dir / "私聊_入口一致_20260813.json").write_text(
                json.dumps(
                    {
                        "weflow": {},
                        "session": {
                            "wxid": "wxid_entry_contract",
                            "nickname": "入口一致",
                            "remark": "",
                            "displayName": "入口一致",
                            "type": "私聊",
                            "messageCount": 1,
                        },
                        "messages": [
                            {
                                "localId": 1,
                                "createTime": 1786579200,
                                "formattedTime": "2026-08-13 08:00:00",
                                "type": "图片消息",
                                "content": "media/images/a.jpg",
                                "source": "",
                                "isSend": 0,
                                "senderUsername": "wxid_entry_contract",
                                "senderDisplayName": "入口一致",
                                "platformMessageId": "image-1",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            config_path = root / "config.toml"
            config_path.write_text(
                f'''[paths]\nraw="{raw.as_posix()}"\nprocessed="{(root / 'processed').as_posix()}"\narchived="{(root / 'archived').as_posix()}"\ninsights="{(root / 'insights').as_posix()}"\n[export_backend]\nbackend="manual"\n[preprocessing]\nimage_ocr_enabled=false\n[preprocessing.image_vision]\nenabled=true\n[daily_export]\ntarget_usernames=[]\nself_moments_usernames=[]''',
                encoding="utf-8",
            )
            cfg = load_config(config_path)
            describer = FakeDescriber("三入口同一描述")

            class ManualBackend:
                name = "manual"
                partial_failures: list[str] = []

            class EmptyClient:
                def fetch_contacts(self, *, limit: int) -> list[dict]:
                    return []

                def fetch_group_members(self, username: str) -> list[dict]:
                    return []

                def fetch_messages(self, username: str, **kwargs) -> list[dict]:
                    return []

            with patch(
                "wechat_diary_core.preprocessing.image_vision.CheapApiVisionDescriber",
                return_value=describer,
            ):
                daily = run_daily_export(
                    cfg,
                    day=date(2026, 8, 13),
                    deps=DailyExportDeps(
                        backend=ManualBackend(),
                        archive_existing_processed=lambda *args, **kwargs: None,
                    ),
                )
                daily_text = daily.diary_files[0].read_text(encoding="utf-8")

                rerun = process_existing_raw(
                    cfg,
                    raw_root=raw,
                    day="2026-08-13",
                    deps=ProcessExistingRawDeps(),
                )
                rerun_text = rerun.diary_files[0].read_text(encoding="utf-8")

                def archive_same_raw(_staging_root: Path, **kwargs) -> list[Path]:
                    return archive(raw, **kwargs)

                on_demand = export_on_demand(
                    cfg,
                    sessions=[{"username": "wxid_entry_contract", "displayName": "入口一致"}],
                    client=EmptyClient(),
                    session_query="wxid_entry_contract",
                    start=date(2026, 8, 13),
                    end=date(2026, 8, 13),
                    out_root=root / "on-demand",
                    copy_media=False,
                    enable_asr=False,
                    archive_fn=archive_same_raw,
                )
                on_demand_text = on_demand.diary_files[0].read_text(encoding="utf-8")

            def extract_description(body: str) -> bytes:
                token = body.split("[图片：", 1)[1].split("]", 1)[0]
                description = token.split("｜", 1)[1] if token.startswith("media/images/") else token
                return description.encode("utf-8")

            self.assertIn("[图片：三入口同一描述]", daily_text)
            self.assertIn("[图片：三入口同一描述]", rerun_text)
            self.assertIn("[图片：media/images/a.jpg｜三入口同一描述]", on_demand_text)
            self.assertEqual(
                [extract_description(text) for text in (daily_text, rerun_text, on_demand_text)],
                ["三入口同一描述".encode("utf-8")] * 3,
            )

    def test_defaults_are_private_and_qwen_gets_model_specific_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            default = load_config(root / "missing.toml").preprocessing.image_vision
            self.assertFalse(default.enabled)
            self.assertEqual((default.provider, default.model, default.max_tokens), ("doubao", "pro", 2000))
            path = root / "config.toml"
            path.write_text('[preprocessing.image_vision]\nprovider="jisuan"\nmodel="qwen35"', encoding="utf-8")
            qwen = load_config(path).preprocessing.image_vision
            self.assertEqual(qwen.max_tokens, 8000)
            path.write_text('[preprocessing.image_vision]\nprovider="jisuan"\nmodel="qwen35"\nmax_tokens=3000', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "不得低于 8000"):
                load_config(path)

    def test_context_anonymizes_real_speakers_and_does_not_expand_other_images(self) -> None:
        settings = replace(load_config(Path("missing.toml")).preprocessing.image_vision, enabled=True)
        messages = [
            {"formattedTime": "2026-08-13 10:00:00", "senderUsername": "wxid_peer", "senderDisplayName": "真实昵称", "content": "前文"},
            {"formattedTime": "2026-08-13 10:00:01", "senderUsername": "wxid_self", "senderDisplayName": "本人真名", "type": "图片消息", "content": "media/images/a.jpg"},
            {"formattedTime": "2026-08-13 10:00:02", "senderUsername": "wxid_other", "senderDisplayName": "会话显示名", "type": "图片消息", "content": "media/images/b.jpg"},
            {"formattedTime": "2026-08-14 00:00:01", "senderUsername": "wxid_peer", "content": "次日"},
        ]
        payload = build_chat_context(messages, 1, settings)
        self.assertEqual(payload, "[他人1]：前文\n[他人2]：[图片]")
        for secret in ("真实昵称", "本人真名", "会话显示名", "wxid_"):
            self.assertNotIn(secret, payload)

    def test_context_preserves_message_body_ending_with_colon(self) -> None:
        settings = replace(load_config(Path("missing.toml")).preprocessing.image_vision, enabled=True)
        messages = [
            {
                "formattedTime": "2026-08-13 10:00:00",
                "senderUsername": "wxid_self",
                "type": "图片消息",
                "content": "media/images/a.jpg",
            },
            {
                "formattedTime": "2026-08-13 10:00:01",
                "senderUsername": "wxid_peer",
                "content": "听我说：",
            },
        ]
        self.assertEqual(build_chat_context(messages, 0, settings), "[他人1]：听我说：")

    def test_vision_precedes_ocr_and_preserve_path_keeps_readable_prefix(self) -> None:
        self.assertEqual(
            render_message_content({"type": "图片消息", "image_vision_inline": "描述", "image_ocr_inline": "OCR"}),
            "[图片：描述]",
        )
        rendered = render_message_content(
            {
                "type": "图片消息",
                "image_vision_inline": "描述｜净化",
                "image_ocr_inline": "media/images/a.jpg",
                "image_render_mode": "preserve_paths",
            }
        )
        self.assertEqual(rendered, "[图片：media/images/a.jpg｜描述｜净化]")
        self.assertEqual(rendered.split("｜", 1)[0].removeprefix("[图片："), "media/images/a.jpg")
        self.assertEqual(
            render_message_content(
                {
                    "type": "图片消息",
                    "image_vision_inline": "视觉描述",
                    "image_ocr_inline": "OCR 提到了 media/images/example.jpg",
                    "image_render_mode": "ocr_inline",
                }
            ),
            "[图片：视觉描述]",
        )

    def test_length_response_retries_jisuan_qwen_family_once_then_falls_back(self) -> None:
        for model in ("qwen35", "qwen38"):
            with self.subTest(model=model), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                image = root / "media" / "images" / "a.jpg"
                image.parent.mkdir(parents=True)
                image.write_bytes(b"image")
                settings = replace(
                    load_config(root / "missing.toml").preprocessing.image_vision,
                    enabled=True,
                    provider="jisuan",
                    model=model,
                    max_tokens=8000,
                )
                responses = [
                    {"choices": [{"finish_reason": "length", "message": {"content": "不得写入"}}]},
                    {"choices": [{"finish_reason": "length", "message": {"content": ""}}]},
                ]

                def runner(command, stdin, timeout):
                    return type("Result", (), {"returncode": 0, "stdout": json.dumps(responses.pop(0)).encode(), "stderr": b""})()

                describer = CheapApiVisionDescriber(settings, root / "cache", runner=runner, api_script=root / "cheap_api.py")
                messages = [{"type": "图片消息", "content": "media/images/a.jpg", "image_ocr_inline": "OCR兜底"}]
                annotated = annotate_vision_descriptions(messages, root, settings, root / "cache", _image_paths, describer=describer)
                self.assertNotIn("image_vision_inline", annotated[0])
                self.assertEqual(render_message_content(annotated[0]), "[图片：OCR兜底]")
                self.assertEqual(responses, [])

    def test_cache_uses_image_context_prompt_and_model_and_second_run_has_zero_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "a.jpg"
            image.write_bytes(b"image")
            settings = replace(load_config(root / "missing.toml").preprocessing.image_vision, enabled=True)
            calls = 0

            def runner(command, stdin, timeout):
                nonlocal calls
                calls += 1
                body = {"choices": [{"finish_reason": "stop", "message": {"content": "\n\n 单行\n描述 "}}]}
                return type("Result", (), {"returncode": 0, "stdout": json.dumps(body, ensure_ascii=False).encode(), "stderr": b""})()

            describer = CheapApiVisionDescriber(settings, root / "cache", runner=runner, api_script=root / "cheap_api.py")
            self.assertEqual(describer.describe(image, "[发图人]：上下文"), "单行 描述")
            self.assertEqual(describer.describe(image, "[发图人]：上下文"), "单行 描述")
            self.assertEqual(calls, 1)
            entry = json.loads(next((root / "cache" / "_image-descriptions").rglob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual(entry["prompt_version"], "img-v1")
            self.assertNotEqual(entry["context_sha256"], "")

    def test_concurrent_identical_requests_are_single_flight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "a.jpg"
            image.write_bytes(b"same-image")
            settings = replace(load_config(root / "missing.toml").preprocessing.image_vision, enabled=True)
            calls = 0

            def runner(command, stdin, timeout):
                nonlocal calls
                calls += 1
                body = {"choices": [{"finish_reason": "stop", "message": {"content": "描述"}}]}
                return type("Result", (), {"returncode": 0, "stdout": json.dumps(body).encode(), "stderr": b""})()

            describer = CheapApiVisionDescriber(settings, root / "cache", runner=runner, api_script=root / "cheap_api.py")
            with ThreadPoolExecutor(max_workers=4) as pool:
                values = list(pool.map(lambda _: describer.describe(image, "same-context"), range(4)))
            self.assertEqual(values, ["描述"] * 4)
            self.assertEqual(calls, 1)

    def test_skip_username_means_zero_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "media" / "images" / "a.jpg"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"image")
            settings = replace(load_config(root / "missing.toml").preprocessing.image_vision, enabled=True, skip_usernames=["wxid_skip"])
            fake = FakeDescriber()
            messages = [{"type": "图片消息", "content": "media/images/a.jpg", "senderUsername": "wxid_skip"}]
            from wechat_diary_core.preprocessing.image_vision import session_is_skipped
            self.assertTrue(session_is_skipped({}, messages, settings.skip_usernames))
            self.assertEqual(fake.calls, [])

    def test_over_7_mib_is_single_image_failure_and_normalizer_strips_newlines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "large.jpg"
            with image.open("wb") as handle:
                handle.truncate(MAX_IMAGE_BYTES + 1)
            settings = load_config(root / "missing.toml").preprocessing.image_vision
            describer = CheapApiVisionDescriber(settings, root / "cache", api_script=root / "cheap_api.py")
            with self.assertRaises(VisionError):
                describer.describe(image, "")
        self.assertEqual(normalize_description("\n\n描述\n第二行", 400), "描述 第二行")


if __name__ == "__main__":
    unittest.main()
