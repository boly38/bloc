import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RemoteTUITests(unittest.TestCase):
    def test_remote_tui_uses_opencode_v2_server_option(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            ssh = temporary_path / "ssh"
            capture = temporary_path / "ssh-arguments"
            ssh.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$@\" > \"$SSH_CAPTURE\"\n",
                encoding="utf-8",
            )
            ssh.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{temporary_directory}{os.pathsep}{environment['PATH']}"
            environment["SSH_CAPTURE"] = str(capture)

            result = subprocess.run(
                [
                    "/bin/sh",
                    str(ROOT / "scripts/compose.sh"),
                    "remote-tui",
                    "coolify",
                    "0123456789ab",
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                capture.read_text(encoding="utf-8").splitlines(),
                [
                    "-t",
                    "coolify",
                    "docker",
                    "exec",
                    "-it",
                    "0123456789ab",
                    "opencode",
                    "--server",
                    "http://127.0.0.1:4096",
                ],
            )


if __name__ == "__main__":
    unittest.main()
