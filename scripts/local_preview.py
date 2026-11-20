# Docker olmadan yerel önizleme: SQLite + kuyruk yerine aynı süreçte arka plan iş parçacığı.
# Sadece arayüze ve modele bakmak içindir; gerçek kuyruk davranışı (ack, yeniden teslim) burada YOK.
# Çalıştırma:  .venv/bin/python scripts/local_preview.py   ->  http://localhost:8000
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # proje kökünden "app" import edilsin
os.environ.setdefault("API_KEY", "demo-key")
os.environ.setdefault("LIVE_TRAFFIC", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///./local_preview.db")

import uvicorn  # noqa: E402

from app import api, db, seed, worker  # noqa: E402


class InProcessPublisher:
    """RabbitMQ'nun yerine geçer: job_id'yi kuyruğa koymak yerine worker fonksiyonunu doğrudan çağırır."""

    def publish(self, job_id: str) -> None:
        threading.Thread(target=worker.run_job, args=(api.engine, api.settings, job_id), daemon=True).start()

    def ping(self) -> bool:
        return True


if __name__ == "__main__":
    db.metadata.drop_all(api.engine)  # şema değişmiş olabilir: her açılışta temiz başla
    seed.load(api.engine)
    api.app.dependency_overrides[api.get_publisher] = InProcessPublisher
    uvicorn.run(api.app, host="127.0.0.1", port=8000, log_config=None)
