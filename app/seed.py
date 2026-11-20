# Demo verisi: 23.11.2026, LTFM çıkışlı 14 gidiş-dönüş görev, 7 ekip (C1-C5 normal, S1-S2 yedek).
# Tüm uçuşlar uydurmadır (THY çağrı kodu biçimi ve gerçek ICAO meydan kodları kullanıldı).
# Kurgu: C1'in P01 (THY1591) dönüşü 14:10, P02 kalkışı 15:15 -> 65 dk dinlenme; 90 dk gecikme bunu bozar.
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


def load(engine: Engine) -> None:
    """Tabloları boşaltıp demo verisini yükler. Prova sırasında tekrar tekrar çalıştırılabilir."""
    db.init_db(engine)
    with engine.begin() as conn:
        for table in (db.dead_letters, db.jobs, db.assignments, db.flights, db.crews):
            conn.execute(delete(table))
        conn.execute(insert(db.crews), [{"id": c, "is_standby": s} for c, s in CREWS])
        for pairing_id, crew_id, legs in PAIRINGS:
            conn.execute(insert(db.assignments).values(pairing_id=pairing_id, crew_id=crew_id))
            for leg_no, (callsign, adep, ades, std, sta) in enumerate(legs, start=1):
                conn.execute(
                    insert(db.flights).values(
                        pairing_id=pairing_id, leg_no=leg_no, callsign=callsign, dof=OPS_DATE,
                        adep=adep, ades=ades, std_min=minutes(std), sta_min=minutes(sta),
                        orig_std_min=minutes(std), status="SCHEDULED",
                    )
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
