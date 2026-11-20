# NOTAM ayrıştırıcı (ICAO biçimi): sadece planı etkileyen tek durum desteklenir, meydan kapanışı (Q-kodu QFALC).
# Saf modül, aftn.py ile aynı sözleşme: metin girer, Notam ya da NotamParseError çıkar.
# Örnek:  (A1234/26 NOTAMN  Q) LTBB/QFALC/IV/NBO/A/000/999/4116N02844E005
#          A) LTFM B) 2611230800 C) 2611231000  E) AD CLSD DUE TO HEAVY SNOW)
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime

ID_RE = re.compile(r"\(?\s*([A-Z]\d{4}/\d{2})\s+NOTAM([NRC])")  # seri+numara/yıl ve tip (N yeni, R değişiklik, C iptal)
QCODE_RE = re.compile(r"Q\)\s*[A-Z]{4}/(Q[A-Z]{4})/")  # Q satırındaki beş harfli konu+durum kodu
ITEM_RE = {
    "A": re.compile(r"\bA\)\s*([A-Z]{4})\b"),  # meydan
    "B": re.compile(r"\bB\)\s*(\d{10})\b"),  # başlangıç YYMMDDHHMM (UTC)
    "C": re.compile(r"\bC\)\s*(\d{10})\b"),  # bitiş
    "E": re.compile(r"\bE\)\s*(.+?)\s*\)?\s*$", re.DOTALL),  # serbest metin
}
AERODROME_CLOSED = "QFALC"  # FA = aerodrome, LC = closed


class NotamParseError(ValueError):
    """NOTAM okunamadı ya da desteklenmeyen türde; neden metni dead_letters tablosuna yazılır."""


@dataclass(frozen=True)
class Notam:
    notam_id: str  # örn. A1234/26
    qcode: str
    aerodrome: str
    start: datetime  # kapanış başlangıcı (UTC)
    end: datetime  # kapanış bitişi (UTC)
    text: str
    body: str  # normalize edilmiş metin

    def body_hash(self) -> str:
        return hashlib.sha256(self.body.encode()).hexdigest()[:16]

    def summary(self) -> str:
        return f"NOTAM {self.notam_id} {self.aerodrome} AD CLSD {self.start:%H%M}-{self.end:%H%M}"


def is_notam(raw: str) -> bool:
    return "NOTAM" in raw.upper()


def parse(raw: str) -> Notam:
    text = raw.strip().upper()
    start = text.find("(")
    body = re.sub(r"\s+", " ", text[start:] if start != -1 else text)  # AFTN başlığını at, boşlukları sadeleştir

    header = ID_RE.search(body)
    if not header:
        raise NotamParseError("NOTAM id line not found (expected e.g. 'A1234/26 NOTAMN')")
    notam_id, kind = header.groups()
    if kind != "N":
        raise NotamParseError(f"unsupported NOTAM type: NOTAM{kind} (only NOTAMN handled)")

    qcode = QCODE_RE.search(body)
    if not qcode:
        raise NotamParseError("Q) line missing or malformed")
    if qcode.group(1) != AERODROME_CLOSED:
        raise NotamParseError(
            f"unsupported Q-code {qcode.group(1)}: only {AERODROME_CLOSED} (aerodrome closed) triggers replan"
        )

    items = {}
    for name, pattern in ITEM_RE.items():
        match = pattern.search(body)
        if not match:
            raise NotamParseError(f"item {name}) missing or malformed")
        items[name] = match.group(1)

    try:
        begin = datetime.strptime(items["B"], "%y%m%d%H%M")
        finish = datetime.strptime(items["C"], "%y%m%d%H%M")
    except ValueError as exc:
        raise NotamParseError(f"invalid date-time in B) or C): {exc}") from exc
    if finish <= begin:
        raise NotamParseError("C) must be later than B)")
    return Notam(notam_id, qcode.group(1), items["A"], begin, finish, items["E"], body)
