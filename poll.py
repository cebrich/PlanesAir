#!/usr/bin/env python3
"""Avisa por Telegram de aviones y helicópteros (ONLY_HELICOPTERS=1 limita a helicópteros) a <= RADIUS_KM de la ubicación en vivo de cada
usuario autorizado y por debajo de MAX_ALT_FT. Solo usa la librería estándar.

Usuarios: el dueño (TELEGRAM_CHAT_ID) y, opcionalmente, los miembros del grupo
TELEGRAM_GROUP_ID. Cada uno se "suscribe" compartiendo su ubicación en tiempo
real con el bot: no hace falta apuntar sus ids en ningún sitio."""
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])        # dueño: siempre autorizado
GROUP_ID = os.getenv("TELEGRAM_GROUP_ID", "").strip()  # opcional: grupo privado con los demás usuarios
RADIUS_KM = float(os.getenv("RADIUS_KM", "5"))
MAX_ALT_FT = float(os.getenv("MAX_ALT_FT", "3200"))
INTERVAL = int(os.getenv("INTERVAL_S", "15"))        # segundos entre consultas
DURATION = int(os.getenv("DURATION_S", "3300"))      # duración de cada ejecución
COOLDOWN = int(os.getenv("COOLDOWN_S", "900"))       # no repetir aviso del mismo avión
MAX_LOC_AGE = int(os.getenv("MAX_LOC_AGE_S", "600")) # ubicación más vieja que esto = ignorar
PHOTO_MODE = os.getenv("PHOTO_MODE", "preview")      # "preview" = enlace con vista previa; "photo" = sendPhoto
ONLY_HELI = os.getenv("ONLY_HELICOPTERS", "0") == "1"  # 0 = aviones y helicópteros; 1 = solo helicópteros

# Un helicóptero se detecta por la categoría ADS-B "A7" (rotorcraft) o, si el
# avión no la emite, por su código de tipo OACI. Amplía la lista con HELI_TYPES=AAA,BBB
HELI_TYPES = {
    "EC20", "EC25", "EC30", "EC35", "EC45", "EC55", "EC75", "H160", "H175",
    "AS32", "AS3B", "AS50", "AS55", "AS65", "PUMA", "GAZL", "ALO2", "ALO3", "LAMA", "ALOU",
    "B06", "B407", "B412", "B429", "B430", "B212", "B222", "B230", "B47G", "B47J", "B105",
    "B427", "B505", "B204", "B205", "B214", "UH1", "BK17",
    "R22", "R44", "R66", "A109", "A119", "A139", "A149", "A169", "A189", "A129",
    "S76", "S92", "S61", "S58", "S64", "S70", "H47", "H64", "H60", "H53", "H46", "H500", "H269",
    "MI8", "MI17", "MI24", "MI26", "KA32", "KA27", "KA26", "NH90", "EH10",
    "EN28", "EN48", "MD52", "MD60", "MD90",
} | {t.strip().upper() for t in os.getenv("HELI_TYPES", "").split(",") if t.strip()}

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

locations = {}     # chat_id -> (lat, lon, timestamp) de cada usuario
member_cache = {}  # user_id -> (autorizado, cuándo se comprobó)
notified = set()   # usuarios no autorizados ya avisados en esta ejecución
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


def authorized(uid):
    """Dueño, o miembro del grupo privado (comprobado con getChatMember, en caché 10 min)."""
    if str(uid) == CHAT_ID:
        return True
    if not GROUP_ID:
        return False
    ok, checked = member_cache.get(uid, (False, 0))
    if time.time() - checked < 600:
        return ok
    try:
        r = tg("getChatMember", chat_id=GROUP_ID, user_id=uid)["result"]
        ok = r["status"] in ("creator", "administrator", "member") or (
            r["status"] == "restricted" and r.get("is_member", False))
    except Exception as e:
        print(f"No se pudo comprobar la pertenencia al grupo: {e}", file=sys.stderr)
        ok = False
    member_cache[uid] = (ok, time.time())
    return ok


def refresh_locations():
    """Lee los últimos 100 updates (offset negativo: sin consumir ni perder estado)
    y guarda la ubicación más reciente de cada usuario autorizado."""
    res = tg("getUpdates", offset=-100, limit=100,
             allowed_updates=json.dumps(["message", "edited_message"]))
    for u in res.get("result", []):
        m = u.get("edited_message") or u.get("message")
        if not m or "location" not in m or m["chat"].get("type") != "private":
            continue
        uid = (m.get("from") or {}).get("id")
        if uid is None:
            continue
        cid = str(m["chat"]["id"])
        ts = m.get("edit_date") or m["date"]
        if cid in locations and locations[cid][2] >= ts:
            continue
        if not authorized(uid):
            if cid not in notified:
                notified.add(cid)
                try:
                    tg("sendMessage", chat_id=cid,
                       text="Este bot es privado. Pide al administrador que te añada al grupo.")
                except Exception as e:
                    print(f"No se pudo avisar a un usuario no autorizado: {e}", file=sys.stderr)
            continue
        loc = m["location"]
        locations[cid] = (loc["latitude"], loc["longitude"], ts)


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


