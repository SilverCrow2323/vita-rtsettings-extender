"""Host-reference transaction engine for the expandable Rt:Settings installer.

This module is deliberately filesystem-rooted and host-testable. It does not
perform network I/O or claim to be the Vita runtime backend.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import fcntl
import stat
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import uuid
from typing import Iterable


class TransactionError(RuntimeError):
    """A transaction was rejected or could not be completed safely."""


@dataclass(frozen=True)
class FileEntry:
    path: str
    source: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class ModulePlan:
    module_id: str
    version: str
    operation: str
    payload_root: Path | str = Path(".")
    files: tuple[FileEntry, ...] = ()
    owned_paths: tuple[str, ...] = ()
    data_paths: tuple[str, ...] = ()


_MODULE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")
_TRANSACTION_ID = re.compile(r"^[a-f0-9]{32}$")


def _safe_relative(raw: str) -> PurePosixPath:
    if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw:
        raise TransactionError(f"unsafe relative path: {raw!r}")
    if raw.startswith("/") or ":" in raw:
        raise TransactionError(f"absolute or device path is not allowed here: {raw!r}")
    parts = raw.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise TransactionError(f"path traversal or empty segment: {raw!r}")
    return PurePosixPath(*parts)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_symlink_components(path: Path, stop: Path | None = None) -> None:
    absolute = path.absolute()
    parts = [absolute, *absolute.parents]
    if stop is not None:
        stop_abs = stop.absolute()
        try:
            relative = absolute.relative_to(stop_abs)
        except ValueError as exc:
            raise TransactionError(f"path escaped expected root: {path}") from exc
        current = stop_abs
        if current.exists() and current.is_symlink():
            raise TransactionError(f"symlink root is not allowed: {current}")
        for part in relative.parts:
            current = current / part
            if current.exists() and current.is_symlink():
                raise TransactionError(f"symlink path component is not allowed: {current}")
        return
    for component in reversed(parts):
        if component.exists() and component.is_symlink():
            raise TransactionError(f"symlink path component is not allowed: {component}")


class InstallerEngine:
    """Apply install/update/remove plans with backups and explicit rollback."""

    REGISTRY_REL = PurePosixPath(".rtse/registry.json")

    def __init__(self, target_root: Path | str):
        self.root = Path(target_root).absolute()
        _reject_symlink_components(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise TransactionError("target root must not be a symlink")
        self.state = self.root / ".rtse"
        if self.state.exists() and self.state.is_symlink():
            raise TransactionError("installer state directory must not be a symlink")
        self.transactions = self.state / "transactions"
        self.transactions.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.root / self.REGISTRY_REL.as_posix()

    @contextmanager
    def _exclusive_lock(self):
        """Serialize installer mutations across processes sharing this target root."""
        lock_path = self.state / "installer.lock"
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise TransactionError(f"cannot open installer transaction lock: {exc}") from exc
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise TransactionError("installer transaction lock is not a regular file")
            try:
                os.fchmod(fd, 0o600)
            except OSError as exc:
                raise TransactionError(f"cannot secure installer transaction lock: {exc}") from exc
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError as exc:
                raise TransactionError(f"cannot acquire installer transaction lock: {exc}") from exc
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _target(self, relative: str) -> Path:
        rel = _safe_relative(relative)
        target = self.root.joinpath(*rel.parts)
        _reject_symlink_components(target, self.root)
        return target

    def _load_registry(self) -> dict:
        if not self.registry_path.exists():
            return {"schema_version": 1, "modules": {}}
        if self.registry_path.is_symlink():
            raise TransactionError("registry must not be a symlink")
        try:
            value = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TransactionError(f"cannot read installer registry: {exc}") from exc
        if value.get("schema_version") != 1 or not isinstance(value.get("modules"), dict):
            raise TransactionError("installer registry has an unsupported structure")
        return value

    @staticmethod
    def _write_json_atomic(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def _validate_plan(self, plan: ModulePlan) -> tuple[Path, dict[str, FileEntry]]:
        if not isinstance(plan, ModulePlan):
            raise TransactionError("plan must be a ModulePlan")
        if not _MODULE_ID.fullmatch(plan.module_id):
            raise TransactionError("invalid module id")
        if not _VERSION.fullmatch(plan.version):
            raise TransactionError("module version must be semantic x.y.z")
        if plan.operation not in {"install", "update", "remove"}:
            raise TransactionError("operation must be install, update or remove")
        owned = [_safe_relative(path).as_posix() for path in plan.owned_paths]
        if len(set(owned)) != len(owned):
            raise TransactionError("duplicate owned path")
        for index, first in enumerate(owned):
            for second in owned[index + 1:]:
                if first.startswith(second + "/") or second.startswith(first + "/"):
                    raise TransactionError("overlapping owned paths are not allowed")
        for data_path in plan.data_paths:
            if not isinstance(data_path, str) or not data_path.startswith(("ur0:", "ux0:", "uma0:", "imc0:", "xmc0:")):
                raise TransactionError(f"invalid preserved data path: {data_path!r}")
        payload_root = Path(plan.payload_root).absolute()
        entries: dict[str, FileEntry] = {}
        if plan.operation != "remove":
            _reject_symlink_components(payload_root)
            if not payload_root.is_dir():
                raise TransactionError("payload root must be a directory")
            for entry in plan.files:
                target_rel = _safe_relative(entry.path).as_posix()
                source_rel = _safe_relative(entry.source)
                if target_rel in entries:
                    raise TransactionError(f"duplicate target file: {target_rel}")
                if not any(target_rel == root or target_rel.startswith(root + "/") for root in owned):
                    raise TransactionError(f"file is outside declared module ownership: {target_rel}")
                if not _SHA256.fullmatch(entry.sha256):
                    raise TransactionError(f"invalid SHA-256 for {target_rel}")
                if not isinstance(entry.size_bytes, int) or entry.size_bytes < 0:
                    raise TransactionError(f"invalid size for {target_rel}")
                source = payload_root.joinpath(*source_rel.parts)
                _reject_symlink_components(source, payload_root)
                if not source.is_file():
                    raise TransactionError(f"payload file is missing: {entry.source}")
                if source.stat().st_size != entry.size_bytes:
                    raise TransactionError(f"payload size mismatch: {entry.source}")
                if _sha256(source).lower() != entry.sha256.lower():
                    raise TransactionError(f"payload SHA-256 mismatch: {entry.source}")
                entries[target_rel] = entry
            if not entries:
                raise TransactionError("install/update plan must contain at least one payload file")
        return payload_root, entries

    def _write_journal(self, directory: Path, journal: dict) -> None:
        self._write_json_atomic(directory / "journal.json", journal)

    @staticmethod
    def _copy_atomic(source: Path, target: Path) -> None:
        """Restore a regular file through a fresh, exclusive temporary file."""
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=target.name + ".rtse-", suffix=".tmp", dir=target.parent
        )
        try:
            with os.fdopen(fd, "wb") as output, source.open("rb") as input_stream:
                shutil.copyfileobj(input_stream, output)
                output.flush()
                os.fsync(output.fileno())
            shutil.copystat(source, temporary, follow_symlinks=False)
            os.replace(temporary, target)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def _restore(self, directory: Path, journal: dict, *, strict: bool) -> None:
        changes = journal.get("changes", [])
        registry_backup = journal.get("registry_backup")

        # Validate the entire rollback before changing any live file. A late
        # conflict must not leave an earlier file already restored.
        for change in changes:
            target = self._target(change["path"])
            after_hash = change.get("after_sha256")
            if strict:
                if after_hash is None:
                    if target.exists() or target.is_symlink():
                        raise TransactionError(
                            f"cannot roll back; a file appeared after commit: {change['path']}"
                        )
                elif target.is_symlink() or not target.is_file() or _sha256(target) != after_hash:
                    raise TransactionError(
                        f"cannot roll back; installed file was modified: {change['path']}"
                    )
            if change["existed"]:
                backup_rel = change.get("backup_rel")
                if not backup_rel:
                    raise TransactionError(f"missing backup reference for {change['path']}")
                backup = directory / backup_rel
                if backup.is_symlink() or not backup.is_file():
                    raise TransactionError(f"transaction backup is missing or unsafe: {backup_rel}")
                before_hash = change.get("before_sha256")
                if before_hash and _sha256(backup) != before_hash:
                    raise TransactionError(f"transaction backup failed integrity check: {backup_rel}")

        if strict:
            expected_registry = journal.get("registry_after_sha256")
            if expected_registry is None:
                if self.registry_path.exists() or self.registry_path.is_symlink():
                    raise TransactionError("cannot roll back; installer registry appeared after commit")
            elif (self.registry_path.is_symlink() or not self.registry_path.is_file() or
                  _sha256(self.registry_path) != expected_registry):
                raise TransactionError("cannot roll back; installer registry was modified after commit")

        if journal.get("registry_existed"):
            if not registry_backup:
                raise TransactionError("missing installer registry backup reference")
            backup = directory / registry_backup
            if backup.is_symlink() or not backup.is_file():
                raise TransactionError("installer registry backup is missing or unsafe")
            before_hash = journal.get("registry_before_sha256")
            if before_hash and _sha256(backup) != before_hash:
                raise TransactionError("installer registry backup failed integrity check")

        for change in reversed(changes):
            target = self._target(change["path"])
            backup_rel = change.get("backup_rel")
            if change["existed"]:
                backup = directory / backup_rel
                self._copy_atomic(backup, target)
            elif target.exists():
                if target.is_symlink() or not target.is_file():
                    raise TransactionError(f"refusing to remove non-regular target: {target}")
                target.unlink()

        if journal.get("registry_existed"):
            backup = directory / registry_backup
            self._copy_atomic(backup, self.registry_path)
        elif self.registry_path.exists():
            if self.registry_path.is_symlink():
                raise TransactionError("refusing to remove a registry symlink")
            self.registry_path.unlink()

    def apply(self, plan: ModulePlan) -> str:
        with self._exclusive_lock():
            return self._apply_locked(plan)

    def _apply_locked(self, plan: ModulePlan) -> str:
        payload_root, entries = self._validate_plan(plan)
        registry = self._load_registry()
        modules = registry["modules"]
        current = modules.get(plan.module_id)
        if plan.operation == "install" and current is not None:
            raise TransactionError(f"module is already installed: {plan.module_id}")
        if plan.operation in {"update", "remove"} and current is None:
            raise TransactionError(f"module is not installed: {plan.module_id}")

        old_files = dict(current.get("files", {})) if current else {}
        desired = {path: entry.sha256.lower() for path, entry in entries.items()}
        owners = {}
        for module_id, module in modules.items():
            for path in module.get("files", {}):
                owners[path] = module_id
        for path in desired:
            owner = owners.get(path)
            if owner and owner != plan.module_id:
                raise TransactionError(f"file is owned by another module: {path} ({owner})")
        for path, old_hash in old_files.items():
            target = self._target(path)
            if target.exists() and (target.is_symlink() or not target.is_file()):
                raise TransactionError(f"owned target is not a regular file: {path}")
            if target.exists() and _sha256(target) != old_hash:
                raise TransactionError(f"owned file was modified outside the installer: {path}")

        for path in desired:
            target = self._target(path)
            if target.exists() and path not in old_files:
                raise TransactionError(f"refusing to overwrite an unowned file: {path}")
        obsolete = sorted(set(old_files) - set(desired)) if plan.operation == "update" else []
        if plan.operation == "remove":
            desired = {}
            obsolete = sorted(old_files)

        transaction_id = uuid.uuid4().hex
        directory = self.transactions / transaction_id
        stage = directory / "stage"
        backup = directory / "backup"
        stage.mkdir(parents=True)
        backup.mkdir(parents=True)
        changes = []
        try:
            for path, entry in entries.items():
                staged = stage.joinpath(*_safe_relative(path).parts)
                staged.parent.mkdir(parents=True, exist_ok=True)
                source = payload_root.joinpath(*_safe_relative(entry.source).parts)
                shutil.copy2(source, staged)
                if _sha256(staged).lower() != entry.sha256.lower():
                    raise TransactionError(f"staged payload verification failed: {path}")

            changed_paths = sorted(set(desired) | set(obsolete))
            for path in changed_paths:
                target = self._target(path)
                existed = target.exists()
                backup_rel = None
                if existed:
                    if target.is_symlink() or not target.is_file():
                        raise TransactionError(f"refusing to back up non-regular file: {path}")
                    backup_path = backup.joinpath(*_safe_relative(path).parts)
                    backup_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, backup_path)
                    backup_rel = str(backup_path.relative_to(directory))
                changes.append({
                    "path": path,
                    "existed": existed,
                    "backup_rel": backup_rel,
                    "before_sha256": _sha256(target) if existed else None,
                    "after_sha256": desired.get(path),
                })

            registry_existed = self.registry_path.exists()
            registry_backup = None
            if registry_existed:
                if self.registry_path.is_symlink() or not self.registry_path.is_file():
                    raise TransactionError("registry is not a regular file")
                shutil.copy2(self.registry_path, backup / "registry.json")
                registry_backup = "backup/registry.json"
                registry_before_sha256 = _sha256(self.registry_path)
            else:
                registry_before_sha256 = None

            journal = {
                "schema_version": 1,
                "transaction_id": transaction_id,
                "module_id": plan.module_id,
                "operation": plan.operation,
                "status": "prepared",
                "registry_existed": registry_existed,
                "registry_backup": registry_backup,
                "registry_before_sha256": registry_before_sha256,
                "changes": changes,
            }
            self._write_journal(directory, journal)

            for path in obsolete:
                target = self._target(path)
                if target.exists():
                    target.unlink()
            for path in desired:
                target = self._target(path)
                target.parent.mkdir(parents=True, exist_ok=True)
                _reject_symlink_components(target, self.root)
                staged = stage.joinpath(*_safe_relative(path).parts)
                os.replace(staged, target)

            if plan.operation == "remove":
                modules.pop(plan.module_id, None)
            else:
                modules[plan.module_id] = {
                    "version": plan.version,
                    "files": desired,
                    "owned_paths": sorted({_safe_relative(path).as_posix() for path in plan.owned_paths}),
                    "data_paths": list(plan.data_paths),
                }
            self._write_json_atomic(self.registry_path, registry)
            journal["registry_after_sha256"] = _sha256(self.registry_path)
            journal["status"] = "committed"
            self._write_journal(directory, journal)
            return transaction_id
        except Exception as exc:
            if "journal" in locals():
                try:
                    self._restore(directory, journal, strict=False)
                    journal["status"] = "rolled_back"
                    journal["failure"] = str(exc)
                    self._write_journal(directory, journal)
                except Exception as rollback_exc:
                    raise TransactionError(f"transaction failed ({exc}); automatic rollback also failed ({rollback_exc})") from rollback_exc
            else:
                shutil.rmtree(directory, ignore_errors=True)
            if isinstance(exc, TransactionError):
                raise
            raise TransactionError(f"transaction failed and was rolled back: {exc}") from exc

    def rollback(self, transaction_id: str) -> None:
        with self._exclusive_lock():
            self._rollback_locked(transaction_id)

    def _rollback_locked(self, transaction_id: str) -> None:
        if not _TRANSACTION_ID.fullmatch(transaction_id):
            raise TransactionError("invalid transaction id")
        directory = self.transactions / transaction_id
        journal_path = directory / "journal.json"
        if not journal_path.is_file() or journal_path.is_symlink():
            raise TransactionError("transaction journal does not exist")
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        if journal.get("status") != "committed":
            raise TransactionError("only committed transactions can be rolled back")
        self._restore(directory, journal, strict=True)
        journal["status"] = "rolled_back"
        self._write_journal(directory, journal)
