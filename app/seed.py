# Demo verisi: 23.11.2026, LTFM çıkışlı 14 gidiş-dönüş görev, 7 ekip (C1-C5 normal, S1-S2 yedek).
# Tüm uçuşlar uydurmadır (THY çağrı kodu biçimi ve gerçek ICAO meydan kodları kullanıldı).
# Kurgu: C1'in P01 (THY1591) dönüşü 14:10, P02 kalkışı 15:15 -> 65 dk dinlenme; 90 dk gecikme bunu bozar.
import random
import sys
from datetime import date

from sqlalchemy import delete, insert, select
from sqlalchemy.engine import Engine

from app import config, db

OPS_DATE = date(2026, 11, 23)
CREWS = [("C1", False), ("C2", False), ("C3", False), ("C4", False), ("C5", False), ("S1", True), ("S2", True)]

# (görev, ekip, [(çağrı kodu, kalkış, varış, STD, STA), ...])  saatler UTC
PAIRINGS = [
    ("P01", "C1", [("THY1591", "LTFM", "EDDF", "07:00", "10:10"), ("THY1592", "EDDF", "LTFM", "11:10", "14:10")]),
    ("P02", "C1", [("THY1845", "LTFM", "LGAV", "15:15", "16:40"), ("THY1846", "LGAV", "LTFM", "17:40", "19:10")]),
    ("P03", "C2", [("THY2130", "LTFM", "LTAC", "05:30", "06:35"), ("THY2131", "LTAC", "LTFM", "07:35", "08:40")]),
    ("P04", "C2", [("THY1043", "LTFM", "LROP", "09:45", "11:15"), ("THY1044", "LROP", "LTFM", "12:15", "13:45")]),
    ("P05", "C2", [("THY1083", "LTFM", "LYBE", "14:50", "16:35"), ("THY1084", "LYBE", "LTFM", "17:35", "19:25")]),
    ("P06", "C3", [("THY2310", "LTFM", "LTBJ", "06:00", "07:05"), ("THY2311", "LTBJ", "LTFM", "08:05", "09:10")]),
    ("P07", "C3", [("THY1859", "LTFM", "LBSF", "10:15", "11:30"), ("THY1860", "LBSF", "LTFM", "12:30", "13:45")]),
    ("P08", "C3", [("THY2412", "LTFM", "LTAI", "15:00", "16:20"), ("THY2413", "LTAI", "LTFM", "17:20", "18:40")]),
    ("P09", "C4", [("THY1003", "LTFM", "LWSK", "05:45", "07:20"), ("THY1004", "LWSK", "LTFM", "08:20", "09:55")]),
    ("P10", "C4", [("THY2870", "LTFM", "LTCG", "11:00", "12:45"), ("THY2871", "LTCG", "LTFM", "13:45", "15:30")]),
    ("P11", "C4", [("THY2134", "LTFM", "LTAC", "16:35", "17:40"), ("THY2135", "LTAC", "LTFM", "18:40", "19:45")]),
    ("P12", "C5", [("THY1885", "LTFM", "LOWW", "05:15", "07:30"), ("THY1886", "LOWW", "LTFM", "08:30", "10:45")]),
    ("P13", "C5", [("THY2314", "LTFM", "LTBJ", "11:50", "12:55"), ("THY2315", "LTBJ", "LTFM", "13:55", "15:00")]),
    ("P14", "C5", [("THY1861", "LTFM", "LBSF", "16:05", "17:20"), ("THY1862", "LBSF", "LTFM", "18:20", "19:35")]),
]


def minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def hhmm(total: int) -> str:
    return f"{total // 60:02d}:{total % 60:02d}"


# Büyük gün için tek yön uçuş süreleri (dk); yerde kalış 60 dk
BLOCK = {"EDDF": 190, "LOWW": 135, "LYBE": 105, "LTCG": 105, "LWSK": 95, "LROP": 90,
         "LGAV": 85, "LTAI": 80, "LBSF": 75, "LTAC": 65, "LTBJ": 65}  # fmt: skip


