# Kuyruk tüketicisi: "reopt_jobs" kuyruğundan job_id alır, işi sahiplenir, modeli çalıştırır, sonucu yazar.
# Güvenilirlik: prefetch=1, iş BİTİNCE ack. Worker çökerse mesaj ack edilmemiştir, broker yeniden teslim eder.
# Entegrasyon kodu burada; model.solve saf kalır (ne veritabanını ne kuyruğu bilir).
import json
import logging
import os
import signal
import threading
import time
from dataclasses import asdict

import pika
from sqlalchemy import delete, select, update
from sqlalchemy.engine import Engine

from app import broker, config, db, logging_json, model
from app.logging_json import log

logger = logging.getLogger("worker")
MAX_ATTEMPTS = 3  # aynı iş worker'ı 3 kez düşürdüyse zehirli mesajdır; sonsuza kadar denemeyiz


def claim(conn, job_id: str, redelivered: bool) -> bool:
    """Atomik sahiplenme: tek UPDATE, satır kilidi yarışı veritabanında çözer.
    - Normal durumda sadece 'queued' iş alınır; satır güncellenmediyse başkası almıştır.
    - redelivered=True ise önceki tüketici ack edemeden ölmüştür: 'running' kalmış iş devralınır."""
    claimable = ["queued", "running"] if redelivered else ["queued"]
    result = conn.execute(
        update(db.jobs)
        .where(db.jobs.c.id == job_id, db.jobs.c.status.in_(claimable))
        .values(status="running", started_at=db.utcnow(), attempts=db.jobs.c.attempts + 1)
    )
    return result.rowcount == 1


def finish(engine: Engine, job_id: str, status: str, **values) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(db.jobs).where(db.jobs.c.id == job_id).values(status=status, finished_at=db.utcnow(), **values)
        )


def run_job(engine: Engine, settings: config.Settings, job_id: str, redelivered: bool = False) -> str:
    """Tek bir işi baştan sona işler; işin son durumunu (ya da 'skipped') döndürür."""
    with engine.begin() as conn:
        if not claim(conn, job_id, redelivered):
            log(logger, logging.INFO, "job not claimable (duplicate delivery), skipping", job_id=job_id)
            return "skipped"
        job = conn.execute(
            select(db.jobs.c.ops_date, db.jobs.c.attempts, db.jobs.c.params).where(db.jobs.c.id == job_id)
        ).one()
    log(logger, logging.INFO, "job claimed", job_id=job_id, attempts=job.attempts, redelivered=redelivered)

    if job.attempts > MAX_ATTEMPTS:
        error = f"gave up after {MAX_ATTEMPTS} attempts: worker died mid-job each time"
        finish(engine, job_id, "failed", error=error)
        log(logger, logging.ERROR, "poison job", job_id=job_id, error=error)
        return "failed"

    # 1) Girdiyi oku ve AYNEN sakla: sonuç sonradan aynı girdiyle yeniden üretilebilsin
    with engine.begin() as conn:
        pairings = db.load_pairings(conn, job.ops_date)
        crews = db.load_crews(conn)
        rules = config.rules(settings, job.params)
        broken = model.violations(pairings, rules)  # mesaj mevcut planda neyi bozdu?
        snapshot = {
            "pairings": [asdict(p) for p in pairings],
            "crews": [asdict(c) for c in crews],
            "rules": asdict(rules),
        }
        conn.execute(
            update(db.jobs).where(db.jobs.c.id == job_id).values(input_snapshot=snapshot, violations=broken)
        )
    log(logger, logging.INFO, "input loaded", job_id=job_id, pairings=len(pairings), violations=broken)

    # 2) Modeli çalıştır (saf fonksiyon). Çözüm sürerken bulunan her çözüm iş kaydına yazılır:
    #    arayüz uzun süren işte amaç ve sınırın yaklaşmasını canlı gösterir.
    last_write = 0.0

    def on_progress(points: list[dict]) -> None:
        nonlocal last_write
        if time.monotonic() - last_write < 0.3:  # veritabanını her çözümde değil, saniyede birkaç kez yaz
            return
        last_write = time.monotonic()
        with engine.begin() as conn:
            conn.execute(update(db.jobs).where(db.jobs.c.id == job_id).values(trace=list(points)))

    result = model.solve(pairings, crews, rules, on_progress)

    # 3) Çözücü durumunu iş durumuna çevir
    error = None
    if result.has_solution:
        status = "succeeded"  # süre sınırında durduysa da: FEASIBLE + gap, eldeki en iyi çözüm kullanılır
    elif result.solver_status == "UNKNOWN":
        status = "timed_out"  # süre doldu ve hiç çözüm bulunamadı: çözümsüz olduğu KANITLANMADI
        error = f"no solution within {rules.time_limit_s}s"
    else:
        status = "failed"  # INFEASIBLE çoğu zaman veri hatasıdır; hangi görevlerin çeliştiğini söyle
        error = f"{result.solver_status}: pairings {result.infeasible_core} cannot all be covered; check input data"

    # 4) Yeni atama + iş sonucu tek transaction'da: yarım yazılmış plan olmaz
    with engine.begin() as conn:
        if status == "succeeded":
            for change in result.changes:
                conn.execute(
                    update(db.assignments)
                    .where(db.assignments.c.pairing_id == change["pairing"])
                    .values(crew_id=change["to"], previous_crew_id=change["from"], changed_by_job=job_id)
                )
        conn.execute(
            update(db.jobs)
            .where(db.jobs.c.id == job_id)
            .values(
                status=status, finished_at=db.utcnow(), error=error,
                solver_status=result.solver_status, objective=result.objective, best_bound=result.best_bound,
                gap=result.gap, solve_seconds=result.solve_seconds,
                changes=result.changes, trace=result.trace, infeasible_core=result.infeasible_core,
                standby_used=result.standby_used,
            )
        )
    level = logging.INFO if status == "succeeded" else logging.ERROR
    log(
        logger, level, "job finished", job_id=job_id, status=status, solver_status=result.solver_status,
        objective=result.objective, best_bound=result.best_bound, gap=result.gap,
        solve_seconds=round(result.solve_seconds, 3), changes=len(result.changes), error=error,
    )
    return status


