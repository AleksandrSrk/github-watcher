from datetime import date

import pytest

from app.db import Store


@pytest.fixture
def store():
    return Store(":memory:")


def day(d, c, u):
    return {"day": d, "count": c, "uniques": u}


class TestTraffic:
    def test_first_pass_is_silent_baseline(self, store):
        # при первом запуске старые просмотры не должны прилететь алертом
        assert store.save_traffic("r", "views", [day("2026-10-20", 5, 2)]) == (0, 0)

    def test_delta_after_baseline(self, store):
        store.save_traffic("r", "views", [day("2026-10-20", 5, 2)])
        # за тот же день цифры выросли + появился новый день
        delta = store.save_traffic("r", "views", [day("2026-10-20", 7, 3), day("2026-10-21", 4, 1)])
        assert delta == (2 + 4, 1 + 1)

    def test_no_duplicates_and_no_growth_means_zero(self, store):
        rows = [day("2026-10-20", 5, 2)]
        store.save_traffic("r", "views", rows)
        assert store.save_traffic("r", "views", rows) == (0, 0)
        n = store.db.execute("SELECT COUNT(*) FROM traffic").fetchone()[0]
        assert n == 1  # PRIMARY KEY + ON CONFLICT: строка одна, а не две

    def test_history_survives_github_14_day_window(self, store):
        store.save_traffic("r", "views", [day("2026-09-01", 10, 3)])
        # GitHub уже не отдаёт сентябрь, но у нас он остался
        store.save_traffic("r", "views", [day("2026-10-20", 1, 1)])
        totals = store.traffic_totals(90, until=date(2026, 10, 20))
        assert totals["r"] == (11, 4)

    def test_lower_number_from_github_does_not_reduce_history(self, store):
        store.save_traffic("r", "views", [day("2026-10-20", 5, 2)])
        assert store.save_traffic("r", "views", [day("2026-10-20", 3, 1)]) == (0, 0)
        assert store.traffic_totals(1, until=date(2026, 10, 20))["r"] == (5, 2)

    def test_kinds_are_separate(self, store):
        store.save_traffic("r", "views", [day("2026-10-20", 5, 2)])
        store.save_traffic("r", "clones", [day("2026-10-20", 40, 20)])
        assert store.traffic_totals(1, "views", until=date(2026, 10, 20))["r"] == (5, 2)


class TestReferrers:
    def test_baseline_then_only_new(self, store):
        assert store.new_referrers("r", ["Google"]) == []
        assert store.new_referrers("r", ["Google", "hh.ru"]) == ["hh.ru"]
        assert store.new_referrers("r", ["Google", "hh.ru"]) == []

    def test_same_source_in_other_repo_is_new_there(self, store):
        store.new_referrers("a", [])
        store.new_referrers("b", [])
        assert store.new_referrers("a", ["hh.ru"]) == ["hh.ru"]
        assert store.new_referrers("b", ["hh.ru"]) == ["hh.ru"]


class TestMembers:
    def test_baseline_is_silent(self, store):
        assert store.sync_members("star", "r", {"alice", "bob"}) == ([], [])

    def test_added_and_removed(self, store):
        store.sync_members("star", "r", {"alice", "bob"})
        assert store.sync_members("star", "r", {"alice", "carol"}) == (["carol"], ["bob"])

    def test_returning_star_is_reported_again(self, store):
        store.sync_members("star", "r", {"alice"})
        store.sync_members("star", "r", set())
        assert store.sync_members("star", "r", {"alice"}) == (["alice"], [])

    def test_counts_only_active(self, store):
        store.sync_members("star", "r", {"alice", "bob"})
        store.sync_members("star", "r", {"alice"})
        assert store.member_counts("star") == {"r": 1}


class TestIssues:
    def test_new_issue_once(self, store):
        it = {"number": 1, "title": "t", "author": "x", "is_pr": False, "url": "u"}
        assert store.new_issues("r", []) == []
        assert store.new_issues("r", [it]) == [it]
        assert store.new_issues("r", [it]) == []
