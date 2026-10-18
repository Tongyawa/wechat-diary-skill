"""Real local process timeout test: synthetic Python children, no model CLI."""
from pathlib import Path
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from wechat_diary_core.preprocessing.vision_transport import run_subprocess


class ProcessTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows process-tree contract")
    def test_timeout_reaps_parent_and_child_and_success_is_a_control(self):
        result = run_subprocess([sys.executable, "-c", "print('ok')"], "", 10)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), b"ok")
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "pids.json"
            code = ("import subprocess,sys,os,json,time; from pathlib import Path; "
                    "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                    f"Path({str(pid_file)!r}).write_text(json.dumps([os.getpid(),p.pid])); time.sleep(60)")
            with self.assertRaises(subprocess.TimeoutExpired):
                started = time.monotonic()
                run_subprocess([sys.executable, "-c", code], "", 2)
            self.assertLess(time.monotonic() - started, 10, "timeout must not wait for natural child exit")
            self.assertTrue(pid_file.exists(), "synthetic process must have started before the timeout")
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            for pid in json.loads(pid_file.read_text()):
                handle = kernel.OpenProcess(0x1000, False, pid)
                if handle:
                    try:
                        code = ctypes.c_ulong()
                        self.assertTrue(kernel.GetExitCodeProcess(handle, ctypes.byref(code)))
                        self.assertNotEqual(code.value, 259, "synthetic child left running")
                    finally:
                        kernel.CloseHandle(handle)
