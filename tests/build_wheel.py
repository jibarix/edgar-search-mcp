"""Build a test wheel of the working tree, with its record (plan v5, change 4).

    python -X utf8 tests/build_wheel.py <out folder> [--builder <python>]

The files a wheel is built from (pyproject.toml, MANIFEST.in, README.md,
LICENSE and the edgar_mcp package) are copied into an empty temp folder
first, so nothing stale in the repository (build/, *.egg-info) can reach the
wheel. The builder runs `pip wheel --no-deps --no-build-isolation --no-index`:
nothing is downloaded or installed. The record, build_record.json beside the
wheel, holds the builder's versions, the sha256 of every source file copied
and of every file in the wheel, and the wheel's own sha256. This is a test
build, not a release: a release wheel is built from a clean `git archive` of a
commit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOP_FILES = ["pyproject.toml", "MANIFEST.in", "README.md", "LICENSE"]
VERSIONS = ("import json, sys, pip, setuptools, wheel; print(json.dumps({'python': sys.version, "
            "'pip': pip.__version__, 'setuptools': setuptools.__version__, 'wheel': wheel.__version__}))")


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def copy_sources(dest):
    """Copy the build inputs into `dest`; their paths and hashes."""
    copied = {}
    for name in TOP_FILES:
        shutil.copy2(os.path.join(ROOT, name), os.path.join(dest, name))
        copied[name] = sha256(os.path.join(dest, name))
    for folder, dirs, files in os.walk(os.path.join(ROOT, "edgar_mcp")):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        rel = os.path.relpath(folder, ROOT)
        os.makedirs(os.path.join(dest, rel), exist_ok=True)
        for name in sorted(files):
            if name.endswith((".pyc", ".pyo")):
                continue
            path = os.path.join(rel, name)
            shutil.copy2(os.path.join(ROOT, path), os.path.join(dest, path))
            copied[path.replace(os.sep, "/")] = sha256(os.path.join(dest, path))
    return copied


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", help="an empty folder for the wheel and its record")
    parser.add_argument("--builder", default=sys.executable, help="the Python that builds the wheel")
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)
    if os.listdir(args.out):
        sys.exit(f"{args.out} is not empty")
    work = tempfile.mkdtemp(prefix="edgar_wheel_source_")
    copied = copy_sources(work)
    versions = subprocess.run([args.builder, "-c", VERSIONS], capture_output=True, text=True, check=True, cwd=work)
    build = subprocess.run([args.builder, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--no-index",
                            "--no-cache-dir", "-w", os.path.abspath(args.out), "."],
                           capture_output=True, text=True, cwd=work)
    print(build.stdout[-2000:], build.stderr[-2000:], sep="\n")
    wheels = [n for n in os.listdir(args.out) if n.endswith(".whl")]
    if build.returncode != 0 or len(wheels) != 1:
        sys.exit(f"the build failed (exit {build.returncode}, wheels {wheels})")
    wheel_path = os.path.join(args.out, wheels[0])
    with zipfile.ZipFile(wheel_path) as z:
        contents = {i.filename: hashlib.sha256(z.read(i)).hexdigest() for i in z.infolist() if not i.is_dir()}
    # Every file of the package in the wheel is the source file copied, byte for byte, and none is missing.
    package = {k: v for k, v in contents.items() if k.startswith("edgar_mcp/")}
    sources = {k: v for k, v in copied.items() if k.startswith("edgar_mcp/")}
    if package != sources:
        differ = sorted(set(package.items()) ^ set(sources.items()))
        sys.exit(f"the wheel's package files differ from the sources: {differ[:10]}")
    record = {
        "label": "test build, not a release",
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "builder": {"path": args.builder, **json.loads(versions.stdout)},
        "command": "pip wheel --no-deps --no-build-isolation --no-index --no-cache-dir -w <out> .",
        "sources": copied,
        "wheel": {"name": wheels[0], "sha256": sha256(wheel_path), "files": contents},
    }
    with open(os.path.join(args.out, "build_record.json"), "w", encoding="utf-8") as f:
        json.dump(record, f, indent=1)
    print(f"built {wheels[0]} sha256 {record['wheel']['sha256']}")


if __name__ == "__main__":
    main()
