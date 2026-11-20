# Yapılandırılmış log: her satır tek bir JSON nesnesi, her satırda job_id alanı (yoksa null).
# Sadece stdlib logging; Loki/ELK/Datadog gibi toplayıcılar bunu olduğu gibi okuyabilir.
import json
import logging
import sys
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "job_id": getattr(record, "job_id", None),
        }
        entry.update(getattr(record, "fields", {}))  # extra={"fields": {...}} ile gelen ek alanlar
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def setup(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    logging.getLogger("pika").setLevel(logging.WARNING)  # pika çok konuşkan


def log(logger: logging.Logger, level: int, msg: str, job_id: str | None = None, **fields) -> None:
    """Kısayol: log(logger, logging.INFO, "job claimed", job_id=..., attempts=1)"""
    logger.log(level, msg, extra={"job_id": job_id, "fields": fields})
