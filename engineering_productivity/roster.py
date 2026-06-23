"""Roster engineer (siapa ditampilkan + chapter) — sumber kebenaran di DB.

config.yaml hanya dipakai sebagai seed awal bila tabel ep_engineers masih kosong;
setelah itu roster dikelola lewat editor di dashboard (tersimpan ke DB).
Tanpa store_dsn → fallback ke config.engineers (perilaku lama).
"""

from __future__ import annotations

from .config import Config, Engineer
from .store import Store, StoreError


def _seed_from_config(store: Store, config: Config) -> list[dict]:
    """Isi ep_engineers dari config.engineers (resolve id via member workspace)."""
    members = store.get_workspace_members()
    by_email = {(m.get("email") or "").lower(): m for m in members}
    rows: list[dict] = []
    for e in config.engineers:
        member = by_email.get((e.email or "").lower()) if e.email else None
        eid = e.id or (member.get("id") if member else None)
        if eid is None:
            continue  # tak bisa di-resolve ke id ClickUp → lewati
        rows.append({
            "engineer_id": int(eid),
            "email": e.email or (member.get("email") if member else None),
            "name": e.name,
            "chapter": e.chapter,
            "active": True,
        })
    if rows:
        store.upsert_engineers(rows)
        store.commit()
    return rows


def effective_engineers(config: Config) -> list[Engineer]:
    """Daftar engineer aktif dari DB (seed dari config bila kosong); fallback config."""
    if not config.store_dsn:
        return config.engineers
    try:
        store = Store.connect(config.store_dsn)
    except StoreError:
        return config.engineers
    try:
        rows = store.get_engineers(active_only=True)
        if not rows:
            rows = _seed_from_config(store, config)
        return [
            Engineer(name=r["name"], email=r["email"], id=r["engineer_id"], chapter=r["chapter"])
            for r in rows
        ]
    finally:
        store.close()
