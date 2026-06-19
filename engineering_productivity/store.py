"""Lapisan persistensi Postgres (cache) untuk data mahal & immutable.

Menyimpan:
  - time_in_status per task (done = immutable → aman dicache permanen)
  - commit per sha + rentang yang sudah ter-cover per project (untuk fetch incremental)

Opsional: bila DSN tak diset / DB tak terjangkau, pipeline fallback ke tarikan live.
Diakses lewat antarmuka kecil sehingga bisa di-fake saat test tanpa Postgres.
"""

from __future__ import annotations

try:
    import psycopg
    from psycopg.types.json import Json
except ImportError:  # driver opsional
    psycopg = None
    Json = None


class StoreError(Exception):
    pass


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ep_time_in_status (
    task_id    TEXT PRIMARY KEY,
    payload    JSONB NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ep_commits (
    sha            TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL,
    author_email   TEXT,
    committed_date timestamptz,
    additions      INT NOT NULL DEFAULT 0,
    deletions      INT NOT NULL DEFAULT 0,
    fetched_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ep_commits_proj_date ON ep_commits (project_id, committed_date);
CREATE TABLE IF NOT EXISTS ep_commit_sync (
    project_id     TEXT PRIMARY KEY,
    earliest_date  DATE NOT NULL,
    latest_date    DATE NOT NULL
);
CREATE TABLE IF NOT EXISTS ep_tasks (
    task_id       TEXT PRIMARY KEY,
    payload       JSONB NOT NULL,
    date_updated  BIGINT,
    developer_ids BIGINT[],
    date_done     BIGINT,
    status_type   TEXT,
    fetched_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ep_tasks_dev ON ep_tasks USING GIN (developer_ids);
CREATE TABLE IF NOT EXISTS ep_task_sync (
    scope     TEXT PRIMARY KEY,
    watermark BIGINT
);
CREATE TABLE IF NOT EXISTS ep_task_backfill (
    engineer_id BIGINT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS ep_projects (
    project_id TEXT PRIMARY KEY,
    path       TEXT,
    name       TEXT,
    web_url    TEXT,
    fetched_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ep_engineer_repos (
    engineer_email TEXT NOT NULL,
    project_id     TEXT NOT NULL,
    first_seen     DATE NOT NULL,
    last_seen      DATE NOT NULL,
    PRIMARY KEY (engineer_email, project_id)
);
CREATE TABLE IF NOT EXISTS ep_discovery_sync (
    engineer_email TEXT PRIMARY KEY,
    earliest_date  DATE NOT NULL,
    latest_date    DATE NOT NULL
);
"""


class Store:
    def __init__(self, conn):
        self.conn = conn

    @classmethod
    def connect(cls, dsn: str) -> "Store":
        if psycopg is None:
            raise StoreError("Driver psycopg tidak terpasang.")
        try:
            conn = psycopg.connect(dsn, connect_timeout=10)
            store = cls(conn)
            store.ensure_schema()
            return store
        except psycopg.Error as exc:  # type: ignore[union-attr]
            raise StoreError(str(exc)) from exc

    def ensure_schema(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(_SCHEMA)
        self.conn.commit()

    # ---------------------------------------------------------- time_in_status
    def get_time_in_status(self, task_ids: list[str]) -> dict[str, dict]:
        if not task_ids:
            return {}
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT task_id, payload FROM ep_time_in_status WHERE task_id = ANY(%s)",
                (list(task_ids),),
            )
            return {tid: payload for tid, payload in cur.fetchall()}

    def put_time_in_status(self, task_id: str, payload: dict) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO ep_time_in_status (task_id, payload) VALUES (%s, %s)
                   ON CONFLICT (task_id) DO UPDATE SET payload = EXCLUDED.payload, fetched_at = now()""",
                (task_id, Json(payload)),
            )

    # ----------------------------------------------------------------- commits
    def get_commit_coverage(self, project_id: str) -> tuple[str, str] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT earliest_date::text, latest_date::text FROM ep_commit_sync WHERE project_id = %s",
                (project_id,),
            )
            row = cur.fetchone()
            return (row[0], row[1]) if row else None

    def set_commit_coverage(self, project_id: str, earliest: str, latest: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO ep_commit_sync (project_id, earliest_date, latest_date) VALUES (%s, %s, %s)
                   ON CONFLICT (project_id) DO UPDATE
                   SET earliest_date = LEAST(ep_commit_sync.earliest_date, EXCLUDED.earliest_date),
                       latest_date   = GREATEST(ep_commit_sync.latest_date, EXCLUDED.latest_date)""",
                (project_id, earliest, latest),
            )

    def upsert_commits(self, rows: list[dict]) -> None:
        if not rows:
            return
        with self.conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO ep_commits (sha, project_id, author_email, committed_date, additions, deletions)
                   VALUES (%(sha)s, %(project_id)s, %(author_email)s, %(committed_date)s, %(additions)s, %(deletions)s)
                   ON CONFLICT (sha) DO NOTHING""",
                rows,
            )

    def get_commits(self, project_ids: list[str], since_date: str, until_date: str) -> list[dict]:
        if not project_ids:
            return []
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT sha, project_id, author_email, committed_date::text, additions, deletions
                   FROM ep_commits
                   WHERE project_id = ANY(%s)
                     AND committed_date >= %s::date
                     AND committed_date < (%s::date + INTERVAL '1 day')""",
                (list(project_ids), since_date, until_date),
            )
            return [
                {"sha": s, "project_id": p, "author_email": e, "committed_date": d,
                 "additions": a, "deletions": x}
                for s, p, e, d, a, x in cur.fetchall()
            ]

    def get_commits_per_repo(
        self, author_emails: list[str], since_date: str, until_date: str
    ) -> list[dict]:
        """Breakdown commit per repo untuk satu set email author (engineer + alias)."""
        if not author_emails:
            return []
        emails = [e.lower() for e in author_emails]
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT project_id, COUNT(*), COALESCE(SUM(additions), 0),
                          COALESCE(SUM(deletions), 0),
                          MIN(committed_date)::text, MAX(committed_date)::text
                   FROM ep_commits
                   WHERE lower(author_email) = ANY(%s)
                     AND committed_date >= %s::date
                     AND committed_date < (%s::date + INTERVAL '1 day')
                   GROUP BY project_id
                   ORDER BY COUNT(*) DESC""",
                (emails, since_date, until_date),
            )
            return [
                {"project_id": p, "commits": c, "additions": a, "deletions": d,
                 "first_commit": f, "last_commit": l}
                for p, c, a, d, f, l in cur.fetchall()
            ]

    # ---------------------------------------------------------------- projects
    def get_projects(self, project_ids: list[str]) -> dict[str, dict]:
        """Kembalikan cache metadata repo {project_id: {path, name, web_url}}."""
        if not project_ids:
            return {}
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT project_id, path, name, web_url FROM ep_projects WHERE project_id = ANY(%s)",
                (list(project_ids),),
            )
            return {
                pid: {"path": path, "name": name, "web_url": web_url}
                for pid, path, name, web_url in cur.fetchall()
            }

    def put_project(self, project_id: str, path: str | None, name: str | None, web_url: str | None) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO ep_projects (project_id, path, name, web_url) VALUES (%s, %s, %s, %s)
                   ON CONFLICT (project_id) DO UPDATE
                   SET path = EXCLUDED.path, name = EXCLUDED.name,
                       web_url = EXCLUDED.web_url, fetched_at = now()""",
                (project_id, path, name, web_url),
            )

    # --------------------------------------------------- discovery engineer->repo
    def get_discovery_coverage(self, email: str) -> tuple[str, str] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT earliest_date::text, latest_date::text FROM ep_discovery_sync WHERE engineer_email = %s",
                (email.lower(),),
            )
            row = cur.fetchone()
            return (row[0], row[1]) if row else None

    def set_discovery_coverage(self, email: str, earliest: str, latest: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO ep_discovery_sync (engineer_email, earliest_date, latest_date) VALUES (%s, %s, %s)
                   ON CONFLICT (engineer_email) DO UPDATE
                   SET earliest_date = LEAST(ep_discovery_sync.earliest_date, EXCLUDED.earliest_date),
                       latest_date   = GREATEST(ep_discovery_sync.latest_date, EXCLUDED.latest_date)""",
                (email.lower(), earliest, latest),
            )

    def upsert_engineer_repos(self, rows: list[dict]) -> None:
        """rows = [{engineer_email, project_id, seen_date}] — merge first/last_seen."""
        if not rows:
            return
        with self.conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO ep_engineer_repos (engineer_email, project_id, first_seen, last_seen)
                   VALUES (%(engineer_email)s, %(project_id)s, %(seen_date)s, %(seen_date)s)
                   ON CONFLICT (engineer_email, project_id) DO UPDATE
                   SET first_seen = LEAST(ep_engineer_repos.first_seen, EXCLUDED.first_seen),
                       last_seen  = GREATEST(ep_engineer_repos.last_seen, EXCLUDED.last_seen)""",
                rows,
            )

    def get_engineer_repos(self, emails: list[str]) -> dict[str, list[dict]]:
        """email -> [{project_id, first_seen, last_seen}] urut last_seen desc."""
        if not emails:
            return {}
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT engineer_email, project_id, first_seen::text, last_seen::text
                   FROM ep_engineer_repos WHERE engineer_email = ANY(%s)
                   ORDER BY last_seen DESC""",
                ([e.lower() for e in emails],),
            )
            out: dict[str, list[dict]] = {}
            for email, pid, first_seen, last_seen in cur.fetchall():
                out.setdefault(email, []).append(
                    {"project_id": pid, "first_seen": first_seen, "last_seen": last_seen}
                )
            return out

    # ------------------------------------------------------------------- tasks
    def get_task_watermark(self) -> int | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT watermark FROM ep_task_sync WHERE scope = 'global'")
            row = cur.fetchone()
            return int(row[0]) if row and row[0] is not None else None

    def set_task_watermark(self, ms: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO ep_task_sync (scope, watermark) VALUES ('global', %s)
                   ON CONFLICT (scope) DO UPDATE SET watermark = EXCLUDED.watermark""",
                (ms,),
            )

    def get_backfilled_engineers(self) -> set[int]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT engineer_id FROM ep_task_backfill")
            return {int(r[0]) for r in cur.fetchall()}

    def mark_backfilled(self, ids: list[int]) -> None:
        if not ids:
            return
        with self.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO ep_task_backfill (engineer_id) VALUES (%s) ON CONFLICT (engineer_id) DO NOTHING",
                [(int(i),) for i in ids],
            )

    def upsert_tasks(self, rows: list[dict]) -> None:
        """rows: {task_id, payload(dict), date_updated, developer_ids(list[int]), date_done, status_type}."""
        if not rows:
            return
        prepared = [
            {
                "task_id": r["task_id"],
                "payload": Json(r["payload"]),
                "date_updated": r.get("date_updated"),
                "developer_ids": list(r.get("developer_ids") or []),
                "date_done": r.get("date_done"),
                "status_type": r.get("status_type"),
            }
            for r in rows
        ]
        with self.conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO ep_tasks (task_id, payload, date_updated, developer_ids, date_done, status_type)
                   VALUES (%(task_id)s, %(payload)s, %(date_updated)s, %(developer_ids)s, %(date_done)s, %(status_type)s)
                   ON CONFLICT (task_id) DO UPDATE SET
                       payload = EXCLUDED.payload,
                       date_updated = EXCLUDED.date_updated,
                       developer_ids = EXCLUDED.developer_ids,
                       date_done = EXCLUDED.date_done,
                       status_type = EXCLUDED.status_type,
                       fetched_at = now()""",
                prepared,
            )

    def get_tasks(self, developer_ids: list[int]) -> list[dict]:
        """Kembalikan list payload (dict) untuk task yang punya overlap developer_ids."""
        if not developer_ids:
            return []
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT payload FROM ep_tasks WHERE developer_ids && %s::bigint[]",
                (list(developer_ids),),
            )
            return [row[0] for row in cur.fetchall()]

    def get_open_tasks(self, developer_ids: list[int]) -> list[dict]:
        """Payload task open/WIP (status belum terminal) untuk developer_ids — snapshot."""
        if not developer_ids:
            return []
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT payload FROM ep_tasks
                   WHERE developer_ids && %s::bigint[]
                     AND COALESCE(status_type, '') NOT IN ('closed', 'done')
                   ORDER BY date_updated DESC NULLS LAST""",
                (list(developer_ids),),
            )
            return [row[0] for row in cur.fetchall()]

    def get_completed_tasks(self, developer_ids: list[int], since_ms: int, until_ms: int) -> list[dict]:
        """Payload task selesai (date_done dalam rentang epoch-ms) untuk developer_ids."""
        if not developer_ids:
            return []
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT payload FROM ep_tasks
                   WHERE developer_ids && %s::bigint[]
                     AND date_done IS NOT NULL
                     AND date_done >= %s AND date_done <= %s
                   ORDER BY date_done DESC""",
                (list(developer_ids), since_ms, until_ms),
            )
            return [row[0] for row in cur.fetchall()]

    # ------------------------------------------------------------------- misc
    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
