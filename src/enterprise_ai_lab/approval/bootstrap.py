"""Explicit, hash-checked local Temporal CLI bootstrap. Verification is offline."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import stat
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request


MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_BINARY_BYTES = 192 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 90
_RELEASE_BASE = "https://github.com/temporalio/cli/releases/download/v1.9.1/"
_DOWNLOAD_HOSTS = frozenset({
    "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
})


class BootstrapError(ValueError):
    """A missing, changed, unsupported or malformed local runtime artifact."""


def _load_lock() -> dict:
    with Path(__file__).with_name("temporal-cli-lock.json").open(encoding="utf-8") as stream:
        return json.load(stream)


def _platform_key(system: str | None = None, machine: str | None = None) -> str:
    system, machine = (system or platform.system()).lower(), (machine or platform.machine()).lower()
    normalized = {"aarch64": "arm64", "x86_64": "amd64", "x64": "amd64"}.get(machine, machine)
    key = f"{system}-{normalized}"
    if key not in _load_lock()["platforms"]:
        raise BootstrapError("supported platforms are Darwin arm64 and Linux amd64")
    return key


def _pin(platform_key: str | None = None) -> tuple[str, dict, dict]:
    lock = _load_lock()
    key = platform_key or _platform_key()
    if key not in lock["platforms"]:
        raise BootstrapError("unsupported platform")
    return key, lock["platforms"][key], lock


def _read_regular(path: Path, maximum: int, *, keep_bytes: bool = False) -> tuple[str, int, bytes | None]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BootstrapError("runtime artifact must be a readable regular file") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= maximum:
            raise BootstrapError("runtime artifact has an invalid size or type")
        digest, total, chunks = hashlib.sha256(), 0, []
        while True:
            block = os.read(descriptor, min(1024 * 1024, maximum + 1 - total))
            if not block:
                break
            total += len(block)
            if total > maximum:
                raise BootstrapError("runtime artifact exceeds its byte limit")
            digest.update(block)
            if keep_bytes:
                chunks.append(block)
        after = os.fstat(descriptor)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or total != before.st_size:
            raise BootstrapError("runtime artifact changed while being read")
        return digest.hexdigest(), total, b"".join(chunks) if keep_bytes else None
    finally:
        os.close(descriptor)


def verify_cli(path: str | Path, *, platform_key: str | None = None) -> dict:
    """Check exact bytes without executing the binary or making network calls."""
    key, pin, lock = _pin(platform_key)
    binary = Path(path)
    digest, count, _ = _read_regular(binary, MAX_BINARY_BYTES)
    if digest != pin["binary_sha256"] or count != pin["binary_bytes"]:
        raise BootstrapError("Temporal executable differs from its pinned artifact")
    if not os.access(binary, os.X_OK):
        raise BootstrapError("Temporal artifact is not executable")
    return {
        "cli_version": lock["cli_version"], "server_version": lock["server_version"],
        "platform": key, "sha256": digest, "bytes": count,
    }


def _validate_download_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https" or parsed.hostname not in _DOWNLOAD_HOSTS
        or parsed.username is not None or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise BootstrapError("download left the official GitHub asset hosts")


class _OfficialRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        _validate_download_url(new_url)
        return super().redirect_request(request, response, code, message, headers, new_url)


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_without_replace(temporary: Path, target: Path) -> None:
    # A hard link gives atomic no-clobber publication on our two supported OSes.
    os.link(temporary, target)
    _sync_directory(target.parent)


def _download_archive(url: str, target: Path, pin: dict) -> None:
    _validate_download_url(url)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _OfficialRedirects())
    request = urllib.request.Request(url, headers={"User-Agent": "enterprise-ai-lab-temporal-bootstrap/1", "Accept-Encoding": "identity"})
    descriptor, name = tempfile.mkstemp(prefix=".temporal-download-", dir=target.parent)
    temporary = Path(name)
    started, total, digest = time.monotonic(), 0, hashlib.sha256()
    try:
        with os.fdopen(descriptor, "wb") as output:
            with opener.open(request, timeout=15) as response:
                _validate_download_url(response.geturl())
                declared = response.headers.get("Content-Length")
                if declared is not None and (not declared.isascii() or not declared.isdecimal() or int(declared) > MAX_ARCHIVE_BYTES):
                    raise BootstrapError("download exceeds its byte limit")
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise BootstrapError("unexpected download encoding")
                while True:
                    if time.monotonic() - started > DOWNLOAD_TIMEOUT_SECONDS:
                        raise BootstrapError("download exceeded its time budget")
                    # HTTPResponse.read1 returns after one buffered socket read;
                    # unlike read(n), a slow trickle cannot fill n indefinitely
                    # without letting us recheck the overall time budget.
                    block = response.read1(1024 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > MAX_ARCHIVE_BYTES:
                        raise BootstrapError("download exceeds its byte limit")
                    digest.update(block)
                    output.write(block)
            output.flush()
            os.fsync(output.fileno())
        if total != pin["archive_bytes"] or digest.hexdigest() != pin["archive_sha256"]:
            raise BootstrapError("downloaded archive differs from its pinned artifact")
        try:
            _publish_without_replace(temporary, target)
        except FileExistsError:
            # Another bootstrap may win; never replace an existing artifact.
            existing, size, _ = _read_regular(target, MAX_ARCHIVE_BYTES)
            if existing != pin["archive_sha256"] or size != pin["archive_bytes"]:
                raise BootstrapError("existing archive differs from its pinned artifact")
    except BootstrapError:
        raise
    except Exception as exc:
        # Never include signed redirect URLs or remote response bodies in errors.
        raise BootstrapError("official Temporal download failed") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _extract_binary(archive: bytes, target: Path, pin: dict) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".temporal-executable-", dir=target.parent)
    temporary = Path(name)
    found = False
    try:
        with os.fdopen(descriptor, "wb") as output:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as compressed:
                for count, member in enumerate(compressed, start=1):
                    if count > 16 or member.size < 0 or member.size > MAX_BINARY_BYTES:
                        raise BootstrapError("invalid Temporal archive member")
                    if member.name != "temporal":
                        if member.size > 1024 * 1024:
                            raise BootstrapError("unexpected archive member")
                        continue
                    if found or not member.isfile() or member.size != pin["binary_bytes"]:
                        raise BootstrapError("archive must contain one regular Temporal executable")
                    found, total, digest = True, 0, hashlib.sha256()
                    source = compressed.extractfile(member)
                    if source is None:
                        raise BootstrapError("missing Temporal executable stream")
                    with source:
                        while True:
                            block = source.read(1024 * 1024)
                            if not block:
                                break
                            total += len(block)
                            if total > MAX_BINARY_BYTES:
                                raise BootstrapError("executable exceeds its byte limit")
                            output.write(block)
                            digest.update(block)
                    if total != pin["binary_bytes"] or digest.hexdigest() != pin["binary_sha256"]:
                        raise BootstrapError("executable differs from its pinned artifact")
            if not found:
                raise BootstrapError("archive lacks the Temporal executable")
            output.flush()
            os.fchmod(output.fileno(), 0o755)
            os.fsync(output.fileno())
        try:
            _publish_without_replace(temporary, target)
        except FileExistsError:
            digest, size, _ = _read_regular(target, MAX_BINARY_BYTES)
            if digest != pin["binary_sha256"] or size != pin["binary_bytes"]:
                raise BootstrapError("existing executable differs from its pinned artifact")
    except BootstrapError:
        raise
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise BootstrapError("invalid Temporal archive") from exc
    finally:
        temporary.unlink(missing_ok=True)


def bootstrap_cli(directory: str | Path, *, platform_key: str | None = None) -> Path:
    """Explicit network-enabled operation; workers and demos call verify_cli only."""
    key, pin, _ = _pin(platform_key)
    destination = Path(directory).expanduser().absolute()
    destination.mkdir(parents=True, exist_ok=True)
    binary = destination / "temporal"
    if binary.exists() or binary.is_symlink():
        verify_cli(binary, platform_key=key)
        return binary
    archive_path = destination / pin["archive"]
    if not archive_path.exists() and not archive_path.is_symlink():
        _download_archive(_RELEASE_BASE + pin["archive"], archive_path, pin)
    digest, count, archive = _read_regular(archive_path, MAX_ARCHIVE_BYTES, keep_bytes=True)
    if digest != pin["archive_sha256"] or count != pin["archive_bytes"]:
        raise BootstrapError("existing archive differs from its pinned artifact")
    assert archive is not None
    _extract_binary(archive, binary, pin)
    verify_cli(binary, platform_key=key)
    return binary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("bootstrap", help="explicitly download the pinned official CLI")
    install.add_argument("--directory", type=Path, required=True)
    check = commands.add_parser("verify", help="verify a local executable offline")
    check.add_argument("--temporal-cli", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        binary = bootstrap_cli(args.directory) if args.command == "bootstrap" else args.temporal_cli
        print(json.dumps({"path": str(binary), **verify_cli(binary)}, sort_keys=True))
    except (BootstrapError, OSError):
        parser.exit(1, "Temporal artifact verification/bootstrap failed; check the pinned files and supported platform.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
