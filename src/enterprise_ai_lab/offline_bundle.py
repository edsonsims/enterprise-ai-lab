"""Bounded core-wheel bundle creation and verification, using only the standard library.

Run this verifier from trusted source and supply the independently retained manifest
SHA-256. A matching digest is not a publisher signature. Wheels are trusted operator
inputs, not sandboxed code. Local pathname checks do not defeat a hostile filesystem.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import stat
import sys
import zipfile
import zlib
from email.parser import BytesParser
from pathlib import Path

MAX_WHEEL_BYTES = 32 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_VERIFIER_BYTES = 128 * 1024
MAX_ZIP_ENTRIES = 2048
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 4 * 1024 * 1024
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_VERSION = re.compile(r"(?:0|[1-9][0-9]{0,5})(?:\.(?:0|[1-9][0-9]{0,5})){2}\Z")
_CODES = frozenset(
    {
        "invalid_input",
        "unsafe_path",
        "existing_output",
        "artifact_too_large",
        "invalid_wheel",
        "invalid_manifest",
        "manifest_digest_mismatch",
        "artifact_mismatch",
        "inventory_mismatch",
        "storage_unavailable",
    }
)


class BundleError(ValueError):
    """A finite failure code; source paths and exception text are not rendered."""

    def __init__(self, code: str):
        if code not in _CODES:
            raise ValueError("Unknown bundle error code")
        self.code = code
        super().__init__(code)


def canonical_bytes(value: dict) -> bytes:
    """Canonical manifest JSON, including one final newline."""
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _path(value) -> Path:
    try:
        if not isinstance(value, (str, Path)):
            raise BundleError("invalid_input")
        path = Path(value).absolute()
        if ".." in path.parts or "\x00" in str(path):
            raise BundleError("unsafe_path")
        return path
    except (OSError, TypeError, ValueError) as exc:
        if isinstance(exc, BundleError):
            raise
        raise BundleError("invalid_input") from None


def _directories(path: Path) -> None:
    """Check existing physical directory components without following symlinks."""
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except OSError:
            raise BundleError("storage_unavailable") from None
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise BundleError("unsafe_path")


def _identity(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mode,
        info.st_nlink,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_uid,
    )


def _regular(info) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.getuid()
    ):
        raise BundleError("unsafe_path")


def _read(path: Path, limit: int) -> bytes:
    _directories(path.parent)
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(descriptor)
        _regular(before)
        if before.st_size <= 0 or before.st_size > limit:
            raise BundleError("artifact_too_large")
        chunks = []
        total = 0
        while total <= limit:
            chunk = os.read(descriptor, min(1024 * 1024, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > limit:
            raise BundleError("artifact_too_large")
        if total != before.st_size or _identity(before) != _identity(
            os.fstat(descriptor)
        ):
            raise BundleError("artifact_mismatch")
        if _identity(path.lstat()) != _identity(before):
            raise BundleError("unsafe_path")
        return b"".join(chunks)
    except OSError:
        raise BundleError("storage_unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _version(value, code="invalid_manifest") -> str:
    if type(value) is not str or _VERSION.fullmatch(value) is None:
        raise BundleError(code)
    return value


def _wheel(data: bytes, filename: str) -> str:
    """Inspect the lab's pure universal wheel; never extract or execute it."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > MAX_ZIP_ENTRIES:
                raise BundleError("invalid_wheel")
            names = [entry.filename for entry in entries]
            if len(set(names)) != len(names):
                raise BundleError("invalid_wheel")
            total = 0
            for entry in entries:
                name = entry.filename
                if (
                    not name
                    or name.startswith("/")
                    or "\\" in name
                    or ":" in name
                    or any(
                        part in ("", ".", "..") for part in name.rstrip("/").split("/")
                    )
                    or entry.flag_bits & 1
                    or entry.compress_type not in (0, 8)
                ):
                    raise BundleError("invalid_wheel")
                mode = entry.external_attr >> 16
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise BundleError("invalid_wheel")
                total += entry.file_size
                if entry.file_size > MAX_MEMBER_BYTES or total > MAX_UNCOMPRESSED_BYTES:
                    raise BundleError("invalid_wheel")
            metadata_paths = [
                name for name in names if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_paths) != 1:
                raise BundleError("invalid_wheel")
            metadata_path = metadata_paths[0]
            if archive.getinfo(metadata_path).file_size > MAX_MANIFEST_BYTES:
                raise BundleError("invalid_wheel")
            metadata = BytesParser().parsebytes(archive.read(metadata_path))
            if (
                metadata.get_all("Name") != ["enterprise-ai-lab"]
                or len(metadata.get_all("Version", [])) != 1
            ):
                raise BundleError("invalid_wheel")
            version = _version(metadata["Version"], "invalid_wheel")
            prefix = f"enterprise_ai_lab-{version}.dist-info/"
            if (
                metadata_path != prefix + "METADATA"
                or filename != f"enterprise_ai_lab-{version}-py3-none-any.whl"
            ):
                raise BundleError("invalid_wheel")
            if not all(
                name.startswith(("enterprise_ai_lab/", prefix)) for name in names
            ):
                raise BundleError("invalid_wheel")
            if not {
                "enterprise_ai_lab/__init__.py",
                "enterprise_ai_lab/cli.py",
                prefix + "WHEEL",
                prefix + "RECORD",
            } <= set(names):
                raise BundleError("invalid_wheel")
            wheel = BytesParser().parsebytes(archive.read(prefix + "WHEEL"))
            if (
                wheel.get_all("Wheel-Version") != ["1.0"]
                or wheel.get_all("Root-Is-Purelib") != ["true"]
                or wheel.get_all("Tag") != ["py3-none-any"]
            ):
                raise BundleError("invalid_wheel")
            if archive.testzip() is not None:
                raise BundleError("invalid_wheel")
            return version
    except (
        zipfile.BadZipFile,
        zlib.error,
        KeyError,
        ValueError,
        RuntimeError,
        OSError,
        UnicodeError,
    ):
        raise BundleError("invalid_wheel") from None


