from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")


def _decode(data: bytes) -> str:
    """Keep both likely PowerShell 5.1 console decodings for assertions."""
    return "\n".join(data.decode(encoding, errors="replace") for encoding in ("utf-8", "gb18030", "cp936"))


def _run_ps(script: Path, *args: str) -> tuple[int, str]:
    completed = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *args],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return completed.returncode, _decode(completed.stdout + b"\n" + completed.stderr)


def _run_ps_command(command: str, env: dict[str, str]) -> tuple[int, str]:
    completed = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-Command", command],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return completed.returncode, _decode(completed.stdout + b"\n" + completed.stderr)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return completed.stdout.decode().strip()


def _make_repo(root: Path) -> Path:
    repo = root / "repo-a"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, cwd=ROOT)
    _git(repo, "config", "user.name", "Bundle Test")
    _git(repo, "config", "user.email", "bundle-test@example.invalid")
    (repo / "payload.txt").write_text("version-1\n", encoding="utf-8")
    _git(repo, "add", "payload.txt")
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Bundle Test", "-c", "user.email=bundle-test@example.invalid", "commit", "-qm", "initial"],
        check=True,
        cwd=ROOT,
    )
    return repo


def _commit_second_version(repo: Path) -> None:
    (repo / "payload.txt").write_text("version-2\n", encoding="utf-8")
    _git(repo, "add", "payload.txt")
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Bundle Test", "-c", "user.email=bundle-test@example.invalid", "commit", "-qm", "second"],
        check=True,
        cwd=ROOT,
    )


