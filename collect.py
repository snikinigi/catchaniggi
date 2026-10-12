"""Oman -> UAE ship collector for the free AISStream feed.

Run by GitHub Actions every ~15 minutes: listens for ~100 s, remembers which
commercial ships were last seen inside an Omani port zone, and reports those
that then declare a UAE destination or are seen inside a UAE port zone.
Writes voyages.json (for the web page) and state.json (memory between runs).
"""
import asyncio, calendar, json, math, os, re, time

LISTEN = int(os.environ.get("LISTEN_SECONDS", "100"))
MAXAGE = 5 * 86400                      # forget an Oman departure after 5 days
BOX = [[[15.0, 51.5], [27.5, 60.5]]]    # [lat, lon] corners: Salalah to Hormuz

# country, port, lat, lon, radius km. Approximate: adjust to taste.
PORTS = [
    ("OM", "Sohar", 24.48, 56.62, 10), ("OM", "Shinas", 24.76, 56.47, 6),
    ("OM", "Muscat", 23.63, 58.57, 10), ("OM", "Sur", 22.56, 59.52, 10),
    ("OM", "Duqm", 19.66, 57.70, 12), ("OM", "Salalah", 16.94, 54.00, 10),
    ("OM", "Khasab", 26.18, 56.25, 6),
    ("AE", "Jebel Ali", 25.01, 55.06, 12), ("AE", "Dubai", 25.27, 55.28, 7),
    ("AE", "Sharjah", 25.36, 55.38, 8), ("AE", "Ras Al Khaimah", 25.85, 55.98, 18),
    ("AE", "Fujairah", 25.17, 56.37, 20), ("AE", "Khor Fakkan", 25.36, 56.38, 9),
    ("AE", "Abu Dhabi", 24.52, 54.37, 12), ("AE", "Khalifa Port", 24.80, 54.65, 10),
    ("AE", "Ruwais", 24.12, 52.73, 10),
]
# Crew-typed AIS destination text -> UAE port
DEST = [(n, re.compile(r, re.I)) for n, r in [
    ("Jebel Ali", r"jebel|ae\s*jea|\bjea\b"), ("Khalifa Port", r"khalifa"),
    ("Abu Dhabi", r"abu\s*dhabi|zayed|ae\s*auh"),
    ("Fujairah", r"fujairah|fujeira|ae\s*fjr|\bfjr\b"),
    ("Khor Fakkan", r"khor\s*fakkan|khorfakkan|ae\s*klf"),
    ("Sharjah", r"sharjah|hamriyah|ae\s*shj"),
    ("Ras Al Khaimah", r"ras\s*al\s*khaimah|ae\s*rkt|\brak\b|saqr"),
    ("Dubai", r"dubai|rashid|ae\s*dxb|\bdxb\b"), ("Ruwais", r"ruwais")]]


