"""Dashboard Streamlit untuk engineering-productivity.

Jalankan:
    export CLICKUP_TOKEN=pk_...        # dan GITLAB_TOKEN=glpat-... bila pakai sumber GitLab
    streamlit run dashboard.py

Membaca config.yaml (atau path di env EP_CONFIG). Memakai pipeline yang sama
dengan CLI (engineering_productivity.pipeline.gather_report), hasilnya di-cache.
"""

from __future__ import annotations

import dataclasses
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

# Pastikan paket lokal bisa diimpor apa pun launcher-nya (streamlit run / AppTest).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from engineering_productivity.config import Config, ConfigError, load_config
from engineering_productivity.gitlab import GitLabClient
from engineering_productivity.metrics import ReportData
from engineering_productivity.pipeline import GatherOptions, gather_report
from engineering_productivity.report import render_markdown
from engineering_productivity.store import Store

CONFIG_PATH = os.environ.get("EP_CONFIG", "config.yaml")

_WIB = timezone(timedelta(hours=7))


def _ms_to_date(ms) -> str:
    """Epoch-ms (string/int) -> 'YYYY-MM-DD' WIB; '' bila kosong."""
    if ms in (None, "", 0, "0"):
        return ""
    try:
        return datetime.fromtimestamp(int(ms) / 1000, _WIB).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return ""


def _num(v) -> float:
    try:
        return float(v) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _engineer_emails(cfg: Config, name: str) -> list[str]:
    """Email author commit untuk satu engineer: canonical + alias (lowercase)."""
    eng = next((e for e in cfg.engineers if e.name == name), None)
    if not eng or not eng.email:
        return []
    canon = eng.email.lower()
    aliases = cfg.gitlab.aliases.items() if cfg.gitlab else []
    extra = {a.lower() for a, c in aliases if c.lower() == canon}
    return sorted({canon} | extra)


@st.cache_resource(show_spinner=False)
def _store(dsn: str):
    try:
        return Store.connect(dsn)
    except Exception:  # noqa: BLE001 — DB tak terjangkau -> detail dinonaktifkan
        return None


@st.cache_data(show_spinner=False)
def _detail_open(dsn: str, engineer_id: int) -> list[dict]:
    s = _store(dsn)
    return s.get_open_tasks([engineer_id]) if s else []


@st.cache_data(show_spinner=False)
def _detail_done(dsn: str, engineer_id: int, since_ms: int, until_ms: int) -> list[dict]:
    s = _store(dsn)
    return s.get_completed_tasks([engineer_id], since_ms, until_ms) if s else []


@st.cache_data(show_spinner=False)
def _detail_repos(dsn: str, emails: tuple[str, ...], since: str, until: str) -> list[dict]:
    s = _store(dsn)
    return s.get_commits_per_repo(list(emails), since, until) if s else []


@st.cache_data(show_spinner="Resolusi nama repo ...")
def _repo_names(dsn: str, url: str | None, token: str | None, project_ids: tuple[str, ...]) -> dict[str, dict]:
    """project_id -> {path, web_url}; cache di ep_projects, resolve lazy via GitLab."""
    s = _store(dsn)
    if not s or not project_ids:
        return {}
    names = dict(s.get_projects(list(project_ids)))
    missing = [p for p in project_ids if p not in names]
    if missing and url and token:
        gl = GitLabClient(url, token)
        for pid in missing:
            meta = gl.get_project(pid)
            if meta:
                rec = {"path": meta.get("path_with_namespace"), "name": meta.get("name"),
                       "web_url": meta.get("web_url")}
                s.put_project(pid, rec["path"], rec["name"], rec["web_url"])
                names[pid] = rec
        s.commit()
    return names

st.set_page_config(page_title="Engineering Productivity", page_icon="📊", layout="wide")

