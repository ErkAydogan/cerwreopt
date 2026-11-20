# AFTN (ICAO Doc 4444) ATS mesaj ayrıştırıcısı: DLA, CNL, DEP, ARR.
# Saf modül: veritabanı ya da ağ bilgisi yok; metin girer, AftnMessage ya da AftnParseError çıkar.
# Başlık satırları (öncelik + adresler) atlanır, sadece parantez içindeki gövde okunur.
import hashlib
import re
from dataclasses import dataclass
from datetime import date

# Yeniden planlama tetikleyen tipler; DEP/ARR sadece uçuş durumunu günceller.
REPLAN_TYPES = {"DLA", "CNL"}
SUPPORTED_TYPES = {"DLA", "CNL", "DEP", "ARR"}

CALLSIGN_RE = re.compile(r"^[A-Z]{3}[0-9][0-9A-Z]{0,3}$")  # örn. THY1591
AERODROME_RE = re.compile(r"^[A-Z]{4}$")  # ICAO meydan kodu, örn. LTFM
AERODROME_TIME_RE = re.compile(r"^([A-Z]{4})(\d{4})$")  # örn. LTFM0830
DOF_RE = re.compile(r"\bDOF/(\d{6})\b")  # alan 18 içinde DOF/YYMMDD


class AftnParseError(ValueError):
    """Mesaj okunamadı; neden metni dead_letters tablosuna yazılır."""


@dataclass(frozen=True)
class AftnMessage:
    msg_type: str  # DLA | CNL | DEP | ARR
    callsign: str  # alan 7
    adep: str  # kalkış meydanı
    ades: str  # varış meydanı
    time_hhmm: str  # DLA: yeni EOBT, CNL: EOBT, DEP: gerçek kalkış, ARR: gerçek varış (UTC)
    dof: date  # uçuş tarihi
    body: str  # normalize edilmiş gövde, örn. "(DLA-THY1591-LTFM0830-EDDF-DOF/261123)"

    @property
    def triggers_replan(self) -> bool:
        return self.msg_type in REPLAN_TYPES

    @property
    def minutes(self) -> int:
        """HHMM'yi gün başından itibaren dakikaya çevirir (model dakika ile çalışır)."""
        return int(self.time_hhmm[:2]) * 60 + int(self.time_hhmm[2:])

    def body_hash(self) -> str:
        """Başlıktan bağımsız özet: aynı mesaj farklı başlıkla tekrar gelse de aynı hash."""
        return hashlib.sha256(self.body.encode()).hexdigest()[:16]


def parse(raw: str) -> AftnMessage:
    body = _extract_body(raw)
    fields = body[1:-1].split("-")  # parantezleri at, alanlara böl

    msg_type = fields[0]
    if msg_type not in SUPPORTED_TYPES:
        raise AftnParseError(f"unsupported message type: {msg_type!r}")
    if len(fields) < 5:
        raise AftnParseError(f"{msg_type}: expected 5 fields, got {len(fields)}")

    callsign = _check(CALLSIGN_RE, fields[1], "callsign")

    if msg_type == "ARR":
        # ARR: (ARR-<çağrı>-<ADEP>-<ADES><varış saati>-DOF/..)  -> saat varış meydanına bitişik
        adep = _check(AERODROME_RE, fields[2], "departure aerodrome")
        ades, hhmm = _split_aerodrome_time(fields[3], "arrival aerodrome+time")
    else:
        # DLA/CNL/DEP: (<tip>-<çağrı>-<ADEP><saat>-<ADES>-DOF/..)
        adep, hhmm = _split_aerodrome_time(fields[2], "departure aerodrome+time")
        ades = _check(AERODROME_RE, fields[3], "destination aerodrome")

    # Alan 18 tire içerebilir mi? Doc 4444'te hayır; yine de kalan her şeyi birleştirip DOF arıyoruz.
    dof = _parse_dof("-".join(fields[4:]))

    return AftnMessage(msg_type, callsign, adep, ades, hhmm, dof, body)


def _extract_body(raw: str) -> str:
    """Başlığı atlar, ilk '(' ile ondan sonraki ilk ')' arasını tek satır halinde döndürür."""
    text = raw.strip().upper()
    start = text.find("(")
    if start == -1:
        raise AftnParseError("no '(' found: ATS message body missing")
    end = text.find(")", start)
    if end == -1:
        raise AftnParseError("no closing ')' found: message truncated?")
    # AFTN satırları ~69 karakterde kırılabilir; satır sonlarını ve fazla boşlukları sil.
    body = re.sub(r"\s*\n\s*", "", text[start : end + 1])
    return re.sub(r"\s+", " ", body)


def _check(pattern: re.Pattern, value: str, name: str) -> str:
    if not pattern.match(value):
        raise AftnParseError(f"invalid {name}: {value!r}")
    return value


def _split_aerodrome_time(value: str, name: str) -> tuple[str, str]:
    m = AERODROME_TIME_RE.match(value)
    if not m:
        raise AftnParseError(f"invalid {name}: {value!r} (expected e.g. LTFM0830)")
    aerodrome, hhmm = m.groups()
    if int(hhmm[:2]) > 23 or int(hhmm[2:]) > 59:
        raise AftnParseError(f"invalid time: {hhmm!r}")
    return aerodrome, hhmm


def _parse_dof(field18: str) -> date:
    m = DOF_RE.search(field18)
    if not m:
        raise AftnParseError("DOF/ (date of flight) missing in field 18")
    yymmdd = m.group(1)
    try:
        return date(2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:]))
    except ValueError as exc:
        raise AftnParseError(f"invalid DOF: {yymmdd!r}") from exc


class UnknownFlightError(LookupError):
    """Mesaj geçerli ama uçuş tablomuzda böyle bir bacak yok."""


def resolve_flight(msg: AftnMessage, flights: dict[tuple[str, date, str], int]) -> int:
    """(çağrı kodu, tarih, kalkış meydanı) anahtarıyla uçuş id'sini bulur.
    Sözlüğü çağıran taraf (API) veritabanından doldurur; burada sadece eşleştirme var."""
    key = (msg.callsign, msg.dof, msg.adep)
    if key not in flights:
        raise UnknownFlightError(f"unknown flight {msg.callsign} {msg.adep} on {msg.dof}")
    return flights[key]
