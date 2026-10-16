from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from wechat_diary_core.config import DEFAULT_CONFIG


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")
PYTHON_ENTRYPOINTS = {
    "process_existing_raw.py",
    "export_on_demand.py",
    "export_bulk_raw.py",
    "backfill_group_names.py",
    "repair_group_names.py",
    "refill_appmsg_content.py",
    "doctor.py",
    "archive_exports.py",
    "init_worktree_config.py",
}
PYTHON_SUBCOMMANDS = {
    "repair_group_names.py": ("prepare", "recompute", "rescan"),
}
POWERSHELL_ENTRYPOINTS = {
    "run_daily_export.ps1",
    "Open-LatestInsights.ps1",
    "Open-InsightsByDate.ps1",
}
SCRIPT_RE = re.compile(r"`([\w-]+\.(?:py|ps1))`")
SECTION_RE = re.compile(r"^## \d+\. .+$", re.MULTILINE)
PARAMETER_HEADER_RE = re.compile(r"`([\w-]+\.ps1)`.*?参数.*?：")
OPTION_RE = re.compile(r"`(-{1,2}[A-Za-z][\w-]*)")
MAX_REPORTED_MISMATCHES = 12


def _table_options(markdown: str) -> set[str]:
    return {
        option
        for line in markdown.splitlines()
        if line.lstrip().startswith("|")
        for option in OPTION_RE.findall(line)
    }


def documented_options() -> dict[str, set[str]]:
    """Parse the parameter tables, rather than maintain a second option list."""
    text = (ROOT / "references" / "entrypoints.md").read_text(encoding="utf-8")
    sections = list(SECTION_RE.finditer(text))
    documented: dict[str, set[str]] = {}
    for index, heading in enumerate(sections):
        body = text[heading.end() : sections[index + 1].start() if index + 1 < len(sections) else None]
        names = SCRIPT_RE.findall(heading.group())
        if len(names) == 1:
            documented[names[0]] = _table_options(body)
            continue

        # Section 6 intentionally documents two PowerShell entry points with
        # different parameter tables. Split at their own “参数” labels instead
        # of incorrectly applying the union to both scripts.
        parameter_headers = list(PARAMETER_HEADER_RE.finditer(body))
        for position, parameter_heading in enumerate(parameter_headers):
            end = (
                parameter_headers[position + 1].start()
                if position + 1 < len(parameter_headers)
                else len(body)
            )
            documented[parameter_heading.group(1)] = _table_options(
                body[parameter_heading.end() : end]
            )
    return documented