# Sumber data tiap kolom — dipakai sebagai tooltip "?" di header tabel.
COLUMN_HELP = {
    "Engineer": "Nama dari daftar engineer (member ClickUp).",
    "Selesai": "ClickUp — task berstatus done dengan tanggal selesai dalam periode.",
    "Selesai terakhir": "ClickUp — tanggal task terakhir berstatus done (lintas periode, mode --last-done).",
    "Lead median (hari)": "ClickUp — median (tanggal selesai − tanggal dibuat).",
    "Cycle median (hari)": "ClickUp time_in_status (mode Deep) — median waktu di status aktif.",
    "Commits": "GitLab — jumlah commit (dicocokkan via email penulis).",
    "Hari aktif": "GitLab — jumlah hari yang ada commit.",
    "Repo": "GitLab — jumlah repo yang disentuh.",
    "+Baris": "GitLab — baris ditambah (mentah, atau tanpa noise bila filter aktif).",
    "-Baris": "GitLab — baris dihapus (mentah, atau tanpa noise bila filter aktif).",
    "WIP": "ClickUp — jumlah task open (belum done) yang di-assign ke engineer.",
    "Story point": "ClickUp — field native 'points' (sprint point) dari task selesai + open.",
    "Skor": "Dihitung — rata-rata percentile lintas sinyal (0–100; makin rendah = makin underutilized).",
    "Sinyal rendah": "Sinyal di mana engineer ada di sepertiga terbawah tim.",
}
_NUMERIC = {"Selesai", "Lead median (hari)", "Cycle median (hari)", "Commits",
            "Hari aktif", "Repo", "+Baris", "-Baris", "WIP", "Story point"}


def cols(df: pd.DataFrame) -> dict:
    """Bangun column_config (tooltip sumber data + format) untuk kolom yang ada di df."""
    cfg = {}
    for c in df.columns:
        h = COLUMN_HELP.get(c)
        if c == "Skor":
            cfg[c] = st.column_config.ProgressColumn(c, help=h, min_value=0, max_value=100, format="%.0f")
        elif c in _NUMERIC:
            cfg[c] = st.column_config.NumberColumn(c, help=h)
        else:
            cfg[c] = st.column_config.TextColumn(c, help=h)
    return cfg


def topn_bar(df: pd.DataFrame, col: str, n: int, top: bool, title: str):
    """Bar horizontal Top/Bottom-N untuk satu metrik (skalabel ke banyak engineer)."""
    d = df[["Engineer", col]].dropna().sort_values(col, ascending=not top).head(n)
    fig = px.bar(d, x=col, y="Engineer", orientation="h", title=title, text=col)
    fig.update_layout(
        yaxis={"categoryorder": "total ascending" if top else "total descending"},
        height=max(280, 26 * len(d) + 80), margin=dict(t=40, b=10),
    )
    return fig


@st.cache_data(show_spinner="Menarik data dari ClickUp/GitLab ...")
def gather_cached(
    config_path: str,
    engineer_names: tuple[str, ...],
    since: str,
    until: str,
    deep: bool,
    max_age: int | None,
    no_discover: bool,
    exclude_noise: bool,
    last_done: bool,
) -> ReportData:
    cfg = load_config(config_path)
    if engineer_names:
        chosen = set(engineer_names)
        cfg = dataclasses.replace(cfg, engineers=[e for e in cfg.engineers if e.name in chosen])
    opts = GatherOptions(
        since=since, until=until, deep=deep, max_age=max_age,
        no_discover=no_discover, exclude_noise=exclude_noise, last_done=last_done,
    )  # tz=+7, utilisasi & commit GitLab selalu nyala (default)
    return gather_report(cfg, opts)


def summary_frame(data: ReportData) -> pd.DataFrame:
    rows = [{
        "Engineer": e.name,
        "Selesai": e.completed,
        **({"Selesai terakhir": e.last_done_date or "—"} if data.has_last_done else {}),
        "Lead median (hari)": e.lead_median,
        "Cycle median (hari)": e.cycle_median if e.cycle_times_days else None,
        "Commits": e.commits,
        "Hari aktif": e.active_days,
        "Repo": e.repos_touched,
    } for e in data.engineers]
    return pd.DataFrame(rows)


