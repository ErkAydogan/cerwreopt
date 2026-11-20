# Ekip yeniden atama modeli (OR-Tools CP-SAT): kurallara uyan ve mevcut plandan EN AZ sapan atama.
# Saf fonksiyonlar: veri (dataclass) girer, SolveResult çıkar. Veritabanı, kuyruk, log bilgisi yok;
# OR ekibinin modeli ile entegrasyon kodu (worker.py) bu sınırdan ayrılır.
from dataclasses import dataclass, field

from ortools.sat.python import cp_model


@dataclass(frozen=True)
class Pairing:
    id: str
    start: int  # gün başından dakika (UTC), ilk bacağın kalkışı
    end: int  # son bacağın varışı
    current_crew: str | None  # mevcut plandaki ekip
    cancelled: bool = False
    locked: bool = False  # kalkmış (DEP) ya da inmiş görev: ekibi artık değiştirilemez

    @property
    def duration(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class Crew:
    id: str
    is_standby: bool = False


@dataclass(frozen=True)
class Rules:
    min_rest: int = 60  # iki görev arasında en az dinlenme (dk)
    max_duty: int = 13 * 60  # ekip başına günlük toplam görev süresi (dk), basitleştirilmiş
    change_penalty: int = 100  # mevcut ekibinden alınan her görev
    standby_penalty: int = 30  # kullanılan her yedek ekip
    time_limit_s: float = 10.0


@dataclass
class SolveResult:
    solver_status: str  # OPTIMAL | FEASIBLE | INFEASIBLE | MODEL_INVALID | UNKNOWN
    assignment: dict[str, str] = field(default_factory=dict)  # pairing_id -> crew_id
    changes: list[dict] = field(default_factory=list)  # [{pairing, from, to, reason}]
    objective: float | None = None
    best_bound: float | None = None
    gap: float | None = None
    solve_seconds: float = 0.0
    trace: list[dict] = field(default_factory=list)  # her yeni çözüm: {t, objective, bound}
    infeasible_core: list[str] = field(default_factory=list)  # birlikte karşılanamayan görevler
    standby_used: int = 0  # kullanılan yedek ekip sayısı (amaç değerinin dökümü için)

    @property
    def has_solution(self) -> bool:
        return self.solver_status in ("OPTIMAL", "FEASIBLE")


def conflicting_pairs(pairings: list[Pairing], min_rest: int) -> list[tuple[str, str]]:
    """Aynı ekibe verilemeyecek görev çiftleri: aralarında min_rest dakikadan az boşluk var.
    Modelin dışında, düz Python ile hesaplanır; böylece kısıt tek satır kalır ve ayrıca test edilebilir."""
    pairs = []
    ordered = sorted(pairings, key=lambda p: p.start)
    for i, a in enumerate(ordered):
        for b in ordered[i + 1 :]:
            if b.start < a.end + min_rest:  # b, a bitip dinlenme dolmadan başlıyor
                pairs.append((a.id, b.id))
    return pairs


def _add_rules(model, x, active, crews, rules, cover=None):
    """Üç kural. cover verilirse 'görev p karşılansın' kuralı cover[p] literaline bağlanır;
    bu sadece çözümsüzlük teşhisinde kullanılır (bkz. explain_infeasibility)."""
    # Kısıt 1: her aktif görev tam olarak bir ekibe
    for p in active:
        c = model.add(sum(x[p.id, e.id] for e in crews) == 1)
        if cover is not None:
            c.only_enforce_if(cover[p.id])

    # Kısıt 2: çakışan (ya da dinlenmesi yetmeyen) iki görev aynı ekipte olamaz
    for a, b in conflicting_pairs(active, rules.min_rest):
        for e in crews:
            model.add(x[a, e.id] + x[b, e.id] <= 1)

    # Kısıt 3: ekip başına günlük toplam görev süresi sınırı
    for e in crews:
        model.add(sum(p.duration * x[p.id, e.id] for p in active) <= rules.max_duty)

    # Kilit: uçuş kalktıysa ekip sabittir, çözücü sadece geri kalan görevleri oynatabilir
    crew_ids = {e.id for e in crews}
    for p in active:
        if p.locked and p.current_crew in crew_ids:
            model.add(x[p.id, p.current_crew] == 1)


def _clean(value: float) -> float:
    """Katsayılar tam sayı; çözücünün döndürdüğü 129.99999999999997 gibi kayan nokta artıklarını temizler."""
    return round(value, 6)


class _Trace(cp_model.CpSolverSolutionCallback):
    """Çözücü her daha iyi çözüm bulduğunda amaç ve sınırı kaydeder: arayüzde gap'in kapanışı çizilir."""

    def __init__(self):
        super().__init__()
        self.points = []

    def on_solution_callback(self):
        self.points.append(
            {
                "t": round(self.wall_time, 3),
                "objective": _clean(self.objective_value),
                "bound": _clean(self.best_objective_bound),
            }
        )


def solve(pairings: list[Pairing], crews: list[Crew], rules: Rules | None = None) -> SolveResult:
    rules = rules or Rules()
    active = [p for p in pairings if not p.cancelled]  # iptal edilenler modele girmez
    crew_ids = {e.id for e in crews}
    model = cp_model.CpModel()

    # Karar değişkeni: x[p, e] = 1  <=>  p görevi e ekibine atanır
    x = {(p.id, e.id): model.new_bool_var(f"x[{p.id},{e.id}]") for p in active for e in crews}
    _add_rules(model, x, active, crews, rules)

    # Yedek kullanımı: used[s] = 1 eğer yedek s en az bir görev aldıysa
    used = {}
    for e in crews:
        if e.is_standby:
            used[e.id] = model.new_bool_var(f"used[{e.id}]")
            for p in active:
                model.add(x[p.id, e.id] <= used[e.id])

    # Amaç: 100 * (ekibi değişen görev) + 30 * (kullanılan yedek)
    # "Değişti" = mevcut ekibindeki x sıfır oldu, yani 1 - x[p, mevcut]
    changed = [1 - x[p.id, p.current_crew] for p in active if p.current_crew in crew_ids]
    model.minimize(rules.change_penalty * sum(changed) + rules.standby_penalty * sum(used.values()))

    # Mevcut planı ipucu olarak ver: çözücü aramaya oradan başlar (warm start)
    for p in active:
        if p.current_crew in crew_ids:
            model.add_hint(x[p.id, p.current_crew], 1)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = rules.time_limit_s
    solver.parameters.random_seed = 0  # aynı girdi -> aynı sonuç (demo ve testler için)
    trace = _Trace()
    status = solver.solve(model, trace)
    result = SolveResult(solver.status_name(status), solve_seconds=solver.wall_time, trace=trace.points)

    if status == cp_model.INFEASIBLE:
        result.infeasible_core = explain_infeasibility(pairings, crews, rules)
    if not result.has_solution:
        return result  # INFEASIBLE / UNKNOWN: atama yok, kararı çağıran verir

    for p in active:
        result.assignment[p.id] = next(e.id for e in crews if solver.value(x[p.id, e.id]))
    by_id = {p.id: p for p in active}
    for p in active:
        new_crew = result.assignment[p.id]
        if new_crew != p.current_crew:
            reason = explain_change(p, result.assignment, by_id, rules)
            result.changes.append({"pairing": p.id, "from": p.current_crew, "to": new_crew, "reason": reason})

    result.standby_used = sum(solver.value(u) for u in used.values())
    result.objective = _clean(solver.objective_value)
    result.best_bound = _clean(solver.best_objective_bound)
    # Göreli boşluk: 0 ise optimum kanıtlandı. Amaç 0 iken bölme yapmıyoruz.
    result.gap = 0.0 if result.objective == 0 else abs(result.objective - result.best_bound) / abs(result.objective)
    return result


def why_not(p: Pairing, crew_id: str, on_crew: list[Pairing], rules: Rules) -> str | None:
    """p görevi, elinde on_crew görevleri olan ekibe verilebilir mi? Verilemezse bozulan ilk kuralı döndürür.
    Çözücüdeki kuralların düz Python hali; 'neden bu ekip değil?' sorusunu çözücüyü çalıştırmadan cevaplar."""
    for q in on_crew:
        first, second = sorted((p, q), key=lambda r: r.start)
        rest = second.start - first.end
        if rest < 0:
            return f"{crew_id} flies {q.id}: {first.id} and {second.id} would overlap by {-rest} min"
        if rest < rules.min_rest:
            return (
                f"{crew_id} flies {q.id}: rest between {first.id} and {second.id} "
                f"would be {rest} min (< {rules.min_rest})"
            )
    duty = sum(q.duration for q in on_crew) + p.duration
    if duty > rules.max_duty:
        return f"{crew_id} would reach {duty} min duty (> {rules.max_duty})"
    return None


def explain_change(p: Pairing, assignment: dict[str, str], by_id: dict[str, Pairing], rules: Rules) -> str:
    """Görev neden eski ekibinde kalamadı? Operasyon kontrolörünün soracağı soruya düz metin cevap."""
    old = p.current_crew
    if old is None:
        return "was unassigned"
    kept = [by_id[q] for q, e in assignment.items() if e == old]
    return why_not(p, old, kept, rules) or "no rule forces this move; it lowers the total cost"


def violations(pairings: list[Pairing], rules: Rules) -> list[str]:
    """Mevcut plan (current_crew) hangi kuralları bozuyor? Çözümden ÖNCE çalışır: gelen mesajın etkisini gösterir."""
    active = [p for p in pairings if not p.cancelled and p.current_crew]
    by_id = {p.id: p for p in active}
    found = []
    for a, b in conflicting_pairs(active, rules.min_rest):
        if by_id[a].current_crew == by_id[b].current_crew:
            rest = by_id[b].start - by_id[a].end
            problem = f"overlap by {-rest} min" if rest < 0 else f"have {rest} min rest (< {rules.min_rest})"
            found.append(f"{by_id[a].current_crew}: {a} and {b} {problem}")
    for crew_id in sorted({p.current_crew for p in active}):
        duty = sum(p.duration for p in active if p.current_crew == crew_id)
        if duty > rules.max_duty:
            found.append(f"{crew_id}: duty is {duty} min (> {rules.max_duty})")
    return found


def explain_infeasibility(pairings: list[Pairing], crews: list[Crew], rules: Rules) -> list[str]:
    """INFEASIBLE çoğu zaman veri hatasıdır; 'çözüm yok' demek yerine hangi görevlerin birlikte
    karşılanamadığını bul. Her görevin 'karşılansın' kuralı bir varsayım (assumption) literaline
    bağlanır, CP-SAT çelişen varsayımları döndürür, sonra küçültme (deletion filter) ile minimal küme."""
    active = [p for p in pairings if not p.cancelled]
    model = cp_model.CpModel()
    x = {(p.id, e.id): model.new_bool_var(f"x[{p.id},{e.id}]") for p in active for e in crews}
    cover = {p.id: model.new_bool_var(f"cover[{p.id}]") for p in active}
    _add_rules(model, x, active, crews, rules, cover)

    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1  # varsayım çekirdeği tek iş parçacığında güvenilir
    solver.parameters.max_time_in_seconds = rules.time_limit_s

    def infeasible_with(ids: list[str]) -> bool:
        model.clear_assumptions()
        model.add_assumptions([cover[i] for i in ids])
        return solver.solve(model) == cp_model.INFEASIBLE

    if not infeasible_with(list(cover)):
        return []
    index_to_id = {cover[i].index: i for i in cover}
    core = [index_to_id[i] for i in solver.sufficient_assumptions_for_infeasibility()]

    # Küçültme: bir görevi çıkarınca hâlâ çözümsüzse o görev gerekli değil, at.
    for pid in list(core):
        trial = [q for q in core if q != pid]
        if infeasible_with(trial):
            core = trial
    return sorted(core)
