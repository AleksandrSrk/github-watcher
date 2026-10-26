"""Полный проход на фейковом GitHub (httpx.MockTransport): без сети и без токена."""
import httpx
import pytest

from app.db import Store
from app.github import GitHub
from app.views import format_cycle
from app.watcher import run_cycle

USER = "me"


class FakeGitHub:
    """Состояние «GitHub», которое тесты меняют между проходами."""

    def __init__(self):
        self.repos = [{"name": "bot", "fork": False, "private": False}]
        self.views = {"bot": [{"timestamp": "2026-10-20T00:00:00Z", "count": 2, "uniques": 1}]}
        self.refs = {"bot": [{"referrer": "Google", "count": 2, "uniques": 1}]}
        self.stars = {"bot": ["alice"]}
        self.followers = ["fan"]
        self.broken = set()  # репо, на которых API отвечает 500
        self.token_lacks_star_rights = False  # как у реального fine-grained токена
        self.public_calls = 0

    def public_handler(self, req: httpx.Request) -> httpx.Response:
        self.public_calls += 1
        return self._route(req.url.path)

    def handler(self, req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if self.token_lacks_star_rights and (p.endswith("/stargazers") or p.endswith("/subscribers")):
            return httpx.Response(403, json={"message": "Resource not accessible by personal access token"})
        return self._route(p)

    def _route(self, p: str) -> httpx.Response:
        if p == "/user/repos":
            return httpx.Response(200, json=self.repos)
        if p == f"/users/{USER}/followers":
            return httpx.Response(200, json=[{"login": x} for x in self.followers])
        repo = p.split("/")[3]
        if repo in self.broken:
            return httpx.Response(500, text="boom")
        if p.endswith("/traffic/views"):
            return httpx.Response(200, json={"views": self.views.get(repo, [])})
        if p.endswith("/traffic/clones"):
            return httpx.Response(200, json={"clones": []})
        if p.endswith("/popular/paths"):
            return httpx.Response(200, json=[{"path": f"/{USER}/{repo}", "title": repo, "count": 2, "uniques": 1}])
        if p.endswith("/popular/referrers"):
            return httpx.Response(200, json=self.refs.get(repo, []))
        if p.endswith("/stargazers"):
            return httpx.Response(200, json=[{"login": x} for x in self.stars.get(repo, [])])
        if p.endswith("/forks") or p.endswith("/subscribers") or p.endswith("/issues"):
            return httpx.Response(200, json=[])
        return httpx.Response(404)


def make_gh(fake: FakeGitHub) -> GitHub:
    client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(fake.handler))
    public = httpx.AsyncClient(base_url="https://api.github.com",
                               transport=httpx.MockTransport(fake.public_handler))
    return GitHub("t", USER, client=client, public_client=public)


@pytest.fixture
def env():
    fake = FakeGitHub()
    return fake, make_gh(fake), Store(":memory:")


async def test_first_run_is_silent(env):
    fake, gh, store = env
    res = await run_cycle(gh, store, include_private=False)
    assert format_cycle(USER, res) == []


async def test_changes_are_reported(env):
    fake, gh, store = env
    await run_cycle(gh, store, False)
    fake.views["bot"][0]["count"] = 5
    fake.views["bot"][0]["uniques"] = 2
    fake.refs["bot"].append({"referrer": "hh.ru", "count": 3, "uniques": 1})
    fake.stars["bot"].append("recruiter")
    fake.followers.append("newfan")

    res = await run_cycle(gh, store, False)
    bot = res.repos[0]
    assert (bot.views, bot.view_uniques) == (3, 1)
    assert bot.new_referrers == ["hh.ru"]
    assert bot.stars_added == ["recruiter"]
    assert res.followers_added == ["newfan"]
    text = "\n".join(format_cycle(USER, res))
    assert "🔥 новый источник: <b>hh.ru</b>" in text
    assert "recruiter" in text and "newfan" in text


async def test_new_repo_is_picked_up_and_announced(env):
    fake, gh, store = env
    await run_cycle(gh, store, False)
    fake.repos.append({"name": "github-watcher", "fork": False, "private": False})

    res = await run_cycle(gh, store, False)
    assert res.repos_added == ["github-watcher"]
    assert {r.repo for r in res.repos} == {"bot", "github-watcher"}
    assert "новый репозиторий под контролем" in format_cycle(USER, res)[0]


async def test_removed_repo_is_announced(env):
    fake, gh, store = env
    await run_cycle(gh, store, False)
    fake.repos = []
    res = await run_cycle(gh, store, False)
    assert res.repos_removed == ["bot"]


async def test_forks_and_private_are_skipped(env):
    fake, gh, store = env
    fake.repos += [{"name": "someones-fork", "fork": True, "private": False},
                   {"name": "secret", "fork": False, "private": True}]
    res = await run_cycle(gh, store, include_private=False)
    assert [r.repo for r in res.repos] == ["bot"]


async def test_one_broken_repo_does_not_break_others(env):
    fake, gh, store = env
    fake.repos.append({"name": "broken", "fork": False, "private": False})
    fake.broken.add("broken")
    res = await run_cycle(gh, store, False)
    by_name = {r.repo: r for r in res.repos}
    assert by_name["broken"].error and "500" in by_name["broken"].error
    assert by_name["bot"].error is None


async def test_fallback_to_public_api_when_token_lacks_rights(env):
    fake, gh, store = env
    fake.token_lacks_star_rights = True
    await run_cycle(gh, store, False)
    fake.stars["bot"].append("recruiter")
    res = await run_cycle(gh, store, False)
    assert res.repos[0].error is None
    assert res.repos[0].stars_added == ["recruiter"]
    assert fake.public_calls > 0


async def test_failed_endpoint_does_not_lose_other_data(env):
    fake, gh, store = env
    await run_cycle(gh, store, False)
    fake.views["bot"][0]["count"] = 9
    fake.stars["bot"].append("recruiter")
    fake.refs["bot"] = None  # сломанный ответ источников
    res = await run_cycle(gh, store, False)
    bot = res.repos[0]
    assert bot.error and "referrers" in bot.error
    assert bot.views == 7                     # просмотры всё равно посчитаны
    assert bot.stars_added == ["recruiter"]   # и звёзды тоже