def weekly_frame(data: ReportData) -> pd.DataFrame:
    rows = {e.name: {w: e.per_week.get(w, 0) for w in data.weeks} for e in data.engineers}
    return pd.DataFrame(rows).T  # baris=engineer, kolom=minggu


def bottleneck_frame(data: ReportData) -> pd.DataFrame:
    return pd.DataFrame([{
        "Status": b.status,
        "Median (jam)": b.median_hours,
        "p90 (jam)": b.p90_hours,
        "Rata-rata (jam)": b.avg_hours,
        "Jumlah task": b.count,
    } for b in data.status_flow])


def util_frame(data: ReportData) -> pd.DataFrame:
    rows = [{
        "Engineer": e.name,
        "Skor": e.utilization_score,
        "WIP": e.open_tasks,
        "Hari aktif": e.active_days,
        "Selesai": e.completed,
        "Story point": e.story_points,
        "Sinyal rendah": ", ".join(e.low_signals) or "—",
    } for e in data.engineers]
    df = pd.DataFrame(rows)
    return df.sort_values("Skor", na_position="last").reset_index(drop=True)


# ---------------------------------------------------------------- sidebar
try:
    base_config = load_config(CONFIG_PATH)
except ConfigError as exc:
    st.error(f"Konfigurasi belum siap: {exc}")
    st.stop()

st.sidebar.title("⚙️ Filter")
chapters = sorted({e.chapter for e in base_config.engineers if e.chapter})
if chapters:
    sel_chapters = st.sidebar.multiselect("Chapter", chapters, default=chapters)
    names_in_scope = [e.name for e in base_config.engineers if e.chapter in sel_chapters or not e.chapter]
else:
    names_in_scope = [e.name for e in base_config.engineers]
sel_names = st.sidebar.multiselect("Engineer", names_in_scope, default=names_in_scope)

today = date.today()
default_start = today - timedelta(days=30)
rng = st.sidebar.date_input("Periode", value=(default_start, today), max_value=today)
if isinstance(rng, tuple) and len(rng) == 2:
    since_d, until_d = rng
else:
    since_d, until_d = default_start, today

deep = st.sidebar.toggle("Deep (cycle time & bottleneck)", value=False, help="Lebih lambat: 1 API call per task")
max_age_in = st.sidebar.number_input("Abaikan task basi > N hari (0 = nonaktif)", value=60, min_value=0, step=10)
no_discover = st.sidebar.toggle("Jangan auto-discover repo", value=False)
exclude_noise = st.sidebar.toggle("Filter file noise (+/- baris)", value=False, help="Lebih lambat: ambil diff tiap commit")
last_done = st.sidebar.toggle("Tanggal selesai terakhir", value=False, help="Query ekstra: kapan tiap engineer terakhir menutup task (lintas periode)")

if st.sidebar.button("🔄 Refresh data", width="stretch"):
    gather_cached.clear()
    st.rerun()

# ---------------------------------------------------------------- body
st.title("📊 Engineering Productivity")

if not sel_names:
    st.warning("Pilih minimal satu engineer di sidebar.")
    st.stop()

try:
    data = gather_cached(
        CONFIG_PATH, tuple(sel_names),
        since_d.isoformat(), until_d.isoformat(),
        deep, (max_age_in or None), no_discover, exclude_noise, last_done,
    )
except Exception as exc:  # noqa: BLE001 — tampilkan error apa pun ke UI
    st.error(f"Gagal menarik data: {exc}")
    st.stop()

# KPI
c1, c2, c3, c4 = st.columns(4)
c1.metric("Total task selesai", data.total_tasks)
c2.metric("Engineer", len(data.engineers))
total_commits = sum(e.commits for e in data.engineers)
c3.metric("Total commit", total_commits if data.has_commit_data else "—")
c4.metric("Periode", f"{data.since} → {data.until}")

if data.has_commit_data:
    st.caption(f"Sumber commit: {data.commit_source}")
