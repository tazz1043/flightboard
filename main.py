import time
import math
import urllib.request
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
import asyncio
import requests
from FlightRadar24 import FlightRadar24API

app = FastAPI()
fr_api = FlightRadar24API()

# --- 1. CONFIGURATION ---
VOCB_LAT = 11.0300
VOCB_LON = 77.0434
AIRPORT_ELEV = 1322 
TARGET_IATA = "CJB"
REGION_BOUNDS = "40.0,-5.0,50.0,110.0"
EXPECTED_CALLSIGNS = []

ACTIVE_RUNWAY = "23"
LAST_METAR_FETCH = 0
DYNAMIC_WATCHLIST = {}
LAST_SCHEDULE_FETCH = 0
NORMALIZED_MANUAL_LIST = set()
strips = {}

def get_active_runway():
    global ACTIVE_RUNWAY, LAST_METAR_FETCH
    if time.time() - LAST_METAR_FETCH < 1800:
        return ACTIVE_RUNWAY
    try:
        url = "https://tgftp.nws.noaa.gov/data/observations/metar/stations/VOCB.TXT"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = response.read().decode('utf-8')
            lines = data.split('\n')
            if len(lines) > 1:
                metar = lines[1]
                for p in metar.split():
                    if p.endswith('KT'):
                        wind_dir_str = p[:3]
                        if wind_dir_str.isdigit():
                            wind_dir = int(wind_dir_str)
                            ACTIVE_RUNWAY = "23" if 143 <= wind_dir <= 323 else "05"
    except Exception: pass
    LAST_METAR_FETCH = time.time()
    return ACTIVE_RUNWAY

def calculate_bearing(lat1, lon1, lat2, lon2):
    dLon = math.radians(lon2 - lon1)
    y = math.sin(dLon) * math.cos(math.radians(lat2))
    x = math.cos(math.radians(lat1)) * math.sin(math.radians(lat2)) - \
        math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(dLon)
    brng = math.atan2(y, x)
    return (math.degrees(brng) + 360) % 360

AIRLINE_MAP = {
    "6E": "IGO", "AI": "AIC", "UK": "VTI", "SG": "SEJ", "I5": "IAD",
    "IX": "AXB", "QP": "AKJ", "9I": "LLR", "S5": "SDG", "S9": "FLG",
    "IC": "GOA", "I7": "IOA", "G9": "ABY", "TR": "TGW", "EK": "UAE"
}

AIRPORT_MAP = {
    "CJB": "VOCB", "DEL": "VIDP", "BOM": "VABB", "BLR": "VOBL",
    "MAA": "VOMM", "HYD": "VOHS", "COK": "VOCI", "SIN": "WSSS",
    "SHJ": "OMSJ", "GOI": "VOGO", "GOX": "VOGA"
}

def normalize_callsign(callsign):
    if not callsign: return "UNK"
    callsign = callsign.strip().upper()
    if callsign.startswith(tuple(AIRLINE_MAP.values())):
        return callsign
    for iata, icao in AIRLINE_MAP.items():
        if callsign.startswith(iata): return callsign.replace(iata, icao, 1)
    return callsign

def get_icao_airport(iata): return AIRPORT_MAP.get(iata, iata)

def distance_to_vocb(lat, lon):
    R = 6371
    dlat, dlon = math.radians(VOCB_LAT - lat), math.radians(VOCB_LON - lon)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat)) * math.cos(math.radians(VOCB_LAT)) * math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def update_dynamic_watchlist():
    global DYNAMIC_WATCHLIST, LAST_SCHEDULE_FETCH
   
    # FIX: Check schedule every 3 minutes (180s) instead of every 30 minutes!
    if time.time() - LAST_SCHEDULE_FETCH < 180: return
       
    url = "https://api.flightradar24.com/common/v1/airport.json"
    params = {"code": TARGET_IATA, "plugin[]": "schedule", "plugin-setting[schedule][mode]": "arrivals", "plugin-setting[schedule][timestamp]": int(time.time()), "page": 1, "limit": 100}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
   
    try:
        r = requests.get(url, headers=headers, params=params, timeout=10)
        if r.status_code == 200:
            arrivals = r.json().get("result", {}).get("response", {}).get("airport", {}).get("pluginData", {}).get("schedule", {}).get("arrivals", {}).get("data", [])
            new_dict = {}
            for entry in arrivals:
                f_info = entry.get("flight", {})
               
                cs_raw = f_info.get("identification", {}).get("callsign")
                num_raw = f_info.get("identification", {}).get("number", {}).get("default")
               
                times = f_info.get("time", {})
                real_dep = times.get("real", {}).get("departure")
                sch_dep = times.get("scheduled", {}).get("departure")
               
                if real_dep:
                    dep_str = "ATD: " + datetime.fromtimestamp(real_dep, timezone.utc).strftime("%H:%M")
                elif sch_dep:
                    dep_str = "STD: " + datetime.fromtimestamp(sch_dep, timezone.utc).strftime("%H:%M")
                else:
                    dep_str = "DEP: --:--"
                   
                if cs_raw:
                    new_dict[normalize_callsign(cs_raw)] = dep_str
                if num_raw:
                    new_dict[num_raw.strip().upper().replace(" ", "")] = dep_str
                   
            if new_dict:
                DYNAMIC_WATCHLIST = new_dict
    except Exception: pass
    LAST_SCHEDULE_FETCH = time.time()

