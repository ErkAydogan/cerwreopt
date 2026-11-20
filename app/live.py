# Canlı trafik katmanı (isteğe bağlı): OpenSky Network'ten harita bölgesindeki THY çağrı kodlu uçakların konumu.
# Sadece haritada gösterilir; plan, ekipler ve senaryo bu veriye BAĞLI DEĞİL. Servis cevap vermezse katman boş kalır.
# Anonim kullanımda günlük sorgu kotası düşük olduğu için sonuç bellekte LIVE_TTL_S saniye (varsayılan 15 dk)
# saklanır; arayüz iki sorgu arasında uçakları son hız ve yönlerine göre ilerletir.
import json
import time
import urllib.request

# Harita penceresiyle aynı kutu (bkz. scripts/build_map.py)
URL = "https://opensky-network.org/api/states/all?lamin=34.5&lomin=5&lamax=52&lomax=42"
_cache = {"at": 0.0, "aircraft": [], "error": None}


def parse_states(payload: dict, prefix: str = "THY") -> list[dict]:
    """OpenSky durum vektörlerinden (liste içinde liste) havadaki THY uçaklarını seçer.
    Alan sırası: [0] ICAO24 adresi, [1] çağrı kodu, [2] tescil ülkesi, [5] boylam, [6] enlem, [7] irtifa (m),
    [8] yerde mi, [9] yer hızı (m/s), [10] rota açısı, [11] dikey hız (m/s)."""
    aircraft = []
    for state in payload.get("states") or []:
        callsign = (state[1] or "").strip()
        if callsign.startswith(prefix) and state[5] is not None and state[6] is not None and not state[8]:
            aircraft.append(
                {
                    "callsign": callsign, "icao24": state[0], "country": state[2],
                    "lon": state[5], "lat": state[6], "heading": state[10] or 0,
                    "altitude_m": state[7], "speed_ms": state[9], "vertical_ms": state[11],
                }
            )
    return aircraft


def thy_aircraft(ttl_s: float) -> dict:
    now = time.time()
    if now - _cache["at"] >= ttl_s:
        _cache["at"] = now  # hata olsa da süre dolana kadar yeniden denenmez: kota ve yanıt süresi korunur
        try:
            with urllib.request.urlopen(URL, timeout=5) as response:
                _cache["aircraft"] = parse_states(json.load(response))
                _cache["error"] = None
        except Exception as exc:  # ağ hatası, kota, bozuk yanıt: katman eski veriyle (ya da boş) kalır
            _cache["error"] = f"{type(exc).__name__}: {exc}"
    return {"aircraft": _cache["aircraft"], "error": _cache["error"], "age_s": round(now - _cache["at"])}