def km(a, b, c, d):
    p = math.pi / 180
    h = math.sin((c - a) * p / 2) ** 2 + math.cos(a * p) * math.cos(c * p) * math.sin((d - b) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def zone(lat, lon):
    best = None
    for c, n, la, lo, r in PORTS:
        f = km(lat, lon, la, lo) / r
        if f <= 1 and (best is None or f < best[2]):
            best = (c, n, f)
    return best


def match(dest):
    for n, rx in DEST:
        if rx.search(dest or ""):
            return n


def kind(t):
    t = t if isinstance(t, int) else 0
    return "Passenger" if 60 <= t < 70 else "Cargo" if 70 <= t < 80 else "Tanker" if 80 <= t < 90 else None


def eta_ts(e, now):
    try:
        mo, d, h, mi = e["Month"], e["Day"], e["Hour"], e["Minute"]
        if not (1 <= mo <= 12 and 1 <= d <= 31 and h < 24 and mi < 60):
            return None
        y = time.gmtime(now).tm_year
        t = calendar.timegm((y, mo, d, h, mi, 0))
        return t if t > now - 30 * 86400 else calendar.timegm((y + 1, mo, d, h, mi, 0))
    except Exception:
        return None


def load():
    try:
        S = json.load(open("state.json"))
    except Exception:
        S = {}
    for k, v in (("static", {}), ("om", {}), ("open", {}), ("done", []), ("skip", [])):
        S.setdefault(k, v)
    S["skip"] = set(S["skip"])
    return S


def save(S):
    with open("state.json", "w") as f:
        json.dump(dict(S, skip=sorted(S["skip"])), f)


def newv(S, mmsi, port, t0):
    st = S["static"].get(mmsi, {})
    return {"mmsi": mmsi, "name": st.get("name") or mmsi, "imo": st.get("imo") or "",
            "type": kind(st.get("type")), "from": port, "to": None, "dep": t0,
            "arr": None, "status": "Underway"}


def handle(S, m, now):
    if "error" in m:
        raise SystemExit("AISStream error: %s" % m["error"])
    md = m.get("MetaData") or {}
    mmsi = str(md.get("MMSI") or "")
    body = m.get("Message") or {}
    if not mmsi or mmsi in S["skip"]:
        return
    if m.get("MessageType") == "ShipStaticData":
        d = body.get("ShipStaticData") or {}
        if kind(d.get("Type")) is None:          # fishing, tug, yacht, naval...
            S["skip"].add(mmsi)
            for k in ("static", "om", "open"):
                S[k].pop(mmsi, None)
            return
        S["static"][mmsi] = {
            "name": (d.get("Name") or "").strip(), "imo": d.get("ImoNumber") or "",
            "type": d.get("Type"), "dest": (d.get("Destination") or "").replace("@", "").strip(),
            "eta": eta_ts(d.get("Eta"), now), "seen": now}
        return
    p = body.get("PositionReport") or {}
    lat, lon = p.get("Latitude", md.get("latitude")), p.get("Longitude", md.get("longitude"))
    if lat is None or lon is None:
        return
    z = zone(lat, lon)
    if z and z[0] == "OM":                       # in an Omani port zone: not departed yet
        S["om"][mmsi] = [z[1], now, False]
        S["open"].pop(mmsi, None)
    elif mmsi in S["om"]:
        port, t0, _ = S["om"][mmsi]
        S["om"][mmsi][2] = True                  # has left the zone
        if z and z[0] == "AE" and not any(d["mmsi"] == mmsi and d["dep"] == t0 for d in S["done"]):
            v = S["open"].pop(mmsi, None) or newv(S, mmsi, port, t0)
            v.update(to=z[1], arr=now, status="Arrived")
            S["done"].append(v)


async def listen(S):
    import websockets
    n, end = 0, time.time() + LISTEN
    async with websockets.connect("wss://stream.aisstream.io/v0/stream") as ws:
        await ws.send(json.dumps({"APIKey": os.environ["AISSTREAM_API_KEY"], "BoundingBoxes": BOX,
                                  "FilterMessageTypes": ["PositionReport", "ShipStaticData"]}))
        while time.time() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), max(1, end - time.time()))
            except asyncio.TimeoutError:
                break
            handle(S, json.loads(raw), time.time())
            n += 1
    return n


def finalize(S, now):
    for m in list(S["om"]):
        port, t0, left = S["om"][m]
        if now - t0 > MAXAGE:
            S["om"].pop(m)
            S["open"].pop(m, None)
            continue
        if not left or any(d["mmsi"] == m and d["dep"] == t0 for d in S["done"]):
            continue
        st = S["static"].get(m, {})
        to = match(st.get("dest"))
        if to:                                   # left Oman and declares a UAE destination
            v = S["open"].get(m) or newv(S, m, port, t0)
            v.update(to=to, arr=st.get("eta"))
            S["open"][m] = v
        else:
            S["open"].pop(m, None)
    for v in list(S["open"].values()) + S["done"]:
        st = S["static"].get(v["mmsi"])
        if st:
            v.update(name=st["name"] or v["name"], imo=st["imo"], type=kind(st["type"]))
    S["done"] = [d for d in S["done"] if now - d["arr"] < 2 * 86400]
    S["static"] = {k: s for k, s in S["static"].items() if now - s.get("seen", 0) < 3 * 86400 or k in S["om"]}


def export(S, now):
    out = []
    for v in list(S["open"].values()) + S["done"]:
        if v.get("type") and v.get("to"):
            out.append(dict(v, dep=int(v["dep"] * 1000), arr=int(v["arr"] * 1000) if v.get("arr") else None))
    out.sort(key=lambda v: v["dep"], reverse=True)
    with open("voyages.json", "w") as f:
        json.dump({"updated": int(now * 1000), "voyages": out}, f)
    return len(out)


def main():
    S, n = load(), 0
    try:
        n = asyncio.run(listen(S))
    except Exception as e:                       # feed hiccup: keep going with saved state
        print("Feed problem, using saved state:", repr(e))
    now = time.time()
    finalize(S, now)
    save(S)
    print("messages:", n, "| voyages:", export(S, now))


if __name__ == "__main__":
    main()
