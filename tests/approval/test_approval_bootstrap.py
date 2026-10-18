"""Bootstrap boundary tests use controlled archive bytes, never live downloads."""

import hashlib
import io
import tarfile

import pytest

from enterprise_ai_lab.approval import bootstrap


def archive_with(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as compressed:
        for name, content, kind in entries:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.size = len(content) if kind == tarfile.REGTYPE else 0
            info.linkname = "elsewhere" if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ""
            compressed.addfile(info, io.BytesIO(content) if info.size else None)
    return buffer.getvalue()


@pytest.fixture
def controlled_pin(monkeypatch):
    binary = b"controlled executable fixture\n"
    archive = archive_with([("LICENSE", b"MIT fixture", tarfile.REGTYPE), ("temporal", binary, tarfile.REGTYPE)])
    pin = {
        "archive": "temporal_cli_1.9.1_linux_amd64.tar.gz",
        "archive_sha256": hashlib.sha256(archive).hexdigest(), "archive_bytes": len(archive),
        "binary_sha256": hashlib.sha256(binary).hexdigest(), "binary_bytes": len(binary),
    }
    lock = {"cli_version": "1.9.1", "server_version": "1.32.0", "platforms": {"linux-amd64": pin}}
    monkeypatch.setattr(bootstrap, "_load_lock", lambda: lock)
    return binary, archive, pin


@pytest.mark.parametrize("system,machine,key", [("Darwin", "arm64", "darwin-arm64"), ("Darwin", "aarch64", "darwin-arm64"), ("Linux", "x86_64", "linux-amd64"), ("Linux", "amd64", "linux-amd64")])
def test_supported_platform_mapping(system, machine, key):
    assert bootstrap._platform_key(system, machine) == key


@pytest.mark.parametrize("system,machine", [("Windows", "AMD64"), ("Darwin", "x86_64"), ("Linux", "arm64"), ("Linux", "untrusted")])
def test_unsupported_platforms_are_explicit(system, machine):
    with pytest.raises(bootstrap.BootstrapError, match="supported platforms"):
        bootstrap._platform_key(system, machine)


def test_packaged_lock_contains_exact_official_pins():
    lock = bootstrap._load_lock()
    assert lock["cli_version"] == "1.9.1" and lock["server_version"] == "1.32.0"
    assert lock["platforms"]["darwin-arm64"]["archive_sha256"] == "41e0425378fcb4fb5766340b97435e20fe47bbff2d7bf644ec2d51f7662b7c56"
    assert lock["platforms"]["linux-amd64"]["binary_sha256"] == "712217694cac9f786a72d4b6c876cd15f3dcf1bf31434d9fed6af25242a327a0"


def test_cached_archive_bootstrap_then_offline_verify_and_reuse(tmp_path, monkeypatch, controlled_pin):
    binary, archive, pin = controlled_pin
    (tmp_path / pin["archive"]).write_bytes(archive)
    monkeypatch.setattr(bootstrap, "_download_archive", lambda *args: pytest.fail("cached bootstrap attempted network"))
    path = bootstrap.bootstrap_cli(tmp_path, platform_key="linux-amd64")
    assert path.read_bytes() == binary and path.stat().st_mode & 0o111
    assert bootstrap.verify_cli(path, platform_key="linux-amd64")["sha256"] == pin["binary_sha256"]
    (tmp_path / pin["archive"]).unlink()
    assert bootstrap.bootstrap_cli(tmp_path, platform_key="linux-amd64") == path


@pytest.mark.parametrize("which", ["archive", "binary"])
def test_changed_existing_artifacts_are_never_replaced(tmp_path, monkeypatch, controlled_pin, which):
    _, _, pin = controlled_pin
    target = tmp_path / (pin["archive"] if which == "archive" else "temporal")
    target.write_bytes(b"owner changed bytes")
    target.chmod(0o755)
    monkeypatch.setattr(bootstrap, "_download_archive", lambda *args: pytest.fail("changed file triggered network"))
    with pytest.raises(bootstrap.BootstrapError, match="differs"):
        bootstrap.bootstrap_cli(tmp_path, platform_key="linux-amd64")
    assert target.read_bytes() == b"owner changed bytes"


@pytest.mark.parametrize("kind", ["symlink", "directory", "empty"])
def test_executable_must_be_a_bounded_regular_file(tmp_path, controlled_pin, kind):
    binary, _, _ = controlled_pin
    path = tmp_path / "temporal"
    if kind == "symlink":
        actual = tmp_path / "target"
        actual.write_bytes(binary)
        path.symlink_to(actual)
    elif kind == "directory":
        path.mkdir()
    else:
        path.touch()
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.verify_cli(path, platform_key="linux-amd64")


def test_read_and_download_caps_are_enforced(tmp_path, monkeypatch, controlled_pin):
    _, archive, pin = controlled_pin
    (tmp_path / pin["archive"]).write_bytes(archive)
    monkeypatch.setattr(bootstrap, "MAX_ARCHIVE_BYTES", len(archive) - 1)
    with pytest.raises(bootstrap.BootstrapError, match="size or type"):
        bootstrap.bootstrap_cli(tmp_path, platform_key="linux-amd64")
    assert not (tmp_path / "temporal").exists()


@pytest.mark.parametrize("entries", [
    [("temporal", b"", tarfile.SYMTYPE)],
    [("temporal", b"", tarfile.LNKTYPE)],
    [("temporal", b"controlled executable fixture\n", tarfile.REGTYPE)] * 2,
    [("../temporal", b"controlled executable fixture\n", tarfile.REGTYPE)],
    [("temporal", b"altered binary", tarfile.REGTYPE)],
])
def test_tar_links_duplicates_missing_and_changed_binaries_fail_without_output(tmp_path, controlled_pin, entries):
    _, _, pin = controlled_pin
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap._extract_binary(archive_with(entries), tmp_path / "temporal", pin)
    assert list(tmp_path.iterdir()) == []


def test_archive_paths_are_never_extracted(tmp_path, controlled_pin):
    binary, _, pin = controlled_pin
    raw = archive_with([("../escape", b"must stay in archive", tarfile.REGTYPE), ("temporal", binary, tarfile.REGTYPE)])
    bootstrap._extract_binary(raw, tmp_path / "temporal", pin)
    assert (tmp_path / "temporal").read_bytes() == binary
    assert not (tmp_path.parent / "escape").exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["temporal"]


@pytest.mark.parametrize("url", ["http://github.com/a", "https://attacker.test/a", "https://github.com:444/a", "https://user:password@github.com/a", "file:///tmp/a", "https://github.com.attacker.test/a"])
def test_downloads_cannot_leave_official_https_hosts(url):
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap._validate_download_url(url)


class Response(io.BytesIO):
    def __init__(self, body, **headers):
        super().__init__(body)
        self.headers = headers

    def geturl(self):
        return "https://release-assets.githubusercontent.com/fixture"


def mock_download(monkeypatch, response):
    class Opener:
        def open(self, *args, **kwargs):
            return response
    monkeypatch.setattr(bootstrap.urllib.request, "build_opener", lambda *args: Opener())


def test_explicit_download_checks_hashes_and_publishes_atomically(tmp_path, monkeypatch, controlled_pin):
    _, archive, pin = controlled_pin
    mock_download(monkeypatch, Response(archive, **{"Content-Length": str(len(archive))}))
    target = tmp_path / pin["archive"]
    bootstrap._download_archive(bootstrap._RELEASE_BASE + pin["archive"], target, pin)
    assert target.read_bytes() == archive
    assert sorted(path.name for path in tmp_path.iterdir()) == [pin["archive"]]


@pytest.mark.parametrize("case", ["changed", "encoded", "oversized", "timeout"])
def test_failed_downloads_clean_temporary_files(tmp_path, monkeypatch, controlled_pin, case):
    _, archive, pin = controlled_pin
    headers = {}
    if case == "changed":
        archive += b"changed"
    elif case == "encoded":
        headers["Content-Encoding"] = "gzip"
    elif case == "oversized":
        headers["Content-Length"] = str(bootstrap.MAX_ARCHIVE_BYTES + 1)
    else:
        ticks = iter((0, bootstrap.DOWNLOAD_TIMEOUT_SECONDS + 1))
        monkeypatch.setattr(bootstrap.time, "monotonic", lambda: next(ticks))
    mock_download(monkeypatch, Response(archive, **headers))
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap._download_archive(bootstrap._RELEASE_BASE + pin["archive"], tmp_path / pin["archive"], pin)
    assert list(tmp_path.iterdir()) == []
