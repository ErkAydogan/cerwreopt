# Test ortamı: SQLite dosyası + sahte yayıncı. Postgres ya da RabbitMQ gerekmez, CI'da saniyeler sürer.
# Ortam değişkenleri app.api import edilmeden ÖNCE ayarlanmalı (ayarlar import anında okunur).
import os
import tempfile

os.environ["API_KEY"] = "test-key"
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/test.db"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import api, db, seed  # noqa: E402


class FakePublisher:
    def __init__(self):
        self.published = []
        self.fail = False

    def publish(self, job_id):
        if self.fail:
            raise ConnectionError("broker down")
        self.published.append(job_id)

    def ping(self):
        return True


@pytest.fixture
def publisher():
    return FakePublisher()


@pytest.fixture
def engine():
    db.metadata.drop_all(api.engine)
    seed.load(api.engine)  # her test temiz demo verisiyle başlar
    return api.engine


@pytest.fixture
def client(engine, publisher):
    api.app.dependency_overrides[api.get_publisher] = lambda: publisher
    with TestClient(api.app, headers={"X-API-Key": "test-key"}) as c:
        yield c
    api.app.dependency_overrides.clear()
