import time

import pytest

from app.db import Store
from app.report import build_report, split_messages
from app.watcher import run_cycle
from tests.test_watcher import USER, FakeGitHub, make_gh


@pytest.fixture
def env():
    fake = FakeGitHub()
    return fake, make_gh(fake), Store(":memory:")


async def test_initial_report_shows_everything_already_there(env):
    fake, gh, store = env
    res = await run_cycle(gh, store, False)
    text = "\n".join(build_report("start", USER, 14, store, res, only_new=False))
    assert "bot" in text
    assert "👁 2 просм." in text
    assert "alice" in text          # текущая звезда видна в стартовом отчёте
    assert "Google (2)" in text
    assert "главная (2)" in text    # популярные страницы
    assert "fan" in text            # подписчики профиля


async def test_weekly_report_hides_baseline_and_shows_new(env):
    fake, gh, store = env
    await run_cycle(gh, store, False)
    time.sleep(1.1)  # first_seen хранится с точностью до секунды
    from datetime import datetime, timezone
    store.set_meta("baseline_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    time.sleep(1.1)
    fake.stars["bot"].append("recruiter")
    res = await run_cycle(gh, store, False)
    text = "\n".join(build_report("week", USER, 7, store, res, only_new=True))
    assert "recruiter" in text
    assert "alice" not in text      # была ещё до запуска — не «новая за неделю»


def test_split_messages_respects_limit():
    blocks = ["x" * 1000] * 10
    msgs = split_messages(blocks)
    assert len(msgs) > 1
    assert all(len(m) <= 3800 for m in msgs)
    assert sum(m.count("x") for m in msgs) == 10_000
