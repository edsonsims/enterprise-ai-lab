"""Core manifest/filesystem controls; small wheels here are authored ZIP fixtures."""

import hashlib
import json
import os
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from enterprise_ai_lab import offline_bundle as bundle


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.wheel = self.make_wheel()

    def make_wheel(
        self, version="0.12.0", *, extra=None, metadata=None, wheel_metadata=None
    ):
        path = self.root / f"enterprise_ai_lab-{version}-py3-none-any.whl"
        prefix = f"enterprise_ai_lab-{version}.dist-info/"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("enterprise_ai_lab/__init__.py", "# PACKAGE_PAYLOAD\n")
            archive.writestr("enterprise_ai_lab/cli.py", "def main(): return 0\n")
            archive.writestr(
                prefix + "METADATA",
                metadata
                or f"Metadata-Version: 2.4\nName: enterprise-ai-lab\nVersion: {version}\n",
            )
            archive.writestr(
                prefix + "WHEEL",
                wheel_metadata
                or "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            )
            archive.writestr(prefix + "RECORD", "")
            if extra is not None:
                name, value = extra
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    archive.writestr(name, value)
        return path

    def create(self, name="bundle"):
        out = self.root / name
        result = bundle.create_bundle(self.wheel, out)
        return out, result

    def rewrite(self, root, manifest):
        data = bundle.canonical_bytes(manifest)
        (root / "manifest.json").write_bytes(data)
        return hashlib.sha256(data).hexdigest()

    def error(self, function, *args, code=None):
        with self.assertRaises(bundle.BundleError) as caught:
            function(*args)
        if code:
            self.assertEqual(caught.exception.code, code)

    def test_same_frozen_input_has_same_canonical_bundle_manifest(self):
        first, a = self.create("a")
        second, b = self.create("b")
        self.assertEqual(a, b)
        self.assertEqual(
            (first / "manifest.json").read_bytes(),
            (second / "manifest.json").read_bytes(),
        )
        self.assertEqual(
            a["manifest_sha256"],
            hashlib.sha256((first / "manifest.json").read_bytes()).hexdigest(),
        )
        self.assertEqual(
            a["manifest"]["project"], {"name": "enterprise-ai-lab", "version": "0.12.0"}
        )
        for inventory in ["dependencies", "models", "container_images"]:
            self.assertEqual(a["manifest"][inventory], [])
        self.assertEqual(bundle.verify_bundle(first, a["manifest_sha256"]), a)

    def test_exact_inventory_and_private_output_modes(self):
        out, result = self.create()
        files = {str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()}
        self.assertEqual(
            files, {"manifest.json", "verify_bundle.py", result["wheel_relative_path"]}
        )
        for p in [out, *out.rglob("*")]:
            self.assertEqual(
                stat.S_IMODE(p.stat().st_mode), 0o700 if p.is_dir() else 0o600
            )
        verifier = Path(bundle.__file__).read_bytes()
        self.assertEqual((out / "verify_bundle.py").read_bytes(), verifier)

    def test_digest_required_and_bad_digest_rejected(self):
        out, result = self.create()
        for digest in [
            None,
            True,
            42,
            "",
            "0" * 63,
            "G" * 64,
            result["manifest_sha256"].upper(),
        ]:
            self.error(bundle.verify_bundle, out, digest, code="invalid_input")
        self.error(bundle.verify_bundle, out, "0" * 64, code="manifest_digest_mismatch")

    def test_unknown_file_or_empty_directory_refused(self):
        for name, directory in [(".hidden", False), ("unexpected", True)]:
            with self.subTest(name=name):
                out, result = self.create(name.replace(".", "") + "-bundle")
                path = out / name
                path.mkdir() if directory else path.write_text("unexpected")
                self.error(
                    bundle.verify_bundle,
                    out,
                    result["manifest_sha256"],
                    code="inventory_mismatch",
                )

    def test_tampered_wheel_or_verifier_is_not_accepted(self):
        for target in ["wheel", "verifier"]:
            with self.subTest(target=target):
                out, result = self.create(target)
                path = out / (
                    result["wheel_relative_path"]
                    if target == "wheel"
                    else "verify_bundle.py"
                )
                path.write_bytes(path.read_bytes() + b"# tamper")
                self.error(
                    bundle.verify_bundle,
                    out,
                    result["manifest_sha256"],
                    code="artifact_mismatch",
                )

    def test_missing_manifest_artifact_and_partial_output_refused(self):
        out, result = self.create()
        (out / result["wheel_relative_path"]).unlink()
        self.error(bundle.verify_bundle, out, result["manifest_sha256"])
        partial = self.root / "partial"
        partial.mkdir()
        self.error(bundle.verify_bundle, partial, result["manifest_sha256"])
        self.error(bundle.create_bundle, self.wheel, partial, code="existing_output")

    def test_json_duplicate_nonfinite_float_unknown_and_noncanonical_refused(self):
        out, result = self.create()
        raw = (out / "manifest.json").read_bytes()
        variants = [
            raw.replace(b"{", b'{"schema_version":1,', 1),
            raw.replace(b'"schema_version":1', b'"schema_version":1.0'),
            raw.replace(b'"schema_version":1', b'"schema_version":NaN'),
            raw.replace(b'"schema_version":1', b'"schema_version":true'),
            b" " + raw,
        ]
        extra = deepcopy(result["manifest"])
        extra["extra"] = "not permitted"
        variants.append(bundle.canonical_bytes(extra))
        for data in variants:
            with self.subTest(data=data[:25]):
                (out / "manifest.json").write_bytes(data)
                self.error(
                    bundle.verify_bundle,
                    out,
                    hashlib.sha256(data).hexdigest(),
                    code="invalid_manifest",
                )

    def test_manifest_paths_identity_sizes_and_nonempty_inventories_strict(self):
        out, result = self.create()
        mutations = [
            lambda m: m["artifacts"][0].update(path="../outside.whl"),
            lambda m: m["artifacts"][0].update(path="/absolute.whl"),
            lambda m: m["artifacts"][0].update(size_bytes=True),
            lambda m: m["artifacts"][0].update(sha256="A" * 64),
            lambda m: m["project"].update(name="another-project"),
            lambda m: m["project"].update(version="1.2.3/escape"),
            lambda m: m.update(container_images=["claimed-image"]),
            lambda m: m.update(dependencies=["not-bundled"]),
            lambda m: m.update(models={}),
            lambda m: m["artifacts"].reverse(),
        ]
        for change in mutations:
            manifest = deepcopy(result["manifest"])
            change(manifest)
            digest = self.rewrite(out, manifest)
            self.error(bundle.verify_bundle, out, digest, code="invalid_manifest")

    def test_input_and_output_symlinks_and_hardlinks_refused(self):
        alias = self.root / "alias.whl"
        alias.symlink_to(self.wheel)
        self.error(bundle.create_bundle, alias, self.root / "from-link")
        alias.unlink()
        os.link(self.wheel, alias)
        self.error(bundle.create_bundle, self.wheel, self.root / "from-hardlink")
        alias.unlink()
        parent = self.root / "linked-parent"
        parent.symlink_to(self.root, target_is_directory=True)
        self.error(bundle.create_bundle, self.wheel, parent / "new", code="unsafe_path")
        output = self.root / "dangling-output"
        output.symlink_to(self.root / "missing")
        self.error(bundle.create_bundle, self.wheel, output, code="existing_output")
        self.assertFalse((self.root / "from-link").exists())
        self.assertFalse((self.root / "from-hardlink").exists())

    def test_bundle_file_hardlink_and_parent_symlink_refused(self):
        out, result = self.create()
        os.link(out / "verify_bundle.py", self.root / "verifier-hardlink")
        self.error(
            bundle.verify_bundle, out, result["manifest_sha256"], code="unsafe_path"
        )
        (self.root / "verifier-hardlink").unlink()
        link = self.root / "linked-bundle"
        link.symlink_to(out, target_is_directory=True)
        self.error(
            bundle.verify_bundle, link, result["manifest_sha256"], code="unsafe_path"
        )

    def test_bundle_directory_and_file_symlinks_refused_without_follow(self):
        for directory in [False, True]:
            out, result = self.create("dir" if directory else "file")
            target = out / ("artifacts" if directory else "verify_bundle.py")
            moved = self.root / ("moved-dir" if directory else "moved-file")
            target.rename(moved)
            target.symlink_to(moved, target_is_directory=directory)
            self.error(
                bundle.verify_bundle, out, result["manifest_sha256"], code="unsafe_path"
            )

    def test_fifo_refused_promptly_without_waiting_for_writer(self):
        fifo = self.root / "input-fifo.whl"
        os.mkfifo(fifo)
        self.error(
            bundle.create_bundle, fifo, self.root / "from-fifo", code="unsafe_path"
        )
        self.assertFalse((self.root / "from-fifo").exists())

    def test_wheel_and_manifest_byte_limits_before_large_read(self):
        huge = self.root / "huge.whl"
        with huge.open("wb") as f:
            f.truncate(bundle.MAX_WHEEL_BYTES + 1)
        self.error(
            bundle.create_bundle,
            huge,
            self.root / "huge-output",
            code="artifact_too_large",
        )
        out, result = self.create()
        with (out / "manifest.json").open("wb") as f:
            f.truncate(bundle.MAX_MANIFEST_BYTES + 1)
        self.error(
            bundle.verify_bundle,
            out,
            result["manifest_sha256"],
            code="artifact_too_large",
        )

    def test_wrong_wheel_project_version_tag_and_filename_refused(self):
        variants = [
            {"metadata": "Name: other\nVersion: 0.12.0\n"},
            {"metadata": "Name: enterprise-ai-lab\nVersion: 0.12.0\nVersion: 0.12.0\n"},
            {"metadata": "Name: enterprise-ai-lab\nVersion: 0.13.0\n"},
            {
                "wheel_metadata": "Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: py3-none-any\n"
            },
        ]
        for i, kwargs in enumerate(variants):
            self.make_wheel(**kwargs)
            self.error(
                bundle.create_bundle,
                self.wheel,
                self.root / f"bad-{i}",
                code="invalid_wheel",
            )
        self.make_wheel()
        renamed = self.root / "untrusted_name.whl"
        self.wheel.rename(renamed)
        self.error(
            bundle.create_bundle, renamed, self.root / "bad-name", code="invalid_wheel"
        )

    def test_zip_traversal_absolute_duplicate_and_symlink_refused(self):
        variants = [
            ("../outside", "bad"),
            ("/absolute", "bad"),
            ("enterprise_ai_lab/../../outside", "bad"),
            ("enterprise_ai_lab\\outside", "bad"),
            ("enterprise_ai_lab/__init__.py", "duplicate"),
        ]
        symbolic = zipfile.ZipInfo("enterprise_ai_lab/symbolic")
        symbolic.external_attr = (stat.S_IFLNK | 0o777) << 16
        variants.append((symbolic, "../outside"))
        for i, extra in enumerate(variants):
            self.make_wheel(extra=extra)
            self.error(
                bundle.create_bundle,
                self.wheel,
                self.root / f"zip-{i}",
                code="invalid_wheel",
            )

    def test_zip_count_decompression_and_crc_limits(self):
        with patch.object(bundle, "MAX_ZIP_ENTRIES", 4):
            self.error(
                bundle.create_bundle,
                self.wheel,
                self.root / "too-many",
                code="invalid_wheel",
            )
        with patch.object(bundle, "MAX_UNCOMPRESSED_BYTES", 10):
            self.error(
                bundle.create_bundle,
                self.wheel,
                self.root / "too-expanded",
                code="invalid_wheel",
            )
        raw = self.wheel.read_bytes().replace(b"PACKAGE_PAYLOAD", b"PACKAGE_CHANGED")
        self.wheel.write_bytes(raw)
        self.error(
            bundle.create_bundle, self.wheel, self.root / "crc", code="invalid_wheel"
        )

    def test_corrupt_deflate_has_finite_error(self):
        path = self.root / "compressed.whl"
        with (
            zipfile.ZipFile(self.wheel) as before,
            zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as after,
        ):
            for info in before.infolist():
                after.writestr(info.filename, before.read(info.filename))
        with zipfile.ZipFile(path) as archive:
            offset = archive.getinfo("enterprise_ai_lab/__init__.py").header_offset
        data = bytearray(path.read_bytes())
        name_size, extra_size = struct.unpack_from("<HH", data, offset + 26)
        data[offset + 30 + name_size + extra_size] = 255
        self.wheel.write_bytes(data)
        self.error(
            bundle.create_bundle,
            self.wheel,
            self.root / "deflate",
            code="invalid_wheel",
        )

    def test_read_rechecks_real_changed_and_replaced_input(self):
        original_read = os.read
        fired = False

        def mutate(fd, size):
            nonlocal fired
            data = original_read(fd, size)
            if not fired:
                fired = True
                self.wheel.write_bytes(self.wheel.read_bytes() + b"changed")
            return data

        with patch.object(bundle.os, "read", mutate):
            self.error(
                bundle.create_bundle,
                self.wheel,
                self.root / "changed",
                code="artifact_mismatch",
            )
        self.make_wheel()
        fired = False

        def replace(fd, size):
            nonlocal fired
            data = original_read(fd, size)
            if not fired:
                fired = True
                self.wheel.rename(self.root / "old-wheel")
                self.wheel.write_bytes(data)
            return data

        with patch.object(bundle.os, "read", replace):
            self.error(
                bundle.create_bundle,
                self.wheel,
                self.root / "replaced",
                code="artifact_mismatch",
            )

    def test_real_copied_verifier_runs_standalone_and_requires_digest(self):
        out, result = self.create()
        command = [
            sys.executable,
            "-B",
            "-S",
            str(out / "verify_bundle.py"),
            "verify",
            "--bundle",
            str(out),
        ]
        run = subprocess.run(
            command + ["--expected-manifest-sha256", result["manifest_sha256"]],
            check=False,
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), result)
        missing = subprocess.run(
            command,
            check=False,
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(missing.returncode, 2)
        self.assertIn("expected-manifest-sha256", missing.stderr)
        (out / result["wheel_relative_path"]).write_bytes(b"broken")
        bad = subprocess.run(
            command + ["--expected-manifest-sha256", result["manifest_sha256"]],
            check=False,
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(json.loads(bad.stderr), {"error": "artifact_mismatch"})
        self.assertNotIn(str(self.root), bad.stderr)

    def test_existing_output_preserves_original_bundle(self):
        out, result = self.create()
        before = (out / "manifest.json").read_bytes()
        self.error(bundle.create_bundle, self.wheel, out, code="existing_output")
        self.assertEqual((out / "manifest.json").read_bytes(), before)
        self.assertEqual(bundle.verify_bundle(out, result["manifest_sha256"]), result)


if __name__ == "__main__":
    unittest.main()
