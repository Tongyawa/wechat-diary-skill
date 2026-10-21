"""Conservative sink inventory: new file writers require an explicit review.

This is not a whole-program proof. Dynamic calls, external executables and
arbitrary caller-selected roots remain outside this static inventory's reach.
"""
import ast
from collections import Counter
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SINKS = {
    "write_text", "write_bytes", "fdopen", "write", "dump", "copy", "copy2", "copyfile",
    "copytree", "move", "replace", "rename", "unlink", "rmdir", "rmtree", "mkdir",
    "mkstemp", "mkdtemp", "_atomic_bytes", "atomic_replace_bytes", "_replace_file",
    "merge_tree", "merge_raw_exports_into_archive", "apply_raw_ingest_plan",
    "publish_range", "_unlink", "merge_processed_into_archive",
}


def inventory(source):
    tree = ast.parse(source)
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            aliases.update({a.asname or a.name: a.name for a in node.names})
    found = Counter()
    class Scan(ast.NodeVisitor):
        def __init__(self):
            self.stack = []
        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()
        visit_AsyncFunctionDef = visit_FunctionDef
        def visit_Call(self, node):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else (
                aliases.get(node.func.id, node.func.id) if isinstance(node.func, ast.Name) else "")
            is_open_write = name == "open" and any(
                isinstance(value, ast.Constant) and isinstance(value.value, str)
                and any(flag in value.value for flag in "wax+")
                for value in [*(node.args[1:] if isinstance(node.func, ast.Name) else node.args),
                              *(k.value for k in node.keywords if k.arg == "mode")])
            if name in SINKS or is_open_write:
                found[(".".join(self.stack) or "<module>") + ":" + name] += 1
            self.generic_visit(node)
    Scan().visit(tree)
    return dict(sorted(found.items()))


class ArchiveWriteContractTests(unittest.TestCase):
    def test_runtime_sink_inventory_has_no_unreviewed_writer(self):
        expected = json.loads((ROOT / "tests/fixtures/archive_write_sinks.json").read_text(encoding="utf-8"))
        actual = {}
        for directory in (ROOT / "scripts", ROOT / "wechat_diary_core"):
            for path in sorted(directory.rglob("*.py")):
                sinks = inventory(path.read_text(encoding="utf-8-sig"))
                if sinks:
                    actual[path.relative_to(ROOT).as_posix()] = sinks
        self.assertEqual(actual, expected, "new or changed write sink: review its archive reachability")

    def test_known_bad_writer_shapes_and_import_aliases_are_detected(self):
        clean = inventory("def helper():\n    return 1\n")
        for source in [
            "import shutil\ndef unsafe(a,b):\n    shutil.copy2(a,b)\n",
            "def unsafe(path):\n    with path.open('wb') as f:\n        f.write(b'x')\n",
            "from shutil import copyfile as hidden\ndef unsafe(a,b):\n    hidden(a,b)\n",
            "from ranges import publish_range as hidden\ndef unsafe(a,b):\n    hidden(a,b)\n",
            "from workspace import merge_processed_into_archive as hidden\ndef unsafe(a,b):\n    hidden(a,b)\n",
        ]:
            with self.subTest(source=source):
                self.assertNotEqual(inventory(source), clean)

    def test_range_publisher_is_only_called_by_live_export_not_archive_ingestion(self):
        callers = {}
        for directory in (ROOT / 'scripts', ROOT / 'wechat_diary_core'):
            for path in directory.rglob('*.py'):
                sites = {name: count for name, count in inventory(path.read_text(encoding='utf-8-sig')).items()
                         if name.endswith(':publish_range')}
                if sites:
                    callers[path.relative_to(ROOT).as_posix()] = sites
        self.assertEqual(callers, {'wechat_diary_core/backends/weflow_api/mapper.py':
                                 {'write_moments_export:publish_range': 1}})


if __name__ == "__main__":
    unittest.main()
