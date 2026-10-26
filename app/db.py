"""Хранилище SQLite. Дедуп обеспечивает сама база (PRIMARY KEY + ON CONFLICT), а не проверки в коде."""
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- дневной трафик: GitHub хранит только 14 дней, у нас — бессрочно
CREATE TABLE IF NOT EXISTS traffic (
    repo    TEXT NOT NULL,
    kind    TEXT NOT NULL,          -- views | clones
    day     TEXT NOT NULL,          -- YYYY-MM-DD (UTC)
    count   INTEGER NOT NULL,
    uniques INTEGER NOT NULL,
    PRIMARY KEY (repo, kind, day)
);
-- источники переходов, которые уже видели
CREATE TABLE IF NOT EXISTS referrers (
    repo       TEXT NOT NULL,
    name       TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    PRIMARY KEY (repo, name)
);
-- звёзды, форки, watchers (repo = имя) и подписчики профиля (repo = '')
CREATE TABLE IF NOT EXISTS members (
    kind       TEXT NOT NULL,       -- star | fork | watch | follower
    repo       TEXT NOT NULL,
    login      TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (kind, repo, login)
);
CREATE TABLE IF NOT EXISTS issues (
    repo   TEXT NOT NULL,
    number INTEGER NOT NULL,
    PRIMARY KEY (repo, number)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # --- служебное -----------------------------------------------------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str):
        with self.db:
            self.db.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def _baseline(self, scope: str) -> bool:
        """True при самом первом проходе по scope: запоминаем молча, без алертов."""
        key = f"init:{scope}"
        if self.get_meta(key):
            return False
        self.set_meta(key, _now())
        return True

    # --- трафик --------------------------------------------------------------
    def save_traffic(self, repo: str, kind: str, rows: list[dict]) -> tuple[int, int]:
        """Сохраняет дневные цифры, возвращает прирост (просмотры, уникальные) с прошлого прохода."""
        baseline = self._baseline(f"traffic:{kind}:{repo}")
        d_count = d_uniq = 0
        with self.db:
            for r in rows:
                old = self.db.execute(
                    "SELECT count, uniques FROM traffic WHERE repo=? AND kind=? AND day=?",
                    (repo, kind, r["day"]),
                ).fetchone()
                d_count += max(0, r["count"] - (old["count"] if old else 0))
                d_uniq += max(0, r["uniques"] - (old["uniques"] if old else 0))
                self.db.execute(
                    "INSERT INTO traffic(repo, kind, day, count, uniques) VALUES(?, ?, ?, ?, ?) "
                    "ON CONFLICT(repo, kind, day) DO UPDATE SET "
                    "count=MAX(traffic.count, excluded.count), uniques=MAX(traffic.uniques, excluded.uniques)",
                    (repo, kind, r["day"], r["count"], r["uniques"]),
                )
        return (0, 0) if baseline else (d_count, d_uniq)

    def traffic_totals(self, days: int, kind: str = "views", until: date | None = None) -> dict[str, tuple[int, int]]:
        """{repo: (просмотры, сумма дневных уникальных)} за последние days дней."""
        until = until or datetime.now(timezone.utc).date()
        since = (until - timedelta(days=days - 1)).isoformat()
        rows = self.db.execute(
            "SELECT repo, SUM(count) AS c, SUM(uniques) AS u FROM traffic "
            "WHERE kind=? AND day>=? AND day<=? GROUP BY repo HAVING c > 0 ORDER BY c DESC",
            (kind, since, until.isoformat()),
        ).fetchall()
        return {r["repo"]: (r["c"], r["u"]) for r in rows}

    # --- источники переходов ---------------------------------------------------
    def new_referrers(self, repo: str, names: list[str]) -> list[str]:
        baseline = self._baseline(f"ref:{repo}")
        new = []
        with self.db:
            for name in names:
                cur = self.db.execute(
                    "INSERT INTO referrers(repo, name, first_seen) VALUES(?, ?, ?) "
                    "ON CONFLICT(repo, name) DO NOTHING",
                    (repo, name, _now()),
                )
                if cur.rowcount:  # вставилась новая строка — значит, источник новый
                    new.append(name)
        return [] if baseline else new

    def referrer_names(self, repo: str | None = None) -> list[tuple[str, str]]:
        q = "SELECT repo, name FROM referrers"
        args: tuple = ()
        if repo:
            q += " WHERE repo=?"
            args = (repo,)
        return [(r["repo"], r["name"]) for r in self.db.execute(q + " ORDER BY first_seen DESC", args)]

    # --- звёзды / форки / подписки -------------------------------------------
    def sync_members(self, kind: str, repo: str, current: set[str]) -> tuple[list[str], list[str]]:
        """Сравнивает с прошлым состоянием. Возвращает (появились, ушли)."""
        baseline = self._baseline(f"{kind}:{repo}")
        active = {
            r["login"] for r in self.db.execute(
                "SELECT login FROM members WHERE kind=? AND repo=? AND active=1", (kind, repo))
        }
        added, removed = sorted(current - active), sorted(active - current)
        with self.db:
            for login in added:
                self.db.execute(
                    "INSERT INTO members(kind, repo, login, first_seen, active) VALUES(?, ?, ?, ?, 1) "
                    "ON CONFLICT(kind, repo, login) DO UPDATE SET active=1",
                    (kind, repo, login, _now()),
                )
            for login in removed:
                self.db.execute(
                    "UPDATE members SET active=0 WHERE kind=? AND repo=? AND login=?", (kind, repo, login))
        return ([], []) if baseline else (added, removed)

    def members(self, kind: str, since: str | None = None) -> list[tuple[str, str]]:
        """[(repo, login)] активных участников; since — только появившиеся после даты (ISO)."""
        q, args = "SELECT repo, login FROM members WHERE kind=? AND active=1", [kind]
        if since:
            q += " AND first_seen>=?"
            args.append(since)
        return [(r["repo"], r["login"]) for r in self.db.execute(q + " ORDER BY repo, login", args)]

    def member_counts(self, kind: str) -> dict[str, int]:
        rows = self.db.execute(
            "SELECT repo, COUNT(*) AS n FROM members WHERE kind=? AND active=1 GROUP BY repo", (kind,))
        return {r["repo"]: r["n"] for r in rows}

    # --- чужие issues / PR ---------------------------------------------------
    def new_issues(self, repo: str, items: list[dict]) -> list[dict]:
        baseline = self._baseline(f"issues:{repo}")
        new = []
        with self.db:
            for it in items:
                cur = self.db.execute(
                    "INSERT INTO issues(repo, number) VALUES(?, ?) ON CONFLICT(repo, number) DO NOTHING",
                    (repo, it["number"]),
                )
                if cur.rowcount:
                    new.append(it)
        return [] if baseline else new
