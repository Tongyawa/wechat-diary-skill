from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# 只被别的脚本调用、或纯开发用途，不需要出现在 SKILL.md 的路由表里。
# 新增脚本默认视为面向用户：要么写进 SKILL.md，要么显式加进本表并说明理由。
INTERNAL_SCRIPTS = {
    "WorkspaceDiscovery.psm1",  # PowerShell 入口共享的工作区解析模块，不可独立运行
    "run_daily_export.py",  # 由 run_daily_export.ps1 调用
    "process_existing_raw.ps1",  # process_existing_raw.py 的 PowerShell 包装
    "print_config_path.py",  # 供 ps1 侧读 config，禁止在 ps1 手写 TOML 解析
    "print_backup_config.py",  # 同上，以 JSON 输出 [backup] 段给 Invoke-BundleBackup.ps1
    "sensevoice_worker.py",  # 语音转写常驻 worker，由导出链路自动拉起
    "validate_weflow_automation.py",  # legacy GUI 后端校验
}


class SkillLayoutTests(unittest.TestCase):
    def test_skill_is_discoverable_at_repository_root(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("\nname: wechat-diary-skill\n", skill)
        self.assertIn("references/prompt-daily.md", skill)
        self.assertIn("references/prompt-summarize.md", skill)
        self.assertIn("references/processed-format.md", skill)
        self.assertIn("references/entrypoints.md", skill)
        self.assertFalse((ROOT / ".claude" / "skills" / "wechat-diary").exists())

    def test_prompt_contracts_live_in_references(self) -> None:
        daily = (ROOT / "references" / "prompt-daily.md").read_text(encoding="utf-8")
        summarize = (ROOT / "references" / "prompt-summarize.md").read_text(
            encoding="utf-8"
        )

        for prompt_number in range(7):
            self.assertIn(f"Prompt {prompt_number}", daily)
        self.assertIn("# {会话名} 总结", summarize)
        self.assertNotIn("### Summarize Prompt", daily)

    def test_placeholder_contract_is_single_sourced(self) -> None:
        """占位符含义只在 processed-format.md 定义，prompt 不得各自复述。

        2026-08-08：曾有三份互相矛盾的占位说明（两份公开 prompt + 私有 skill），
        全部过时。抽成单一事实源后，用本测试防止复述回潮。
        """
        fmt = ROOT / "references" / "processed-format.md"
        self.assertTrue(fmt.exists(), "processed-format.md 是占位契约的单一事实源，不得删除")

        daily = (ROOT / "references" / "prompt-daily.md").read_text(encoding="utf-8")
        summarize = (ROOT / "references" / "prompt-summarize.md").read_text(
            encoding="utf-8"
        )
        for name, text in (("prompt-daily", daily), ("prompt-summarize", summarize)):
            with self.subTest(prompt=name):
                self.assertIn(
                    "processed-format.md",
                    text,
                    f"{name} 必须引用占位契约，而不是自己复述",
                )

    def test_skill_routes_every_user_facing_script(self) -> None:
        """无雪藏功能：每个面向用户的脚本都要在 SKILL.md 里被点名。

        这是「冷启动 agent 只读 skill 就能发现全部能力」这条验收标准的机器判据。
        渐进式展示允许把*细节*下沉到 references/，但不允许让某个能力在入口处
        完全不可见。
        """
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")

        scripts = {
            path.name
            for path in (ROOT / "scripts").iterdir()
            if path.is_file() and path.suffix in {".py", ".ps1", ".psm1"}
        }
        user_facing = sorted(scripts - INTERNAL_SCRIPTS)
        self.assertTrue(user_facing, "scripts/ 下没扫到任何面向用户的入口，判据失效")

        missing = [name for name in user_facing if name not in skill]
        self.assertEqual(
            [],
            missing,
            "以下入口没有出现在 SKILL.md 的路由表里（要么补进路由表，"
            f"要么在 INTERNAL_SCRIPTS 里说明为何属内部）：{missing}",
        )

    def test_internal_allowlist_has_no_stale_entries(self) -> None:
        """允许表不得指向已不存在的脚本，否则它会悄悄放行同名新脚本。"""
        existing = {path.name for path in (ROOT / "scripts").iterdir() if path.is_file()}
        stale = sorted(INTERNAL_SCRIPTS - existing)
        self.assertEqual([], stale, f"INTERNAL_SCRIPTS 里有已不存在的脚本：{stale}")

    def test_entrypoint_docs_do_not_exempt_scripts(self) -> None:
        """入口参数详表中的脚本必须受 SKILL 路由守卫，而非人工豁免。"""
        entrypoints = (ROOT / "references" / "entrypoints.md").read_text(
            encoding="utf-8"
        )
        documented = set(re.findall(r"`([\w-]+\.(?:py|ps1|psm1))`", entrypoints))
        overlaps = sorted(documented & INTERNAL_SCRIPTS)
        self.assertEqual(
            [],
            overlaps,
            "references/entrypoints.md 已把这些脚本列为可调用入口，却又在 "
            f"INTERNAL_SCRIPTS 中豁免了 SKILL 路由检查：{overlaps}。"
            "请从豁免表移除并补入 SKILL.md，或从入口参数详表移除。",
        )


if __name__ == "__main__":
    unittest.main()