def is_helicopter(ac):
    return ac.get("category") == "A7" or (ac.get("t") or "").upper() in HELI_TYPES


def format_msg(ac, dist, brg):
    flight = (ac.get("flight") or "").strip() or "sin indicativo"
    head = "🚁 HELICÓPTERO ·" if is_helicopter(ac) else "✈️"
    lines = [
        f"{head} {flight} ({ac.get('t') or '?'}, {ac.get('r') or '?'})",
        f"📏 A {dist:.1f} km al {compass(brg)}",
        f"⬇️ {ac['alt_baro'] * 0.3048:.0f} m ({int(ac['alt_baro'])} ft)",
    ]
    gs, track = ac.get("gs"), ac.get("track")
    if gs is not None:
        extra = f", rumbo {track:.0f}°" if track is not None else ""
        lines.append(f"💨 {gs * 1.852:.0f} km/h{extra}")
    lines.append(f"https://globe.adsbexchange.com/?icao={ac['hex']}")
    return "\n".join(lines)


def _planespotters(kind, value):
    """Consulta la API de Planespotters por 'hex' o 'reg'. Devuelve (foto, página, autor) o None."""
    url = f"https://api.planespotters.net/pub/photos/{kind}/{urllib.parse.quote(value)}"
    photos = http_json(url, timeout=10).get("photos") or []
    if photos:
        p = photos[0]
        thumb = p.get("thumbnail_large") or p.get("thumbnail") or {}
        if thumb.get("src") and p.get("link"):
            return (thumb["src"], p["link"], p.get("photographer") or "autor desconocido")
    return None


def get_photo(hex_code, reg=None):
    """Última foto del avión: primero por código hex y, si no hay, por matrícula."""
    if hex_code in photo_cache:
        return photo_cache[hex_code]
    result, failed = None, False
    for kind, value in (("hex", hex_code.upper()), ("reg", reg)):
        if not value:
            continue
        try:
            result = _planespotters(kind, value)
        except Exception as e:
            failed = True
            print(f"Foto por {kind} no disponible: {e}", file=sys.stderr)
            continue
        if result:
            break
    print("Foto: " + ("encontrada" if result else ("error al consultar Planespotters" if failed else "sin foto en Planespotters")))
    if result or not failed:  # un error de red/403 no se cachea: se reintentará
        photo_cache[hex_code] = result
    return result


def send_alert(chat_id, hex_code, text, reg=None):
    photo = get_photo(hex_code, reg)
    if photo:
        src, link, author = photo
        credit = f"📷 {author} · Planespotters.net"
        if PHOTO_MODE == "photo":
            try:
                tg("sendPhoto", chat_id=chat_id, photo=src, caption=f"{text}\n{credit}\n{link}"[:1024])
                return
            except Exception as e:
                print(f"sendPhoto falló, pruebo con vista previa del enlace: {e}", file=sys.stderr)
        # Vista previa del enlace de la foto (Telegram muestra la imagen de la página)
        try:
            tg("sendMessage", chat_id=chat_id, text=f"{text}\n{credit}\n{link}",
               link_preview_options=json.dumps({"url": link, "prefer_large_media": True}))
            return
        except Exception as e:
            print(f"Vista previa falló, envío sin foto: {e}", file=sys.stderr)
    tg("sendMessage", chat_id=chat_id, text=f"{text}\n📷 FOTO NO DISPONIBLE",
       disable_web_page_preview="true")


def alert_user(cid, lat, lon, seen):
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
        if ONLY_HELI and not is_helicopter(ac):
            print(f"Ignorado (no es helicóptero): tipo={ac.get('t')} categoría={ac.get('category')}")
            continue
        key = (cid, ac["hex"])
        if now - seen.get(key, 0) < COOLDOWN:
            continue
        brg = bearing(lat, lon, ac["lat"], ac["lon"])
        send_alert(cid, ac["hex"], format_msg(ac, dist, brg), ac.get("r"))
        seen[key] = now


def step(seen):
    refresh_locations()
    now = time.time()
    active = {cid: loc for cid, loc in locations.items() if now - loc[2] <= MAX_LOC_AGE}
    if not active:
        print("Sin ubicaciones recientes; comparte la ubicación en tiempo real con el bot.")
        return
    print(f"{len(active)} usuario(s) con ubicación reciente")
    for i, (cid, (lat, lon, _)) in enumerate(active.items()):
        if i:
            time.sleep(1.1)  # las APIs permiten ~1 petición/segundo
        try:
            alert_user(cid, lat, lon, seen)
        except Exception as e:  # un usuario con fallo no debe frenar a los demás
            print(f"Error con un usuario: {e}", file=sys.stderr)


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
