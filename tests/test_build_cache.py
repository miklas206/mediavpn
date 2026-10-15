"""Exercise the Dockerfile's real manifest command across code and metadata upgrades."""

import shlex
import subprocess
import sys
import tomllib
from pathlib import Path


def manifest_command():
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    line = next(
        line for line in dockerfile.splitlines() if line.startswith("RUN python -c ")
    )
    return [sys.executable, *shlex.split(line.removeprefix("RUN "))[1:]]


def test_dependency_manifest_is_stable_for_code_and_version_but_tracks_requirements(
    tmp_path,
):
    root = Path(__file__).resolve().parents[1]
    original = (root / "pyproject.toml").read_text()
    project = tomllib.loads(original)
    path = tmp_path / "pyproject.toml"
    path.write_text(original)
    command = manifest_command()
    subprocess.run(command, cwd=tmp_path, check=True)
    first = (tmp_path / "requirements.txt").read_bytes()
    requirements = first.decode().splitlines()
    assert set(requirements) == set(
        project["project"]["dependencies"] + project["build-system"]["requires"]
    )
    path.write_text(original.replace(project["project"]["version"], "99.0.0"))
    (tmp_path / "app.py").write_text("# changed UI/code must not affect dependencies")
    subprocess.run(command, cwd=tmp_path, check=True)
    assert (tmp_path / "requirements.txt").read_bytes() == first
    path.write_text(original.replace('"httpx>=0.28,<1"', '"httpx>=0.29,<1"'))
    subprocess.run(command, cwd=tmp_path, check=True)
    assert (tmp_path / "requirements.txt").read_bytes() != first
    path.write_text(original.replace('"setuptools>=75"', '"setuptools>=76"'))
    subprocess.run(command, cwd=tmp_path, check=True)
    assert (tmp_path / "requirements.txt").read_bytes() != first


def test_local_package_install_cannot_resolve_or_download_dependencies():
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    package_install = next(
        line for line in dockerfile.splitlines() if "--no-deps" in line
    )
    assert "--no-build-isolation" in package_install
    assert "--no-index" in package_install
    assert dockerfile.index("-r /tmp/requirements.txt") < dockerfile.index(
        "COPY mediavpn ./mediavpn"
    )
