# HTTP katmanı (FastAPI): AFTN mesajı alır, uçuş tablosunu günceller, yeniden planlama işini kuyruğa koyar.
# Çözücüyü burada ÇALIŞTIRMAZ: iş satırını yazar, job_id yayınlar, hemen 202 döner; işi worker yapar.
# Yazma uçları X-API-Key ister; GET uçları açıktır.
import io
import logging
import secrets
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

import segno
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from app import aftn, broker, config, db, live, logging_json, model, notam, seed
from app.airports import AIRPORTS
from app.logging_json import log

settings = config.load()
logging_json.setup(settings.log_level)
logger = logging.getLogger("api")
engine = db.make_engine(settings.database_url)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db(engine)
    yield


STATIC_DIR = Path(__file__).parent / "static"
app = FastAPI(title="crewreopt", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")  # harita altlığı gibi sabit dosyalar


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    # compare_digest: karşılaştırma süresi anahtarın ne kadarının tuttuğunu sızdırmaz
    if not secrets.compare_digest(x_api_key or "", settings.api_key):
        raise HTTPException(status_code=401, detail="missing or invalid X-API-Key")


def get_publisher() -> broker.Publisher:
    return broker.Publisher(settings.rabbitmq_url)  # testlerde sahte yayıncıyla değiştirilir


# --- Yardımcılar --------------------------------------------------------------------------------


def dead_letter(raw: str, error: str) -> JSONResponse:
    """Okunamayan mesaj kaybolmaz: ham metin + neden saklanır, gönderen 422 alır."""
    with engine.begin() as conn:
        conn.execute(insert(db.dead_letters).values(raw=raw, error=error))
    log(logger, logging.WARNING, "message dead-lettered", error=error)
    return JSONResponse(status_code=422, content={"error": error, "dead_lettered": True})


def apply_message(conn: Connection, msg: aftn.AftnMessage, flight_id: int) -> None:
    """Mesajı uçuş tablosuna işler. Dört tip de tekrar uygulanınca aynı sonucu verir (idempotent):
    DLA mutlak saat taşır, yani ikinci uygulamada kaydırma 0 çıkar."""
    leg = conn.execute(select(db.flights).where(db.flights.c.id == flight_id)).one()
    same_pairing = db.flights.c.pairing_id == leg.pairing_id
    if msg.msg_type == "DLA":
        delta = msg.minutes - leg.std_min
        # Gecikme sonraki bacaklara aynen yansır (yerde kalış süresi sabit varsayımı)
        conn.execute(
            update(db.flights)
            .where(same_pairing, db.flights.c.leg_no >= leg.leg_no)
            .values(std_min=db.flights.c.std_min + delta, sta_min=db.flights.c.sta_min + delta)
        )
        conn.execute(update(db.flights).where(db.flights.c.id == flight_id).values(status="DELAYED"))
    elif msg.msg_type == "CNL":
        # Bir bacak iptalse gidiş-dönüş görevin tamamı iptaldir
        conn.execute(update(db.flights).where(same_pairing).values(status="CANCELLED"))
    else:
        status = {"DEP": "DEPARTED", "ARR": "ARRIVED"}[msg.msg_type]
        conn.execute(update(db.flights).where(db.flights.c.id == flight_id).values(status=status))


def apply_closure(conn: Connection, closure: notam.Notam) -> int:
    """Meydan kapanışını uçuş tablosuna işler; etkilenen bacak sayısını döndürür.
    Kapalı aralıkta o meydandan kalkacak ya da oraya inecek her bacak açılış saatine ertelenir; erteleme aynı
    görevin sonraki bacaklarına taşınır. Tekrar uygulanırsa aralıkta bacak kalmadığı için hiçbir şey değişmez."""
    day = closure.start.date()
    midnight = datetime.combine(day, datetime.min.time())
    closed_from = int((closure.start - midnight).total_seconds() // 60)
    closed_to = int((closure.end - midnight).total_seconds() // 60)
    legs = conn.execute(
        select(db.flights).where(db.flights.c.dof == day).order_by(db.flights.c.pairing_id, db.flights.c.leg_no)
    ).all()
    carried: dict[str, int] = {}  # görev -> önceki bacaklardan devreden gecikme (dk)
    affected = 0
    for leg in legs:
        if leg.status == "CANCELLED":
            continue
        shift = carried.get(leg.pairing_id, 0)
        std, sta = leg.std_min + shift, leg.sta_min + shift
        hold = 0
        if leg.adep == closure.aerodrome and closed_from <= std < closed_to:
            hold = closed_to - std  # kalkış açılışa kadar bekler
        elif leg.ades == closure.aerodrome and closed_from <= sta < closed_to:
            hold = closed_to - sta  # iniş açılışa denk gelecek şekilde geç kalkar
        shift += hold
        carried[leg.pairing_id] = shift
        if shift:
            values = {"std_min": leg.std_min + shift, "sta_min": leg.sta_min + shift}
            if hold:
                values["status"] = "DELAYED"
                affected += 1
            conn.execute(update(db.flights).where(db.flights.c.id == leg.id).values(**values))
    return affected


def find_job(conn: Connection, key: str):
    return conn.execute(select(db.jobs.c.id, db.jobs.c.status).where(db.jobs.c.idempotency_key == key)).first()


def create_job_once(key: str, ops_date: date, trigger: str, before_insert=None, params=None) -> tuple[str, str, bool]:
    """İşi en fazla bir kez oluşturur; (job_id, status, duplicate) döner.
    Önce anahtara bakar; yarış durumunda son sözü unique constraint söyler."""
    try:
        with engine.begin() as conn:  # tek transaction: uçuş güncellemesi + iş satırı ya birlikte ya hiç
            existing = find_job(conn, key)
            if existing:
                return existing.id, existing.status, True
            if before_insert:
                before_insert(conn)
            job_id = str(uuid4())
            conn.execute(
                insert(db.jobs).values(
                    id=job_id, idempotency_key=key, ops_date=ops_date, trigger=trigger, status="queued", params=params
                )
            )
            return job_id, "queued", False
    except IntegrityError:
        # Aynı mesaj aynı anda iki istekle geldi; diğeri kazandı. Onun işini döndür.
        with engine.connect() as conn:
            existing = find_job(conn, key)
        return existing.id, existing.status, True


def enqueue(job_id: str, status: str, duplicate: bool, publisher: broker.Publisher) -> JSONResponse:
    """İş hâlâ 'queued' ise yayınla. Yinelenen istekte de yayınlarız: ilk yayın başarısız olmuş olabilir;
    mesaj iki kez giderse worker'daki atomik queued->running geçişi ikincisini eler."""
    if status == "queued":
        try:
            publisher.publish(job_id)
        except Exception as exc:
            log(logger, logging.ERROR, "publish failed, job stays queued", job_id=job_id, error=str(exc))
            detail = {"job_id": job_id, "error": "queue unavailable; job is saved, resend to retry"}
            raise HTTPException(status_code=503, detail=detail) from exc
    log(logger, logging.INFO, "job accepted", job_id=job_id, duplicate=duplicate, status=status)
    return JSONResponse(status_code=202, content={"job_id": job_id, "status": status, "duplicate": duplicate})


# --- Yazma uçları -------------------------------------------------------------------------------


@app.post("/messages", dependencies=[Depends(require_api_key)])
async def post_message(request: Request, publisher: broker.Publisher = Depends(get_publisher)):
    raw = (await request.body()).decode("utf-8", errors="replace")
    if notam.is_notam(raw):  # AFTN ağı NOTAM da taşır; meydan kapanışı birçok uçuşu birden etkiler
        try:
            closure = notam.parse(raw)
        except notam.NotamParseError as exc:
            return dead_letter(raw, str(exc))
        ops_date = closure.start.date()
        key = f"reopt:{ops_date.isoformat()}:{closure.body_hash()}"
        job_id, status, duplicate = create_job_once(
            key, ops_date, closure.summary(), before_insert=lambda conn: apply_closure(conn, closure)
        )
        return enqueue(job_id, status, duplicate, publisher)
    try:
        msg = aftn.parse(raw)
        with engine.connect() as conn:
            flight_id = aftn.resolve_flight(msg, db.flight_index(conn, msg.dof))
    except (aftn.AftnParseError, aftn.UnknownFlightError) as exc:
        return dead_letter(raw, str(exc))

    if not msg.triggers_replan:  # DEP / ARR: sadece durum güncellenir
        with engine.begin() as conn:
            apply_message(conn, msg, flight_id)
        log(logger, logging.INFO, "flight status updated", type=msg.msg_type, callsign=msg.callsign)
        return {"type": msg.msg_type, "callsign": msg.callsign, "job_id": None}

    # Anahtar başlıktan değil gövdeden: aynı mesaj yeniden iletilirse ikinci iş oluşmaz
    key = f"reopt:{msg.dof.isoformat()}:{msg.body_hash()}"
    job_id, status, duplicate = create_job_once(
        key, msg.dof, msg.body, before_insert=lambda conn: apply_message(conn, msg, flight_id)
    )
    return enqueue(job_id, status, duplicate, publisher)


@app.post("/jobs", dependencies=[Depends(require_api_key)])
def post_job(
    ops_date: date | None = None,
    change_penalty: int | None = Query(default=None, ge=0),
    standby_penalty: int | None = Query(default=None, ge=0),
    idempotency_key: str | None = Header(default=None),
    publisher: broker.Publisher = Depends(get_publisher),
):
    """Elle yeniden planlama. İstemci Idempotency-Key gönderirse tekrar denemeler aynı işi döndürür.
    Ağırlıklar verilirse sadece bu iş için geçerlidir: 'yedek daha pahalı olsaydı plan ne olurdu?'"""
    ops_date = ops_date or settings.ops_date
    weights = {"change_penalty": change_penalty, "standby_penalty": standby_penalty}
    params = {name: value for name, value in weights.items() if value is not None}
    trigger = "manual" + (f" {params}" if params else "")
    key = f"reopt:{ops_date.isoformat()}:manual:{idempotency_key or uuid4()}"
    job_id, status, duplicate = create_job_once(key, ops_date, trigger, params=params or None)
    return enqueue(job_id, status, duplicate, publisher)


@app.post("/admin/reset", dependencies=[Depends(require_api_key)])
def reset(size: str = "demo"):
    """Demo provası için veriyi baştan yükler. size=demo: 14 görev; size=big: fırtına senaryosunun büyük günü."""
    if size == "big":
        crews, pairings = seed.big_day()
        seed.load(engine, crews, pairings)
    else:
        seed.load(engine)
    return {"reset": True, "size": size}


@app.post("/admin/chaos", dependencies=[Depends(require_api_key)])
def chaos():
    """Çökme provası: worker, çalıştırdığı iş 2 saniyeyi geçince kendini aniden kapatır (bkz. worker.chaos_watchdog)."""
    with engine.begin() as conn:
        conn.execute(delete(db.flags).where(db.flags.c.name == "kill_worker"))
        conn.execute(insert(db.flags).values(name="kill_worker", value="armed"))
    log(logger, logging.WARNING, "chaos armed: worker will be killed during its next long job")
    return {"armed": True}


# --- Okuma uçları -------------------------------------------------------------------------------

JOB_FIELDS = [c for c in db.jobs.c if c.name != "input_snapshot"]  # anlık görüntü büyük; ayrı uçtan verilir


@app.get("/jobs")
def list_jobs(limit: int = 10):
    with engine.connect() as conn:
        rows = conn.execute(select(*JOB_FIELDS).order_by(db.jobs.c.created_at.desc()).limit(limit))
        return [dict(r._mapping) for r in rows]


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    with engine.connect() as conn:
        row = conn.execute(select(*JOB_FIELDS).where(db.jobs.c.id == job_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    return dict(row._mapping)


@app.get("/jobs/{job_id}/input")
def get_job_input(job_id: str):
    """Modele verilen girdinin aynısı. 'Dün model neden bunu önerdi?' sorusu bununla yeniden üretilir."""
    with engine.connect() as conn:
        row = conn.execute(select(db.jobs.c.input_snapshot).where(db.jobs.c.id == job_id)).first()
    if row is None or row.input_snapshot is None:
        raise HTTPException(status_code=404, detail="no input snapshot (job not started yet?)")
    return row.input_snapshot


@app.get("/schedule")
def get_schedule(ops_date: date | None = None):
    ops_date = ops_date or settings.ops_date
    with engine.connect() as conn:
        crew_rows = conn.execute(select(db.crews).order_by(db.crews.c.is_standby, db.crews.c.id)).all()
        assigned = {r.pairing_id: r for r in conn.execute(select(db.assignments))}
        legs = conn.execute(
            select(db.flights)
            .where(db.flights.c.dof == ops_date)
            .order_by(db.flights.c.pairing_id, db.flights.c.leg_no)
        ).all()
    pairings: dict[str, dict] = {}
    for leg in legs:
        a = assigned.get(leg.pairing_id)
        p = pairings.setdefault(
            leg.pairing_id,
            {
                "id": leg.pairing_id,
                "crew": a.crew_id if a else None,
                "previous_crew": a.previous_crew_id if a else None,
                "changed_by_job": a.changed_by_job if a else None,
                "legs": [],
            },
        )
        p["legs"].append(
            {
                "callsign": leg.callsign, "adep": leg.adep, "ades": leg.ades, "status": leg.status,
                "std_min": leg.std_min, "sta_min": leg.sta_min, "orig_std_min": leg.orig_std_min,
            }
        )
    return {
        "date": ops_date.isoformat(),
        "rules": asdict(config.rules(settings)),
        "airports": {icao: {"name": name, "lat": lat, "lon": lon} for icao, (name, lat, lon) in AIRPORTS.items()},
        "crews": [{"id": c.id, "is_standby": c.is_standby} for c in crew_rows],
        "pairings": list(pairings.values()),
    }


@app.get("/pairings/{pairing_id}/options")
def pairing_options(pairing_id: str, ops_date: date | None = None):
    """Bu görevi hangi ekipler uçabilir? Her ekip için: uygun mu, değilse hangi kural bozulur, uygunsa maliyeti.
    Çözücü çalışmaz; modeldeki kuralların düz Python hali (model.why_not) kullanılır."""
    with engine.connect() as conn:
        pairings = db.load_pairings(conn, ops_date or settings.ops_date)
        crews = db.load_crews(conn)
    target = next((p for p in pairings if p.id == pairing_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="pairing not found")
    rules = config.rules(settings)
    others = [p for p in pairings if not p.cancelled and p.id != pairing_id]
    options = []
    for crew in crews:
        on_crew = [p for p in others if p.current_crew == crew.id]
        reason = model.why_not(target, crew.id, on_crew, rules)
        is_current = crew.id == target.current_crew
        # Maliyet amaç fonksiyonuyla aynı: değişiklik + (boştaki bir yedek devreye giriyorsa) yedek
        activates_standby = crew.is_standby and not on_crew
        cost = 0 if is_current else rules.change_penalty + (rules.standby_penalty if activates_standby else 0)
        options.append(
            {
                "crew": crew.id, "is_standby": crew.is_standby, "current": is_current,
                "feasible": reason is None, "reason": reason, "cost": cost if reason is None else None,
                "duty_after": sum(p.duration for p in on_crew) + target.duration,
            }
        )
    return {
        "pairing": pairing_id, "start": target.start, "end": target.end, "duration": target.duration,
        "locked": target.locked, "cancelled": target.cancelled, "options": options,
    }


@app.get("/health")
def health(publisher: broker.Publisher = Depends(get_publisher)):
    checks = {}
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception as exc:
        checks["db"] = f"error: {exc}"
    try:
        publisher.ping()
        checks["rabbitmq"] = "ok"
    except Exception as exc:
        checks["rabbitmq"] = f"error: {exc}"
    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse(status_code=200 if healthy else 503, content={**checks, "git_sha": settings.git_sha})


@app.get("/live/thy")
def live_thy():
    """Haritadaki canlı katman: bölgedeki THY çağrı kodlu uçakların anlık konumu (kaynak: OpenSky Network)."""
    if not settings.live_traffic:
        return {"enabled": False, "aircraft": []}
    return {"enabled": True, "source": "OpenSky Network", **live.thy_aircraft(settings.live_ttl_s)}


@app.get("/qr.svg", include_in_schema=False)
def qr():
    """Demonun genel adresini QR olarak verir: izleyen kişi telefonundan açıp kendisi deneyebilir."""
    if not settings.public_url:
        raise HTTPException(status_code=404, detail="PUBLIC_URL not set")
    buffer = io.BytesIO()
    segno.make(settings.public_url).save(buffer, kind="svg", scale=4, border=2)
    return Response(content=buffer.getvalue(), media_type="image/svg+xml")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")
