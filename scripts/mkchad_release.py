"""Validate opt-in release refs and built distributions without importing the package.

The CLI reads .github/mkchad-release.json in the checkout. check-ref validates
GITHUB_REF and tag ancestry, printing the version. verify-dist validates wheel
and sdist metadata, then writes SHA256SUMS and build-provenance.json in --dist.
Invalid refs, metadata or Git state fail before publication; no network is used.
"""

import argparse
import ast
import hashlib
import json
import os
import platform
import re
import subprocess
import tarfile
import zipfile
from email.parser import BytesParser
from importlib.metadata import version as installed_version
from pathlib import Path

VERSION_PATTERN = r"[0-9]+\.[0-9]+\.[0-9]+\+mkchad\.[1-9][0-9]*"


def package_version(path: Path) -> str:
    """Read a JSON version or literal Python __version__; raise ValueError if absent."""
    if path.suffix == ".json":
        return str(json.loads(path.read_text())["version"])
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        ):
            return str(ast.literal_eval(node.value))
    raise ValueError(f"No literal version in {path}")


def validate_ref(ref: str, version: str, branch: str) -> bool:
    """Return whether ref is a release tag; reject invalid versions or other refs.

    version must be MAJOR.MINOR.PATCH+mkchad.N with N positive. The only accepted
    refs are the named maintenance branch and the exact v-prefixed version tag.
    Raises ValueError on mismatch and has no side effects.
    """
    if not re.fullmatch(VERSION_PATTERN, version):
        raise ValueError("Expected MAJOR.MINOR.PATCH+mkchad.N (N >= 1)")
    if ref == f"refs/heads/{branch}":
        return False
    if ref != f"refs/tags/v{version}":
        raise ValueError("Release tag must exactly match the package's MkChad version")
    return True


def verify_dist(
    dist: Path, distribution: str, version: str, commit: str, frontend: dict | None = None
) -> None:
    """Check wheel/sdist name and version, then write checksums and source provenance.

    Requires exactly one wheel and one sdist in dist. Metadata must match the
    expected distribution/version. Raises ValueError on mismatch. Only the two
    manifest files beneath dist are written; existing archives are never changed.
    Optional frontend records the pinned upstream asset origin for backend-only builds.
    """
    wheels = list(dist.glob("*.whl"))
    sdists = list(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("Expected exactly one wheel and one sdist")
    with zipfile.ZipFile(wheels[0]) as archive:
        names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise ValueError("Expected one wheel METADATA")
        metadata = [archive.read(names[0])]
    with tarfile.open(sdists[0]) as sdist:
        names = [
            name for name in sdist.getnames() if name.count("/") == 1 and name.endswith("/PKG-INFO")
        ]
        if len(names) != 1:
            raise ValueError("Expected one top-level sdist PKG-INFO")
        member = sdist.extractfile(names[0])
        if member is None:
            raise ValueError("Missing sdist metadata")
        metadata.append(member.read())

    def normalize(value: str) -> str:
        return re.sub(r"[-_.]+", "-", value).lower()

    for content in metadata:
        fields = BytesParser().parsebytes(content)
        if normalize(fields["Name"]) != normalize(distribution) or fields["Version"] != version:
            raise ValueError("Built distribution metadata does not match the source version")
    hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(wheels + sdists)
    }
    provenance = {
        "distribution": distribution,
        "version": version,
        "source_commit": commit,
        "python": platform.python_version(),
        "build": installed_version("build"),
        "hatchling": installed_version("hatchling"),
        "sha256": hashes,
    }
    if frontend is not None:
        provenance["frontend"] = frontend
    (dist / "build-provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    hashes["build-provenance.json"] = hashlib.sha256(
        (dist / "build-provenance.json").read_bytes()
    ).hexdigest()
    (dist / "SHA256SUMS").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in hashes.items())
    )


def main() -> None:
    """Run check-ref or verify-dist against this checkout; propagate validation errors."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check-ref", "verify-dist"])
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    args = parser.parse_args()
    config = json.loads(Path(".github/mkchad-release.json").read_text())
    version = package_version(Path(config["version_file"]))
    release = validate_ref(os.environ["GITHUB_REF"], version, config["maintenance_branch"])
    if release:
        subprocess.run(
            [
                "git",
                "merge-base",
                "--is-ancestor",
                "HEAD",
                f"refs/remotes/origin/{config['maintenance_branch']}",
            ],
            check=True,
        )
    if args.command == "check-ref":
        print(version)
    else:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        verify_dist(args.dist, config["distribution"], version, commit, config.get("frontend"))


if __name__ == "__main__":
    main()
