# Worker testleri: kuyruk olmadan run_job doğrudan çağrılır (RabbitMQ sadece job_id taşır).
# İki demo senaryosu ve güvenilirlik kuralları: çift teslim, çökme sonrası devralma, zehirli iş.
from sqlalchemy import select, update

from app import api, db, worker

DLA = "(DLA-THY1591-LTFM0830-EDDF-DOF/261123)"
CNL = "(CNL-THY2412-LTFM1500-LTAI-DOF/261123)"


def submit(client, message):
    return client.post("/messages", content=message).json()["job_id"]


def job_row(engine, job_id):
    with engine.connect() as conn:
        return conn.execute(select(db.jobs).where(db.jobs.c.id == job_id)).one()


def crew_of(engine, pairing_id):
    with engine.connect() as conn:
        return conn.execute(
            select(db.assignments.c.crew_id).where(db.assignments.c.pairing_id == pairing_id)
        ).scalar_one()


def test_demo1_delay_is_fixed_with_one_change_to_standby(client, engine):
    job_id = submit(client, DLA)
    assert worker.run_job(engine, api.settings, job_id) == "succeeded"
    job = job_row(engine, job_id)
    assert job.solver_status == "OPTIMAL" and job.gap == 0
    assert job.objective == 130  # 1 değişiklik (100) + 1 yedek (30)
    assert len(job.changes) == 1
    change = job.changes[0]
    assert change["from"] == "C1" and change["to"] in ("S1", "S2")
    assert "rest between P01 and P02 would be -25 min" in change["reason"]
    assert crew_of(engine, change["pairing"]) == change["to"]
    # Girdi anlık görüntüsü saklandı ve API'den okunabiliyor
    snapshot = client.get(f"/jobs/{job_id}/input").json()
    assert len(snapshot["pairings"]) == 14 and snapshot["rules"]["min_rest"] == 60


def test_demo2_cancel_then_delay_uses_freed_crew_instead_of_standby(client, engine):
    cnl_job = submit(client, CNL)
    assert worker.run_job(engine, api.settings, cnl_job) == "succeeded"
    assert job_row(engine, cnl_job).changes == []  # kural bozulmadı: plan aynı kalır

    dla_job = submit(client, DLA)
    assert worker.run_job(engine, api.settings, dla_job) == "succeeded"
    job = job_row(engine, dla_job)
    assert job.objective == 100  # yedek yerine boşa çıkan C3 kullanıldı
    assert [(c["pairing"], c["from"], c["to"]) for c in job.changes] == [("P02", "C1", "C3")]


def test_duplicate_delivery_is_skipped(client, engine):
    job_id = submit(client, DLA)
    assert worker.run_job(engine, api.settings, job_id) == "succeeded"
    assert worker.run_job(engine, api.settings, job_id) == "skipped"
    assert job_row(engine, job_id).attempts == 1


def test_running_job_is_taken_over_only_on_redelivery(client, engine):
    job_id = submit(client, DLA)
    with engine.begin() as conn:  # worker işi aldı ve ack edemeden öldü
        conn.execute(update(db.jobs).where(db.jobs.c.id == job_id).values(status="running", attempts=1))
    assert worker.run_job(engine, api.settings, job_id, redelivered=False) == "skipped"
    assert worker.run_job(engine, api.settings, job_id, redelivered=True) == "succeeded"
    assert job_row(engine, job_id).attempts == 2


def test_poison_job_fails_after_max_attempts(client, engine):
    job_id = submit(client, DLA)
    with engine.begin() as conn:
        conn.execute(
            update(db.jobs).where(db.jobs.c.id == job_id).values(status="running", attempts=worker.MAX_ATTEMPTS)
        )
    assert worker.run_job(engine, api.settings, job_id, redelivered=True) == "failed"
    assert "gave up" in job_row(engine, job_id).error


def test_infeasible_job_fails_with_diagnosis_and_keeps_plan(client, engine):
    job_id = submit(client, DLA)
    with engine.begin() as conn:  # veri hatası: P01 dönüş varışı yanlış girilmiş, görev 16,5 saat sürüyor
        conn.execute(
            update(db.flights).where(db.flights.c.pairing_id == "P01", db.flights.c.leg_no == 2).values(sta_min=1500)
        )
    assert worker.run_job(engine, api.settings, job_id) == "failed"
    job = job_row(engine, job_id)
    assert job.solver_status == "INFEASIBLE"
    assert job.infeasible_core == ["P01"]  # teşhis doğrudan bozuk görevi gösteriyor
    assert "check input data" in job.error
    assert crew_of(engine, "P02") == "C1"  # başarısız iş mevcut plana dokunmaz