def _write_config(path: Path, repo: Path, dest: Path, keep: int = 1) -> None:
    path.write_text(
        "[backup]\n"
        f'bundle_dest = "{dest.as_posix()}"\n'
        f"keep = {keep}\n"
        f'repos = [ {{ name = "demo", path = "{repo.as_posix()}" }} ]\n',
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _locked_file_run(script: Path, *, repo: Path, dest: Path, bundle: Path, config: Path | None = None) -> tuple[int, str]:
    env = os.environ.copy()
    env.update(
        {
            "BUNDLE_TEST_SCRIPT": str(script),
            "BUNDLE_TEST_REPO": str(repo),
            "BUNDLE_TEST_DEST": str(dest),
            "BUNDLE_TEST_BUNDLE": str(bundle),
        }
    )
    if config is not None:
        env["BUNDLE_TEST_CONFIG"] = str(config)
        invoke = '& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $env:BUNDLE_TEST_SCRIPT -Config $env:BUNDLE_TEST_CONFIG -NoPopup'
    else:
        invoke = '& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $env:BUNDLE_TEST_SCRIPT -RepoPath $env:BUNDLE_TEST_REPO -Destination $env:BUNDLE_TEST_DEST -Name demo -Keep 1 -Slot 1'
    command = rf'''
$ErrorActionPreference = "Continue"
$stream = [System.IO.File]::Open($env:BUNDLE_TEST_BUNDLE, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
try {{
  {invoke}
  exit $LASTEXITCODE
}} finally {{
  $stream.Dispose()
}}
'''
    return _run_ps_command(command, env)


def _mutex_name(dest: Path, name: str = "demo") -> str:
    key = (str(dest.resolve()).rstrip("\\/") + "|" + name).lower()
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"Global\\wechat-diary-bundle-{digest}"


def _start_mutex_holder(dest: Path, ready: Path, release: Path) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env.update(
        {
            "BUNDLE_TEST_MUTEX": _mutex_name(dest),
            "BUNDLE_TEST_READY": str(ready),
            "BUNDLE_TEST_RELEASE": str(release),
        }
    )
    command = r'''
$mutex = $null
$held = $false
try {
  try {
    $mutex = New-Object -TypeName System.Threading.Mutex -ArgumentList @($false, $env:BUNDLE_TEST_MUTEX)
  } catch {
    $mutex = New-Object -TypeName System.Threading.Mutex -ArgumentList @($false, ($env:BUNDLE_TEST_MUTEX -replace '^Global\\', 'Local\\'))
  }
  try { $held = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $held = $true }
  if (-not $held) { [System.IO.File]::WriteAllText($env:BUNDLE_TEST_READY, 'FAILED'); exit 3 }
  [System.IO.File]::WriteAllText($env:BUNDLE_TEST_READY, 'READY')
  while (-not (Test-Path -LiteralPath $env:BUNDLE_TEST_RELEASE)) { Start-Sleep -Milliseconds 50 }
} finally {
  if ($held -and $mutex) { try { $mutex.ReleaseMutex() } catch { } }
  if ($mutex) { $mutex.Dispose() }
}
'''
    return subprocess.Popen(
        [str(POWERSHELL), "-NoProfile", "-Command", command],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "需要 Windows 上的 powershell.exe 5.1；当前环境不满足")
class BackupPowerShellRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = _make_repo(self.root)
        self.dest = self.root / "bundles"
        self.dest.mkdir()
        self.config = self.root / "config.toml"
        self.backup_script = ROOT / "scripts" / "Backup-GitRepo.ps1"
        self.invoke_script = ROOT / "scripts" / "Invoke-BundleBackup.ps1"
        self.bundle = self.dest / "demo-slot-1.bundle"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_backup(self) -> tuple[int, str]:
        return _run_ps(
            self.backup_script,
            "-RepoPath",
            str(self.repo),
            "-Destination",
            str(self.dest),
            "-Name",
            "demo",
            "-Keep",
            "1",
            "-Slot",
            "1",
        )

    def test_locked_replace_is_explicit_failure_and_preserves_state(self) -> None:
        _write_config(self.config, self.repo, self.dest)
        code, output = _run_ps(self.invoke_script, "-Config", str(self.config), "-NoPopup")
        self.assertEqual(0, code, output)
        old_sha = _sha256(self.bundle)
        state_path = self.dest / "last-run.json"
        old_state = json.loads(state_path.read_text(encoding="utf-8-sig"))
        old_slot_time = old_state["slotIndex"]["demo"]["1"]

        _commit_second_version(self.repo)
        code, output = _locked_file_run(self.backup_script, repo=self.repo, dest=self.dest, bundle=self.bundle)
        self.assertNotEqual(0, code, output)
        self.assertEqual(old_sha, _sha256(self.bundle))

        code, output = _locked_file_run(
            self.invoke_script,
            repo=self.repo,
            dest=self.dest,
            bundle=self.bundle,
            config=self.config,
        )
        self.assertNotEqual(0, code, output)
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
        self.assertEqual("failed", state["overall"])
        self.assertEqual(old_slot_time, state["slotIndex"]["demo"]["1"])
        self.assertEqual(old_sha, _sha256(self.bundle))
        self.assertTrue((self.dest / "last-run-failure.txt").is_file())

    def test_pending_names_are_bounded_across_success_and_failure(self) -> None:
        def assert_pending_bound() -> None:
            pending = sorted(path.name for path in self.dest.glob("*.pending"))
            self.assertLessEqual(len(pending), 1)
            self.assertNotIn("PID", " ".join(pending).upper())

        for _ in range(3):
            code, output = self._run_backup()
            self.assertEqual(0, code, output)
            assert_pending_bound()

        head = _git(self.repo, "rev-parse", "HEAD")
        object_path = self.repo / ".git" / "objects" / head[:2] / head[2:]
        object_bytes = object_path.read_bytes()
        os.chmod(object_path, stat.S_IWRITE | stat.S_IREAD)
        object_path.unlink()
        try:
            for _ in range(3):
                code, output = self._run_backup()
                self.assertNotEqual(0, code, output)
                assert_pending_bound()
        finally:
            object_path.write_bytes(object_bytes)

        assert_pending_bound()

    def test_second_writer_fails_with_actionable_concurrency_message(self) -> None:
        code, output = self._run_backup()
        self.assertEqual(0, code, output)
        old_sha = _sha256(self.bundle)
        ready = self.root / "mutex-ready.txt"
        release = self.root / "mutex-release.txt"
        holder = _start_mutex_holder(self.dest, ready, release)
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(ready.exists(), "mutex holder did not signal readiness")
            self.assertEqual("READY", ready.read_text(encoding="utf-8"))
            code, output = self._run_backup()
            self.assertNotEqual(0, code, output)
            self.assertIn("另一次备份正在写同一目标", output)
            self.assertEqual(old_sha, _sha256(self.bundle))
        finally:
            release.write_text("release", encoding="ascii")
            try:
                holder.wait(timeout=10)
            except subprocess.TimeoutExpired:
                holder.kill()
                holder.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
