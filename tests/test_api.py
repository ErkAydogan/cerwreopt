# API testleri: kimlik doğrulama, dead-letter, idempotency ve kuyruk hatasında davranış.
# Güvenilirlik kurallarının her biri için en az bir test var; görüşmede bunlar gösterilebilir.
from sqlalchemy import func, select

from app import db

HEADER = "FF LTFMZPZX\n231200 EDDFZQZX\n"
DLA = "(DLA-THY1591-LTFM0830-EDDF-DOF/261123)"


def count(engine, table):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def legs_of(engine, pairing_id):
    with engine.connect() as conn:
        rows = conn.execute(
            select(db.flights).where(db.flights.c.pairing_id == pairing_id).order_by(db.flights.c.leg_no)
        )
        return [(r.std_min, r.sta_min, r.status) for r in rows]


def test_health_is_open_and_reports_sha(client):
    r = client.get("/health", headers={"X-API-Key": ""})
    assert r.status_code == 200
    assert r.json() == {"db": "ok", "rabbitmq": "ok", "git_sha": "dev"}


def test_write_endpoints_require_api_key(client):
    assert client.post("/messages", content=DLA, headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.post("/jobs", headers={"X-API-Key": ""}).status_code == 401


def test_malformed_message_is_dead_lettered(client, engine, publisher):
    r = client.post("/messages", content="(DLA-THY1591-LTFM2460-EDDF-DOF/261123)")
    assert r.status_code == 422
    assert "invalid time" in r.json()["error"]
    assert count(engine, db.dead_letters) == 1
    assert publisher.published == []


def test_unknown_flight_is_dead_lettered(client, engine):
    r = client.post("/messages", content="(DLA-THY9999-LTFM0830-EDDF-DOF/261123)")
    assert r.status_code == 422
    assert "unknown flight" in r.json()["error"]
    assert count(engine, db.dead_letters) == 1


def test_dla_shifts_both_legs_and_queues_one_job(client, engine, publisher):
    r = client.post("/messages", content=HEADER + DLA)
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued" and body["duplicate"] is False
    assert publisher.published == [body["job_id"]]
    # 07:00 -> 08:30: gidiş ve dönüş 90 dk kayar
    assert legs_of(engine, "P01") == [(510, 700, "DELAYED"), (760, 940, "SCHEDULED")]


def test_same_message_twice_creates_one_job(client, engine):
    first = client.post("/messages", content=HEADER + DLA).json()
    # Aynı gövde, farklı başlık zaman damgası: yeniden iletim
    second = client.post("/messages", content=HEADER.replace("231200", "231207") + DLA).json()
    assert second["job_id"] == first["job_id"]
    assert second["duplicate"] is True
    assert count(engine, db.jobs) == 1
    assert legs_of(engine, "P01")[0][0] == 510  # gecikme iki kez uygulanmadı


def test_cnl_cancels_whole_pairing(client, engine):
    r = client.post("/messages", content="(CNL-THY2412-LTFM1500-LTAI-DOF/261123)")
    assert r.status_code == 202
    assert [s for _, _, s in legs_of(engine, "P08")] == ["CANCELLED", "CANCELLED"]


def test_dep_updates_status_without_job(client, engine, publisher):
    r = client.post("/messages", content="(DEP-THY1591-LTFM0702-EDDF-DOF/261123)")
    assert r.status_code == 200
    assert r.json()["job_id"] is None
    assert legs_of(engine, "P01")[0][2] == "DEPARTED"
    assert count(engine, db.jobs) == 0 and publisher.published == []


def test_manual_job_respects_idempotency_key(client, engine):
    a = client.post("/jobs", headers={"Idempotency-Key": "abc"}).json()
    b = client.post("/jobs", headers={"Idempotency-Key": "abc"}).json()
    c = client.post("/jobs").json()
    assert a["job_id"] == b["job_id"] != c["job_id"]
    assert count(engine, db.jobs) == 2


def test_queue_outage_keeps_job_and_retry_publishes_it(client, engine, publisher):
    publisher.fail = True
    r = client.post("/messages", content=DLA)
    assert r.status_code == 503
    job_id = r.json()["detail"]["job_id"]
    assert count(engine, db.jobs) == 1  # iş kaybolmadı, 'queued' bekliyor

    publisher.fail = False
    retry = client.post("/messages", content=DLA)
    assert retry.status_code == 202
    assert retry.json() == {"job_id": job_id, "status": "queued", "duplicate": True}
    assert publisher.published == [job_id]


def test_job_and_schedule_are_readable(client):
    job_id = client.post("/messages", content=DLA).json()["job_id"]
    job = client.get(f"/jobs/{job_id}").json()
    assert job["status"] == "queued" and job["trigger"] == DLA
    assert client.get("/jobs/nope").status_code == 404
    schedule = client.get("/schedule").json()
    assert len(schedule["pairings"]) == 14 and len(schedule["crews"]) == 7


def test_pairing_options_explain_who_can_fly_it(client):
    client.post("/messages", content=DLA)  # gecikme uygulandı, henüz çözülmedi
    options = {o["crew"]: o for o in client.get("/pairings/P02/options").json()["options"]}
    assert options["C1"]["current"] and not options["C1"]["feasible"]  # mevcut ekip artık uçamaz
    assert "P01 and P02 would overlap by 25 min" in options["C1"]["reason"]
    assert not options["C3"]["feasible"] and "P08" in options["C3"]["reason"]
    assert options["S1"] == {
        "crew": "S1", "is_standby": True, "current": False, "feasible": True, "reason": None, "cost": 130,
        "duty_after": 235,
    }
    assert client.get("/pairings/NOPE/options").status_code == 404
