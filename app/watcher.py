"""Один проход сбора: опрашивает GitHub, сравнивает с базой, возвращает события для уведомлений."""
import asyncio
import logging
from dataclasses import dataclass, field

from .db import Store
from .github import GitHub

log = logging.getLogger("watcher")

# Сколько репозиториев опрашиваем одновременно. Здесь семафор уместен:
# запросы и так асинхронные (httpx), он лишь ограничивает нагрузку на API.
CONCURRENCY = 4


@dataclass
class RepoReport:
    repo: str
    views: int = 0
    view_uniques: int = 0
    clones: int = 0
    referrers: list[dict] = field(default_factory=list)   # текущий топ источников
    paths: list[dict] = field(default_factory=list)       # какие страницы открывали
    new_referrers: list[str] = field(default_factory=list)
    stars: int = 0            # сейчас всего (кто именно — GitHub не сообщает)
    stars_delta: int = 0      # изменение с прошлого прохода
    watchers: int = 0
    watchers_delta: int = 0
    forks_added: list[str] = field(default_factory=list)
    new_issues: list[dict] = field(default_factory=list)
    error: str | None = None

    @property
    def has_news(self) -> bool:
        """Есть ли что-то, ради чего стоит писать в Telegram. Клоны не считаем — там в основном боты."""
        return bool(
            self.views or self.new_referrers or self.stars_delta or self.watchers_delta
            or self.forks_added or self.new_issues
        )


@dataclass
class CycleResult:
    repos: list[RepoReport]
    repos_added: list[str] = field(default_factory=list)    # новые репо, взяты под контроль
    repos_removed: list[str] = field(default_factory=list)  # удалены или стали приватными
    followers_added: list[str] = field(default_factory=list)
    followers_removed: list[str] = field(default_factory=list)
    error: str | None = None


async def check_repo(gh: GitHub, store: Store, repo: str, own_watch: bool = False) -> RepoReport:
    """Каждый источник обрабатывается отдельно: сбой одного запроса не теряет остальные данные."""
    rep = RepoReport(repo)
    names = ("views", "clones", "referrers", "paths", "counters", "forks", "issues")
    results = await asyncio.gather(
        gh.traffic(repo, "views"),
        gh.traffic(repo, "clones"),
        gh.referrers(repo),
        gh.paths(repo),
        gh.counters(repo),
        gh.forks(repo),
        gh.foreign_issues(repo),
        return_exceptions=True,
    )
    data, errors = {}, []
    for name, res in zip(names, results):
        if isinstance(res, Exception):
            log.warning("[%s] %s: %s", repo, name, res)
            errors.append(f"{name}: {res}")
        else:
            data[name] = res

    if "views" in data:
        rep.views, rep.view_uniques = store.save_traffic(repo, "views", data["views"])
    if "clones" in data:
        rep.clones, _ = store.save_traffic(repo, "clones", data["clones"])
    if "referrers" in data:
        rep.referrers = data["referrers"]
        rep.new_referrers = store.new_referrers(repo, [r["name"] for r in data["referrers"]])
    rep.paths = data.get("paths", [])
    if "counters" in data:
        rep.stars = data["counters"]["star"]
        rep.stars_delta = store.update_counter(repo, "star", rep.stars)
        # владелец сам подписан на свой репо — его не считаем
        rep.watchers = max(0, data["counters"]["watch"] - (1 if own_watch else 0))
        rep.watchers_delta = store.update_counter(repo, "watch", rep.watchers)
    if "forks" in data:
        rep.forks_added, _ = store.sync_members("fork", repo, data["forks"])
    if "issues" in data:
        rep.new_issues = store.new_issues(repo, data["issues"])
    if errors:
        rep.error = "; ".join(e[:150] for e in errors)
    return rep


async def run_cycle(gh: GitHub, store: Store, include_private: bool) -> CycleResult:
    try:
        repos = await gh.repos(include_private)
    except Exception as e:
        log.exception("не удалось получить список репозиториев")
        return CycleResult(repos=[], error=str(e))

    # список репо тоже храним как «участников»: так новые и пропавшие ловятся тем же механизмом
    repos_added, repos_removed = store.sync_members("repo", "", set(repos))

    try:
        own = await gh.own_subscriptions()
    except Exception as e:
        log.warning("не удалось получить свои подписки, подписчики репо считаются с владельцем: %s", e)
        own = set()

    sem = asyncio.Semaphore(CONCURRENCY)

    async def limited(repo: str) -> RepoReport:
        async with sem:
            return await check_repo(gh, store, repo, own_watch=repo in own)

    reports = await asyncio.gather(*(limited(r) for r in repos))
    result = CycleResult(repos=list(reports), repos_added=repos_added, repos_removed=repos_removed)
    try:
        result.followers_added, result.followers_removed = store.sync_members(
            "follower", "", await gh.followers())
    except Exception as e:
        log.exception("ошибка при проверке подписчиков")
        result.error = str(e)
    record_events(store, result)
    log.info("проход: репозиториев %d, с новостями %d",
             len(reports), sum(r.has_news for r in reports))
    return result


def signed(n: int) -> str:
    return f"+{n}" if n > 0 else f"−{-n}"


def record_events(store: Store, res: CycleResult):
    """Всё новое из прохода — в журнал, чтобы потом показывать в отчётах с датами."""
    for name in res.repos_added:
        store.add_event(name, "repo", "репозиторий взят под контроль")
    for name in res.repos_removed:
        store.add_event(name, "repo", "убран из контроля (удалён или стал приватным)")
    for r in res.repos:
        if r.views:
            store.add_event(r.repo, "views", f"+{r.views} просмотров ({r.view_uniques} уник.)")
        for src in r.new_referrers:
            store.add_event(r.repo, "referrer", f"новый источник: {src}")
        if r.stars_delta:
            store.add_event(r.repo, "star", f"{signed(r.stars_delta)} звезда (всего {r.stars})")
        if r.watchers_delta:
            store.add_event(r.repo, "watch", f"{signed(r.watchers_delta)} подписчик репо (всего {r.watchers})")
        for login in r.forks_added:
            store.add_event(r.repo, "fork", f"форк от {login}")
        for it in r.new_issues:
            kind = "PR" if it["is_pr"] else "issue"
            store.add_event(r.repo, "issue", f"{kind} #{it['number']} от {it['author']}: {it['title'][:80]}")
    for login in res.followers_added:
        store.add_event("", "follower", f"новый подписчик профиля: {login}")
    for login in res.followers_removed:
        store.add_event("", "follower", f"отписался от профиля: {login}")
