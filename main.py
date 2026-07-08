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

ORIGIN_COORDS = {
    "DEL": (28.5562, 77.1000), "MAA": (12.9941, 80.1709),
    "BLR": (13.1986, 77.7066), "BOM": (19.0900, 72.8680),
    "HYD": (17.2403, 78.4294), "SIN": (1.3644, 103.9915),
    "SHJ": (25.3286, 55.5172), "PNQ": (18.5822, 73.9197),
    "COK": (10.1520, 76.3930), "AUH": (24.4330, 54.6511),
    "DXB": (25.2532, 55.3657), "CJB": (11.0300, 77.0434),
    "CCU": (22.6547, 88.4467), "AMD": (23.0734, 72.6347),
    "TRV": (8.4821, 76.9201),  "IXM": (9.8345, 78.0934),
    "DOH": (25.2731, 51.6080), "MCT": (23.5933, 58.2844),
    "JED": (21.6796, 39.1565), "RUH": (24.9576, 46.6988),
    "NMI": (18.9944, 73.0703)
}

def get_active_runway():
    global ACTIVE_RUNWAY, LAST_METAR_FETCH
    if time.time() - LAST_METAR_FETCH < 1800:
        return ACTIVE_RUNWAY
    try:
        url = "https://tgftp.nws.noaa.gov/data/observations/metar/stations/VOCB.TXT"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = response.read().decode('utf-8')
            lines = data.splitlines()
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

def get_distance(lat1, lon1, lat2, lon2):
    R = 6371
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

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
    "SHJ": "OMSJ", "GOI": "VOGO", "GOX": "VOGA", "PNQ": "VAPO",
    "CCU": "VECC", "AMD": "VAAH", "TRV": "VOCL", "IXM": "VOMD",
    "IXZ": "VOPB", "CNN": "VOCA", "TRZ": "VOTV", "VTZ": "VOTR",
    "RPR": "VERP", "NAG": "VARP", "BBI": "VEBS", "PAT": "VEPT",
    "IXC": "VICG", "SXR": "VISR", "ATQ": "VIAR", "GAU": "VEGT",
    "JAI": "VIJP", "LKO": "VILK", "BHO": "VIBN", "IXB": "VIBK",
    "BDQ": "VABO", "IDR": "VAID", "AUH": "OMAA", "DXB": "OMDB",
    "DOH": "OTHH", "JED": "OEJN", "RUH": "OERK", "KWI": "OKBK",
    "MCT": "OOMS", "BAH": "OBBI", "CMB": "VCBI", "KTM": "VNKT",
    "NMI": "VANM"
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

def update_dynamic_watchlist():
    global DYNAMIC_WATCHLIST, LAST_SCHEDULE_FETCH
    if time.time() - LAST_SCHEDULE_FETCH < 180: return
       
    url = "https://api.flightradar24.com/common/v1/airport.json"
    params = {"code": TARGET_IATA, "plugin[]": "schedule", "plugin-setting[schedule][mode]": "arrivals", "plugin-setting[schedule][timestamp]": int(time.time()), "page": 1, "limit": 100}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
   
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
                   
                if cs_raw: new_dict[normalize_callsign(cs_raw)] = dep_str
                if num_raw: new_dict[num_raw.strip().upper().replace(" ", "")] = dep_str
                   
            if new_dict: DYNAMIC_WATCHLIST = new_dict
    except Exception: pass
    LAST_SCHEDULE_FETCH = time.time()

def get_deep_atd(flight_id):
    try:
        details = fr_api.get_flight_details(flight_id)
        if details and isinstance(details, dict):
            real_dep = details.get("time", {}).get("real", {}).get("departure")
            if real_dep:
                return "ATD: " + datetime.fromtimestamp(real_dep, timezone.utc).strftime("%H:%M")
            sch_dep = details.get("time", {}).get("scheduled", {}).get("departure")
            if sch_dep:
                return "STD: " + datetime.fromtimestamp(sch_dep, timezone.utc).strftime("%H:%M")
    except Exception:
        pass
    return None

