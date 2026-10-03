"""Deploy AsOf to a Hugging Face Space (Docker SDK, free CPU hardware).

What it does
  1. asks for your Hugging Face WRITE token (typed input is hidden, nothing is saved)
  2. creates the Space <your-username>/<name> if it does not exist
  3. stores GROQ_API_KEY and the NEO4J_* values from your local .env as Space *secrets*
     (they never appear in the uploaded files or in this window)
  4. uploads the code, the Dockerfile and the built index (data/processed + data/index)
  5. prints the Space URL - Hugging Face then builds the Docker image (~10-15 min)

Run:  deploy_hf.bat            (or: python scripts/deploy_hf.py --space asof)
"""
from __future__ import annotations

import argparse
import getpass
import os
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRETS = ("GROQ_API_KEY", "NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD", "NEO4J_DATABASE")
SPACE_README = """---
title: AsOf - RBI regulations on any date
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: Time-aware GraphRAG over RBI circulars
---

# AsOf

Ask what the RBI rules were on any date. Point-in-time hybrid retrieval, an amendment graph in
Neo4j, a LangGraph agent and citation checks. Not legal advice.

Source code and benchmark: see the project repository.
"""


def data_dir() -> Path:
    if os.getenv("ASOF_DATA_DIR"):
        return Path(os.environ["ASOF_DATA_DIR"])
    local = Path(os.getenv("LOCALAPPDATA", "")) / "asof" / "data"
    return local if local.exists() else ROOT / "data"


def stage_files(dest: Path, src_data: Path) -> int:
    for name in ("Dockerfile", "requirements.txt", "pyproject.toml", ".dockerignore"):
        shutil.copy2(ROOT / name, dest / name)
    shutil.copytree(ROOT / "src", dest / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
    (dest / "README.md").write_text(SPACE_README, encoding="utf-8")
    n = 0
    for sub, skip in (("processed", ()), ("index", ("embedding_cache.npz",))):
        src = src_data / sub
        if not src.exists():
            raise SystemExit(f"Missing {src} - run the build first (rebuild.bat).")
        out = dest / "data" / sub
        out.mkdir(parents=True, exist_ok=True)
        for f in src.iterdir():
            if f.is_file() and f.name not in skip:
                shutil.copy2(f, out / f.name)
                n += 1
    return n


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--space", default="asof", help="Space name (default: asof)")
    p.add_argument("--private", action="store_true", help="create the Space as private")
    a = p.parse_args()

    from dotenv import dotenv_values
    from huggingface_hub import HfApi

    token = os.getenv("HF_TOKEN") or getpass.getpass("Paste your Hugging Face WRITE token (hidden) and press Enter: ").strip()
    if not token:
        raise SystemExit("No token given.")
    api = HfApi(token=token)
    user = api.whoami()["name"]
    repo = f"{user}/{a.space}"
    print(f"Creating/using Space {repo} ...")
    api.create_repo(repo, repo_type="space", space_sdk="docker", private=a.private, exist_ok=True)

    env = dotenv_values(ROOT / ".env")
    for key in SECRETS:
        if env.get(key):
            api.add_space_secret(repo, key, env[key])
            print(f"  secret {key}: set")
    api.add_space_variable(repo, "TOP_K", "6")

    src_data = data_dir()
    print(f"Using index from {src_data}")
    with tempfile.TemporaryDirectory() as tmp:
        n = stage_files(Path(tmp), src_data)
        print(f"Uploading code + {n} index files (large files go through Git LFS automatically) ...")
        api.upload_folder(folder_path=tmp, repo_id=repo, repo_type="space", commit_message="Deploy AsOf")
    print("\nDone. Hugging Face is now building the app (about 10-15 minutes).")
    print(f"Open: https://huggingface.co/spaces/{repo}")


if __name__ == "__main__":
    main()
