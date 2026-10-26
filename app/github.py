"""Асинхронный клиент GitHub REST API — только чтение."""
import logging

import httpx

API = "https://api.github.com"
log = logging.getLogger("github")

HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "github-watcher",
}


class GitHubError(Exception):
    pass


class GitHub:
    def __init__(self, token: str, user: str, client: httpx.AsyncClient | None = None):
        self.user = user
        self._client = client or httpx.AsyncClient(
            base_url=API, timeout=30, headers={**HEADERS, "Authorization": f"Bearer {token}"})

    async def close(self):
        await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None):
        r = await self._client.get(path, params=params)
        if r.status_code == 403 and r.headers.get("x-ratelimit-remaining") == "0":
            raise GitHubError("исчерпан лимит запросов GitHub API")
        if r.status_code >= 400:
            raise GitHubError(f"{path}: HTTP {r.status_code} {r.text[:200]}")
        return r

    async def _paged(self, path: str, params: dict | None = None) -> list:
        """Собирает все страницы по заголовку Link."""
        items, url, p = [], path, {"per_page": 100, **(params or {})}
        while url:
            r = await self._get(url, params=p)
            items.extend(r.json())
            url = r.links.get("next", {}).get("url")
            p = None  # в next-ссылке параметры уже есть
        return items

    # --- репозитории ---------------------------------------------------------
    async def repos(self, include_private: bool) -> list[str]:
        data = await self._paged("/user/repos", {"affiliation": "owner"})
        return sorted(
            r["name"] for r in data
            if not r["fork"] and (include_private or not r["private"])
        )

    async def counters(self, repo: str) -> dict[str, int]:
        """Количество звёзд и подписчиков репо.

        Кто именно поставил звезду или подписался, GitHub не отдаёт никому, даже владельцу:
        REST отвечает 403/404, а GraphQL возвращает пустой список при ненулевом счётчике.
        Поэтому следим за количеством.
        """
        r = (await self._get(f"/repos/{self.user}/{repo}")).json()
        return {"star": r.get("stargazers_count", 0), "watch": r.get("subscribers_count", 0)}

    # --- трафик (хранится на GitHub только 14 дней) ---------------------------
    async def traffic(self, repo: str, kind: str) -> list[dict]:
        """kind: views | clones. Возвращает [{'day': 'YYYY-MM-DD', 'count': n, 'uniques': u}]."""
        r = await self._get(f"/repos/{self.user}/{repo}/traffic/{kind}", {"per": "day"})
        return [
            {"day": x["timestamp"][:10], "count": x["count"], "uniques": x["uniques"]}
            for x in r.json().get(kind, [])
        ]

    async def referrers(self, repo: str) -> list[dict]:
        r = await self._get(f"/repos/{self.user}/{repo}/traffic/popular/referrers")
        return [{"name": x["referrer"], "count": x["count"], "uniques": x["uniques"]} for x in r.json()]

    async def paths(self, repo: str) -> list[dict]:
        r = await self._get(f"/repos/{self.user}/{repo}/traffic/popular/paths")
        return [{"name": x["path"], "count": x["count"], "uniques": x["uniques"]} for x in r.json()]

    # --- люди ----------------------------------------------------------------
    async def forks(self, repo: str) -> set[str]:
        data = await self._paged(f"/repos/{self.user}/{repo}/forks")
        return {x["owner"]["login"] for x in data}

    async def followers(self) -> set[str]:
        data = await self._paged(f"/users/{self.user}/followers")
        return {x["login"] for x in data}

    async def foreign_issues(self, repo: str) -> list[dict]:
        """Issues и PR, открытые НЕ владельцем (endpoint issues отдаёт и PR тоже)."""
        data = await self._paged(f"/repos/{self.user}/{repo}/issues", {"state": "all"})
        return [
            {
                "number": x["number"],
                "title": x["title"],
                "author": x["user"]["login"],
                "is_pr": "pull_request" in x,
                "url": x["html_url"],
            }
            for x in data if x["user"]["login"] != self.user
        ]
