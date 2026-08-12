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


def _run_ps(
    script: Path,
    *args: str,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    completed = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *args],
        cwd=ROOT,
        env=env,
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


def _kill_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Kill PowerShell and any Python child left behind by a timed-out probe."""
    subprocess.run(
        ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if process.poll() is None:
        process.kill()


def _run_ps_with_timeout(
    script: Path,
    *args: str,
    env: dict[str, str] | None = None,
    timeout: float,
) -> tuple[int | None, str, bool]:
    process = subprocess.Popen(
        [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *args],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return process.returncode, _decode(stdout + b"\n" + stderr), False
    except subprocess.TimeoutExpired:
        _kill_process_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=10)
        return process.returncode, _decode(stdout + b"\n" + stderr), True


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


def _batch_mutex_name(dest: Path) -> str:
    key = str(dest.resolve()).rstrip("\\/").lower()
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"Global\\wechat-diary-batch-{digest}"


def _uppercase_path_environment() -> dict[str, str]:
    """Model Agent/CI parents that expose PATH with a different key casing."""
    env = os.environ.copy()
    path_value = next(value for key, value in env.items() if key.lower() == "path")
    for key in [key for key in env if key.lower() == "path"]:
        del env[key]
    env["PATH"] = path_value
    return env


def _start_named_mutex_holder(
    mutex_name: str,
    ready: Path,
    release: Path,
) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env.update(
        {
            "BUNDLE_TEST_MUTEX": mutex_name,
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


def _start_mutex_holder(dest: Path, ready: Path, release: Path) -> subprocess.Popen[bytes]:
    return _start_named_mutex_holder(_mutex_name(dest), ready, release)


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

    def test_invoke_reads_config_with_uppercase_path_environment(self) -> None:
        """PowerShell 5.1 Start-Process crashes on inherited Path/PATH casing.

        Agent and CI hosts commonly expose the key as uppercase ``PATH``. The
        batch entry point must not require callers to rewrite their environment
        before loading the skill.
        """
        _write_config(self.config, self.repo, self.dest)
        uppercase_env = _uppercase_path_environment()
        dual_case_env = uppercase_env.copy()
        dual_case_env["Path"] = uppercase_env["PATH"]
        for label, env in (("uppercase", uppercase_env), ("dual-case", dual_case_env)):
            with self.subTest(environment=label):
                code, output = _run_ps(
                    self.invoke_script,
                    "-Config",
                    str(self.config),
                    "-NoPopup",
                    env=env,
                )
                self.assertEqual(0, code, output)
        self.assertTrue(self.bundle.is_file())
        state = json.loads((self.dest / "last-run.json").read_text(encoding="utf-8-sig"))
        self.assertEqual("ok", state["overall"])

    def test_invoke_drains_large_config_stderr_without_pipe_deadlock(self) -> None:
        """stderr must be drained while stdout is being read, not afterwards."""
        scripts_dir = self.root / "injected scripts"
        scripts_dir.mkdir()
        invoke_copy = scripts_dir / self.invoke_script.name
        shutil.copy2(self.invoke_script, invoke_copy)
        shutil.copy2(self.backup_script, scripts_dir / self.backup_script.name)
        shutil.copy2(ROOT / "scripts" / "WorkspaceDiscovery.psm1", scripts_dir / "WorkspaceDiscovery.psm1")

        stub = scripts_dir / "print_backup_config.py"
        stub.write_text(
            "import json\n"
            "import os\n"
            "import sys\n"
            "sys.stderr.write('E' * 65536)\n"
            "sys.stderr.flush()\n"
            "print(json.dumps({\n"
            "    'enabled': True,\n"
            "    'problems': [],\n"
            "    'bundleDest': os.environ['BUNDLE_TEST_DEST'],\n"
            "    'stateFile': os.path.join(os.environ['BUNDLE_TEST_DEST'], 'last-run.json'),\n"
            "    'keep': 1,\n"
            "    'staleWarnDays': 30,\n"
            "    'repos': [{'name': 'demo', 'path': os.environ['BUNDLE_TEST_REPO']}],\n"
            "}))\n",
            encoding="utf-8",
        )
        _write_config(self.config, self.repo, self.dest)
        env = os.environ.copy()
        env.update({"BUNDLE_TEST_DEST": str(self.dest), "BUNDLE_TEST_REPO": str(self.repo)})

        fixed_code, fixed_output, fixed_timed_out = _run_ps_with_timeout(
            invoke_copy,
            "-Config",
            str(self.config),
            "-NoPopup",
            env=env,
            timeout=15,
        )

        # Prove the fixture detects the regression: run a copy with the old,
        # sequential ReadToEnd order. This must time out, but the probe itself
        # has a subprocess timeout and kills the complete child process tree.
        source = invoke_copy.read_text(encoding="utf-8-sig")
        async_reader = (
            "  # 必须先发起 stderr 的异步读取，再 drain stdout：如果子进程先把 stderr\n"
            "  # 管道写满，它会等待读取而不再关闭 stdout；同步依次 ReadToEnd 会与它互等。\n"
            "  $configErrTask = $proc.StandardError.ReadToEndAsync()\n"
            "  $configJson = $proc.StandardOutput.ReadToEnd()\n"
            "  $configErr = $configErrTask.Result"
        )
        sequential_reader = (
            "  $configJson = $proc.StandardOutput.ReadToEnd()\n"
            "  $configErr = $proc.StandardError.ReadToEnd()"
        )
        self.assertIn(async_reader, source)
        broken_copy = scripts_dir / "Invoke-BundleBackup-sequential.ps1"
        broken_copy.write_text(source.replace(async_reader, sequential_reader, 1), encoding="utf-8-sig")
        broken_code, broken_output, broken_timed_out = _run_ps_with_timeout(
            broken_copy,
            "-Config",
            str(self.config),
            "-NoPopup",
            env=env,
            timeout=8,
        )

        artifact_dir = ROOT / "tests" / "_artifacts" / "2026-08-12-workspace-discovery"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "large-stderr-pipe-report.json").write_text(
            json.dumps(
                {
                    "fixed": {
                        "returncode": fixed_code,
                        "timed_out": fixed_timed_out,
                        "bundle_exists": self.bundle.is_file(),
                    },
                    "sequential_regression": {
                        "returncode": broken_code,
                        "timed_out": broken_timed_out,
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        self.assertFalse(fixed_timed_out, fixed_output)
        self.assertEqual(0, fixed_code, fixed_output)
        self.assertTrue(self.bundle.is_file(), fixed_output)
        self.assertTrue(broken_timed_out, f"old sequential reader unexpectedly completed: {broken_output}")

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

    def test_second_batch_run_skips_without_touching_shared_signals(self) -> None:
        """A reentrant batch call must not race last-run state or its report."""
        _write_config(self.config, self.repo, self.dest)
        code, output = _run_ps(self.invoke_script, "-Config", str(self.config), "-NoPopup")
        self.assertEqual(0, code, output)

        state_path = self.dest / "last-run.json"
        report_path = self.dest / "last-run-failure.txt"
        state_before = state_path.read_bytes()
        report_path.write_text("sentinel-report", encoding="utf-8")

        ready = self.root / "batch-mutex-ready.txt"
        release = self.root / "batch-mutex-release.txt"
        holder = _start_named_mutex_holder(_batch_mutex_name(self.dest), ready, release)
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(ready.exists(), "batch mutex holder did not signal readiness")
            self.assertEqual("READY", ready.read_text(encoding="utf-8"))

            code, output = _run_ps(self.invoke_script, "-Config", str(self.config), "-NoPopup")
            self.assertEqual(0, code, output)
            self.assertIn("已有批量 bundle 冷备正在写同一目标", output)
            self.assertEqual(state_before, state_path.read_bytes())
            self.assertEqual("sentinel-report", report_path.read_text(encoding="utf-8"))
        finally:
            release.write_text("release", encoding="ascii")
            try:
                holder.wait(timeout=10)
            except subprocess.TimeoutExpired:
                holder.kill()
                holder.wait(timeout=10)

    def test_two_real_batch_processes_do_not_race_shared_signals(self) -> None:
        """A real overlapping Invoke pair has one owner and one idempotent skip."""
        _write_config(self.config, self.repo, self.dest)

        # A size-based overlap probe is timing-dependent. Put a git.cmd shim
        # first on PATH for the owner process: once it reaches ``bundle create``
        # (therefore after acquiring the outer mutex), signal readiness and
        # pause. The second process starts only after that observable boundary.
        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        shim_root = self.root / "git-shim"
        shim_root.mkdir()
        ready = self.root / "first-batch-ready.txt"
        shim = shim_root / "git.cmd"
        shim.write_text(
            "@echo off\r\n"
            'if /I "%~3"=="bundle" if /I "%~4"=="create" (\r\n'
            '  > "%BUNDLE_TEST_GIT_READY%" echo READY\r\n'
            '  powershell.exe -NoProfile -Command "Start-Sleep -Seconds 5"\r\n'
            ")\r\n"
            f'"{real_git}" %*\r\n',
            encoding="ascii",
        )
        owner_env = os.environ.copy()
        path_key = next(key for key in owner_env if key.lower() == "path")
        owner_env[path_key] = str(shim_root) + os.pathsep + owner_env[path_key]
        owner_env["BUNDLE_TEST_GIT_READY"] = str(ready)

        command = [
            str(POWERSHELL),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self.invoke_script),
            "-Config",
            str(self.config),
            "-NoPopup",
        ]
        first = subprocess.Popen(
            command,
            cwd=ROOT,
            env=owner_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        first_communicated = False
        try:
            deadline = time.monotonic() + 15
            while not ready.exists() and first.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "first batch did not reach the mutex-owned bundle stage")

            second_code, second_output = _run_ps(
                self.invoke_script,
                "-Config",
                str(self.config),
                "-NoPopup",
            )
            self.assertEqual(0, second_code, second_output)
            self.assertIn("已有批量 bundle 冷备正在写同一目标", second_output)

            first_stdout, first_stderr = first.communicate(timeout=60)
            first_communicated = True
            first_output = _decode(first_stdout + b"\n" + first_stderr)
            self.assertEqual(0, first.returncode, first_output)
        finally:
            if first.poll() is None:
                first.kill()
            if not first_communicated:
                first.communicate(timeout=10)

        state = json.loads((self.dest / "last-run.json").read_text(encoding="utf-8-sig"))
        self.assertEqual("ok", state["overall"])
        self.assertFalse((self.dest / "last-run-failure.txt").exists())
        self.assertTrue(self.bundle.is_file())
        self.assertFalse((self.dest / "demo.pending").exists())


if __name__ == "__main__":
    unittest.main()