def chaos_watchdog(engine: Engine, job_id: str, done: threading.Event) -> None:
    """Çökme provası (sadece demo): API 'kill_worker' bayrağını koyduysa, iş 2 saniyedir sürerken süreci öldürür.
    os._exit temizlik yapmaz: ack gitmez, bağlantı kopar. Gerçek bir çökmeyle aynı sonuç: broker mesajı
    yeniden teslim eder, yeniden başlayan worker işi devralır."""
    done.wait(2)
    while not done.is_set():
        with engine.begin() as conn:
            armed = conn.execute(delete(db.flags).where(db.flags.c.name == "kill_worker")).rowcount
        if armed:  # bayrak silindi: ikinci denemede tekrar ölmeyiz
            log(logger, logging.ERROR, "chaos: killing worker mid-job", job_id=job_id)
            os._exit(1)
        done.wait(0.5)


def main() -> None:
    settings = config.load()
    logging_json.setup(settings.log_level)
    engine = db.make_engine(settings.database_url)
    db.init_db(engine)
    signal.signal(signal.SIGTERM, signal.default_int_handler)  # docker stop -> temiz kapanış

    params = pika.URLParameters(settings.rabbitmq_url)
    # Çözücü çalışırken bu iş parçacığı heartbeat gönderemez. Heartbeat süresi çözüm süresinden kısaysa
    # broker bağlantıyı ölü sayar ve mesajı yeniden teslim eder: iş iki kez koşar. O yüzden pay bırakıyoruz.
    params.heartbeat = int(settings.solver_time_limit_s * 2) + 60

    def on_message(channel, method, properties, body):
        try:
            job_id = json.loads(body)["job_id"]
        except (ValueError, KeyError, TypeError):
            log(logger, logging.ERROR, "unreadable queue message dropped", body=body[:200])
            channel.basic_ack(method.delivery_tag)
            return
        done = threading.Event()
        threading.Thread(target=chaos_watchdog, args=(engine, job_id, done), daemon=True).start()
        try:
            run_job(engine, settings, job_id, method.redelivered)
        except Exception as exc:  # modelden ya da koddan beklenmeyen hata: işi 'failed' yap, kuyruğu tıkama
            logger.exception("job crashed", extra={"job_id": job_id})
            finish(engine, job_id, "failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            done.set()
        # ack EN SONDA: buraya gelmeden süreç ölürse broker mesajı başka bir worker'a verir
        channel.basic_ack(method.delivery_tag)

    while True:  # broker henüz hazır değilse ya da bağlantı koparsa yeniden bağlan
        try:
            with pika.BlockingConnection(params) as connection:
                channel = connection.channel()
                broker.declare(channel)
                channel.basic_qos(prefetch_count=1)  # aynı anda tek iş; bitmeden yenisi verilmez
                channel.basic_consume(queue=broker.QUEUE, on_message_callback=on_message, auto_ack=False)
                log(logger, logging.INFO, "worker waiting for jobs", queue=broker.QUEUE, git_sha=settings.git_sha)
                channel.start_consuming()
        except pika.exceptions.AMQPConnectionError as exc:
            log(logger, logging.WARNING, "broker connection lost, retrying in 3s", error=str(exc))
            time.sleep(3)
        except KeyboardInterrupt:
            log(logger, logging.INFO, "worker stopping")
            return


if __name__ == "__main__":
    main()
