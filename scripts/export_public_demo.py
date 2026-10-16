"""Package only the public lab and the exact Python modules it imports.

The output must be a new private staging directory. Medical corpus files,
provider configuration and the full app dependency stack are not deployed.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
SOURCES = [
    "demo/live.py", "backend/__init__.py", "backend/vectorstore/__init__.py",
    "backend/vectorstore/retrieval.py", "backend/contracts/__init__.py",
    "backend/contracts/models.py", "backend/contracts/ports.py", "backend/contracts/registry.py",
    "backend/app/__init__.py", "backend/app/retrieval/__init__.py", "backend/app/retrieval/rarity.py",
    "backend/app/corpus/__init__.py", "backend/app/corpus/provenance.py",
    "backend/app/corpus/versioning.py", "backend/app/corpus/retention.py",
    "backend/app/verify/__init__.py", "backend/app/verify/grounding.py",
]


def export(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    for name in SOURCES:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    shutil.copytree(ROOT / "public-proof", destination / "site", ignore=shutil.ignore_patterns("api", "__pycache__"))
    shutil.copytree(ROOT / "public-proof/api", destination / "api", ignore=shutil.ignore_patterns("__pycache__"))
    for filename in ("retrieval_eval.json", "grounding_eval.json"):
        shutil.copy2(ROOT / "backend/measurement/results" / filename, destination / "site" / filename)
    (destination / "requirements.txt").write_text("PyYAML==6.0.3\n")
    (destination / ".python-version").write_text("3.12\n")
    (destination / "vercel.json").write_text(json.dumps({
        "buildCommand": "", "installCommand": "true", "outputDirectory": "site",
        "functions": {"api/search.py": {"maxDuration": 10, "includeFiles": "{backend/**,demo/**}"}},
        "headers": [{"source": "/(.*)", "headers": [
            {"key": "X-Content-Type-Options", "value": "nosniff"},
            {"key": "Referrer-Policy", "value": "no-referrer"},
        ]}],
    }, indent=2))
    hashes = {str(p.relative_to(destination)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(destination.rglob("*")) if p.is_file()}
    (destination / "source-manifest.json").write_text(json.dumps(hashes, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    export(parser.parse_args().output)
