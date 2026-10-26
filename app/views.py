"""Тексты сообщений в Telegram (HTML-разметка)."""
from html import escape

from .watcher import CycleResult, RepoReport

GH = "https://github.com"

# Источники, которые особенно интересны: похоже на рекрутера или заказчика
HOT_SOURCES = ("hh.ru", "kwork.ru", "fl.ru", "habr", "career", "t.me", "linkedin")


def _user(login: str) -> str:
    return f'<a href="{GH}/{escape(login)}">{escape(login)}</a>'


def _repo(user: str, repo: str) -> str:
    return f'<a href="{GH}/{escape(user)}/{escape(repo)}">{escape(repo)}</a>'


def is_hot(source: str) -> bool:
    s = source.lower()
    return any(h in s for h in HOT_SOURCES)


def format_repo(user: str, r: RepoReport) -> str:
    lines = [f"📦 <b>{_repo(user, r.repo)}</b>"]
    if r.views:
        lines.append(f"👁 +{r.views} просмотр(ов), уникальных +{r.view_uniques}")
    for src in r.new_referrers:
        mark = "🔥" if is_hot(src) else "🆕"
        lines.append(f"{mark} новый источник: <b>{escape(src)}</b>")
    if r.views and r.referrers:
        top = ", ".join(f"{escape(x['name'])} ({x['count']})" for x in r.referrers[:5])
        lines.append(f"↪️ откуда (14 дн.): {top}")
    for login in r.stars_added:
        lines.append(f"⭐ звезда от {_user(login)}")
    for login in r.stars_removed:
        lines.append(f"💔 {_user(login)} убрал звезду")
    for login in r.forks_added:
        lines.append(f"🍴 форк от {_user(login)}")
    for login in r.watchers_added:
        lines.append(f"👀 {_user(login)} подписался на репо")
    for it in r.new_issues:
        kind = "PR" if it["is_pr"] else "issue"
        lines.append(f'💬 {kind} от {_user(it["author"])}: <a href="{escape(it["url"])}">'
                     f'#{it["number"]} {escape(it["title"][:80])}</a>')
    return "\n".join(lines)


def format_cycle(user: str, res: CycleResult) -> list[str]:
    """Сообщения по итогам прохода. Пустой список — новостей нет, никого не беспокоим."""
    msgs = []
    tracked = [f"🆕 новый репозиторий под контролем: {_repo(user, x)}" for x in res.repos_added]
    tracked += [f"🗑 убран из контроля (удалён или стал приватным): {escape(x)}" for x in res.repos_removed]
    if tracked:
        msgs.append("\n".join(tracked))
    msgs += [format_repo(user, r) for r in res.repos if r.has_news]
    people = [f"👤 новый подписчик: {_user(x)}" for x in res.followers_added]
    people += [f"👋 отписался: {_user(x)}" for x in res.followers_removed]
    if people:
        msgs.append("\n".join(people))
    return msgs


def format_errors(res: CycleResult) -> str | None:
    errs = [f"• {escape(r.repo)}: {escape(r.error[:200])}" for r in res.repos if r.error]
    if res.error:
        errs.insert(0, f"• общее: {escape(res.error[:300])}")
    return ("⚠️ <b>Ошибки при опросе GitHub</b>\n" + "\n".join(errs)) if errs else None


def format_totals(title: str, totals: dict[str, tuple[int, int]], stars: dict[str, int]) -> str:
    if not totals:
        return f"<b>{title}</b>\nПросмотров не было."
    lines = [f"<b>{title}</b>"]
    for repo, (count, uniq) in totals.items():
        s = f" ⭐{stars[repo]}" if stars.get(repo) else ""
        lines.append(f"• {escape(repo)}: {count} просм. ({uniq} уник.){s}")
    lines.append(f"\nВсего: {sum(c for c, _ in totals.values())} просмотров")
    return "\n".join(lines)


def format_sources(pairs: list[tuple[str, str]]) -> str:
    if not pairs:
        return "Источников переходов пока не было."
    lines = ["<b>Источники переходов</b> (новые сверху)"]
    for repo, name in pairs[:30]:
        mark = "🔥" if is_hot(name) else "•"
        lines.append(f"{mark} {escape(name)} → {escape(repo)}")
    return "\n".join(lines)