if data.max_age_days is not None and data.filtered_stale:
    st.caption(f"🧹 {data.filtered_stale} task basi (lead time > {data.max_age_days} hari) diabaikan.")

summary = summary_frame(data)
emap = {e.name: e for e in data.engineers}


def _slider(label, total, key, default=15):
    hi = max(1, total)
    return st.slider(label, 1, hi, min(default, hi), key=key) if hi > 1 else hi


# ===================================================================== 1) UTILIZATION (paling atas)
st.header("🎯 Engineer Utilization")
if data.has_utilization:
    uf = util_frame(data)
    st.caption(
        f"Skor 0–100 relatif tim (sinyal: {', '.join(data.utilization_signals) or '—'}). "
        "Makin rendah = makin underutilized. **Bukan vonis kinerja** — pemicu obrolan kapasitas."
    )
    flagged = uf[uf["Skor"].notna() & (uf["Skor"] <= 33.3)]
    if len(flagged):
        st.warning("⚠️ **Perlu perhatian** (sepertiga terbawah): "
                   + ", ".join(f"{r.Engineer} ({r.Skor:.0f})" for r in flagged.itertuples()))
    else:
        st.success("Tidak ada engineer di sepertiga terbawah.")
    uc1, uc2 = st.columns([3, 2])
    with uc1:
        n = _slider("Tampilkan N skor terendah", len(uf), "util_n")
        st.plotly_chart(topn_bar(uf, "Skor", n, top=False, title=f"{n} skor terendah"), width="stretch")
    with uc2:
        st.plotly_chart(px.histogram(uf, x="Skor", nbins=10, title="Distribusi skor"), width="stretch")
    st.dataframe(uf, column_config=cols(uf), width="stretch", hide_index=True)
else:
    st.info("Nyalakan **Analisis utilisasi** di sidebar untuk skor utilisasi & daftar engineer underutilized.")

# ===================================================================== 2) RINGKASAN & METRIK (tabel-sentris)
st.header("📋 Ringkasan & Metrik")
metric_opts = [c for c in summary.columns if c in _NUMERIC]
if metric_opts:
    m1, m2, m3 = st.columns([2, 1, 2])
    metric = m1.selectbox("Metrik", metric_opts,
                          index=metric_opts.index("Selesai") if "Selesai" in metric_opts else 0)
    top = m2.radio("Urutan", ["Top", "Bottom"], horizontal=True) == "Top"
    n2 = m3.slider("N", 1, max(1, len(summary)), min(15, len(summary)), key="sum_n") if len(summary) > 1 else 1
    st.plotly_chart(topn_bar(summary, metric, n2, top, f"{'Top' if top else 'Bottom'} {n2} — {metric}"), width="stretch")
st.dataframe(summary, column_config=cols(summary), width="stretch", hide_index=True)

# ===================================================================== 3) MATRIKS TASK vs COMMIT
if data.has_commit_data:
    st.header("🔭 Matriks Task vs Commit")
    sc = pd.DataFrame([{"Engineer": e.name, "Selesai": e.completed, "Hari aktif": e.active_days,
                        "Skor": e.utilization_score} for e in data.engineers])
    t_med, a_med = sc["Selesai"].median(), sc["Hari aktif"].median()
    color = "Skor" if (data.has_utilization and sc["Skor"].notna().any()) else "Selesai"
    fig = px.scatter(sc, x="Selesai", y="Hari aktif", hover_name="Engineer", color=color,
                     color_continuous_scale="RdYlGn",
                     labels={"Selesai": "Task selesai (ClickUp)", "Hari aktif": "Hari aktif commit (GitLab)"})
    fig.add_vline(x=t_med, line_dash="dash", line_color="gray")
    fig.add_hline(y=a_med, line_dash="dash", line_color="gray")
    fig.update_traces(marker=dict(size=11))
    fig.update_layout(height=480, margin=dict(t=30))
    st.plotly_chart(fig, width="stretch")
    st.caption(f"Hover untuk nama. Garis = median (task {t_med:g}, hari aktif {a_med:g}). "
               "Kanan-bawah = banyak task sedikit commit; kiri-atas = aktif ngoding jarang update task.")

