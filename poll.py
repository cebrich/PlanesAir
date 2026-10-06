#!/usr/bin/env python3
"""Avisa por Telegram de aviones a <= RADIUS_KM de tu ubicación en vivo
y por debajo de MAX_ALT_FT. Solo usa la librería estándar."""
import json
import math
import os
import sys
import time
import urllib.error
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
PHOTO_MODE = os.getenv("PHOTO_MODE", "preview")      # "preview" = enlace con vista previa; "photo" = sendPhoto

# Planespotters exige un User-Agent único con URL o email de contacto.
# En GitHub Actions GITHUB_REPOSITORY existe solo (usuario/repositorio).
_repo = os.getenv("GITHUB_REPOSITORY")
CONTACT = os.getenv("CONTACT") or (f"https://github.com/{_repo}" if _repo else "sin-contacto")
USER_AGENT = f"AvionesTelegram/1.0 (+{CONTACT})"

# Todas devuelven el formato ADSBExchange v2. Se prueban en orden y, si una
# responde 429/403 (IPs compartidas de GitHub), se aparta un rato y se usa la siguiente.
SOURCES = [
    ("adsb.fi", "https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{nm}"),
    ("adsb.lol", "https://api.adsb.lol/v2/point/{lat}/{lon}/{nm}"),
    ("adsb.one", "https://api.adsb.one/v2/point/{lat}/{lon}/{nm}"),
    ("airplanes.live", "https://api.airplanes.live/v2/point/{lat}/{lon}/{nm}"),
]
blocked_until = {}  # fuente -> timestamp hasta el que no se usa
PUNTOS = ["N", "NE", "E", "SE", "S", "SO", "O", "NO"]

last_loc = None  # (lat, lon, timestamp)
photo_cache = {}  # hex -> (url_foto, url_pagina, fotógrafo) o None


def http_json(url, data=None, timeout=15):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def tg(method, **params):
    data = urllib.parse.urlencode(params).encode()
    try:
        return http_json(f"https://api.telegram.org/bot{TOKEN}/{method}", data)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"Telegram {method}: HTTP {e.code} {body}") from None


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
    nm = math.ceil(RADIUS_KM / 1.852) + 1  # margen; luego se filtra con Haversine
    now = time.time()
    for name, tpl in SOURCES:
        if blocked_until.get(name, 0) > now:
            continue
        url = tpl.format(lat=f"{lat:.5f}", lon=f"{lon:.5f}", nm=nm)
        try:
            data = http_json(url)
            ac = data.get("ac")
            if ac is None:
                ac = data.get("aircraft") or []
            print(f"{name}: {len(ac)} aviones en el área")
            return ac
        except urllib.error.HTTPError as e:
            blocked_until[name] = now + (600 if e.code == 403 else 60)
            print(f"{name} falló: HTTP {e.code}", file=sys.stderr)
        except Exception as e:
            blocked_until[name] = now + 30
            print(f"{name} falló: {e}", file=sys.stderr)
    return None


def format_msg(ac, dist, brg):
    flight = (ac.get("flight") or "").strip() or "sin indicativo"
    lines = [
        f"✈️ {flight} ({ac.get('t') or '?'}, {ac.get('r') or '?'})",
        f"📏 A {dist:.1f} km al {compass(brg)}",
        f"⬇️ {ac['alt_baro'] * 0.3048:.0f} m ({int(ac['alt_baro'])} ft)",
    ]
    gs, track = ac.get("gs"), ac.get("track")
    if gs is not None:
        extra = f", rumbo {track:.0f}°" if track is not None else ""
        lines.append(f"💨 {gs * 1.852:.0f} km/h{extra}")
    lines.append(f"https://globe.adsbexchange.com/?icao={ac['hex']}")
    return "\n".join(lines)


def get_photo(hex_code):
    """Última foto del avión en Planespotters.net (API pública, sin clave)."""
    if hex_code in photo_cache:
        return photo_cache[hex_code]
    result = None
    try:
        data = http_json(f"https://api.planespotters.net/pub/photos/hex/{hex_code.upper()}", timeout=10)
        photos = data.get("photos") or []
        if photos:
            p = photos[0]
            thumb = p.get("thumbnail_large") or p.get("thumbnail") or {}
            if thumb.get("src") and p.get("link"):
                result = (thumb["src"], p["link"], p.get("photographer") or "autor desconocido")
    except Exception as e:
        print(f"Foto no disponible ({hex_code}): {e}", file=sys.stderr)
        return None  # no se cachea el fallo: se reintenta la próxima vez
    print(f"Foto {hex_code}: " + ("encontrada" if result else "sin foto en Planespotters"))
    photo_cache[hex_code] = result
    return result


def send_alert(hex_code, text):
    photo = get_photo(hex_code)
    if photo:
        src, link, author = photo
        if PHOTO_MODE == "photo":
            try:
                tg("sendPhoto", chat_id=CHAT_ID, photo=src, caption=f"{text}"[:1024])
                return
            except Exception as e:
                print(f"sendPhoto falló, pruebo con vista previa del enlace: {e}", file=sys.stderr)
        # Vista previa del enlace de la foto (Telegram muestra la imagen de la página)
        try:
            tg("sendMessage", chat_id=CHAT_ID, text=f"{text}",
               link_preview_options=json.dumps({"url": link, "prefer_large_media": True}))
            return
        except Exception as e:
            print(f"Vista previa falló, envío solo texto: {e}", file=sys.stderr)
    tg("sendMessage", chat_id=CHAT_ID, text=text, disable_web_page_preview="true")


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
        send_alert(ac["hex"], format_msg(ac, dist, brg))
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
