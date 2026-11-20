# CP-SAT modelinin testleri: elle çözülebilecek kadar küçük veriler, beklenen sonuç önceden bilinir.
# Senaryolar: bozulma yok, gecikme sonrası tek değişiklik, iptal, kurallara uyulamayan (INFEASIBLE) durum.
from dataclasses import replace

from app.model import Crew, Pairing, Rules, conflicting_pairs, solve


def hm(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


CREWS = [Crew("A"), Crew("B"), Crew("S1", is_standby=True)]
PLAN = [
    Pairing("P1", hm("08:00"), hm("12:00"), "A"),
    Pairing("P2", hm("13:00"), hm("17:00"), "A"),  # P1'den tam 60 dk sonra
    Pairing("P3", hm("09:00"), hm("14:00"), "B"),
]


def test_conflicts_respect_min_rest():
    assert conflicting_pairs(PLAN, min_rest=60) == [("P1", "P3"), ("P3", "P2")]
    assert ("P1", "P2") in conflicting_pairs(PLAN, min_rest=61)


def test_undisrupted_plan_is_kept():
    result = solve(PLAN, CREWS)
    assert result.solver_status == "OPTIMAL"
    assert result.changes == []
    assert result.objective == 0
    assert result.gap == 0


def test_delay_breaks_rest_and_one_change_fixes_it():
    # P1 90 dk gecikir -> 13:30'da biter, P2 13:00'te başlar: A ikisini birden uçamaz.
    # B dolu (P3 14:00'e kadar). En ucuz çözüm: tek görevi yedeğe vermek = 100 + 30.
    delayed = [replace(PLAN[0], start=hm("09:30"), end=hm("13:30")), *PLAN[1:]]
    result = solve(delayed, CREWS)
    assert result.solver_status == "OPTIMAL"
    assert len(result.changes) == 1
    assert result.changes[0]["to"] == "S1"
    assert "rest between P1 and P2 would be -30 min" in result.changes[0]["reason"]
    assert result.objective == 130
    assert result.gap == 0
    assert result.trace and result.trace[-1]["objective"] == 130  # çözücü ilerlemesi kaydedildi


def test_cancelled_pairing_is_not_assigned():
    cancelled = [replace(PLAN[0], cancelled=True), *PLAN[1:]]
    result = solve(cancelled, CREWS)
    assert "P1" not in result.assignment
    assert result.changes == []


def test_max_duty_is_enforced():
    # A'nın iki görevi toplam 8 saat; sınır 7 saat olursa biri başka ekibe gitmeli.
    result = solve(PLAN, CREWS, Rules(max_duty=7 * 60))
    assert result.solver_status == "OPTIMAL"
    assert len(result.changes) == 1


def test_infeasible_when_no_crew_can_cover():
    # Üç görev aynı anda, sadece iki ekip var ve yedek yok: kurallara uyan atama yok.
    overlapping = [Pairing(f"P{i}", hm("08:00"), hm("12:00"), "A") for i in range(3)]
    result = solve(overlapping, [Crew("A"), Crew("B")])
    assert result.solver_status == "INFEASIBLE"
    assert not result.has_solution
    assert result.assignment == {}
    assert result.infeasible_core == ["P0", "P1", "P2"]  # herhangi ikisi tek başına çözülebilir


def test_infeasibility_points_at_bad_data():
    # Veri hatası: bir görev yanlışlıkla 15 saat girilmiş. Teşhis sadece o görevi göstermeli.
    broken = [*PLAN, Pairing("P9", hm("06:00"), hm("21:00"), "B")]
    result = solve(broken, CREWS)
    assert result.solver_status == "INFEASIBLE"
    assert result.infeasible_core == ["P9"]


def test_departed_pairing_is_locked_to_its_crew():
    # Gecikme senaryosunun aynısı, ama P1 kalkmış (DEP geldi): yedeğe gidebilecek tek görev P2.
    delayed = [replace(PLAN[0], start=hm("09:30"), end=hm("13:30"), locked=True), *PLAN[1:]]
    result = solve(delayed, CREWS)
    assert [(c["pairing"], c["to"]) for c in result.changes] == [("P2", "S1")]
