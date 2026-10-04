"""Profile loading — YAML -> :class:`Profile`.

A *profile* is the plug-and-play unit: point prospector at any niche by dropping
a new ``profiles/<name>.yaml`` next to the others. No code changes.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml

from prospector.models import (
    CommentConfig,
    EvidenceThresholds,
    LexiconRule,
    Profile,
)


def _candidate_dirs(profiles_dir: Optional[Path]) -> list[Path]:
    """Ordered list of directories to search for profiles."""
    dirs: list[Path] = []
    if profiles_dir is not None:
        dirs.append(Path(profiles_dir))
    env = os.environ.get("PROSPECTOR_PROFILES_DIR")
    if env:
        dirs.append(Path(env))
    dirs.append(Path.cwd() / "profiles")
    # repo layout: <repo>/profiles  (this file is <repo>/prospector/profiles.py)
    dirs.append(Path(__file__).resolve().parent.parent / "profiles")
    # installed layout: profiles shipped inside the package as _profiles
    dirs.append(Path(__file__).resolve().parent / "_profiles")
    # de-dupe, preserve order
    seen: set[str] = set()
    out: list[Path] = []
    for d in dirs:
        key = str(d)
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def resolve_profiles_dir(profiles_dir: Optional[Path] = None) -> Path:
    """Return the first existing profiles directory."""
    for d in _candidate_dirs(profiles_dir):
        if d.is_dir():
            return d
    # fall back to cwd/profiles even if missing, for clear error messages
    return Path.cwd() / "profiles"


def list_profiles(profiles_dir: Optional[Path] = None) -> list[str]:
    """List available profile names (without the .yaml extension)."""
    found: dict[str, None] = {}
    for d in _candidate_dirs(profiles_dir):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.yaml")):
            found.setdefault(f.stem, None)
        for f in sorted(d.glob("*.yml")):
            found.setdefault(f.stem, None)
    return list(found.keys())


def _profile_from_dict(data: dict) -> Profile:
    lexicon = [
        LexiconRule(pattern=str(r["pattern"]), weight=float(r.get("w", r.get("weight", 1))))
        for r in (data.get("pain_lexicon") or [])
    ]
    ev = data.get("evidence") or {}
    evidence = EvidenceThresholds(
        min_items=int(ev.get("min_items", 5)),
        min_subreddits=int(ev.get("min_subreddits", 3)),
        min_authors=int(ev.get("min_authors", 3)),
    )
    cm = data.get("comments") or {}
    comments = CommentConfig(
        max_per_thread=int(cm.get("max_per_thread", 40)),
        min_score=int(cm.get("min_score", 2)),
        depth=int(cm.get("depth", 2)),
    )
    return Profile(
        name=str(data["name"]),
        description=str(data.get("description", "")),
        subreddits=[str(s) for s in (data.get("subreddits") or [])],
        search_terms=[str(s) for s in (data.get("search_terms") or [])],
        time_window=str(data.get("time_window", "year")),
        listing_limit=int(data.get("listing_limit", 100)),
        max_threads=int(data.get("max_threads", 60)),
        pain_lexicon=lexicon,
        pain_threshold=float(data.get("pain_threshold", 3.0)),
        evidence=evidence,
        comments=comments,
        rss_comment_threads=int(data.get("rss_comment_threads", 10)),
    )


def load_profile(name_or_path: str, profiles_dir: Optional[Path] = None) -> Profile:
    """Load a profile by name (looked up in the profiles dirs) or by file path."""
    p = Path(name_or_path)
    if p.suffix in {".yaml", ".yml"} and p.is_file():
        path = p
    else:
        path = None
        for d in _candidate_dirs(profiles_dir):
            for ext in (".yaml", ".yml"):
                cand = d / f"{name_or_path}{ext}"
                if cand.is_file():
                    path = cand
                    break
            if path:
                break
        if path is None:
            available = ", ".join(list_profiles(profiles_dir)) or "(none found)"
            raise FileNotFoundError(
                f"Profile '{name_or_path}' not found. Available: {available}"
            )
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if "name" not in data:
        data["name"] = path.stem
    return _profile_from_dict(data)
