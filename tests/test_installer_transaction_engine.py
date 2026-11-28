import hashlib
import json
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from installer.engine.transaction_engine import (
    FileEntry, InstallerEngine, ModulePlan, TransactionError,
)


class InstallerTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "vita-root"
        self.payload = self.base / "payload"
        self.payload.mkdir()

    def make_plan(self, operation, version, files=(), owned=("settings/demo",), data=("ur0:data/RtSettingsExtended/Demo/",)):
        entries = []
        for target, content in files:
            source = self.payload / target.rsplit("/", 1)[-1]
            source.write_bytes(content)
            entries.append(FileEntry(
                path=target,
                source=source.name,
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
            ))
        return ModulePlan(
            module_id="demo-module",
            version=version,
            operation=operation,
            payload_root=self.payload,
            files=tuple(entries),
            owned_paths=tuple(owned),
            data_paths=tuple(data),
        )

    def test_install_writes_verified_files_and_registry(self):
        engine = InstallerEngine(self.root)
        plan = self.make_plan("install", "1.0.0", [
            ("settings/demo/config.ini", b"enabled=1\n"),
        ])
        transaction_id = engine.apply(plan)
        self.assertRegex(transaction_id, r"^[a-f0-9]{32}$")
        self.assertEqual((self.root / "settings/demo/config.ini").read_bytes(), b"enabled=1\n")
        registry = json.loads((self.root / ".rtse/registry.json").read_text())
        self.assertEqual(registry["modules"]["demo-module"]["version"], "1.0.0")
        journal = json.loads((self.root / ".rtse/transactions" / transaction_id / "journal.json").read_text())
        self.assertEqual(journal["status"], "committed")

    def test_update_then_rollback_restores_previous_version(self):
        engine = InstallerEngine(self.root)
        engine.apply(self.make_plan("install", "1.0.0", [
            ("settings/demo/old.ini", b"old-value"),
        ]))
        update_id = engine.apply(self.make_plan("update", "2.0.0", [
            ("settings/demo/new.ini", b"new-value"),
        ]))
        self.assertFalse((self.root / "settings/demo/old.ini").exists())
        self.assertEqual((self.root / "settings/demo/new.ini").read_bytes(), b"new-value")

        engine.rollback(update_id)
        self.assertEqual((self.root / "settings/demo/old.ini").read_bytes(), b"old-value")
        self.assertFalse((self.root / "settings/demo/new.ini").exists())
        registry = json.loads((self.root / ".rtse/registry.json").read_text())
        self.assertEqual(registry["modules"]["demo-module"]["version"], "1.0.0")

    def test_remove_deletes_owned_files_but_never_user_data(self):
        engine = InstallerEngine(self.root)
        engine.apply(self.make_plan("install", "1.0.0", [
            ("settings/demo/config.ini", b"keep-user-settings-separate"),
        ]))
        user_data = self.base / "user-data" / "tai-saver"
        user_data.mkdir(parents=True)
        archive = user_data / "known-good.tar"
        archive.write_bytes(b"valuable backup")

        engine.apply(self.make_plan("remove", "1.0.0", files=()))
        self.assertFalse((self.root / "settings/demo/config.ini").exists())
        self.assertTrue(archive.exists())
        registry = json.loads((self.root / ".rtse/registry.json").read_text())
        self.assertNotIn("demo-module", registry["modules"])

    def test_failed_registry_commit_rolls_back_payload(self):
        engine = InstallerEngine(self.root)
        plan = self.make_plan("install", "1.0.0", [
            ("settings/demo/config.ini", b"must-not-remain"),
        ])
        original = engine._write_json_atomic

        def fail_registry(path, value):
            if path == engine.registry_path:
                raise OSError("injected registry failure")
            return original(path, value)

        with mock.patch.object(engine, "_write_json_atomic", side_effect=fail_registry):
            with self.assertRaises(TransactionError):
                engine.apply(plan)
        self.assertFalse((self.root / "settings/demo/config.ini").exists())
        self.assertFalse((self.root / ".rtse/registry.json").exists())
        journals = list((self.root / ".rtse/transactions").glob("*/journal.json"))
        self.assertEqual(len(journals), 1)
        self.assertEqual(json.loads(journals[0].read_text())["status"], "rolled_back")

    def test_refuses_to_overwrite_unowned_file(self):
        engine = InstallerEngine(self.root)
        target = self.root / "settings/demo/config.ini"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"owned by user")
        plan = self.make_plan("install", "1.0.0", [
            ("settings/demo/config.ini", b"replacement"),
        ])
        with self.assertRaisesRegex(TransactionError, "unowned file"):
            engine.apply(plan)
        self.assertEqual(target.read_bytes(), b"owned by user")

    def test_rejects_payload_hash_mismatch(self):
        engine = InstallerEngine(self.root)
        plan = self.make_plan("install", "1.0.0", [
            ("settings/demo/config.ini", b"payload"),
        ])
        wrong = FileEntry(
            path=plan.files[0].path,
            source=plan.files[0].source,
            sha256="0" * 64,
            size_bytes=plan.files[0].size_bytes,
        )
        with self.assertRaisesRegex(TransactionError, "SHA-256 mismatch"):
            engine.apply(ModulePlan(
                plan.module_id, plan.version, plan.operation, plan.payload_root,
                (wrong,), plan.owned_paths, plan.data_paths,
            ))
        self.assertFalse((self.root / "settings/demo/config.ini").exists())

    def test_rejects_symlinked_destination_parent(self):
        engine = InstallerEngine(self.root)
        outside = self.base / "outside"
        outside.mkdir()
        (self.root / "settings").symlink_to(outside, target_is_directory=True)
        plan = self.make_plan("install", "1.0.0", [
            ("settings/demo/config.ini", b"no escape"),
        ])
        with self.assertRaises(TransactionError):
            engine.apply(plan)
        self.assertEqual(list(outside.iterdir()), [])

    def test_rollback_preflights_every_file_before_restoring_any(self):
        engine = InstallerEngine(self.root)
        transaction_id = engine.apply(self.make_plan("install", "1.0.0", [
            ("settings/demo/a.ini", b"first"),
            ("settings/demo/b.ini", b"second"),
        ]))
        (self.root / "settings/demo/b.ini").write_bytes(b"externally changed")
        with self.assertRaisesRegex(TransactionError, "modified"):
            engine.rollback(transaction_id)
        self.assertEqual((self.root / "settings/demo/a.ini").read_bytes(), b"first")
        self.assertEqual((self.root / "settings/demo/b.ini").read_bytes(), b"externally changed")

    def test_rollback_refuses_a_registry_changed_after_commit(self):
        engine = InstallerEngine(self.root)
        transaction_id = engine.apply(self.make_plan("install", "1.0.0", [
            ("settings/demo/config.ini", b"installed"),
        ]))
        registry = self.root / ".rtse/registry.json"
        registry.write_text('{"schema_version":1,"modules":{}}', encoding="utf-8")
        with self.assertRaisesRegex(TransactionError, "registry was modified"):
            engine.rollback(transaction_id)
        self.assertEqual((self.root / "settings/demo/config.ini").read_bytes(), b"installed")

    def test_rollback_uses_fresh_temp_and_does_not_follow_restore_symlink(self):
        engine = InstallerEngine(self.root)
        transaction_id = engine.apply(self.make_plan("install", "1.0.0", [
            ("settings/demo/config.ini", b"installed"),
        ]))
        target = self.root / "settings/demo/config.ini"
        outside = self.base / "outside.txt"
        outside.write_bytes(b"must remain untouched")
        restore_link = target.with_name(target.name + ".rtse-restore")
        restore_link.symlink_to(outside)
        engine.rollback(transaction_id)
        self.assertFalse(target.exists())
        self.assertEqual(outside.read_bytes(), b"must remain untouched")
        self.assertTrue(restore_link.is_symlink())


    def test_concurrent_install_attempts_are_serialized(self):
        engine = InstallerEngine(self.root)
        plan = self.make_plan("install", "1.0.0", [
            ("settings/demo/config.ini", b"one installation only"),
        ])
        def attempt(_):
            try:
                return ("ok", engine.apply(plan))
            except TransactionError as exc:
                return ("error", str(exc))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertEqual(sum(kind == "ok" for kind, _ in results), 1)
        self.assertEqual(sum(kind == "error" for kind, _ in results), 1)
        self.assertIn("already installed", next(value for kind, value in results if kind == "error"))
        registry = json.loads((self.root / ".rtse/registry.json").read_text())
        self.assertEqual(list(registry["modules"]), ["demo-module"])
        transactions = list((self.root / ".rtse/transactions").glob("*/journal.json"))
        self.assertEqual(len(transactions), 1)


    def test_refuses_symlinked_transaction_lock(self):
        engine = InstallerEngine(self.root)
        outside = self.base / "lock-target"
        outside.write_bytes(b"untouched")
        (self.root / ".rtse/installer.lock").symlink_to(outside)
        plan = self.make_plan("install", "1.0.0", [
            ("settings/demo/config.ini", b"no install"),
        ])
        with self.assertRaises(TransactionError):
            engine.apply(plan)
        self.assertEqual(outside.read_bytes(), b"untouched")
        self.assertFalse((self.root / "settings/demo/config.ini").exists())

    def test_rejects_path_traversal_in_module_plan(self):
        engine = InstallerEngine(self.root)
        plan = self.make_plan("install", "1.0.0", [
            ("settings/demo/../../escape", b"no escape"),
        ])
        with self.assertRaises(TransactionError):
            engine.apply(plan)



    def test_existing_transaction_lock_is_restricted_to_owner_only(self):
        engine = InstallerEngine(self.root)
        lock_path = self.root / ".rtse/installer.lock"
        lock_path.write_bytes(b"")
        lock_path.chmod(0o644)

        with engine._exclusive_lock():
            self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)

    def test_transaction_lock_is_released_when_operation_raises(self):
        engine = InstallerEngine(self.root)
        with self.assertRaisesRegex(RuntimeError, "injected failure"):
            with engine._exclusive_lock():
                raise RuntimeError("injected failure")

        # A second acquisition in the same process must not remain blocked.
        with engine._exclusive_lock():
            self.assertTrue((self.root / ".rtse/installer.lock").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
