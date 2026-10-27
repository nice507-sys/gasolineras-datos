"""
Genera los servicios de todas las gasolineras de España a partir de OpenStreetMap.

Entrada : un archivo .osm.pbf (o .osm) ya filtrado con osmium tags-filter.
Salida  : carpeta con teselas JSON de 0,5° x 0,5° (s/<lat>_<lon>.json) + meta.json.

Cada gasolinera de OSM se guarda con sus coordenadas y los servicios que tiene
o que hay a pocos metros (cafetería, cargadores eléctricos...). La app busca la
gasolinera de OSM más cercana (< 70 m) a cada gasolinera del Ministerio.

Uso: python generar.py filtrado.osm.pbf salida/
"""
import json
import math
import os
import sys
import time
from collections import defaultdict

import osmium

TILE = 0.5          # tamaño de tesela en grados
CELL = 0.01         # celda de la rejilla para buscar vecinos (~1 km)

AMENITIES = {"charging_station", "restaurant", "cafe", "fast_food", "toilets", "atm",
             "car_wash", "compressed_air", "vending_machine"}
SHOPS = {"convenience", "kiosk", "supermarket"}


def yes(v):
    return bool(v) and v != "no"


class Recoger(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.fuel = []      # (lat, lon, tags)
        self.otros = []     # (lat, lon, tags)

    def _guardar(self, lat, lon, tags):
        t = {k: v for k, v in tags}
        a, sh = t.get("amenity"), t.get("shop")
        if a == "fuel":
            self.fuel.append((lat, lon, t))
        elif a in AMENITIES or sh in SHOPS:
            self.otros.append((lat, lon, t))

    def node(self, n):
        if n.tags and n.location.valid():
            a, sh = n.tags.get("amenity"), n.tags.get("shop")
            if a == "fuel" or a in AMENITIES or sh in SHOPS:
                self._guardar(n.location.lat, n.location.lon, n.tags)

    def way(self, w):
        a, sh = w.tags.get("amenity"), w.tags.get("shop")
        if not (a == "fuel" or a in AMENITIES or sh in SHOPS):
            return
        pts = [(nd.location.lat, nd.location.lon) for nd in w.nodes if nd.location.valid()]
        if not pts:
            return
        lat = sum(p[0] for p in pts) / len(pts)
        lon = sum(p[1] for p in pts) / len(pts)
        self._guardar(lat, lon, w.tags)


def dist_m(a, b, c, d):
    r = math.radians
    h = math.sin(r(c - a) / 2) ** 2 + math.cos(r(a)) * math.cos(r(c)) * math.sin(r(d - b) / 2) ** 2
    return 6371000 * 2 * math.atan2(math.sqrt(h), math.sqrt(1 - h))


def num(v):
    try:
        return float(str(v).lower().replace("kw", "").replace(",", ".").strip().split()[0])
    except (ValueError, IndexError):
        return None


def cargador(t):
    plugs = []
    if yes(t.get("socket:type2")): plugs.append("Tipo 2")
    if yes(t.get("socket:type2_combo")): plugs.append("CCS")
    if yes(t.get("socket:chademo")): plugs.append("CHAdeMO")
    if yes(t.get("socket:tesla_supercharger")) or yes(t.get("socket:nacs")): plugs.append("Tesla")
    if yes(t.get("socket:schuko")): plugs.append("Schuko")
    kws = [num(v) for k, v in t.items() if (k.startswith("socket:") and k.endswith(":output")) or k in ("charging_station:output", "output")]
    kws = [k for k in kws if k]
    try:
        cap = int(t.get("capacity", "")) or None
    except ValueError:
        cap = None
    return {"op": (t.get("operator") or t.get("brand") or t.get("network") or "")[:40],
            "kw": max(kws) if kws else None, "p": plugs, "n": cap}


def servicios(lat, lon, t, vecinos):
    """Mismo criterio que la app: lo que tiene la gasolinera y lo que hay a pocos metros."""
    s = {}
    def add(k, nombre=None):
        if k not in s:
            s[k] = nombre or 1
    if yes(t.get("toilets")): add("toilets")
    if yes(t.get("car_wash")): add("car_wash")
    if yes(t.get("compressed_air")): add("air")
    if yes(t.get("vacuum_cleaner")): add("vacuum")
    if yes(t.get("atm")): add("atm")
    if yes(t.get("shop")) or yes(t.get("convenience")): add("shop")
    if yes(t.get("cafe")): add("cafe")
    if yes(t.get("restaurant")): add("restaurant")
    if t.get("self_service") == "yes" or t.get("automated") == "yes": add("self")
    if t.get("self_service") == "no" or t.get("full_service") == "yes": add("attended")
    if yes(t.get("hgv")): add("hgv")
    if t.get("wheelchair") == "yes": add("wheelchair")
    if any(yes(t.get(k)) for k in ("payment:cards", "payment:credit_cards", "payment:debit_cards")): add("cards")
    ch = []
    for (la, lo, g) in vecinos:
        d = dist_m(lat, lon, la, lo)
        a, sh = g.get("amenity"), g.get("shop")
        lim = 150 if a == "charging_station" else 120 if sh else 130
        if d > lim:
            continue
        if a == "charging_station":
            c = cargador(g); c["d"] = round(d); ch.append(c)
        elif a in ("restaurant", "fast_food"): add("restaurant", g.get("name"))
        elif a == "cafe": add("cafe", g.get("name"))
        elif a == "toilets": add("toilets")
        elif a == "atm": add("atm")
        elif a == "car_wash": add("car_wash")
        elif a == "compressed_air": add("air")
        elif a == "vending_machine": add("vending")
        elif sh in SHOPS: add("shop", g.get("name"))
    ch.sort(key=lambda c: c["d"])
    return s, ch[:4]


def tile_key(lat, lon):
    return f"{math.floor(lat / TILE) * TILE:.1f}_{math.floor(lon / TILE) * TILE:.1f}"


def main(entrada, salida):
    t0 = time.time()
    h = Recoger()
    h.apply_file(entrada, locations=True)
    print(f"gasolineras OSM: {len(h.fuel)} · otros puntos: {len(h.otros)}")

    rejilla = defaultdict(list)
    for p in h.otros:
        rejilla[(math.floor(p[0] / CELL), math.floor(p[1] / CELL))].append(p)

    teselas = defaultdict(list)
    for (lat, lon, t) in h.fuel:
        ci, cj = math.floor(lat / CELL), math.floor(lon / CELL)
        vec = [p for di in (-1, 0, 1) for dj in (-1, 0, 1) for p in rejilla.get((ci + di, cj + dj), [])]
        s, ch = servicios(lat, lon, t, vec)
        e = {"a": round(lat, 5), "o": round(lon, 5)}
        if s: e["s"] = s
        if ch: e["c"] = ch
        teselas[tile_key(lat, lon)].append(e)

    os.makedirs(os.path.join(salida, "s"), exist_ok=True)
    total = 0
    for k, lista in teselas.items():
        with open(os.path.join(salida, "s", k + ".json"), "w", encoding="utf-8") as f:
            json.dump(lista, f, ensure_ascii=False, separators=(",", ":"))
        total += len(lista)
    meta = {"fecha": time.strftime("%Y-%m-%d"), "gasolineras": total, "teselas": len(teselas), "tam": TILE}
    with open(os.path.join(salida, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)
    with open(os.path.join(salida, "index.html"), "w", encoding="utf-8") as f:
        f.write("<!doctype html><meta charset='utf-8'><title>Datos Gasolineras Cerca</title>"
                f"<p>Servicios de {total} gasolineras · actualizado {meta['fecha']}.</p>"
                "<p>Datos © colaboradores de OpenStreetMap, licencia ODbL.</p>")
    print(f"{total} gasolineras en {len(teselas)} teselas · {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
