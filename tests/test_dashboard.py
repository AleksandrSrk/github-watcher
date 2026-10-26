import time
from datetime import datetime, timezone

import pytest

from app.dashboard import overview, repo_detail
from app.db import Store
from app.watcher import run_cycle
from tests.test_watcher import USER, FakeGitHub, make_gh


@pytest.fixture
def env():
    fake = FakeGitHub()
    return fake, make_gh(fake), Store(":memory:")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def test_overview_lists_repos_with_buttons(env):
    fake, gh, store = env
    await run_cycle(gh, store, False)
    text, kb = overview(USER, store, seen_at="")
    assert "bot" in text and "👁 2/1" in text
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "repo:bot" in data and "overview" in data


async def test_new_events_are_marked_and_listed(env):
    fake, gh, store = env
    await run_cycle(gh, store, False)
    time.sleep(1.1)
    seen = now()
    time.sleep(1.1)
    fake.stars["bot"].append("recruiter")
    fake.refs["bot"].append({"referrer": "hh.ru", "count": 1, "uniques": 1})
    await run_cycle(gh, store, False)

    text, kb = overview(USER, store, seen_at=seen)
    assert "🆕" in text
    assert "звезда от recruiter" in text and "новый источник: hh.ru" in text
    assert any(b.text == "🆕 bot" for row in kb.inline_keyboard for b in row)

    # открыли отчёт — при следующем просмотре старое уже не «новое»
    text2, _ = overview(USER, store, seen_at=now())
    assert "нового ничего" in text2


async def test_repo_detail_has_days_people_and_events(env):
    fake, gh, store = env
    await run_cycle(gh, store, False)
    fake.views["bot"].append({"timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z"),
                              "count": 4, "uniques": 3})
    fake.stars["bot"].append("recruiter")
    res = await run_cycle(gh, store, False)

    text, kb = repo_detail(USER, store, "bot", res)
    assert "По дням" in text and "👁 4 (3 уник.)" in text
    assert "recruiter" in text and "alice" in text        # все звёзды в карточке
    assert "События" in text and "звезда от recruiter" in text
    assert "Google" in text                                 # источники
    assert kb.inline_keyboard[0][0].callback_data == "overview"


async def test_baseline_does_not_create_events(env):
    fake, gh, store = env
    await run_cycle(gh, store, False)
    assert store.events() == []
