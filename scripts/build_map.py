# Harita altlığını üretir: Natural Earth (kamu malı) 1:110m ülke sınırlarını Avrupa-Türkiye penceresine
# kırpıp tek bir SVG yoluna çevirir ve app/static/map.js dosyasına yazar. Bir kez çalıştırılır; çıktı repoda.
# Kullanım: python scripts/build_map.py ne_110m_admin_0_countries.geojson
import json
import math
import sys
from pathlib import Path

LON0, LON1, LAT0, LAT1 = 5.0, 42.0, 34.5, 52.0  # gösterilen pencere
SCALE = 20.0  # derece başına piksel (enlem)
KX = SCALE * math.cos(math.radians((LAT0 + LAT1) / 2))  # boylam, orta enlemde daraltılır (eşdikdörtgen izdüşüm)


def project(lon: float, lat: float) -> tuple[float, float]:
    return round((lon - LON0) * KX, 1), round((LAT1 - lat) * SCALE, 1)


def main(source: str) -> None:
    features = json.loads(Path(source).read_text())["features"]
    parts = []
    for feature in features:
        geometry = feature["geometry"]
        polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
        for polygon in polygons:
            ring = polygon[0]  # dış halka yeter
            if not any(LON0 - 2 <= lon <= LON1 + 2 and LAT0 - 2 <= lat <= LAT1 + 2 for lon, lat in ring):
                continue  # pencereye hiç girmeyen kara parçası
            points = [project(lon, lat) for lon, lat in ring]
            parts.append("M" + "L".join(f"{x},{y}" for x, y in points) + "Z")
    data = {
        "width": round((LON1 - LON0) * KX),
        "height": round((LAT1 - LAT0) * SCALE),
        "lon0": LON0, "lat1": LAT1, "kx": round(KX, 3), "ky": SCALE,
        "land": "".join(parts),
    }
    out = Path(__file__).resolve().parent.parent / "app" / "static" / "map.js"
    out.write_text("// scripts/build_map.py tarafından üretildi (Natural Earth, kamu malı). Elle düzenlemeyin.\n"
                   f"const MAP = {json.dumps(data)};\n")
    print(f"{out.name}: {len(parts)} polygons, {out.stat().st_size} bytes, {data['width']}x{data['height']}")


if __name__ == "__main__":
    main(sys.argv[1])