# ===================================================================== 4) THROUGHPUT MINGGUAN (total tim)
if data.weeks:
    st.header("📈 Throughput per minggu (total tim)")
    st.bar_chart(weekly_frame(data).sum(axis=0))
    st.caption("Total task selesai tim per minggu. Rincian per engineer ada di drill-down di bawah.")

# ===================================================================== 5) BOTTLENECK
if data.deep and data.status_flow:
    st.header("🧱 Bottleneck (median jam per status)")
    bf = bottleneck_frame(data)
    st.plotly_chart(px.bar(bf.sort_values("Median (jam)"), x="Median (jam)", y="Status",
                           orientation="h", height=max(280, 26 * len(bf) + 80)), width="stretch")
    st.dataframe(bf, width="stretch", hide_index=True)

# ===================================================================== 6) DRILL-DOWN per engineer
st.header("🔎 Detail per engineer")
who = st.selectbox("Pilih engineer", [e.name for e in data.engineers])
e = emap[who]
d1, d2, d3, d4 = st.columns(4)
d1.metric("Task selesai", e.completed)
d2.metric("Commits", e.commits if data.has_commit_data else "—")
d3.metric("Hari aktif", e.active_days if data.has_commit_data else "—")
d4.metric("WIP", e.open_tasks if data.has_utilization else "—")
extra = []
if data.has_utilization and e.utilization_score is not None:
    s = f"Skor utilisasi **{e.utilization_score:.0f}**"
    if e.low_signals:
        s += f" · sinyal rendah: {', '.join(e.low_signals)}"
    extra.append(s)
if data.has_last_done:
    extra.append(f"Selesai terakhir: **{e.last_done_date or '—'}**")
if e.cycle_times_days:
    extra.append(f"Cycle median: **{e.cycle_median}** hari")
if data.has_commit_data:
    note = "tanpa noise" if data.commit_noise_filtered else "mentah"
    extra.append(f"±baris ({note}): +{e.commit_additions}/-{e.commit_deletions} · {e.repos_touched} repo")
if extra:
    st.markdown(" · ".join(extra))
if data.weeks:
    st.bar_chart(pd.Series({w: e.per_week.get(w, 0) for w in data.weeks}))

# --- tab detail: tiket open, tiket selesai, commit per repo (query store on-demand) ---
_dsn = base_config.store_dsn
if not _dsn:
    st.info("Aktifkan `store.dsn` di config.yaml untuk melihat daftar tiket & commit per repo.")
elif _store(_dsn) is None:
    st.warning("Store DB tak terjangkau — detail tiket & commit per repo tidak tersedia.")
