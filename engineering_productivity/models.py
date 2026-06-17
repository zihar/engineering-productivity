"""Tipe data bersama untuk statistik commit (dari GitLab API)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CommitStats:
    commits: int = 0
    additions: int = 0
    deletions: int = 0
    active_days: int = 0
    repos: int = 0
