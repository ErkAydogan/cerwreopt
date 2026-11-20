# Veritabanı şeması (tek dosya, SQLAlchemy 2 Core) ve API ile worker'ın paylaştığı sorgular.
# Uçuş = bacak. Pairing (gidiş-dönüş görev) ayrı tablo değil: flights.pairing_id ile gruplanır.
# Burası entegrasyon katmanı: veritabanı satırlarını modelin dataclass'larına çevirir.
from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    select,
)
from sqlalchemy.engine import Connection, Engine

from app.model import Crew, Pairing


def utcnow() -> datetime:
    return datetime.now(UTC)


metadata = MetaData()

crews = Table(
    "crews",
    metadata,
    Column("id", String(16), primary_key=True),
    Column("is_standby", Boolean, nullable=False, default=False),
)

flights = Table(
    "flights",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("pairing_id", String(16), nullable=False, index=True),
    Column("leg_no", Integer, nullable=False),  # 1 = gidiş, 2 = dönüş
    Column("callsign", String(8), nullable=False),
    Column("dof", Date, nullable=False),
    Column("adep", String(4), nullable=False),
    Column("ades", String(4), nullable=False),
    Column("std_min", Integer, nullable=False),  # güncel planlanan kalkış: gün başından dakika (UTC)
    Column("sta_min", Integer, nullable=False),  # güncel planlanan varış
    Column("orig_std_min", Integer, nullable=False),  # ilk plan; arayüzde gecikmeyi göstermek için
    Column("status", String(12), nullable=False, default="SCHEDULED"),  # DELAYED|CANCELLED|DEPARTED|ARRIVED
    UniqueConstraint("callsign", "dof", "adep", name="uq_flight_leg"),  # AFTN mesajını bacağa eşleyen anahtar
)

assignments = Table(
    "assignments",
    metadata,
    Column("pairing_id", String(16), primary_key=True),
    Column("crew_id", String(16), ForeignKey("crews.id"), nullable=False),
    Column("previous_crew_id", String(16), nullable=True),  # son değişiklikten önceki ekip (arayüz vurgusu)
    Column("changed_by_job", String(36), nullable=True),
)

jobs = Table(
    "jobs",
    metadata,
    Column("id", String(36), primary_key=True),
    # Aynı tetikleyici iki kez gelirse ikinci iş oluşmasın: veritabanı garantisi, uygulama kontrolü değil.
    Column("idempotency_key", String(200), nullable=False, unique=True),
    Column("ops_date", Date, nullable=False),
    Column("trigger", Text, nullable=False),  # AFTN gövdesi ya da "manual"
    Column("status", String(12), nullable=False, default="queued"),  # queued|running|succeeded|failed|timed_out
    Column("attempts", Integer, nullable=False, default=0),
    Column("created_at", DateTime(timezone=True), nullable=False, default=utcnow),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    Column("solver_status", String(16)),
    Column("objective", Float),
    Column("best_bound", Float),
    Column("gap", Float),
    Column("solve_seconds", Float),
    Column("changes", JSON),
    Column("trace", JSON),
    Column("infeasible_core", JSON),
    Column("standby_used", Integer),
    Column("violations", JSON),  # çözümden önce mevcut planın bozduğu kurallar
    Column("params", JSON),  # elle başlatılan işte ağırlık değişiklikleri (what-if)
    Column("error", Text),
    Column("input_snapshot", JSON),  # modele verilen girdi: sonucu sonradan aynen yeniden üretebilmek için
)

dead_letters = Table(
    "dead_letters",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("received_at", DateTime(timezone=True), nullable=False, default=utcnow),
    Column("raw", Text, nullable=False),
    Column("error", Text, nullable=False),
)


def make_engine(url: str) -> Engine:
    # pool_pre_ping: veritabanı yeniden başladıysa ölü bağlantıyı kullanmadan önce fark et
    return create_engine(url, pool_pre_ping=True)


def init_db(engine: Engine) -> None:
    metadata.create_all(engine)  # tablo yoksa oluşturur, varsa dokunmaz


# --- Paylaşılan sorgular ------------------------------------------------------------------------


def flight_index(conn: Connection, dof: date) -> dict[tuple[str, date, str], int]:
    """aftn.resolve_flight için: (çağrı kodu, tarih, kalkış meydanı) -> flights.id"""
    rows = conn.execute(select(flights.c.id, flights.c.callsign, flights.c.adep).where(flights.c.dof == dof))
    return {(r.callsign, dof, r.adep): r.id for r in rows}


def load_crews(conn: Connection) -> list[Crew]:
    return [Crew(r.id, r.is_standby) for r in conn.execute(select(crews).order_by(crews.c.id))]


def load_pairings(conn: Connection, ops_date: date) -> list[Pairing]:
    """Bacakları pairing'e toplar: başlangıç = ilk kalkış, bitiş = son varış, bir bacak iptalse görev iptal."""
    current = dict(conn.execute(select(assignments.c.pairing_id, assignments.c.crew_id)).all())
    legs: dict[str, list] = {}
    for leg in conn.execute(select(flights).where(flights.c.dof == ops_date).order_by(flights.c.leg_no)):
        legs.setdefault(leg.pairing_id, []).append(leg)
    return [
        Pairing(
            id=pid,
            start=min(leg.std_min for leg in group),
            end=max(leg.sta_min for leg in group),
            current_crew=current.get(pid),
            cancelled=any(leg.status == "CANCELLED" for leg in group),
            locked=any(leg.status in ("DEPARTED", "ARRIVED") for leg in group),
        )
        for pid, group in sorted(legs.items())
    ]
