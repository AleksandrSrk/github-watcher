"""Точка входа: Telegram-бот + фоновый цикл опроса GitHub в одном процессе."""
import asyncio
import logging
import logging.handlers
import os
import sys
import time
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, KeyboardButton, Message, ReplyKeyboardMarkup

from .config import Settings, load_settings
from .dashboard import REPORT_BUTTON, overview, repo_detail
from .db import Store
from .github import GitHub
from .report import build_report
from .views import format_cycle, format_errors, format_sources, format_totals
from .watcher import CycleResult, run_cycle

log = logging.getLogger("main")


def setup_logging(data_dir: str):
    os.makedirs(os.path.join(data_dir, "logs"), exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    h1 = logging.StreamHandler(sys.stdout)
    h2 = logging.handlers.RotatingFileHandler(
        os.path.join(data_dir, "logs", "watcher.log"), maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    for h in (h1, h2):
        h.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [h1, h2]
    logging.getLogger("httpx").setLevel(logging.WARNING)  # не светить каждый запрос в логах


def heartbeat(data_dir: str):
    with open(os.path.join(data_dir, "heartbeat"), "w") as f:
        f.write(str(int(time.time())))


class App:
    def __init__(self, settings: Settings):
        self.s = settings
        self.store = Store(settings.db_path)
        self.gh = GitHub(settings.github_token, settings.github_user)
        self.bot = Bot(settings.tg_token, default=DefaultBotProperties(
            parse_mode="HTML", link_preview_is_disabled=True))
        self.lock = asyncio.Lock()  # проход и ручная /check не должны идти одновременно
        self.last: CycleResult | None = None  # последний проход: текущие источники и страницы

    async def send(self, text: str, reply_markup=None):
        if not self.s.tg_admin:
            log.warning("TG_ADMIN_ID не задан, сообщение не отправлено")
            return
        try:
            await self.bot.send_message(self.s.tg_admin, text[:4000], reply_markup=reply_markup)
        except Exception:
            log.exception("не удалось отправить сообщение в Telegram")

    async def cycle(self, report_empty: bool = False):
        async with self.lock:
            res = await run_cycle(self.gh, self.store, self.s.include_private)
        self.last = res
        msgs = format_cycle(self.s.github_user, res)
        for m in msgs:
            await self.send(m)
        await self.report_errors(res)
        if report_empty and not msgs:
            await self.send(f"Проверил {len(res.repos)} репозиториев — нового ничего.")
        self.store.set_meta("last_cycle", datetime.now().strftime("%d.%m.%Y %H:%M"))

    async def report_errors(self, res: CycleResult):
        """Одна и та же ошибка приходит один раз, а не каждый час. Когда проходит — сообщаем."""
        sig = "|".join(sorted(
            [f"{r.repo}:{e.split(':')[0]}" for r in res.repos if r.error for e in r.error.split("; ")]
            + (["general"] if res.error else [])))
        prev = self.store.get_meta("errors_sig", "")
        if sig == prev:
            return
        if sig:
            if err := format_errors(res):
                await self.send(err + "\n\nПока ошибки те же — повторно писать не буду.")
        else:
            await self.send("✅ Ошибки при опросе GitHub ушли, всё работает.")
        self.store.set_meta("errors_sig", sig)

    def current_report(self) -> tuple:
        """Общий отчёт; запоминаем момент просмотра, чтобы в следующий раз отметить новое 🆕."""
        seen = self.store.get_meta("seen_at", "")
        text, kb = overview(self.s.github_user, self.store, seen)
        self.store.set_meta("seen_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        return text, kb

    async def send_report(self, title: str, days: int, only_new: bool):
        for msg in build_report(title, self.s.github_user, days, self.store, self.last, only_new=only_new):
            await self.send(msg)

    async def maybe_initial_report(self):
        """Первый запуск: отчёт по всему, что GitHub хранит (трафик — 14 дней)."""
        if self.store.get_meta("initial_report"):
            return
        await self.send_report("🚀 Стартовый отчёт: вся доступная история (трафик — 14 дней, больше GitHub не хранит)",
                               14, only_new=False)
        await self.send(f"Кнопка «{REPORT_BUTTON}» внизу — сводка по всем репо с разбивкой по дням.",
                        reply_markup=MAIN_KB)
        self.store.set_meta("initial_report", datetime.now().isoformat(timespec="seconds"))
        self.store.set_meta("baseline_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        # чтобы в эту же неделю не прилетел ещё и недельный отчёт
        self.store.set_meta("last_digest", datetime.now().strftime("%G-W%V"))

    async def maybe_digest(self):
        now = datetime.now()
        key = now.strftime("%G-W%V")  # один отчёт на неделю
        if (now.weekday() == self.s.digest_weekday and now.hour >= self.s.digest_hour
                and self.store.get_meta("last_digest") != key):
            await self.send_report("📊 Итоги недели", 7, only_new=True)
            self.store.set_meta("last_digest", key)

    def week_text(self) -> str:
        return format_totals("📊 Просмотры за 7 дней", self.store.traffic_totals(7),
                             self.store.counters("star"))

    async def loop(self):
        while True:
            try:
                await self.cycle()
                await self.maybe_initial_report()
                await self.maybe_digest()
            except Exception:
                log.exception("ошибка в цикле")
            heartbeat(self.s.data_dir)
            # спим кусками, чтобы heartbeat не протухал при длинном интервале
            slept = 0
            while slept < self.s.poll_interval:
                await asyncio.sleep(min(300, self.s.poll_interval - slept))
                slept += 300
                heartbeat(self.s.data_dir)


MAIN_KB = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=REPORT_BUTTON)]], resize_keyboard=True,
                              is_persistent=True)


