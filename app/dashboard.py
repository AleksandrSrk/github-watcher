"""Интерактивный отчёт в Telegram: общий список репо с кнопками и карточка репо с разбивкой по дням."""
from datetime import datetime
from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .db import Store
from .report import _page
from .views import _repo, _user, is_hot
from .watcher import CycleResult

REPORT_BUTTON = "📊 Текущий отчёт"
DAYS = 14          # окно для цифр в общем отчёте (столько хранит GitHub)
DETAIL_DAYS = 30   # сколько дней показывать в карточке репо (у нас история длиннее, чем у GitHub)


def local_time(ts: str, fmt: str = "%d.%m %H:%M") -> str:
    """UTC ISO из базы -> местное время контейнера (TZ задан в Dockerfile)."""
    try:
        return datetime.fromisoformat(ts).astimezone().strftime(fmt)
    except ValueError:
        return ts


def tracked_repos(store: Store) -> list[str]:
    return [login for _, login in store.members("repo")]


def _members_of(store: Store, kind: str, repo: str) -> list[str]:
    return [login for r, login in store.members(kind) if r == repo]


def overview(user: str, store: Store, seen_at: str) -> tuple[str, InlineKeyboardMarkup]:
    """Сводка по всем репо. seen_at — когда отчёт открывали прошлый раз: всё новее помечаем 🆕."""
    repos = tracked_repos(store)
    views = store.traffic_totals(DAYS, "views")
    clones = store.traffic_totals(DAYS, "clones")
    stars = store.counters("star")
    forks = store.member_counts("fork")
    fresh = store.repos_with_events_since(seen_at) if seen_at else set()
    repos.sort(key=lambda r: (-views.get(r, (0, 0))[0], -stars.get(r, 0), r.lower()))

    lines = [f"<b>📊 Отчёт за {DAYS} дней</b>  (👁 просмотры/уник. · 📥 клоны · ⭐ · 🍴)", ""]
    for r in repos:
        v, vu = views.get(r, (0, 0))
        parts = [f"👁 {v}/{vu}" if v else "👁 —"]
        if clones.get(r):
            parts.append(f"📥 {clones[r][0]}")
        if stars.get(r):
            parts.append(f"⭐ {stars[r]}")
        if forks.get(r):
            parts.append(f"🍴 {forks[r]}")
        mark = "🆕 " if r in fresh else ""
        lines.append(f"{mark}{_repo(user, r)} — {' · '.join(parts)}")
    total = sum(v for v, _ in views.values())
    lines += ["", f"Всего просмотров: {total}, подписчиков профиля: {len(store.members('follower'))}"]

    new = store.events(since=seen_at, limit=15) if seen_at else []
    if new:
        lines += ["", f"<b>🆕 Новое с прошлого просмотра</b> ({local_time(seen_at)}):"]
        for e in new:
            where = escape(e["repo"]) if e["repo"] else "профиль"
            lines.append(f"• {local_time(e['ts'])} — <b>{where}</b>: {escape(e['text'])}")
    elif seen_at:
        lines += ["", f"С прошлого просмотра ({local_time(seen_at)}) нового ничего."]
    lines.append("\nНажми на репо, чтобы посмотреть по дням 👇")

    buttons = [
        InlineKeyboardButton(text=("🆕 " if r in fresh else "") + r, callback_data=f"repo:{r}"[:64])
        for r in repos
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="overview")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def repo_detail(user: str, store: Store, repo: str, last: CycleResult | None) -> tuple[str, InlineKeyboardMarkup]:
    lines = [f"📦 <b>{_repo(user, repo)}</b>", ""]

    days = store.traffic_by_day(repo, DETAIL_DAYS)
    if days:
        v = sum(d["views"] for d in days)
        c = sum(d["clones"] for d in days)
        lines.append(f"<b>По дням</b> (за {DETAIL_DAYS} дн.: 👁 {v}, 📥 {c}):")
        for d in days:
            day = datetime.fromisoformat(d["day"]).strftime("%d.%m")
            parts = []
            if d["views"]:
                parts.append(f"👁 {d['views']} ({d['view_uniq']} уник.)")
            if d["clones"]:
                parts.append(f"📥 {d['clones']} ({d['clone_uniq']} уник.)")
            lines.append(f"<code>{day}</code>  " + " · ".join(parts))
    else:
        lines.append(f"За {DETAIL_DAYS} дней ни просмотров, ни клонов.")

    snap = next((r for r in last.repos if r.repo == repo), None) if last else None
    if snap and snap.referrers:
        lines += ["", "<b>Откуда приходят</b> (14 дн.):"]
        lines += [f"{'🔥' if is_hot(x['name']) else '•'} {escape(x['name'])} — {x['count']} ({x['uniques']} уник.)"
                  for x in snap.referrers]
    if snap and snap.paths:
        lines += ["", "<b>Что открывали</b> (14 дн.):"]
        lines += [f"• {escape(_page(x['name']))} — {x['count']}" for x in snap.paths[:5]]

    stars = store.counters("star").get(repo, 0)
    watch = store.counters("watch").get(repo, 0)
    if stars or watch:
        lines += ["", f"⭐ звёзд: {stars} · 👀 подписчиков репо: {watch}",
                  "<i>кто именно — GitHub не сообщает, поэтому только количество</i>"]
    forks = _members_of(store, "fork", repo)
    if forks:
        lines += ["", "<b>🍴 Форки:</b> " + ", ".join(_user(p) for p in forks)]

    evs = store.events(repo=repo, limit=15)
    if evs:
        lines += ["", "<b>События:</b>"]
        lines += [f"• {local_time(e['ts'])} — {escape(e['text'])}" for e in evs]

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ Ко всем репо", callback_data="overview"),
        InlineKeyboardButton(text="🔄", callback_data=f"repo:{repo}"[:64]),
    ]])
    return "\n".join(lines)[:4000], kb