def _keys(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise BundleError("invalid_manifest")


def _manifest(value: dict) -> str:
    _keys(
        value,
        {
            "schema_version",
            "bundle_kind",
            "project",
            "artifacts",
            "dependencies",
            "models",
            "container_images",
        },
    )
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["bundle_kind"] != "enterprise_ai_lab_core_wheel"
    ):
        raise BundleError("invalid_manifest")
    for name in ("dependencies", "models", "container_images"):
        if type(value[name]) is not list or value[name]:
            raise BundleError("invalid_manifest")
    project = value["project"]
    _keys(project, {"name", "version"})
    if project["name"] != "enterprise-ai-lab":
        raise BundleError("invalid_manifest")
    version = _version(project["version"])
    records = value["artifacts"]
    if type(records) is not list or len(records) != 2:
        raise BundleError("invalid_manifest")
    for i, (kind, limit) in enumerate(
        (("wheel", MAX_WHEEL_BYTES), ("verifier", MAX_VERIFIER_BYTES))
    ):
        record = records[i]
        _keys(
            record,
            {"kind", "path", "sha256", "size_bytes"}
            | ({"filename"} if i == 0 else set()),
        )
        if (
            record["kind"] != kind
            or type(record["sha256"]) is not str
            or _HASH.fullmatch(record["sha256"]) is None
        ):
            raise BundleError("invalid_manifest")
        if (
            type(record["size_bytes"]) is not int
            or not 0 < record["size_bytes"] <= limit
        ):
            raise BundleError("invalid_manifest")
        expected = "verify_bundle.py"
        if i == 0:
            filename = f"enterprise_ai_lab-{version}-py3-none-any.whl"
            if record["filename"] != filename:
                raise BundleError("invalid_manifest")
            expected = f"artifacts/{record['sha256']}/{filename}"
        if record["path"] != expected:
            raise BundleError("invalid_manifest")
    return records[0]["path"]


def _strict_json(data: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise BundleError("invalid_manifest")
            result[key] = value
        return result

    def reject(_):
        raise BundleError("invalid_manifest")

    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_float=reject,
            parse_constant=reject,
        )
        _manifest(value)
        if canonical_bytes(value) != data:
            raise BundleError("invalid_manifest")
        return value
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise BundleError("invalid_manifest") from None


def _inventory(root: Path, wheel_relative_path: str) -> None:
    expected_files = {"manifest.json", "verify_bundle.py", wheel_relative_path}
    expected_dirs = {"artifacts", str(Path(wheel_relative_path).parent)}
    found_files, found_dirs = set(), set()

    def visit(directory):
        with os.scandir(directory) as entries:
            for entry in entries:
                rel = str(Path(entry.path).relative_to(root))
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    if rel not in expected_dirs:
                        raise BundleError("inventory_mismatch")
                    found_dirs.add(rel)
                    visit(Path(entry.path))
                elif stat.S_ISREG(info.st_mode):
                    _regular(info)
                    found_files.add(rel)
                    if rel not in expected_files:
                        raise BundleError("inventory_mismatch")
                else:
                    raise BundleError("unsafe_path")

    try:
        visit(root)
    except OSError:
        raise BundleError("storage_unavailable") from None
    if found_files != expected_files or found_dirs != expected_dirs:
        raise BundleError("inventory_mismatch")


