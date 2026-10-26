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
    stars_added: list[str] = field(default_factory=list)
    stars_removed: list[str] = field(default_factory=list)
    forks_added: list[str] = field(default_factory=list)
    watchers_added: list[str] = field(default_factory=list)
    new_issues: list[dict] = field(default_factory=list)
    error: str | None = None

    @property
    def has_news(self) -> bool:
        """Есть ли что-то, ради чего стоит писать в Telegram. Клоны не считаем — там в основном боты."""
        return bool(
            self.views or self.new_referrers or self.stars_added or self.stars_removed
            or self.forks_added or self.watchers_added or self.new_issues
        )


@dataclass
class CycleResult:
    repos: list[RepoReport]
    repos_added: list[str] = field(default_factory=list)    # новые репо, взяты под контроль
    repos_removed: list[str] = field(default_factory=list)  # удалены или стали приватными
    followers_added: list[str] = field(default_factory=list)
    followers_removed: list[str] = field(default_factory=list)
    error: str | None = None


async def check_repo(gh: GitHub, store: Store, repo: str) -> RepoReport:
    """Каждый источник обрабатывается отдельно: сбой одного запроса не теряет остальные данные."""
    rep = RepoReport(repo)
    names = ("views", "clones", "referrers", "paths", "stars", "forks", "watchers", "issues")
    results = await asyncio.gather(
        gh.traffic(repo, "views"),
        gh.traffic(repo, "clones"),
        gh.referrers(repo),
        gh.paths(repo),
        gh.stargazers(repo),
        gh.forks(repo),
        gh.watchers(repo),
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
    if "stars" in data:
        rep.stars_added, rep.stars_removed = store.sync_members("star", repo, data["stars"])
    if "forks" in data:
        rep.forks_added, _ = store.sync_members("fork", repo, data["forks"])
    if "watchers" in data:
        rep.watchers_added, _ = store.sync_members("watch", repo, data["watchers"])
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

    sem = asyncio.Semaphore(CONCURRENCY)

    async def limited(repo: str) -> RepoReport:
        async with sem:
            return await check_repo(gh, store, repo)

    reports = await asyncio.gather(*(limited(r) for r in repos))
    result = CycleResult(repos=list(reports), repos_added=repos_added, repos_removed=repos_removed)
    try:
        result.followers_added, result.followers_removed = store.sync_members(
            "follower", "", await gh.followers())
    except Exception as e:
        log.exception("ошибка при проверке подписчиков")
        result.error = str(e)
    log.info("проход: репозиториев %d, с новостями %d",
             len(reports), sum(r.has_news for r in reports))
    return result