def build_router(app: App) -> Router:
    r = Router()
    # бот отвечает только владельцу, остальных молча игнорирует
    r.message.filter(F.from_user.id == app.s.tg_admin)
    r.callback_query.filter(F.from_user.id == app.s.tg_admin)

    @r.message(F.text == REPORT_BUTTON)
    @r.message(Command("now"))
    async def current(m: Message):
        text, kb = app.current_report()
        await m.answer(text, reply_markup=kb)

    @r.callback_query(F.data == "overview")
    async def cb_overview(c: CallbackQuery):
        text, kb = app.current_report()
        await _edit(c, text, kb)

    @r.callback_query(F.data.startswith("repo:"))
    async def cb_repo(c: CallbackQuery):
        text, kb = repo_detail(app.s.github_user, app.store, c.data[5:], app.last)
        await _edit(c, text, kb)

    async def _edit(c: CallbackQuery, text: str, kb):
        try:
            await c.message.edit_text(text, reply_markup=kb)
        except Exception as e:  # «message is not modified» при повторном нажатии — не ошибка
            if "not modified" not in str(e):
                log.exception("не удалось обновить сообщение")
        await c.answer()

    @r.message(Command("start", "help"))
    async def start(m: Message):
        await m.answer(
            "Слежу за GitHub <b>{}</b>.\n\n"
            "Кнопка «{}» внизу — сводка по всем репо, дальше можно провалиться в любой.\n\n"
            "/check — проверить сейчас\n"
            "/stats [дней] — просмотры по репо (по умолчанию 30)\n"
            "/week — сводка просмотров за 7 дней\n"
            "/report — полный отчёт за 7 дней прямо сейчас\n"
            "/sources — откуда приходят\n\n"
            "Последний проход: {}".format(app.s.github_user, REPORT_BUTTON,
                                          app.store.get_meta("last_cycle", "ещё не было")),
            reply_markup=MAIN_KB)

    @r.message(Command("check"))
    async def check(m: Message):
        await m.answer("Проверяю…")
        await app.cycle(report_empty=True)

    @r.message(Command("stats"))
    async def stats(m: Message, command: CommandObject):
        days = int(command.args) if command.args and command.args.isdigit() else 30
        days = max(1, min(days, 3650))
        await m.answer(format_totals(f"📊 Просмотры за {days} дн.", app.store.traffic_totals(days),
                                     app.store.counters("star")))

    @r.message(Command("week"))
    async def week(m: Message):
        await m.answer(app.week_text())

    @r.message(Command("report"))
    async def report(m: Message):
        if not app.last:
            await m.answer("Ещё не было ни одного прохода, подожди минуту.")
            return
        await app.send_report("📊 Отчёт за 7 дней", 7, only_new=True)

    @r.message(Command("sources"))
    async def sources(m: Message):
        await m.answer(format_sources(app.store.referrer_names()))

    return r


async def amain():
    settings = load_settings()
    setup_logging(settings.data_dir)
    app = App(settings)
    dp = Dispatcher()
    dp.include_router(build_router(app))
    log.info("старт: пользователь %s, интервал %d с, приватные репо: %s",
             settings.github_user, settings.poll_interval, "да" if settings.include_private else "нет")
    heartbeat(settings.data_dir)
    task = asyncio.create_task(app.loop())
    try:
        await dp.start_polling(app.bot)
    finally:
        task.cancel()
        await app.gh.close()
        await app.bot.session.close()


def main():
    asyncio.run(amain())


if __name__ == "__main__":
    main()
