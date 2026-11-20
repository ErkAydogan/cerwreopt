# Tüm yapılandırma ortam değişkenlerinden okunur; kodda gizli bilgi ya da ortam farkı yok.
# Test, UAT ve canlı aynı imajı kullanır, sadece ortam değişkenleri değişir (bkz. .env.example).
import os
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Settings:
    database_url: str
    rabbitmq_url: str
    api_key: str
    ops_date: date  # demo günü; GET /schedule varsayılanı
    solver_time_limit_s: float
    min_rest: int
    max_duty: int
    log_level: str
    git_sha: str
    public_url: str  # demonun dışarıdan erişilen adresi; doluysa arayüz bu adresin QR kodunu gösterir


def load() -> Settings:
    return Settings(
        database_url=os.environ.get("DATABASE_URL", "sqlite:///./local.db"),
        rabbitmq_url=os.environ.get("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/%2F"),
        api_key=os.environ["API_KEY"],  # varsayılan yok: anahtarsız açılmasın
        ops_date=date.fromisoformat(os.environ.get("OPS_DATE", "2026-11-23")),
        solver_time_limit_s=float(os.environ.get("SOLVER_TIME_LIMIT_S", "10")),
        min_rest=int(os.environ.get("MIN_REST", "60")),
        max_duty=int(os.environ.get("MAX_DUTY", str(13 * 60))),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        git_sha=os.environ.get("GIT_SHA", "dev"),
        public_url=os.environ.get("PUBLIC_URL", ""),
    )
