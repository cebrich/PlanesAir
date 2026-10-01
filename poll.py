#!/usr/bin/env python3
"""Avisa por Telegram de aviones a <= RADIUS_KM de tu ubicación en vivo
y por debajo de MAX_ALT_FT. Solo usa la librería estándar."""
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])
RADIUS_KM = float(os.getenv("RADIUS_KM", "5"))
MAX_ALT_FT = float(os.getenv("MAX_ALT_FT", "3200"))
INTERVAL = int(os.getenv("INTERVAL_S", "15"))        # segundos entre consultas
DURATION = int(os.getenv("DURATION_S", "3300"))      # duración de cada ejecución
COOLDOWN = int(os.getenv("COOLDOWN_S", "900"))       # no repetir aviso del mismo avión
MAX_LOC_AGE = int(os.getenv("MAX_LOC_AGE_S", "600")) # ubicación más vieja que esto = ignorar

# Ambas devuelven el mismo formato; si una falla se prueba la otra.
SOURCES = ["https://api.adsb.lol/v2/point", "https://api.airplanes.live/v2/point"]
PUNTOS = ["N", "NE", "E", "SE", "S", "SO", "O", "NO"]

last_loc = None  # (lat, lon, timestamp)


def http_json(url, data=None, timeout=15):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "aviones-telegram/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def tg(method, **params):
    data = urllib.parse.urlencode(params).encode()
    return http_json(f"https://api.telegram.org/bot{TOKEN}/{method}", data)


def refresh_location():
    """Lee el último update del bot; si es una ubicación tuya, la guarda."""
    global last_loc
    res = tg("getUpdates", offset=-1, allowed_updates=json.dumps(["message", "edited_message"]))
    for u in res.get("result", []):
        m = u.get("edited_message") or u.get("message")
        if not m or str(m["chat"]["id"]) != CHAT_ID or "location" not in m:
            continue
        loc = m["location"]
        last_loc = (loc["latitude"], loc["longitude"], m.get("edit_date") or m["date"])


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


def bearing(lat1, lon1, lat2, lon2):
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def compass(b):
    return PUNTOS[int((b + 22.5) // 45) % 8]


def fetch_aircraft(lat, lon):
    radius_nm = math.ceil(RADIUS_KM / 1.852) + 1  # margen; luego se filtra con Haversine
    for base in SOURCES:
        try:
            return http_json(f"{base}/{lat:.5f}/{lon:.5f}/{radius_nm}").get("ac", [])
        except Exception as e:
            print(f"Fuente {base} falló: {e}", file=sys.stderr)
    return None


def format_msg(ac, dist, brg):
    flight = (ac.get("flight") or "").strip() or "sin indicativo"
    lines = [
        f"✈️ {flight} ({ac.get('t') or '?'}, {ac.get('r') or '?'})",
        f"📏 A {dist:.1f} km al {compass(brg)}",
        f"⬇️ {int(ac['alt_baro'])} ft",
    ]
    gs, track = ac.get("gs"), ac.get("track")
    if gs is not None:
        extra = f", rumbo {track:.0f}°" if track is not None else ""
        lines.append(f"💨 {gs * 1.852:.0f} km/h{extra}")
    lines.append(f"https://globe.adsbexchange.com/?icao={ac['hex']}")
    return "\n".join(lines)


def step(seen):
    refresh_location()
    if not last_loc or time.time() - last_loc[2] > MAX_LOC_AGE:
        print("Sin ubicación reciente; comparte tu ubicación en tiempo real con el bot.")
        return
    lat, lon, _ = last_loc
    aircraft = fetch_aircraft(lat, lon)
    if aircraft is None:
        return
    now = time.time()
    for ac in aircraft:
        alt = ac.get("alt_baro")  # puede ser "ground" o faltar
        if not isinstance(alt, (int, float)) or alt >= MAX_ALT_FT:
            continue
        if ac.get("lat") is None or ac.get("lon") is None or not ac.get("hex"):
            continue
        dist = haversine_km(lat, lon, ac["lat"], ac["lon"])
        if dist > RADIUS_KM:
            continue
        if now - seen.get(ac["hex"], 0) < COOLDOWN:
            continue
        brg = bearing(lat, lon, ac["lat"], ac["lon"])
        tg("sendMessage", chat_id=CHAT_ID, text=format_msg(ac, dist, brg),
           disable_web_page_preview="true")
        seen[ac["hex"]] = now


def main():
    seen = {}
    end = time.time() + DURATION
    while True:
        t0 = time.time()
        try:
            step(seen)
        except Exception as e:
            print(f"Error en el ciclo: {e}", file=sys.stderr)
        if time.time() + INTERVAL > end:
            break
        time.sleep(max(0, INTERVAL - (time.time() - t0)))


if __name__ == "__main__":
    main()