# --- BACKGROUND RADAR ENGINE ---
async def radar_loop():
    global strips
    while True:
        update_dynamic_watchlist()
        rwy_in_use = get_active_runway()
       
        try:
            flights = fr_api.get_flights(bounds=REGION_BOUNDS)
            now = time.time()
           
            for f in flights:
                if not f.latitude or not f.longitude: continue
               
                norm_cs = normalize_callsign(f.callsign)
                aircraft_type = f.aircraft_code.upper() if f.aircraft_code else ""
               
                TACTICAL_CALLSIGNS = ("IFC", "RAVEN", "SARANG", "TEJAS", "IAF", "VAYU", "SULUR", "DEF", "K1", "K2", "CHETAK")
                MILITARY_AIRCRAFT = ("SU30", "LCA", "AN32", "IL76", "C17", "C130", "HAWK", "D228")
               
                if norm_cs.startswith(TACTICAL_CALLSIGNS) or aircraft_type.startswith(MILITARY_AIRCRAFT):
                    continue
               
                f_num = getattr(f, 'number', '')
                f_num = f_num.strip().upper().replace(" ", "") if f_num else ""
               
                if f.origin_airport_iata == TARGET_IATA: continue
                dest_iata = f.destination_airport_iata
                if dest_iata and dest_iata not in [TARGET_IATA, "N/A", ""]: continue

                dist = distance_to_vocb(f.latitude, f.longitude)
                alt = f.altitude
                gs = f.ground_speed
                on_ground = f.on_ground == 1
               
                is_cjb_bound = dest_iata == TARGET_IATA
                is_auto_expected = (norm_cs in DYNAMIC_WATCHLIST) or (f_num in DYNAMIC_WATCHLIST)
                is_manual_expected = norm_cs in NORMALIZED_MANUAL_LIST
                is_in_airspace = (dist < 100) and (alt < 20000)

                if not (is_cjb_bound or is_auto_expected or is_manual_expected or is_in_airspace): continue

                icao_id = f.id
               
                # Retrieve the freshest data from our 3-minute schedule cache
                current_watchlist_dep = DYNAMIC_WATCHLIST.get(norm_cs) or DYNAMIC_WATCHLIST.get(f_num) or "DEP: --:--"
               
                eta_str = "--:--"
                eta_unix = float('inf')
               
                if gs > 50 and not on_ground:
                    bearing = calculate_bearing(f.latitude, f.longitude, VOCB_LAT, VOCB_LON)
                    rwy_heading = 233 if rwy_in_use == "23" else 53
                    angle_diff = abs((bearing - rwy_heading + 180) % 360 - 180)
                    lateral_miles = dist + 40 if angle_diff > 90 else dist + 15
                   
                    alt_to_lose = max(0, alt - AIRPORT_ELEV)
                   
                    if dist <= 55.56:
                        mins_to_descend = alt_to_lose / 1000.0
                        hours_vertical = mins_to_descend / 60.0
                        speed_kmh = max(gs * 1.852, 220)
                        hours_lateral = lateral_miles / speed_kmh
                        hours_remaining = max(hours_vertical, hours_lateral)
                    else:
                        required_descent_dist_km = (alt_to_lose / 1000) * 3 * 1.852
                        true_track_distance = max(lateral_miles, required_descent_dist_km)
                       
                        if alt < 5000: phase_speed = 260
                        elif alt < 10000: phase_speed = 450
                        elif alt < 20000: phase_speed = 550
                        else: phase_speed = 750
                           
                        blended_speed_kmh = (gs * 1.852 * 0.4) + (phase_speed * 0.6)
                        hours_remaining = true_track_distance / max(blended_speed_kmh, 250)
                   
                    eta_time = datetime.now(timezone.utc) + timedelta(hours=hours_remaining)
                    eta_str = eta_time.strftime("%H:%M")
                    eta_unix = eta_time.timestamp()

                if icao_id not in strips and not on_ground:
                    init_status = "EN ROUTE"
                    if dist < 100: init_status = "APPROACH"
                    if dist < 10 and alt <= AIRPORT_ELEV + 1000: init_status = "LANDED"

                    strips[icao_id] = {
                        "callsign": norm_cs, "origin": get_icao_airport(f.origin_airport_iata) if f.origin_airport_iata else "UNK",
                        "dest": "VOCB", "aircraft": f.aircraft_code if f.aircraft_code else "UNK", "speed": gs,
                        "status": init_status, "dep_time": current_watchlist_dep, "eta": eta_str, "sort_time": eta_unix,
                        "touchdown": None, "last_seen": now, "distance": int(dist)
                    }

                if icao_id in strips:
                    s = strips[icao_id]
                    s["last_seen"] = now
                    s["distance"] = int(dist)
                    s["speed"] = gs
                   
                    if s["status"] != "LANDED":
                        s["eta"] = eta_str
                        s["sort_time"] = eta_unix
                       
                    # --- FIX: Dynamic ATD Overwrite ---
                    # If the 3-minute schedule check found an "ATD", seamlessly overwrite the old "STD" or "DEP"
                    if "ATD" in current_watchlist_dep and "ATD" not in s["dep_time"]:
                        s["dep_time"] = current_watchlist_dep
                    elif "STD" in current_watchlist_dep and "DEP: --" in s["dep_time"]:
                        s["dep_time"] = current_watchlist_dep
                   
                    if s["status"] == "EN ROUTE" and dist < 100: s["status"] = "APPROACH"
                   
                    if s["status"] == "APPROACH" and dist < 10:
                        if on_ground or (alt <= (AIRPORT_ELEV + 100) and gs < 60):
                            if s["status"] != "LANDED":
                                s["status"] = "LANDED"
                                td_time = datetime.now(timezone.utc)
                                s["touchdown"] = td_time.strftime("%H:%M:%S")
                                s["sort_time"] = td_time.timestamp()

        except Exception as e: print(f"Radar polling error: {e}")

        now = time.time()
        for k in list(strips.keys()):
            s = strips[k]
            time_lost = now - s["last_seen"]
           
            if s["status"] == "APPROACH" and s["distance"] < 20 and time_lost > 60:
                s["status"] = "LANDED"
                s["touchdown"] = datetime.now(timezone.utc).strftime("%H:%M:%S")
                s["sort_time"] = time.time()
                s["last_seen"] = now
               
            elif time_lost > 900: del strips[k]
           
        await asyncio.sleep(8)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(radar_loop())