def big_day(n_pairings: int = 150, n_standby: int = 25, rng_seed: int = 7) -> tuple[list, list]:
    """Fırtına senaryosu için büyük bir gün üretir: aynı tohumla hep aynı plan (tekrarlanabilir demo).
    Görevler rastgele saatlere dağıtılır, sonra sıkı ama kurallara uyan bir başlangıç planı kurulur:
    her görev, 65 dk dinlenmeyle araya sığan ve en geç boşalan ekibe verilir; yoksa yeni ekip açılır."""
    rng = random.Random(rng_seed)
    drafts = []
    for i in range(n_pairings):
        dest = rng.choice(list(BLOCK))
        block = BLOCK[dest]
        start = rng.randrange(4 * 60, 21 * 60 - 2 * block - 60, 5)  # dönüş en geç 21:00
        legs = [
            (f"THY{3000 + 2 * i}", "LTFM", dest, hhmm(start), hhmm(start + block)),
            (f"THY{3001 + 2 * i}", dest, "LTFM", hhmm(start + block + 60), hhmm(start + 2 * block + 60)),
        ]
        drafts.append((start, start + 2 * block + 60, legs))
    drafts.sort(key=lambda d: d[0])

    crews = []  # her ekip: [son görevin bitişi, toplam görev süresi]
    pairings = []
    for number, (start, end, legs) in enumerate(drafts, start=1):
        fits = [i for i, (free_at, duty) in enumerate(crews) if free_at + 65 <= start and duty + end - start <= 13 * 60]
        if fits:
            crew = max(fits, key=lambda i: crews[i][0])  # en geç boşalan: plan sıkı olsun
            crews[crew] = [end, crews[crew][1] + end - start]
        else:
            crews.append([end, end - start])
            crew = len(crews) - 1
        pairings.append((f"P{number:03d}", f"C{crew + 1:02d}", legs))
    crew_rows = [(f"C{i + 1:02d}", False) for i in range(len(crews))] + [(f"S{i + 1}", True) for i in range(n_standby)]
    return crew_rows, pairings


def load(engine: Engine, crews: list | None = None, pairings: list | None = None) -> None:
    """Tabloları boşaltıp veriyi yükler (varsayılan: 14 görevlik demo). Tekrar tekrar çalıştırılabilir."""
    crews = crews or CREWS
    pairings = pairings or PAIRINGS
    db.init_db(engine)
    with engine.begin() as conn:
        for table in (db.flags, db.dead_letters, db.jobs, db.assignments, db.flights, db.crews):
            conn.execute(delete(table))
        conn.execute(insert(db.crews), [{"id": c, "is_standby": s} for c, s in crews])
        conn.execute(insert(db.assignments), [{"pairing_id": p, "crew_id": c} for p, c, _ in pairings])
        conn.execute(
            insert(db.flights),
            [
                {
                    "pairing_id": pairing_id, "leg_no": leg_no, "callsign": callsign, "dof": OPS_DATE,
                    "adep": adep, "ades": ades, "std_min": minutes(std), "sta_min": minutes(sta),
                    "orig_std_min": minutes(std), "status": "SCHEDULED",
                }
                for pairing_id, _, legs in pairings
                for leg_no, (callsign, adep, ades, std, sta) in enumerate(legs, start=1)
            ],
        )


if __name__ == "__main__":
    engine = db.make_engine(config.load().database_url)
    db.init_db(engine)
    with engine.connect() as conn:
        has_data = conn.execute(select(db.crews.c.id).limit(1)).first() is not None
    if "--if-empty" in sys.argv and has_data:
        print("data already present, seed skipped")  # yeniden başlatmada demo durumu korunur
    else:
        load(engine)
        print(f"seeded {len(PAIRINGS)} pairings, {len(CREWS)} crews for {OPS_DATE}")