def _write(path: Path, data: bytes):
    descriptor = None
    try:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise BundleError("storage_unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _result(manifest: dict) -> dict:
    return {
        "manifest_sha256": _digest(canonical_bytes(manifest)),
        "manifest": manifest,
        "wheel_relative_path": manifest["artifacts"][0]["path"],
    }


def create_bundle(wheel, out_dir) -> dict:
    """Create an exclusive private bundle from a trusted operator-supplied lab wheel.

    The parent must already exist. Incomplete output from a failed write is never
    accepted by verification and is not silently reused or removed.
    """
    wheel_path, output = _path(wheel), _path(out_dir)
    _directories(output.parent)
    if output.exists() or output.is_symlink():
        raise BundleError("existing_output")
    data = _read(wheel_path, MAX_WHEEL_BYTES)
    version = _wheel(data, wheel_path.name)
    verifier = _read(Path(__file__).absolute(), MAX_VERIFIER_BYTES)
    wheel_sha = _digest(data)
    relative = f"artifacts/{wheel_sha}/{wheel_path.name}"
    manifest = {
        "schema_version": 1,
        "bundle_kind": "enterprise_ai_lab_core_wheel",
        "project": {"name": "enterprise-ai-lab", "version": version},
        "artifacts": [
            {
                "kind": "wheel",
                "path": relative,
                "filename": wheel_path.name,
                "sha256": wheel_sha,
                "size_bytes": len(data),
            },
            {
                "kind": "verifier",
                "path": "verify_bundle.py",
                "sha256": _digest(verifier),
                "size_bytes": len(verifier),
            },
        ],
        "dependencies": [],
        "models": [],
        "container_images": [],
    }
    _manifest(manifest)
    try:
        output.mkdir(mode=0o700)
        (output / "artifacts").mkdir(mode=0o700)
        (output / "artifacts" / wheel_sha).mkdir(mode=0o700)
    except FileExistsError:
        raise BundleError("existing_output") from None
    except OSError:
        raise BundleError("storage_unavailable") from None
    _write(output / relative, data)
    _write(output / "verify_bundle.py", verifier)
    _write(output / "manifest.json", canonical_bytes(manifest))
    return verify_bundle(output, _digest(canonical_bytes(manifest)))


def verify_bundle(bundle, expected_manifest_sha256: str) -> dict:
    """Verify against an independently retained expected digest, without installation.

    Re-run immediately before an isolated installation. Pathname rechecks assume a
    trusted local host; verification is not an immutable handle to a future pip read.
    """
    if (
        type(expected_manifest_sha256) is not str
        or _HASH.fullmatch(expected_manifest_sha256) is None
    ):
        raise BundleError("invalid_input")
    root = _path(bundle)
    _directories(root)
    data = _read(root / "manifest.json", MAX_MANIFEST_BYTES)
    if _digest(data) != expected_manifest_sha256:
        raise BundleError("manifest_digest_mismatch")
    manifest = _strict_json(data)
    relative = manifest["artifacts"][0]["path"]
    _inventory(root, relative)
    for record, limit in zip(
        manifest["artifacts"], (MAX_WHEEL_BYTES, MAX_VERIFIER_BYTES)
    ):
        artifact = _read(root / record["path"], limit)
        if (
            len(artifact) != record["size_bytes"]
            or _digest(artifact) != record["sha256"]
        ):
            raise BundleError("artifact_mismatch")
        if (
            record["kind"] == "wheel"
            and _wheel(artifact, record["filename"]) != manifest["project"]["version"]
        ):
            raise BundleError("artifact_mismatch")
    _inventory(root, relative)
    return _result(manifest)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--wheel", required=True)
    create.add_argument("--out-dir", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--bundle", required=True)
    verify.add_argument("--expected-manifest-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            result = create_bundle(args.wheel, args.out_dir)
        else:
            result = verify_bundle(args.bundle, args.expected_manifest_sha256)
    except BundleError as exc:
        print(json.dumps({"error": exc.code}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
