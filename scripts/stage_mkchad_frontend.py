"""Stage the hash-pinned upstream 0.2.5 frontend in a disposable build checkout.

This backend-only release path rejects frontend-source changes and existing
staging output. It never installs a package or touches a live Jupyter environment.
"""

import argparse
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path


def stage_frontend(wheel: Path, root: Path) -> None:
    """Verify the pinned wheel and unchanged frontend, then stage its assets.

    wheel is the upstream distribution downloaded with the pinned requirements;
    root is a disposable source checkout. Writes only its ignored labextension
    directory. Raises ValueError on changed inputs, wrong hashes or existing output,
    and CalledProcessError if Git cannot validate the baseline. No network is used.
    """
    config = json.loads((root / ".github/mkchad-release.json").read_text())["frontend"]
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != config["wheel_sha256"]:
        raise ValueError("Upstream frontend wheel hash mismatch")
    baseline = config["source_commit"]
    subprocess.run(
        [
            "git",
            "diff",
            "--exit-code",
            baseline,
            "--",
            "src",
            "style",
            "schema",
            "yarn.lock",
            "tsconfig.json",
        ],
        cwd=root,
        check=True,
    )
    original = json.loads(
        subprocess.check_output(["git", "show", f"{baseline}:package.json"], cwd=root, text=True)
    )
    current = json.loads((root / "package.json").read_text())
    original.pop("version")
    current.pop("version")
    if original != current:
        raise ValueError("Frontend package metadata changed; qualify a source frontend build")
    target = root / "jupyter_server_documents/labextension"
    if target.exists():
        raise ValueError("Use a clean disposable checkout; labextension already exists")
    with zipfile.ZipFile(wheel) as archive:
        prefix = (
            "jupyter_server_documents-0.2.5.data/data/share/jupyter/labextensions/"
            "@jupyter-ai-contrib/server-documents/"
        )
        assets = {
            name[len(prefix) :]: archive.read(name)
            for name in archive.namelist()
            if name.startswith(prefix)
            and not name.endswith("/")
            and not name.endswith("/install.json")
        }
    if (
        json.loads(assets["package.json"])["version"] != config["version"]
        or "static/style.js" not in assets
    ):
        raise ValueError("Missing or incorrect published frontend assets")
    for name, content in assets.items():
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)


def main() -> None:
    """Stage --wheel in the current disposable checkout; validation errors propagate."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, type=Path)
    args = parser.parse_args()
    stage_frontend(args.wheel, Path.cwd())


if __name__ == "__main__":
    main()
