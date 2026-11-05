#!/usr/bin/env python3
"""Avisa por Telegram de aviones y helicópteros a <= RADIUS_KM de la ubicación en vivo
de cada usuario autorizado y por debajo de MAX_ALT_FT. Solo usa la librería estándar.

Usuarios: el dueño (TELEGRAM_CHAT_ID) y, opcionalmente, los miembros del grupo
TELEGRAM_GROUP_ID. Cada uno se "suscribe" compartiendo su ubicación en tiempo
real con el bot: no hace falta apuntar sus ids en ningún sitio.

Cada usuario elige qué recibir con comandos (/helicopteros, /aviones, /todo, /estado).
Sin elegir nada, recibe solo helicópteros (DEFAULT_MODE). El modo elegido se guarda en
STATE_FILE; en GitHub Actions lo conserva la caché entre ejecuciones.

Ubicación del dueño desde el móvil (p. ej. Automate al conectar el Bluetooth del coche):
si hay LOCATION_TOKEN, el script lee cada ciclo la variable de repositorio LOCATION_VAR
(valor "lat;lon;ts") y la usa como ubicación del dueño si es más reciente que la de Telegram."""
import calendar
import json
import math
import os
import re
import sys
import time
import unicodedata
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
MODES = {
    "helicopteros": "solo helicópteros 🚁",
    "aviones": "solo aviones ✈️",
    "todo": "aviones y helicópteros ✈️🚁",
}
DEFAULT_MODE = os.getenv("DEFAULT_MODE", "helicopteros").strip().lower()  # modo si el usuario no elige
if DEFAULT_MODE not in MODES:
    DEFAULT_MODE = "helicopteros"
STATE_FILE = os.getenv("STATE_FILE", "state.json")   # modos, ubicaciones y último update leído
LOC_TOKEN = os.getenv("LOCATION_TOKEN", "").strip()  # token fine-grained: solo permiso "Variables" de este repo
LOC_REPO = os.getenv("LOCATION_REPO") or os.getenv("GITHUB_REPOSITORY", "")  # usuario/repositorio
LOC_VAR = os.getenv("LOCATION_VAR", "UBICACION")     # nombre de la variable que actualiza el móvil
STATE_VAR = os.getenv("STATE_VAR", "ESTADO")        # variable del repo donde se guardan modos y último update

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
modes = {}         # chat_id -> "helicopteros" | "aviones" | "todo" (si falta, DEFAULT_MODE)
last_update_id = None  # último update de Telegram ya procesado
loc_etag = None    # ETag de la variable de ubicación (peticiones condicionales)
_saved = ""        # último contenido guardado en STATE_FILE
_remote_saved = ""  # último estado enviado a la variable del repositorio


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


