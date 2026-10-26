"""Настройки из переменных окружения."""
import os
from dataclasses import dataclass


@dataclass
class Settings:
    github_token: str
    github_user: str
    tg_token: str
    tg_admin: int
    poll_interval: int        # секунды между проходами
    data_dir: str
    include_private: bool     # следить и за приватными репо
    digest_weekday: int       # 0 = понедельник, 6 = воскресенье
    digest_hour: int

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "watcher.db")


def load_settings(env=os.environ) -> Settings:
    token = env.get("GITHUB_TOKEN", "").strip()
    user = env.get("GITHUB_USER", "").strip()
    tg_token = env.get("TG_BOT_TOKEN", "").strip()
    if not token or not user or not tg_token:
        raise RuntimeError("GITHUB_TOKEN, GITHUB_USER и TG_BOT_TOKEN обязательны")
    return Settings(
        github_token=token,
        github_user=user,
        tg_token=tg_token,
        tg_admin=int(env.get("TG_ADMIN_ID", "0") or 0),
        poll_interval=int(env.get("POLL_INTERVAL", "3600")),
        data_dir=env.get("DATA_DIR", "/app/data"),
        include_private=env.get("INCLUDE_PRIVATE", "false").lower() in ("1", "true", "yes"),
        digest_weekday=int(env.get("DIGEST_WEEKDAY", "6")),
        digest_hour=int(env.get("DIGEST_HOUR", "20")),
    )