# --- BACKGROUND RADAR ENGINE ---
async def radar_loop():
    global strips
    while True:
        await asyncio.to_thread(update_dynamic_watchlist)
        rwy_in_use = await asyncio.to_thread(get_active_runway)
       
        try:
            flights = await asyncio.to_thread(fr_api.get_flights, bounds=REGION_BOUNDS)
            now = time.time()
           
            for f in flights:
                if not f.latitude or not f.longitude: continue
               
                icao_id = f.id
               
                dist = get_distance(f.latitude, f.longitude, VOCB_LAT, VOCB_LON)
                alt = f.altitude
                gs = f.ground_speed
                v_speed = f.vertical_speed if f.vertical_speed is not None else 0
                on_ground = f.on_ground == 1
                dest_iata = f.destination_airport_iata
                aircraft_type = f.aircraft_code.upper() if f.aircraft_code else ""
               
                norm_cs = normalize_callsign(f.callsign)

                if norm_cs == "UNK":
                    for existing_id, s in list(strips.items()):
                        if s["status"] != "LANDED" and (now - s["last_seen"]) > 10:
                            time_diff = now - s["last_seen"]
                            speed_km_sec = max(s["speed"], 140) * 0.000514
                            expected_dist = s.get("last_real_distance", s["distance"]) - (speed_km_sec * time_diff)
                           
                            if abs(dist - expected_dist) < 8.0:
                                norm_cs = s["callsign"]
                                break
                               
                if not norm_cs or norm_cs == "UNK": continue

                # Added ROBIN and IL78 to blackout filters
                TACTICAL_CALLSIGNS = ("IFC", "RAVEN", "SARANG", "TEJAS", "IAF", "VAYU", "SULUR", "DEF", "K1", "K2", "CHETAK", "VU", "ROBIN")
                MILITARY_AIRCRAFT = ("SU30", "LCA", "AN32", "IL76", "IL78", "C17", "C130", "HAWK", "D228")
               
                if norm_cs.startswith(TACTICAL_CALLSIGNS) or aircraft_type.startswith(MILITARY_AIRCRAFT):
                    continue
               
                f_num = getattr(f, 'number', '')
                f_num = f_num.strip().upper().replace(" ", "") if f_num else ""
               
                duplicate_id = None
                for existing_id, strip_data in list(strips.items()):
                    if strip_data["callsign"] == norm_cs and strip_data["status"] != "LANDED":
                        if existing_id != icao_id:
                            duplicate_id = existing_id
                        break

                is_already_tracked = (icao_id in strips) or (duplicate_id is not None)
                is_auto_expected = (norm_cs in DYNAMIC_WATCHLIST) or (f_num in DYNAMIC_WATCHLIST)
               
                if not is_already_tracked:
                    if dest_iata and dest_iata not in [TARGET_IATA, "N/A", ""]:
                        continue
               
                if not is_already_tracked and dist < 60 and v_speed > 250 and dest_iata != TARGET_IATA:
                    continue
               
                is_cjb_bound = dest_iata == TARGET_IATA
                is_manual_expected = norm_cs in NORMALIZED_MANUAL_LIST
                is_unannounced_arrival = (dest_iata in ["", "N/A"]) and (dist < 75) and (alt < 15000) and (v_speed < -150)

                is_qualified = (
                    is_already_tracked or is_cjb_bound or is_auto_expected or
                    is_manual_expected or is_unannounced_arrival
                )
               
                if not is_qualified:
                    continue

                historical_dep = None
                historical_missed_approach = False
               
                if duplicate_id:
                    historical_dep = strips[duplicate_id]["dep_time"]
                    historical_missed_approach = strips[duplicate_id].get("initiated_missed_approach", False)
                    del strips[duplicate_id]

                current_watchlist_dep = DYNAMIC_WATCHLIST.get(norm_cs) or DYNAMIC_WATCHLIST.get(f_num) or "DEP: --:--"
               
                eta_str = "--:--"
                eta_unix = float('inf')
               
                if gs > 50 and not on_ground:
                    bearing = calculate_bearing(f.latitude, f.longitude, VOCB_LAT, VOCB_LON)
                    rwy_heading = 233 if rwy_in_use == "23" else 53
                    angle_diff = abs((bearing - rwy_heading + 180) % 360 - 180)
                   
                    dist_nm = dist / 1.852
                    gs_knots = max(gs, 150)
                    is_turboprop = aircraft_type.startswith("AT") or "ATR" in aircraft_type or aircraft_type.startswith("DH")
                   
                    arc_penalty = 0
                    if angle_diff > 45:
                        arc_penalty = 4.0 if is_turboprop else 3.0
                       
                    if is_turboprop:
                        if dist_nm <= 50:
                            mins_remaining = (dist_nm / 50.0) * 18.0
                        elif dist_nm <= 100:
                            mins_remaining = 18.0 + ((dist_nm - 50.0) / 50.0) * 12.0
                        else:
                            mins_remaining = 30.0 + ((dist_nm - 100.0) / (gs_knots / 60.0))
                    else:
                        if dist_nm <= 50:
                            mins_remaining = (dist_nm / 50.0) * 13.0
                        elif dist_nm <= 100:
                            mins_remaining = 13.0 + ((dist_nm - 50.0) / 50.0) * 7.0
                        else:
                            mins_remaining = 20.0 + ((dist_nm - 100.0) / (gs_knots / 60.0))
                           
                    mins_remaining += arc_penalty
                    hours_remaining = mins_remaining / 60.0
                   
                    eta_time = datetime.now(timezone.utc) + timedelta(hours=hours_remaining)
                    eta_str = eta_time.strftime("%H:%M")
                    eta_unix = eta_time.timestamp()

                if icao_id not in strips and not on_ground:
                    init_status = "EN ROUTE"
                    if dist < 100: init_status = "APPROACH"
                    if dist < 3.0 and alt <= AIRPORT_ELEV + 1000: init_status = "LANDED"
                   
                    final_dep_str = historical_dep if historical_dep else current_watchlist_dep
                   
                    if "ATD" not in final_dep_str:
                        deep_dep = await asyncio.to_thread(get_deep_atd, f.id)
                        if deep_dep:
                            final_dep_str = deep_dep
                        else:
                            origin_iata = f.origin_airport_iata
                            if origin_iata in ORIGIN_COORDS:
                                o_lat, o_lon = ORIGIN_COORDS[origin_iata]
                                dist_flown = get_distance(o_lat, o_lon, f.latitude, f.longitude)
                               
                                if aircraft_type.startswith("AT") or "ATR" in aircraft_type or aircraft_type.startswith("DH"):
                                    perf_speed = 380.0
                                    perf_sid = 4.0     
                                else:
                                    perf_speed = 680.0
                                    perf_sid = 2.0     
                                   
                                hours_flown = max(0, (dist_flown / perf_speed) + (perf_sid / 60.0))
                                atd_time = datetime.now(timezone.utc) - timedelta(hours=hours_flown)
                                final_dep_str = "ATD: " + atd_time.strftime("%H:%M")

                    strips[icao_id] = {
                        "callsign": norm_cs, "origin": get_icao_airport(f.origin_airport_iata) if f.origin_airport_iata else "UNK",
                        "dest": "VOCB", "aircraft": f.aircraft_code if f.aircraft_code else "UNK", "speed": gs,
                        "status": init_status, "dep_time": final_dep_str, "eta": eta_str, "sort_time": eta_unix,
                        "touchdown": None, "last_seen": now, "distance": int(dist), "last_real_distance": dist,
                        "min_distance": dist,  # NEW: Tracks absolute closest approach
                        "last_dep_check": now, "initiated_missed_approach": historical_missed_approach
                    }

                if icao_id in strips:
                    s = strips[icao_id]
                    s["last_seen"] = now
                    s["min_distance"] = min(s.get("min_distance", dist), dist)
                   
                    # OUTBOUND PURGE 1: Delete any tracked flight that moves away past 100 NM (185 km)
                    if dist > 185 and s["min_distance"] < 100:
                        del strips[icao_id]
                        continue
                       
                    # OUTBOUND PURGE 2: Delete unannounced non-CJB traffic wandering past 60 NM (110 km)
                    if dist > 110 and dest_iata != TARGET_IATA and norm_cs not in NORMALIZED_MANUAL_LIST:
                        del strips[icao_id]
                        continue
                       
                    s["last_real_distance"] = dist
                    s["distance"] = int(dist)
                    s["speed"] = gs

                    if s.get("initiated_missed_approach") and dist > 55.56:
                        del strips[icao_id]
                        continue
                   
                    if s["status"] == "LANDED" and not on_ground and alt > (AIRPORT_ELEV + 800) and gs > 100:
                        s["status"] = "APPROACH"
                        s["touchdown"] = None
                        s["initiated_missed_approach"] = True
                   
                    if s["status"] != "LANDED":
                        s["eta"] = eta_str
                        s["sort_time"] = eta_unix
                       
                    if "ATD" not in s["dep_time"] and (now - s.get("last_dep_check", 0) > 240):
                        deep_dep = await asyncio.to_thread(get_deep_atd, f.id)
                        if deep_dep:
                            s["dep_time"] = deep_dep
                        else:
                            origin_iata = f.origin_airport_iata
                            if origin_iata in ORIGIN_COORDS:
                                o_lat, o_lon = ORIGIN_COORDS[origin_iata]
                                dist_flown = get_distance(o_lat, o_lon, f.latitude, f.longitude)
                               
                                if aircraft_type.startswith("AT") or "ATR" in aircraft_type or aircraft_type.startswith("DH"):
                                    perf_speed = 380.0
                                    perf_sid = 4.0
                                else:
                                    perf_speed = 680.0
                                    perf_sid = 2.0
                                   
                                hours_flown = max(0, (dist_flown / perf_speed) + (perf_sid / 60.0))
                                atd_time = datetime.now(timezone.utc) - timedelta(hours=hours_flown)
                                s["dep_time"] = "ATD: " + atd_time.strftime("%H:%M")
                        s["last_dep_check"] = now
                   
                    if s["status"] == "EN ROUTE" and dist < 100: s["status"] = "APPROACH"
                   
                    if s["status"] == "APPROACH":
                        if (on_ground and dist < 3.0) or (dist < 1.2 and alt <= (AIRPORT_ELEV + 800)):
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
           
            if s["status"] == "APPROACH" and s.get("last_real_distance", 999) < 85 and time_lost > 30:
               
                # Failsafe: Decelerating Ghost Protocol
                last_dist = s.get("last_real_distance", 999)
                if last_dist < 25:
                    ghost_kts = 145.0 
                elif last_dist < 50:
                    ghost_kts = 210.0 
                else:
                    ghost_kts = s["speed"]
                   
                speed_km_sec = max(ghost_kts, 130) * 0.000514
                ghost_dist = last_dist - (speed_km_sec * time_lost)
               
                if ghost_dist <= 0 or (s["distance"] < 6 and time_lost > 75):
                    if s["status"] != "LANDED":
                        exact_td_unix = s["last_seen"] + (last_dist / speed_km_sec)
                        if now >= exact_td_unix:
                            s["status"] = "LANDED"
                            s["distance"] = 0
                            exact_td_time = datetime.fromtimestamp(exact_td_unix, timezone.utc)
                            s["touchdown"] = exact_td_time.strftime("%H:%M:%S")
                            s["sort_time"] = exact_td_unix
                            s["last_seen"] = now 
                else:
                    s["distance"] = int(max(1, ghost_dist))
               
            elif time_lost > 1800: del strips[k]
           
        await asyncio.sleep(8)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(radar_loop())

