# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Prepare trusted gateway ancestors for the pinned NemoClaw runtime."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra/setup"))
import setup_vm


class UserDirectoryPreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ignite-user-dirs-", dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "labuser"
        self.home.mkdir(mode=0o750)
        self.account = SimpleNamespace(pw_dir=str(self.home), pw_uid=os.getuid(), pw_gid=os.getgid())

    def test_missing_gateway_ancestors_are_private_and_user_owned(self):
        setup_vm.prepare_learner_directories(self.account)
        for path in (self.home / ".local", self.home / ".local/state", self.home / ".local/state/nemoclaw"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.stat().st_uid, os.getuid())
        self.assertEqual(self.home.stat().st_mode & 0o777, 0o750)

    def test_group_writable_local_is_fixed_without_changing_gateway_contents(self):
        local = self.home / ".local"
        gateway = local / "state/nemoclaw/openshell-docker-gateway"
        gateway.mkdir(parents=True, mode=0o700)
        local.chmod(0o775)
        state = gateway / "openshell.db"
        state.write_bytes(b"existing gateway state must survive")
        state.chmod(0o600)
        setup_vm.prepare_learner_directories(self.account)
        setup_vm.prepare_learner_directories(self.account)
        self.assertEqual(local.stat().st_mode & 0o777, 0o755)
        self.assertEqual(state.read_bytes(), b"existing gateway state must survive")
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)
        self.assertEqual(gateway.stat().st_mode & 0o777, 0o700)

    def test_symlink_ancestors_are_rejected_without_modifying_the_target(self):
        for relative in (".local", ".local/state", ".local/state/nemoclaw"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory(dir=self.root) as folder:
                home = Path(folder)
                target = home / "unrelated"
                target.mkdir()
                target.chmod(0o775)
                link = home / relative
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(target, target_is_directory=True)
                account = SimpleNamespace(pw_dir=str(home), pw_uid=os.getuid(), pw_gid=os.getgid())
                with self.assertRaisesRegex(RuntimeError, "without symbolic links"):
                    setup_vm.prepare_learner_directories(account)
                self.assertEqual(target.stat().st_mode & 0o777, 0o775)
                self.assertTrue(link.is_symlink())

    def test_symlink_home_and_file_ancestor_fail_without_removal(self):
        alias = self.root / "home-alias"
        alias.symlink_to(self.home, target_is_directory=True)
        account = SimpleNamespace(pw_dir=str(alias), pw_uid=os.getuid(), pw_gid=os.getgid())
        with self.assertRaises(RuntimeError):
            setup_vm.prepare_learner_directories(account)
        local = self.home / ".local"
        local.write_bytes(b"must not replace a file")
        with self.assertRaises(RuntimeError):
            setup_vm.prepare_learner_directories(self.account)
        self.assertEqual(local.read_bytes(), b"must not replace a file")

    def test_unrelated_owner_is_rejected_without_chown_or_chmod_on_that_directory(self):
        local = self.home / ".local"
        local.mkdir()
        local.chmod(0o775)
        real_fstat = os.fstat
        calls = 0
        def fstat(fd):
            nonlocal calls
            calls += 1
            value = real_fstat(fd)
            if calls == 2:
                return SimpleNamespace(st_uid=os.getuid() + 10000, st_gid=value.st_gid, st_mode=value.st_mode)
            return value
        with patch("setup_vm.os.fstat", side_effect=fstat), patch("setup_vm.os.fchown") as chown:
            with self.assertRaisesRegex(RuntimeError, "unrelated user"):
                setup_vm.prepare_learner_directories(self.account)
            chown.assert_not_called()
        self.assertEqual(local.stat().st_mode & 0o777, 0o775)

    def test_root_created_ancestor_is_assigned_to_the_learner(self):
        real_fstat = os.fstat
        calls = 0
        def fstat(fd):
            nonlocal calls
            calls += 1
            value = real_fstat(fd)
            if calls == 2:
                return SimpleNamespace(st_uid=0, st_gid=0, st_mode=value.st_mode)
            return value
        with patch("setup_vm.os.fstat", side_effect=fstat), patch("setup_vm.os.fchown") as chown:
            setup_vm.prepare_learner_directories(self.account)
            if os.getuid() != 0 or os.getgid() != 0:
                chown.assert_called_once()
                self.assertEqual(chown.call_args.args[1:], (os.getuid(), os.getgid()))

    def test_sourced_settings_create_private_files_even_with_an_initial_shared_umask(self):
        config = self.home / "config"
        config.mkdir()
        with patch("setup_vm.os.chown"):
            setup_vm.stage_environment(config, self.account, {"NEMOCLAW_MODEL": "assigned-model"})
        file = self.home / "new-file"
        directory = self.home / "new-directory"
        result = subprocess.run(["bash", "-c", 'umask 0002\nsource "$1"\n: > "$2"\nmkdir "$3"\numask',
            "settings-test", str(config / "environment.sh"), str(file), str(directory)],
            check=True, text=True, capture_output=True)
        self.assertEqual(result.stdout.strip(), "0077")
        self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
