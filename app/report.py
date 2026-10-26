"""Полный отчёт: при первом запуске (вся история, что отдаёт GitHub) и раз в неделю."""
from datetime import datetime, timedelta, timezone
from html import escape

from .db import Store
from .views import _repo, _user, is_hot
from .watcher import CycleResult

TG_LIMIT = 3800


def split_messages(blocks: list[str]) -> list[str]:
    """Склеивает блоки в сообщения, не превышая лимит Telegram."""
    msgs, cur = [], ""
    for b in blocks:
        if cur and len(cur) + len(b) + 2 > TG_LIMIT:
            msgs.append(cur)
            cur = ""
        cur = f"{cur}\n\n{b}" if cur else b
    if cur:
        msgs.append(cur)
    return msgs


def _page(path: str) -> str:
    """'/user/repo/blob/main/README.md' -> 'blob/main/README.md', '/user/repo' -> 'главная'."""
    parts = path.split("/", 3)
    return parts[3] if len(parts) > 3 and parts[3] else "главная"


def _by_repo(pairs: list[tuple[str, str]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for repo, login in pairs:
        out.setdefault(repo, []).append(login)
    return out


def build_report(title: str, user: str, days: int, store: Store, last: CycleResult | None,
                 only_new: bool = False) -> list[str]:
    """only_new=True — недельный режим: форки и подписчики — только появившиеся за период,
    по звёздам — изменение за период (кто ставит звёзды, GitHub не сообщает)."""
    since = None
    if only_new:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        # то, что было на момент первого запуска, — не «новое за неделю»
        since = max(since, store.get_meta("baseline_at", ""))
    views = store.traffic_totals(days, "views")
    clones = store.traffic_totals(days, "clones")
    forks = _by_repo(store.members("fork", since))
    star_total = store.counters("star")
    watch_total = store.counters("watch")
    # за период: события звёзд/подписок из журнала (в стартовом отчёте их нет — только итоги)
    period_events: dict[str, list[str]] = {}
    if only_new:
        for e in store.events(since=since, limit=1000):
            if e["kind"] in ("star", "watch"):
                period_events.setdefault(e["repo"], []).append(e["text"])
    snap = {r.repo: r for r in last.repos} if last else {}

    repos = sorted(set(snap) | set(views) | set(clones),
                   key=lambda r: (-views.get(r, (0, 0))[0], -star_total.get(r, 0), r))

    total_v = sum(v for v, _ in views.values())
    total_c = sum(c for c, _ in clones.values())
    head = [f"<b>{title}</b>",
            f"Репозиториев под контролем: {len(repos)}",
            f"👁 просмотров: {total_v}   📥 клонов: {total_c}",
            f"⭐ звёзд всего: {sum(star_total.values())}"]
    followers = store.members("follower", since)
    label = "новых подписчиков" if only_new else "подписчиков профиля"
    head.append(f"👤 {label}: {len(followers)}"
                + (": " + ", ".join(_user(l) for _, l in followers[:15]) if followers else ""))
    blocks = ["\n".join(head)]

    quiet = []
    for repo in repos:
        v, vu = views.get(repo, (0, 0))
        c, _ = clones.get(repo, (0, 0))
        r = snap.get(repo)
        lines = [f"📦 <b>{_repo(user, repo)}</b>"]
        if v:
            lines.append(f"👁 {v} просм. ({vu} уник. по дням)")
        if c:
            lines.append(f"📥 {c} клонов")
        if r and r.referrers:
            lines.append("↪️ " + ", ".join(
                ("🔥" if is_hot(x["name"]) else "") + f"{escape(x['name'])} ({x['count']})"
                for x in r.referrers[:5]))
        if r and r.paths:
            lines.append("📄 " + ", ".join(f"{escape(_page(x['name']))} ({x['count']})" for x in r.paths[:3]))
        counts = []
        if star_total.get(repo):
            counts.append(f"⭐ {star_total[repo]}")
        if watch_total.get(repo):
            counts.append(f"👀 {watch_total[repo]}")
        if counts and (v or c or not only_new):
            lines.append(" · ".join(counts))
        for text in reversed(period_events.get(repo, [])):
            lines.append(f"🆕 {escape(text)}")
        if forks.get(repo):
            lines.append("🍴 " + ", ".join(_user(n) for n in forks[repo][:10]))
        if len(lines) == 1:
            quiet.append(escape(repo))
        else:
            blocks.append("\n".join(lines))
    if quiet:
        blocks.append("Без активности: " + ", ".join(quiet))
    return split_messages(blocks)
