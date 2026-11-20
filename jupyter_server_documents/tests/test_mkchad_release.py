"""Fail closed on release-ref, artifact, and backend-only frontend mismatches."""

import hashlib
import io
import json
import subprocess
import tarfile
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.mkchad_release import package_version, validate_ref, verify_dist
from scripts.stage_mkchad_frontend import stage_frontend


class ReleaseTests(unittest.TestCase):
    """Check tag eligibility and metadata without network or package imports."""

    def test_only_exact_version_tag_releases(self):
        version = "0.2.5+mkchad.2"
        self.assertFalse(validate_ref("refs/heads/maintenance", version, "maintenance"))
        self.assertTrue(validate_ref("refs/tags/v" + version, version, "maintenance"))
        for ref in [
            "refs/heads/main",
            "refs/tags/v0.2.5",
            "refs/tags/v0.2.5+mkchad.1",
            "refs/tags/v0.2.5+mkchad.2/extra",
        ]:
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                validate_ref(ref, version, "maintenance")

    def test_only_positive_mkchad_local_versions(self):
        for version in [
            "0.2.5",
            "0.2.5.mkchad.1",
            "0.2.5+mkchad.0",
            "0.2.5+mkchad.01",
            "0.2.5+mkchad.1rc1",
        ]:
            with self.subTest(version=version), self.assertRaises(ValueError):
                validate_ref("refs/tags/v" + version, version, "maintenance")

    def test_version_read_does_not_import_package(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "version.py"
            path.write_text("raise RuntimeError('must not import')\n__version__ = '1.2.3+mkchad.4'")
            self.assertEqual(package_version(path), "1.2.3+mkchad.4")
            path = Path(tmp) / "package.json"
            path.write_text('{"version":"1.2.3+mkchad.4"}')
            self.assertEqual(package_version(path), "1.2.3+mkchad.4")

    def test_metadata_mismatch_never_writes_manifests(self):
        with TemporaryDirectory() as tmp:
            dist = Path(tmp)
            metadata = b"Name: example-package\nVersion: 1.2.3+mkchad.1\n"
            with zipfile.ZipFile(dist / "example.whl", "w") as archive:
                archive.writestr("example.dist-info/METADATA", metadata)
            with tarfile.open(dist / "example.tar.gz", "w:gz") as archive:
                member = tarfile.TarInfo("example/PKG-INFO")
                member.size = len(metadata)
                archive.addfile(member, io.BytesIO(metadata))
            with self.assertRaises(ValueError):
                verify_dist(dist, "example_package", "1.2.3+mkchad.2", "test-commit")
            self.assertFalse((dist / "SHA256SUMS").exists())
            with patch("scripts.mkchad_release.installed_version", return_value="test"):
                verify_dist(
                    dist, "example_package", "1.2.3+mkchad.1", "test-commit", {"version": "0.2.5"}
                )
            provenance = json.loads((dist / "build-provenance.json").read_text())
            self.assertEqual(provenance["source_commit"], "test-commit")
            self.assertEqual(provenance["frontend"]["version"], "0.2.5")
            self.assertEqual(len((dist / "SHA256SUMS").read_text().splitlines()), 3)

    def test_missing_artifacts_are_rejected(self):
        with TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            verify_dist(Path(tmp), "example", "1.2.3+mkchad.1", "test-commit")


class FrontendTests(unittest.TestCase):
    """Backend-only packaging may reuse only the expected, unchanged frontend."""

    def test_staging_rejects_modified_inputs_and_preserves_existing_output(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".github").mkdir()
            (root / "package.json").write_text('{"name":"example","version":"0.2.5+mkchad.2"}')
            wheel = root / "upstream.whl"
            prefix = (
                "jupyter_server_documents-0.2.5.data/data/share/jupyter/labextensions/"
                "@jupyter-ai-contrib/server-documents/"
            )
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(prefix + "package.json", '{"version":"0.2.5"}')
                archive.writestr(prefix + "static/style.js", "published styles")
                archive.writestr(prefix + "install.json", "excluded")
            config = {
                "frontend": {
                    "source_commit": "baseline",
                    "version": "0.2.5",
                    "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                }
            }
            (root / ".github/mkchad-release.json").write_text(json.dumps(config))
            target = root / "jupyter_server_documents/labextension"
            with (
                patch("scripts.stage_mkchad_frontend.subprocess.run") as git_diff,
                patch(
                    "scripts.stage_mkchad_frontend.subprocess.check_output",
                    return_value='{"name":"example","version":"0.2.5"}',
                ),
            ):
                git_diff.side_effect = subprocess.CalledProcessError(1, "git diff")
                with self.assertRaises(subprocess.CalledProcessError):
                    stage_frontend(wheel, root)
                self.assertFalse(target.exists())
                git_diff.side_effect = None
                stage_frontend(wheel, root)
                self.assertEqual((target / "static/style.js").read_text(), "published styles")
                self.assertFalse((target / "install.json").exists())
                with self.assertRaisesRegex(ValueError, "already exists"):
                    stage_frontend(wheel, root)
            wheel.write_bytes(b"changed wheel")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                stage_frontend(wheel, root)
            self.assertEqual((target / "static/style.js").read_text(), "published styles")