def python_cli_options(script: str) -> set[str]:
    commands = PYTHON_SUBCOMMANDS.get(script, (None,))
    options: set[str] = set()
    for command in commands:
        argv = [sys.executable, str(ROOT / "scripts" / script)]
        if command is not None:
            argv.append(command)
        argv.append("--help")
        completed = subprocess.run(
            argv,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
        output = completed.stdout + completed.stderr
        if completed.returncode != 0:
            label = f" {command}" if command else ""
            raise AssertionError(
                f"{script}{label} --help failed (exit {completed.returncode}):\n{output}"
            )
        options.update(re.findall(r"(?m)^\s*(--[A-Za-z][\w-]*)", output))
    return options


def powershell_declared_options(script: str) -> set[str]:
    if not POWERSHELL:
        raise unittest.SkipTest("需要 Windows PowerShell 5.1 读取 ps1 ParamBlock")
    command = r"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $env:WECHAT_DIARY_ENTRYPOINT_SCRIPT,
  [ref]$tokens,
  [ref]$errors
)
if ($errors.Count) { $errors | ForEach-Object { $_.Message }; exit 2 }
$ast.ParamBlock.Parameters | ForEach-Object { $_.Name.VariablePath.UserPath }
"""
    environment = os.environ.copy()
    environment["WECHAT_DIARY_ENTRYPOINT_SCRIPT"] = str(ROOT / "scripts" / script)
    completed = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )
    output = completed.stdout + completed.stderr
    if completed.returncode != 0:
        raise AssertionError(f"{script} PowerShell AST parse failed (exit {completed.returncode}):\n{output}")
    return {f"-{line.strip()}" for line in completed.stdout.splitlines() if line.strip()}


class EntrypointParameterContractTests(unittest.TestCase):
    def test_self_moments_mode_and_timeout_contract_are_documented(self) -> None:
        text = (ROOT / "references" / "entrypoints.md").read_text(encoding="utf-8")
        self.assertIn("--self-moments", documented_options()["export_bulk_raw.py"])
        self.assertIn("self_moments_usernames", text)
        self.assertIn("moments_request_timeout_sec", text)
        self.assertIn("未解密/缺失媒体的动态数", text)
        self.assertEqual(DEFAULT_CONFIG["export_backend"]["weflow_api"]["moments_request_timeout_sec"], 3600)

    def test_bulk_export_rolling_config_and_print_range_are_documented(self) -> None:
        text = (ROOT / "references" / "entrypoints.md").read_text(encoding="utf-8")
        configured = DEFAULT_CONFIG["bulk_export"]

        self.assertEqual(configured, {"start": "2010-01-01", "end": ""})
        self.assertIn("[bulk_export]", text)
        self.assertIn("空 `end` 会滚动补到昨天", text)
        self.assertIn("想让区间冻结不再前移", text)
        self.assertIn("`--print-range`", text)

    def test_moments_range_replacement_contract_matches_shared_implementation(self) -> None:
        from wechat_diary_core.moments_ranges import REVIEW_EXIT_CODE, range_digest
        from wechat_diary_core.backends.weflow_api.mapper import moments_filename
        from datetime import date
        text = (ROOT / "references" / "entrypoints.md").read_text(encoding="utf-8")
        name = moments_filename(["self_account"], date(2022, 12, 31), end=date(2026, 1, 1))
        self.assertIsNotNone(range_digest(Path(name)))
        self.assertIsNotNone(range_digest(Path(name.replace("20221231-20260101", "空"))))
        for term in ("实际最早、最晚本地日期", "区间_空_<digest>", "_保留_<32位唯一标识>",
                     "wechat_diary_core/moments_ranges.py", "逐字节一致", "Live Photo", "发布失败不删除旧文件"):
            self.assertIn(term, text)
        self.assertEqual(REVIEW_EXIT_CODE, 3)
        self.assertIn(f"`{REVIEW_EXIT_CODE}` = 新文件已发布", text)

    def test_documented_parameters_match_live_entrypoints(self) -> None:
        documented = documented_options()
        expected_scripts = PYTHON_ENTRYPOINTS | POWERSHELL_ENTRYPOINTS
        self.assertEqual(
            expected_scripts,
            set(documented),
            "references/entrypoints.md 的入口标题与参数契约测试覆盖范围不一致："
            f"文档独有={sorted(set(documented) - expected_scripts)}；"
            f"测试未覆盖={sorted(expected_scripts - set(documented))}",
        )

        mismatches: list[str] = []
        for script in sorted(expected_scripts):
            actual = (
                python_cli_options(script)
                if script in PYTHON_ENTRYPOINTS
                else powershell_declared_options(script)
            )
            documented_for_script = documented[script]
            if actual != documented_for_script:
                mismatches.append(
                    f"{script}: 文档={sorted(documented_for_script)}；"
                    f"代码={sorted(actual)}；"
                    f"文档多写={sorted(documented_for_script - actual)}；"
                    f"文档漏写={sorted(actual - documented_for_script)}"
                )

        # 最坏只展示前 12 个入口，每项只输出两个集合与两组差集；不会把
        # 同一错误按参数/调用点重复展开成不可读的大段日志。
        shown = mismatches[:MAX_REPORTED_MISMATCHES]
        suffix = (
            f"\n另有 {len(mismatches) - len(shown)} 个入口未展示。"
            if len(mismatches) > len(shown)
            else ""
        )
        self.assertFalse(
            mismatches,
            "入口参数契约不一致（逐项补齐文档或代码；doctor.py 仅报告、不在本分支修）：\n"
            + "\n".join(shown)
            + suffix,
        )


if __name__ == "__main__":
    unittest.main()