def gh_var(method, name=None, body=None):
    """Llamada a la API de variables del repositorio (mismo token que la ubicación)."""
    url = f"https://api.github.com/repos/{LOC_REPO}/actions/variables"
    if name:
        url += "/" + urllib.parse.quote(name)
    headers = {"User-Agent": USER_AGENT, "Authorization": f"Bearer {LOC_TOKEN}",
               "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def state_blob():
    return json.dumps({"last_update_id": last_update_id, "modes": modes}, sort_keys=True)


def load_remote_state():
    """Modos y último update guardados en la variable ESTADO (sobreviven a la caché)."""
    global last_update_id, _remote_saved
    if not (LOC_TOKEN and LOC_REPO):
        return False
    try:
        data = gh_var("GET", STATE_VAR)
    except urllib.error.HTTPError as e:
        if e.code != 404:  # 404 = aún no existe; se crea al guardar
            print(f"Variable de estado: HTTP {e.code}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"Variable de estado: {e}", file=sys.stderr)
        return False
    try:
        st = json.loads(data.get("value") or "{}")
        modes.update({str(k): v for k, v in st.get("modes", {}).items() if v in MODES})
        lid = st.get("last_update_id")
        if isinstance(lid, int) and (last_update_id is None or lid > last_update_id):
            last_update_id = lid
    except Exception as e:
        print(f"Variable de estado ilegible: {e}", file=sys.stderr)
        return False
    if json.dumps(st, sort_keys=True) == state_blob():
        _remote_saved = state_blob()
    return True


def push_remote_state():
    """Guarda modos y último update en la variable ESTADO solo cuando cambian."""
    global _remote_saved
    if not (LOC_TOKEN and LOC_REPO):
        return
    blob = state_blob()
    if blob == _remote_saved:
        return
    body = {"name": STATE_VAR, "value": blob}
    try:
        try:
            gh_var("PATCH", STATE_VAR, body)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            gh_var("POST", None, body)  # primera vez: la crea
        _remote_saved = blob
    except urllib.error.HTTPError as e:
        print(f"No se pudo guardar la variable de estado: HTTP {e.code}", file=sys.stderr)
    except Exception as e:
        print(f"No se pudo guardar la variable de estado: {e}", file=sys.stderr)


def load_state():
    """Estado de la caché de Actions, completado con la variable ESTADO del repositorio."""
    ok = load_file_state()
    return load_remote_state() or ok


def load_file_state():
    """Recupera modos, ubicaciones y último update leído de STATE_FILE."""
    global last_update_id, _saved
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            raw = f.read()
        st = json.loads(raw)
        last_update_id = st.get("last_update_id")
        modes.update({str(k): v for k, v in st.get("modes", {}).items() if v in MODES})
        for k, v in st.get("locations", {}).items():
            locations[str(k)] = (float(v[0]), float(v[1]), int(v[2]))
        _saved = raw
        return True
    except FileNotFoundError:
        return False
    except Exception as e:
        print(f"Estado ilegible, empiezo de cero: {e}", file=sys.stderr)
        return False


def save_state():
    global _saved
    raw = json.dumps({"last_update_id": last_update_id, "modes": modes,
                      "locations": {k: list(v) for k, v in locations.items()}})
    if raw == _saved:
        return
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(raw)
        os.replace(tmp, STATE_FILE)
        _saved = raw
    except Exception as e:
        print(f"No se pudo guardar el estado: {e}", file=sys.stderr)


HELP = (
    "Comandos:\n"
    "/helicopteros · solo helicópteros 🚁\n"
    "/aviones · solo aviones ✈️\n"
    "/todo · aviones y helicópteros\n"
    "/estado · ver el modo actual\n\n"
    "Para recibir avisos, comparte tu ubicación en tiempo real con este chat."
)
COMMANDS = {
    "/helicopteros": "helicopteros", "/helicoptero": "helicopteros",
    "/aviones": "aviones", "/avion": "aviones",
    "/todo": "todo", "/todos": "todo",
}


def norm_cmd(text):
    """'/Helicópteros@mi_bot arg' -> '/helicopteros' (sin acentos, mayúsculas ni @bot)."""
    cmd = text.split()[0].split("@")[0].lower()
    return "".join(c for c in unicodedata.normalize("NFKD", cmd) if not unicodedata.combining(c))


def status_text(cid):
    txt = f"Modo actual: {MODES[modes.get(cid, DEFAULT_MODE)]}"
    loc = locations.get(cid)
    if loc and time.time() - loc[2] <= MAX_LOC_AGE:
        return txt + "\n📍 Ubicación: activa"
    return txt + "\n📍 Ubicación: sin datos recientes (comparte la ubicación en tiempo real)"


def handle_command(cid, text, reply):
    cmd = norm_cmd(text)
    if cmd in COMMANDS:
        modes[cid] = COMMANDS[cmd]
        msg = f"✅ Modo: {MODES[modes[cid]]}"
    elif cmd == "/estado":
        msg = status_text(cid)
    else:  # /start, /ayuda, /help o cualquier otro
        msg = f"{HELP}\n\n{status_text(cid)}"
    if reply:
        try:
            tg("sendMessage", chat_id=cid, text=msg)
        except Exception as e:
            print(f"No se pudo responder a un comando: {e}", file=sys.stderr)


def register_commands():
    """Muestra los comandos en el menú '/' de Telegram."""
    try:
        tg("setMyCommands", commands=json.dumps([
            {"command": "helicopteros", "description": "Avisar solo de helicópteros 🚁"},
            {"command": "aviones", "description": "Avisar solo de aviones ✈️"},
            {"command": "todo", "description": "Avisar de aviones y helicópteros"},
            {"command": "estado", "description": "Ver el modo actual"},
        ]))
    except Exception as e:
        print(f"No se pudieron registrar los comandos: {e}", file=sys.stderr)


def process_message(m, reply):
    """Ubicación o comando de un chat privado. Los no autorizados reciben un aviso y se ignoran."""
    if m["chat"].get("type") != "private":
        return
    uid = (m.get("from") or {}).get("id")
    if uid is None:
        return
    cid = str(m["chat"]["id"])
    text = (m.get("text") or "").strip()
    is_cmd = text.startswith("/")
    if "location" not in m and not is_cmd:
        return
    ts = m.get("edit_date") or m["date"]
    if "location" in m and cid in locations and locations[cid][2] >= ts:
        return
    if not authorized(uid):
        if cid not in notified:
            notified.add(cid)
            try:
                tg("sendMessage", chat_id=cid,
                   text="Este bot es privado. Pide al administrador que te añada al grupo.")
            except Exception as e:
                print(f"No se pudo avisar a un usuario no autorizado: {e}", file=sys.stderr)
        return
    if "location" in m:
        loc = m["location"]
        locations[cid] = (loc["latitude"], loc["longitude"], ts)
    else:
        handle_command(cid, text, reply)


def process_updates():
    """Lee los mensajes nuevos (y los confirma con offset). En la primera lectura sin estado
    previo mira los últimos 100 y solo responde a comandos de los últimos 10 minutos."""
    global last_update_id
    first = last_update_id is None
    offset = -100 if first else last_update_id + 1
    now = time.time()
    while True:
        res = tg("getUpdates", offset=offset, limit=100,
                 allowed_updates=json.dumps(["message", "edited_message"]))
        updates = res.get("result", [])
        if not updates:
            break
        for u in updates:
            m = u.get("edited_message") or u.get("message")
            if not m:
                continue
            reply = (not first) or (now - m.get("date", 0) < 600)
            try:
                process_message(m, reply)
            except Exception as e:
                print(f"Error procesando un mensaje: {e}", file=sys.stderr)
        last_update_id = max(u["update_id"] for u in updates)
        offset = last_update_id + 1
        if len(updates) < 100:
            break
    save_state()


def parse_location_value(value):
    """'lat;lon;ts' (o 'lat,lon,ts'); admite coma decimal. Devuelve (lat, lon, ts|None) o None."""
    raw = (value or "").strip()
    parts = raw.split(";") if ";" in raw else raw.split(",")
    if len(parts) < 2:
        return None
    lat, lon = (float(p.strip().replace(",", ".")) for p in parts[:2])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    ts = None
    if len(parts) > 2:
        try:
            ts = float(parts[2].strip().replace(",", "."))
            if ts > 1e11:  # milisegundos
                ts /= 1000
        except ValueError:
            pass
    return lat, lon, ts


def refresh_variable_location():
    """Ubicación del dueño enviada desde el móvil a una variable del repositorio.
    La frescura se mide con la hora que GitHub anota al actualizarla (updated_at)."""
    global loc_etag
    if not (LOC_TOKEN and LOC_REPO):
        return
    headers = {"User-Agent": USER_AGENT, "Authorization": f"Bearer {LOC_TOKEN}",
               "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if loc_etag:
        headers["If-None-Match"] = loc_etag
    url = f"https://api.github.com/repos/{LOC_REPO}/actions/variables/{urllib.parse.quote(LOC_VAR)}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as r:
            etag = r.headers.get("ETag")
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code != 304:  # 304 = sin cambios
            print(f"Variable de ubicación: HTTP {e.code}", file=sys.stderr)
        return
    except Exception as e:
        print(f"Variable de ubicación: {e}", file=sys.stderr)
        return
    loc_etag = etag
    try:
        parsed = parse_location_value(data.get("value"))
    except ValueError:
        parsed = None
    if parsed is None:
        print("Variable de ubicación: valor no válido", file=sys.stderr)
        return
    lat, lon, ts = parsed
    if lat == 0 and lon == 0:  # valor inicial "0;0;0"
        return
    try:
        upd = calendar.timegm(time.strptime(data.get("updated_at", ""), "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        upd = None
    ts = int(min(upd or ts or time.time(), time.time()))
    cur = locations.get(CHAT_ID)
    if cur is None or cur[2] < ts:
        locations[CHAT_ID] = (lat, lon, ts)


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


def photo_page_link(reg):
    """Enlace donde ver fotos de la aeronave: primero Flightradar24; si no, JetPhotos."""
    if not reg:
        return None
    if re.fullmatch(r"[A-Za-z0-9-]{2,10}", reg):
        return ("Flightradar24", f"https://www.flightradar24.com/data/aircraft/{reg.lower()}")
    return ("JetPhotos", f"https://www.jetphotos.com/registration/{urllib.parse.quote(reg)}")


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
    # Sin foto en Planespotters: enlace a la ficha de la aeronave (con sus fotos) para abrirlo
    # en tu navegador. No se accede a esas webs desde el script, solo se construye el enlace.
    link = photo_page_link(reg)
    if link:
        label, url = link
        try:
            tg("sendMessage", chat_id=chat_id,
               text=f"{text}\n📷 FOTO NO DISPONIBLE · Ver en {label}:\n{url}",
               disable_web_page_preview="true")
            return
        except Exception as e:
            print(f"Enlace de fotos falló, envío sin enlace: {e}", file=sys.stderr)
    tg("sendMessage", chat_id=chat_id, text=f"{text}\n📷 FOTO NO DISPONIBLE",
       disable_web_page_preview="true")


def alert_user(cid, lat, lon, seen):
    aircraft = fetch_aircraft(lat, lon)
    if aircraft is None:
        return
    now = time.time()
    mode = modes.get(cid, DEFAULT_MODE)
    for ac in aircraft:
        alt = ac.get("alt_baro")  # puede ser "ground" o faltar
        if not isinstance(alt, (int, float)) or alt >= MAX_ALT_FT:
            continue
        if ac.get("lat") is None or ac.get("lon") is None or not ac.get("hex"):
            continue
        dist = haversine_km(lat, lon, ac["lat"], ac["lon"])
        if dist > RADIUS_KM:
            continue
        heli = is_helicopter(ac)
        if (mode == "helicopteros" and not heli) or (mode == "aviones" and heli):
            print(f"Ignorado (modo {mode}): tipo={ac.get('t')} categoría={ac.get('category')}")
            continue
        key = (cid, ac["hex"])
        if now - seen.get(key, 0) < COOLDOWN:
            continue
        brg = bearing(lat, lon, ac["lat"], ac["lon"])
        send_alert(cid, ac["hex"], format_msg(ac, dist, brg), ac.get("r"))
        seen[key] = now


def step(seen):
    process_updates()
    refresh_variable_location()
    save_state()
    push_remote_state()
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
    print("Estado restaurado" if load_state() else "Sin estado previo: valores por defecto")
    register_commands()
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
