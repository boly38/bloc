import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
VOLUME_NAMES = ("opencode-data", "workspace", "telegram-data")


class VolumeBackupTests(unittest.TestCase):
    def make_fake_docker(self, directory, body):
        docker = Path(directory) / "docker"
        docker.write_text(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "  run)\n"
            f"    {body}\n"
            "    ;;\n"
            "esac\n"
            "exit 0\n",
            encoding="utf-8",
        )
        docker.chmod(0o755)
        return docker.parent

    def run_volumes_script(self, docker_path, *args, input_text=None, project_name=None):
        environment = os.environ.copy()
        environment["PATH"] = f"{docker_path}{os.pathsep}{environment['PATH']}"
        if project_name is not None:
            environment["BLOC_PROJECT_NAME"] = project_name
        return subprocess.run(
            ["/bin/sh", str(ROOT / "scripts/volumes.sh"), *args],
            cwd=ROOT,
            env=environment,
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_backup_writes_archives_as_the_invoking_user_without_chmod(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            docker_path = self.make_fake_docker(temporary_directory, "printf archive-data")
            backup_root = Path(temporary_directory) / "backups"

            result = self.run_volumes_script(docker_path, "backup", str(backup_root))

            self.assertEqual(result.returncode, 0, result.stderr)
            backup_dirs = list(backup_root.glob("bloc-*"))
            self.assertEqual(len(backup_dirs), 1)
            self.assertTrue(backup_dirs[0].name.startswith("bloc-v2-"))
            self.assertEqual((backup_dirs[0] / ".bloc-backup-version").read_text(encoding="utf-8"), "2\n")
            for volume_name in VOLUME_NAMES:
                archive = backup_dirs[0] / f"{volume_name}.tar.gz"
                self.assertEqual(archive.read_text(encoding="utf-8"), "archive-data")
                self.assertEqual(archive.stat().st_uid, os.getuid())

    def test_restore_from_parent_selects_latest_complete_backup(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            docker_path = self.make_fake_docker(temporary_directory, ":")
            backup_root = Path(temporary_directory) / "backups"
            backup_root.mkdir()
            older = backup_root / "bloc-v2-20261018T120000Z"
            latest = backup_root / "bloc-v2-20261019T120000Z"
            for backup_dir in (older, latest):
                backup_dir.mkdir()
                (backup_dir / ".bloc-backup-version").write_text("2\n", encoding="utf-8")
                for volume_name in VOLUME_NAMES:
                    (backup_dir / f"{volume_name}.tar.gz").touch()

            result = self.run_volumes_script(
                docker_path,
                "restore",
                str(backup_root),
                input_text="RESTORE\n",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"Using latest complete backup: {latest}", result.stdout)
            self.assertIn(f"Volumes restored from {latest}", result.stdout)

    def test_restore_rejects_v1_backup_without_version_marker(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            docker_path = self.make_fake_docker(temporary_directory, ":")
            backup_dir = Path(temporary_directory) / "bloc-20261019T120000Z"
            backup_dir.mkdir()
            for volume_name in VOLUME_NAMES:
                (backup_dir / f"{volume_name}.tar.gz").touch()

            result = self.run_volumes_script(
                docker_path,
                "restore",
                str(backup_dir),
                input_text="RESTORE\n",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("BLOC v1 backups cannot be restored", result.stderr)

    def test_backup_uses_the_selected_project_name(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            docker_path = self.make_fake_docker(temporary_directory, "printf archive-data")
            backup_root = Path(temporary_directory) / "backups"

            result = self.run_volumes_script(
                docker_path,
                "backup",
                str(backup_root),
                project_name="bloc-v2-qualification",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            backup_dirs = list(backup_root.glob("bloc-v2-qualification-*"))
            self.assertEqual(len(backup_dirs), 1)


if __name__ == "__main__":
    unittest.main()