else:
    _gl = base_config.gitlab
    _gl_url = _gl.url if _gl else None
    _gl_token = _gl.token if _gl else None
    tab_open, tab_done, tab_repo, tab_push = st.tabs(
        ["📋 Tiket open (WIP)", "✅ Tiket selesai", "📦 Commit per repo", "🗂️ Repo (push history)"]
    )

    with tab_open:
        opens = _detail_open(_dsn, e.engineer_id)
        st.caption(f"{len(opens)} tiket open (snapshot, status belum selesai) — engineer sebagai Developer.")
        if opens:
            df_open = pd.DataFrame([{
                "Tiket": t.get("name") or t.get("id"),
                "Status": (t.get("status") or {}).get("status") or "",
                "Story point": _num(t.get("points")),
                "Dibuat": _ms_to_date(t.get("date_created")),
                "Update terakhir": _ms_to_date(t.get("date_updated")),
                "Link": t.get("url") or "",
            } for t in opens])
            st.dataframe(df_open, width="stretch", hide_index=True, column_config={
                "Link": st.column_config.LinkColumn("Link", display_text="buka ↗"),
                "Story point": st.column_config.NumberColumn(format="%.0f"),
            })

    with tab_done:
        since_ms = int(datetime.combine(since_d, datetime.min.time(), _WIB).timestamp() * 1000)
        until_ms = int(datetime.combine(until_d, datetime.max.time(), _WIB).timestamp() * 1000)
        dones = _detail_done(_dsn, e.engineer_id, since_ms, until_ms)
        st.caption(f"{len(dones)} tiket selesai pada {data.since} … {data.until}.")
        if dones:
            rows_done = []
            for t in dones:
                dd = t.get("date_done") or t.get("date_closed")
                dc = t.get("date_created")
                lead = None
                if dd and dc:
                    try:
                        lead = round((int(dd) - int(dc)) / 86_400_000, 1)
                    except (TypeError, ValueError):
                        lead = None
                rows_done.append({
                    "Tiket": t.get("name") or t.get("id"),
                    "Status": (t.get("status") or {}).get("status") or "",
                    "Story point": _num(t.get("points")),
                    "Selesai": _ms_to_date(dd),
                    "Lead time (hari)": lead,
                    "Link": t.get("url") or "",
                })
            st.dataframe(pd.DataFrame(rows_done), width="stretch", hide_index=True, column_config={
                "Link": st.column_config.LinkColumn("Link", display_text="buka ↗"),
                "Story point": st.column_config.NumberColumn(format="%.0f"),
                "Lead time (hari)": st.column_config.NumberColumn(format="%.1f"),
            })

    with tab_repo:
        emails = _engineer_emails(base_config, who)
        if not emails:
            st.info("Engineer ini tidak punya email di config — tidak bisa memetakan commit.")
        else:
            repos = _detail_repos(_dsn, tuple(emails), data.since, data.until)
            st.caption(f"{len(repos)} repo dengan commit pada {data.since} … {data.until}. Email: {', '.join(emails)}")
            if repos:
                gl = base_config.gitlab
                names = _repo_names(
                    _dsn, gl.url if gl else None, gl.token if gl else None,
                    tuple(r["project_id"] for r in repos),
                )
                df_repo = pd.DataFrame([{
                    "Repo": (names.get(r["project_id"]) or {}).get("path") or f"#{r['project_id']}",
                    "Commit": r["commits"],
                    "+baris": r["additions"],
                    "-baris": r["deletions"],
                    "Pertama": (r["first_commit"] or "")[:10],
                    "Terakhir": (r["last_commit"] or "")[:10],
                    "Link": (names.get(r["project_id"]) or {}).get("web_url") or "",
                } for r in repos])
                st.dataframe(df_repo, width="stretch", hide_index=True, column_config={
                    "Link": st.column_config.LinkColumn("Link", display_text="buka ↗"),
                })

    with tab_push:
        pushed = data.engineer_repos.get(who, [])
        st.caption(f"{len(pushed)} repo pernah di-push (riwayat discovery kumulatif, lintas periode).")
        if pushed:
            names = _repo_names(_dsn, _gl_url, _gl_token, tuple(r["project_id"] for r in pushed))
            df_push = pd.DataFrame([{
                "Repo": (names.get(r["project_id"]) or {}).get("path") or f"#{r['project_id']}",
                "Pertama push": r["first_seen"],
                "Terakhir push": r["last_seen"],
                "Link": (names.get(r["project_id"]) or {}).get("web_url") or "",
            } for r in pushed])
            st.dataframe(df_push, width="stretch", hide_index=True, column_config={
                "Link": st.column_config.LinkColumn("Link", display_text="buka ↗"),
            })
        else:
            st.caption("Mapping belum tersedia — jalankan sekali dengan auto-discover aktif & store DB.")

# Download Markdown
now = datetime.now(timezone(timedelta(hours=7)))  # WIB
md = render_markdown(data, generated_at=now.strftime("%Y-%m-%d %H:%M %Z"))
st.download_button("⬇️ Download laporan Markdown", md, file_name="report.md", mime="text/markdown")
