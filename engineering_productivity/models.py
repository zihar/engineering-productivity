"""Tipe data bersama untuk statistik commit (dari GitLab API)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CommitStats:
    commits: int = 0
    additions: int = 0
    deletions: int = 0
    active_days: int = 0
    repos: int = 0
    # Detail per commit (untuk daftar commit & repo aktif di halaman detail engineer):
    # tiap item {sha, project_id, committed_date, additions, deletions, title}.
    commit_rows: list[dict] = field(default_factory=list)