@app.get("/api/flights")
async def get_flights_api():
    strips_snapshot = list(strips.items())
    current_strips = [{"icao": k, "rwy": ACTIVE_RUNWAY, **v} for k, v in strips_snapshot]
    current_strips.sort(key=lambda x: x["sort_time"])
    return current_strips

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
        .utc-clock { position: absolute; top: 0; right: 0; background-color: #000; color: #00ff00; padding: 8px 15px; border: 2px solid #555; font-size: 1.8em; font-weight: bold; box-shadow: 2px 2px 5px rgba(0,0,0,0.5);}
        .board { display: flex; flex-direction: column; gap: 8px; max-width: 1000px; margin: 0 auto; }
       
        /* 3D Flexbox Strip UI */
        .strip {
            display: flex; width: 100%; box-sizing: border-box;
            background: linear-gradient(to bottom, #ffecb3 0%, #ffb74d 100%);
            border: 1px solid #444;
            border-bottom: 4px solid #333;
            border-radius: 6px;
            box-shadow: 0 6px 10px rgba(0,0,0,0.5), inset 0 2px 2px rgba(255,255,255,0.8);
            height: 65px; font-weight: bold; font-size: 1.1em;
            margin-bottom: 4px;
            transition: all 0.2s ease;
        }
        .strip > div {
            box-sizing: border-box;
            border-right: 1px solid rgba(0,0,0,0.3);
            box-shadow: inset -1px 0 0 rgba(255,255,255,0.4);
            padding: 5px 10px; display: flex; flex-direction: column; justify-content: center; overflow: hidden;
        }
        .strip > div:nth-child(1) { flex: 3; }
        .strip > div:nth-child(2) { flex: 3; }
        .strip > div:nth-child(3) { flex: 2; }
        .strip > div:nth-child(4) { flex: 2; }
        .strip > div:nth-child(5) { flex: 2; border-right: none; box-shadow: none; }
       
        /* 3D States */
        .strip.approach {
            background: linear-gradient(to bottom, #bbdefb 0%, #42a5f5 100%);
        }
        .strip.landed {
            background: linear-gradient(to bottom, #c8e6c9 0%, #66bb6a 100%);
            color: #333;
            border-bottom: 1px solid #444;
            transform: translateY(3px);
            box-shadow: 0 2px 4px rgba(0,0,0,0.4), inset 0 1px 1px rgba(255,255,255,0.5);
        }
       
        .small-text { font-size: 0.75em; color: #444; }
        .large-text { font-size: 1.3em; }
        .status-text { text-align: center; font-size: 1.1em; margin-top: 2px; }
       
        /* Highlighted Distance Badge */
        .dist-highlight {
            font-size: 1.4em;
            font-weight: 900;
            color: #000;
            background-color: rgba(255, 255, 255, 0.7);
            padding: 1px 6px;
            border-radius: 4px;
            border: 1px solid rgba(0,0,0,0.3);
            box-shadow: 1px 1px 3px rgba(0,0,0,0.3);
            margin-left: 5px;
            display: inline-block;
        }

        /* Inset Screen Look for ETA */
        .eta-box {
            background: #fafafa;
            border: 1px solid #777;
            border-top: 2px solid #222;
            border-left: 2px solid #222;
            padding: 2px 12px; margin-top: 4px; border-radius: 4px; text-align: center; display: inline-block; font-size: 1.6em;
            box-shadow: inset 2px 2px 5px rgba(0,0,0,0.2), 0 1px 0 rgba(255,255,255,0.6);
        }
        .landed .eta-box { background: transparent; border: none; box-shadow: none; text-decoration: line-through;}
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
        let usePolling = false;
       
        const initialServerTime = {{SERVER_TIME}};
        const initialLocalTime = Date.now();

        function updateClock() {
            var elapsed = Date.now() - initialLocalTime;
            var trueNow = new Date(initialServerTime + elapsed);
           
            var hours = ('0' + trueNow.getUTCHours()).slice(-2);
            var minutes = ('0' + trueNow.getUTCMinutes()).slice(-2);
            var seconds = ('0' + trueNow.getUTCSeconds()).slice(-2);
            document.getElementById('clock').innerText = hours + ':' + minutes + ':' + seconds;
        }
        setInterval(updateClock, 1000);
        updateClock();

        function renderFlights(flights) {
            const container = document.getElementById('board');
            const rwyDisplay = document.getElementById('rwy-display');
           
            if (flights.length > 0 && flights[0].rwy) { rwyDisplay.innerText = `ACTIVE RUNWAY IN USE: ${flights[0].rwy}`; }

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
               
                const distNM = Math.round(f.distance * 0.539957);

                const block1 = `<div><span class="large-text">${f.callsign}</span><span class="small-text">${f.aircraft} | ${f.speed} kts</span></div>`;
                const block2 = `<div><span class="large-text">${f.origin} ✈️ ${f.dest}</span><span class="small-text">${f.dep_time} | <span class="dist-highlight">${distNM} NM</span></span></div>`;
               
                const block3 = `<div style="align-items: center;">
                                    <svg width="22" height="22" viewBox="0 0 24 24" fill="#000" style="margin-bottom: 2px; filter: drop-shadow(0px 1px 1px rgba(255,255,255,0.4));">
                                        <path d="M9 2v12H4l8 8 8-8h-5V2H9z"/>
                                    </svg>
                                    <span class="status-text">${f.status}</span>
                                </div>`;
               
                const block4 = `<div style="align-items: center;"><span class="small-text">ETA (UTC)</span><span class="eta-box">${f.eta}</span></div>`;
                const tdTime = f.touchdown ? f.touchdown : "--:--:--";
                const tdColor = f.touchdown ? '#d32f2f' : 'inherit';
                const block5 = `<div><span class="small-text">ATA (UTC)</span><span style="color: ${tdColor}; text-align: center; font-size: 1.6em; font-weight: bold;">${tdTime}</span></div>`;

                div.innerHTML = block1 + block2 + block3 + block4 + block5;
                container.appendChild(div);
            });
        }

        function fetchFlightsPolling() {
            fetch('/api/flights')
                .then(response => response.json())
                .then(data => renderFlights(data))
                .catch(err => console.error("HTTP Polling Error:", err));
        }

        function connectWebSocket() {
            if (usePolling) return;

            const ws_protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
            const ws = new WebSocket(ws_protocol + "//" + window.location.host + "/ws");
           
            ws.onmessage = (event) => { renderFlights(JSON.parse(event.data)); };

            ws.onerror = () => {
                console.log("WebSocket blocked. Falling back to HTTP Polling...");
                usePolling = true;
                fetchFlightsPolling();
                setInterval(fetchFlightsPolling, 8000);
            };
            ws.onclose = () => { if (!usePolling) { setTimeout(connectWebSocket, 3000); } };
        }
       
        connectWebSocket();
    </script>
</body>
</html>
"""

@app.get("/")
async def get_webpage():
    server_time_ms = int(time.time() * 1000)
    rendered_html = html_content.replace("{{SERVER_TIME}}", str(server_time_ms))
    return HTMLResponse(rendered_html)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            strips_snapshot = list(strips.items())
            current_strips = [{"icao": k, "rwy": ACTIVE_RUNWAY, **v} for k, v in strips_snapshot]
            current_strips.sort(key=lambda x: x["sort_time"])
            await websocket.send_json(current_strips)
            await asyncio.sleep(8)
    except Exception:
        pass
