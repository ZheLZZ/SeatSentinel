"""Offline release manifest and exact dependency-version checks (stdlib only)."""
from __future__ import annotations

import argparse
import ast
import importlib.metadata
import json
import re
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
SECTIONS = ("light_files", "full_documents", "full_assets")


def manifest_files(root: Path, manifest: dict, section: str) -> list[str]:
    files = manifest[section]
    if not isinstance(files, list) or not files:
        raise ValueError(f"Empty or invalid release section: {section}")
    seen: set[str] = set()
    for relative in files:
        if not isinstance(relative, str):
            raise ValueError("Manifest paths must be strings")
        path = PurePosixPath(relative)
        if (path.is_absolute() or "\\" in relative or ":" in relative or
                ".." in path.parts or "." in relative.split("/") or
                relative.casefold() in seen):
            raise ValueError(f"Unsafe or duplicate manifest path: {relative}")
        seen.add(relative.casefold())
        if set(path.parts) & {".venv", "dist", "build", "logs", "__pycache__"} or path.name in {
            "settings.json", "registered-face.dat", "private-test-unlock.json",
        } or path.suffix in {".log", ".pyc", ".tmp"}:
            raise ValueError(f"Private or generated file in release manifest: {relative}")
        resolved = root.joinpath(*path.parts).resolve()
        if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
            raise ValueError(f"Missing or escaping release file: {relative}")
        current = root
        for part in path.parts:
            current = current / part
            if current.is_symlink() or current.is_junction():
                raise ValueError(f"Linked file in release manifest: {relative}")
    return files


def validate_manifests(root: Path = ROOT) -> None:
    release = json.loads((root / "release-manifest.json").read_text(encoding="utf-8-sig"))
    if release["schema_version"] != 1:
        raise ValueError("Unsupported release manifest version")
    sections = {section: manifest_files(root, release, section) for section in SECTIONS}
    light = set(sections["light_files"])
    # Check the reachable local imports, without including stray untracked .py
    # files in a release or depending on Git being present in source archives.
    pending = ["app.py"]
    checked: set[str] = set()
    while pending:
        name = pending.pop()
        if name in checked:
            continue
        checked.add(name)
        if name not in light:
            raise ValueError(f"Local application module missing from light package: {name}")
        tree = ast.parse((root / name).read_text(encoding="utf-8-sig"), filename=name)
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modules = [node.module.split(".")[0]]
            for module in modules:
                candidate = f"{module}.py"
                if (root / candidate).is_file():
                    pending.append(candidate)
    models = json.loads((root / "model-manifest.json").read_text(encoding="utf-8-sig"))
    if models["SchemaVersion"] != 1 or len(models["Models"]) != 3:
        raise ValueError("Invalid model manifest")
    names = set()
    for model in models["Models"]:
        if not re.fullmatch(r"[a-z0-9-]+", model["Name"]) or model["Name"] in names:
            raise ValueError("Invalid or duplicate model name")
        names.add(model["Name"])
        for kind in ("Xml", "Bin"):
            if not re.fullmatch(r"[0-9A-Fa-f]{64}", model[kind + "Sha256"]):
                raise ValueError("Invalid model hash")
            if model[kind + "MinimumBytes"] <= 0:
                raise ValueError("Invalid model minimum length")


def locked_versions(root: Path = ROOT, filename: str = "requirements-build.lock") -> dict[str, str]:
    pins: dict[str, str] = {}
    pending = [filename]
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        for line in (root / current).read_text(encoding="utf-8").splitlines():
            value = line.split("#", 1)[0].strip()
            if value.startswith("-r "):
                included = value[3:].strip()
                if "/" in included or "\\" in included or included.startswith("."):
                    raise ValueError("Requirement includes must stay at the project root")
                pending.append(included)
            elif "==" in value:
                package, version = value.split("==", 1)
                normalized = package.lower().replace("_", "-")
                if normalized in pins and pins[normalized] != version:
                    raise ValueError(f"Conflicting version pins: {package}")
                pins[normalized] = version
    return pins


def validate_environment() -> None:
    failures = []
    for package, expected in locked_versions().items():
        try:
            actual = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            actual = "not installed"
        if actual != expected:
            failures.append(f"{package}: expected {expected}, found {actual}")
    if failures:
        raise ValueError("Dependency lock mismatch:\n" + "\n".join(failures))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", action="store_true")
    arguments = parser.parse_args()
    validate_manifests()
    if arguments.environment:
        validate_environment()
    print("PASS: release manifests" + (" and locked dependency versions" if arguments.environment else ""))