# --- FRONTEND WEB PAGE ---
html_content = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>VOCB Arrival Board</title>
    <style>
        body { background-color: #546e7a; color: #000; font-family: 'Courier New', Courier, monospace; margin: 0; padding: 20px;}
        .header-container { position: relative; max-width: 1000px; margin: 0 auto 20px auto; text-align: center; }
        h1 { color: #fff; font-family: sans-serif; letter-spacing: 2px; margin-bottom: 5px; margin-top: 0;}
        .rwy-header { color: #ffeb3b; font-weight: bold; font-size: 1.2em;}
        .utc-clock { position: absolute; top: 0; right: 0; background-color: #000; color: #00ff00; padding: 8px 15px; border: 2px solid #555; font-size: 1.4em; font-weight: bold; box-shadow: 2px 2px 5px rgba(0,0,0,0.5);}
        .board { display: flex; flex-direction: column; gap: 8px; max-width: 1000px; margin: 0 auto; }
        .strip { display: grid; grid-template-columns: 1.5fr 1.5fr 1fr 1fr 1fr; background-color: #ffe0b2; border: 2px solid #000; box-shadow: 3px 3px 5px rgba(0,0,0,0.4); height: 65px; font-weight: bold; font-size: 1.1em; }
        .strip > div { border-right: 2px solid #000; padding: 5px 10px; display: flex; flex-direction: column; justify-content: center; }
        .strip > div:last-child { border-right: none; }
        .strip.approach { background-color: #bbdefb; }
        .strip.landed { background-color: #c8e6c9; color: #555; }
        .small-text { font-size: 0.75em; color: #444; }
        .large-text { font-size: 1.3em; }
        .status-text { text-align: center; font-size: 1.1em; }
        .eta-box { background: #fff; border: 1px solid #000; padding: 2px 5px; text-align: center; display: inline-block;}
        .landed .eta-box { background: transparent; border: none; text-decoration: line-through;}
    </style>
</head>
<body>
    <div class="header-container">
        <h1 id="main-title">✈️ COIMBATORE TOWER (VOCB)</h1>
        <div id="rwy-display" class="rwy-header">FETCHING ACTIVE RUNWAY...</div>
        <div id="clock" class="utc-clock">00:00:00</div>
    </div>
    <div id="board" class="board">
        <p style="text-align: center; color: #fff;">Connecting to radar... calculating ETA vectors.</p>
    </div>

    <script>
        function updateClock() {
            const now = new Date();
            const hours = String(now.getUTCHours()).padStart(2, '0');
            const minutes = String(now.getUTCMinutes()).padStart(2, '0');
            const seconds = String(now.getUTCSeconds()).padStart(2, '0');
            document.getElementById('clock').innerText = `${hours}:${minutes}:${seconds}`;
        }
        setInterval(updateClock, 1000);
        updateClock();

        function connectWebSocket() {
            const ws_protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
            const ws = new WebSocket(ws_protocol + "//" + window.location.host + "/ws");
           
            ws.onmessage = (event) => {
                const flights = JSON.parse(event.data);
                const container = document.getElementById('board');
                const rwyDisplay = document.getElementById('rwy-display');
               
                if (flights.length > 0 && flights[0].rwy) {
                    rwyDisplay.innerText = `ACTIVE RUNWAY IN USE: ${flights[0].rwy}`;
                }

                if (flights.length === 0) {
                    container.innerHTML = '<p style="text-align: center; color: #fff;">No inbound flights found currently.</p>';
                    return;
                }

                container.innerHTML = '';
               
                flights.forEach(f => {
                    const div = document.createElement('div');
                    let stripClass = "strip";
                    if (f.status === "APPROACH") stripClass += " approach";
                    if (f.status === "LANDED") stripClass += " landed";
                    div.className = stripClass;

                    const block1 = `<div><span class="large-text">${f.callsign}</span><span class="small-text">${f.aircraft} | ${f.speed} kts</span></div>`;
                    const block2 = `<div><span class="large-text">${f.origin} ✈️ ${f.dest}</span><span class="small-text">${f.dep_time} | ${f.distance} km</span></div>`;
                    const block3 = `<div><span class="status-text">${f.status}</span></div>`;
                    const block4 = `<div style="align-items: center;"><span class="small-text">ETA (UTC)</span><span class="large-text eta-box">${f.eta}</span></div>`;
                    const tdTime = f.touchdown ? f.touchdown : "--:--:--";
                    const tdColor = f.touchdown ? '#d32f2f' : 'inherit';
                    const block5 = `<div><span class="small-text">ATA (UTC)</span><span class="large-text" style="color: ${tdColor}; text-align: center;">${tdTime}</span></div>`;

                    div.innerHTML = block1 + block2 + block3 + block4 + block5;
                    container.appendChild(div);
                });
            };

            ws.onerror = () => { document.getElementById('board').innerHTML = '<p style="text-align: center; color: #ff5252; font-weight: bold;">Connection lost. Reconnecting...</p>'; };
            ws.onclose = () => { setTimeout(connectWebSocket, 3000); };
        }
       
        connectWebSocket();
    </script>
</body>
</html>
"""

@app.get("/")
async def get_webpage():
    return HTMLResponse(html_content)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            current_strips = [{"icao": k, "rwy": ACTIVE_RUNWAY, **v} for k, v in strips.items()]
            current_strips.sort(key=lambda x: x["sort_time"])
            await websocket.send_json(current_strips)
            await asyncio.sleep(8)
    except Exception:
        pass
