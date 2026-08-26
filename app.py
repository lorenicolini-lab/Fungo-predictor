from datetime import date, timedelta
import math
import re
import xml.etree.ElementTree as ET

import folium
import pandas as pd
import plotly.express as px
import requests
import streamlit as st
from streamlit_folium import st_folium

st.set_page_config(page_title="Fungo Predictor", page_icon="🍄", layout="wide")

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
ELEVATION_API = "https://api.open-meteo.com/v1/elevation"
NOMINATIM = "https://nominatim.openstreetmap.org/reverse"
LOMBARDIA_STAZIONI = "https://www.dati.lombardia.it/resource/nf78-nj6b.json"
LOMBARDIA_DATI = "https://www.dati.lombardia.it/resource/647i-nhxk.json"
LOMBARDIA_FOREST_WMS = "https://www.cartografia.servizirl.it/arcgis2/services/agricoltura/carta_forestale/MapServer/WMSServer"
EMILIA_VEGETATION_WFS = "https://servizigis.regione.emilia-romagna.it/wfs/carta_della_vegetazione"
REGIONI = {"Piemonte", "Liguria", "Lombardia", "Emilia-Romagna", "Emilia Romagna"}
HEADERS = {"User-Agent": "fungo-predictor/3.0 (Streamlit; regional-open-data-client)"}


def clamp(value, low=0, high=100):
    return max(low, min(high, value))


def haversine(lat1, lon1, lat2, lon2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def first(row, names, default=None):
    for name in names:
        if name in row and pd.notna(row[name]) and str(row[name]).strip():
            return row[name]
    return default


def normalize_region(region):
    return "Emilia-Romagna" if region == "Emilia Romagna" else region


@st.cache_data(ttl=3600, show_spinner=False)
def reverse_geocode(lat, lon):
    response = requests.get(
        NOMINATIM,
        params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 10, "addressdetails": 1},
        headers=HEADERS,
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    address = payload.get("address", {})
    region = normalize_region(address.get("state", ""))
    locality = (address.get("village") or address.get("town") or address.get("city")
                or address.get("municipality") or address.get("county") or "Punto selezionato")
    return locality, region, payload.get("display_name", locality)


@st.cache_data(ttl=1800, show_spinner=False)
def open_meteo(lat, lon):
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,relative_humidity_2m,precipitation,soil_moisture_9_to_27cm,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,wind_speed_10m_max",
        "past_days": 40,
        "forecast_days": 7,
        "timezone": "Europe/Rome",
    }
    response = requests.get(OPEN_METEO, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


@st.cache_data(ttl=21600, show_spinner=False)
def lombardia_stations():
    response = requests.get(LOMBARDIA_STAZIONI, params={"$limit": 50000}, timeout=45)
    response.raise_for_status()
    raw = pd.DataFrame(response.json())
    if raw.empty:
        return pd.DataFrame()
    raw.columns = [str(c).lower() for c in raw.columns]
    records = []
    for _, row in raw.iterrows():
        sensor_type = str(first(row, ["tipologia", "nometiposensore", "tipo_sensore", "misura"], "")).lower()
        unit = str(first(row, ["unitamisura", "unit_misura", "unita_misura"], "")).lower()
        if "precip" not in sensor_type and "piogg" not in sensor_type and unit != "mm":
            continue
        try:
            lat = float(str(first(row, ["lat", "latitude", "latitudine"])).replace(",", "."))
            lon = float(str(first(row, ["lng", "lon", "longitude", "longitudine"])).replace(",", "."))
            elevation = float(str(first(row, ["quota", "altitudine"], 0)).replace(",", "."))
            sensor_id = str(first(row, ["idsensore", "id_sensore"]))
            station_name = str(first(row, ["nomestazione", "nome_stazione", "stazione"], sensor_id))
        except (TypeError, ValueError):
            continue
        records.append({"sensor_id": sensor_id, "station": station_name, "lat": lat, "lon": lon,
                        "elevation": elevation, "network": "ARPA Lombardia"})
    return pd.DataFrame(records).drop_duplicates("sensor_id") if records else pd.DataFrame()


@st.cache_data(ttl=1800, show_spinner=False)
def lombardia_rain(sensor_id, start_day, end_day):
    start_iso = f"{start_day.isoformat()}T00:00:00"
    end_iso = f"{(end_day + timedelta(days=1)).isoformat()}T00:00:00"
    where = f"idsensore='{sensor_id}' AND data >= '{start_iso}' AND data < '{end_iso}'"
    response = requests.get(LOMBARDIA_DATI, params={"$where": where, "$limit": 50000, "$order": "data ASC"}, timeout=45)
    response.raise_for_status()
    df = pd.DataFrame(response.json())
    if df.empty:
        return pd.DataFrame(columns=["date", "rain"])
    df.columns = [str(c).lower() for c in df.columns]
    df["date"] = pd.to_datetime(df["data"], errors="coerce").dt.date
    df["value"] = pd.to_numeric(df["valore"], errors="coerce")
    if "stato" in df:
        df = df[df["stato"].isin(["VA", "VV"])]
    df = df[(df["value"] >= 0) & (df["value"] < 1000)].copy()
    if "idoperatore" in df and (df["idoperatore"].astype(str) == "4").any():
        daily = df.groupby("date", as_index=False)["value"].max()
    else:
        daily = df.groupby("date", as_index=False)["value"].sum()
    return daily.rename(columns={"value": "rain"})


def nearest_candidates(stations, lat, lon, elevation, max_km):
    if stations.empty:
        return stations
    out = stations.copy()
    out["distance_km"] = out.apply(lambda r: haversine(lat, lon, r["lat"], r["lon"]), axis=1)
    out["elevation_diff_m"] = out["elevation"] - elevation
    out = out[out["distance_km"] <= max_km].copy()
    out["score"] = out["distance_km"] + out["elevation_diff_m"].abs() / 100.0
    return out.sort_values(["score", "distance_km"]).reset_index(drop=True)


@st.cache_data(ttl=86400, show_spinner=False)
def terrain_analysis(lat, lon, spacing_m=120):
    lat_step = spacing_m / 111320.0
    lon_step = spacing_m / (111320.0 * math.cos(math.radians(lat)))
    points = [(lat + dy * lat_step, lon + dx * lon_step) for dy in (-1, 0, 1) for dx in (-1, 0, 1)]
    response = requests.get(
        ELEVATION_API,
        params={"latitude": ",".join(f"{x[0]:.6f}" for x in points),
                "longitude": ",".join(f"{x[1]:.6f}" for x in points)},
        timeout=30,
    )
    response.raise_for_status()
    z = response.json().get("elevation", [])
    if len(z) != 9 or any(v is None for v in z):
        raise ValueError("Griglia altimetrica incompleta")
    west, east = (z[0] + 2*z[3] + z[6])/4, (z[2] + 2*z[5] + z[8])/4
    south, north = (z[0] + 2*z[1] + z[2])/4, (z[6] + 2*z[7] + z[8])/4
    dzdx, dzdy = (east-west)/(2*spacing_m), (north-south)/(2*spacing_m)
    slope_rad = math.atan(math.sqrt(dzdx**2 + dzdy**2))
    slope_deg, slope_pct = math.degrees(slope_rad), math.tan(slope_rad)*100
    aspect_deg = (math.degrees(math.atan2(-dzdx, -dzdy)) + 360) % 360
    names = ["Nord", "Nord-est", "Est", "Sud-est", "Sud", "Sud-ovest", "Ovest", "Nord-ovest"]
    aspect = "Pianeggiante" if slope_deg < 2 else names[int((aspect_deg + 22.5)//45) % 8]
    return {"elevation": float(z[4]), "slope_deg": slope_deg, "slope_pct": slope_pct,
            "aspect": aspect, "aspect_deg": aspect_deg}


def terrain_bonus(aspect, slope_deg):
    base = {"Nord": 4, "Nord-est": 3, "Nord-ovest": 3, "Est": 2, "Ovest": 0,
            "Sud-est": -3, "Sud-ovest": -3, "Sud": -4, "Pianeggiante": 0}.get(aspect, 0)
    factor = clamp(slope_deg / 18.0, 0.25, 1.5) if slope_deg >= 2 else 0
    return round(base * factor)


# ---------------- RICONOSCIMENTO AUTOMATICO DEL BOSCO ----------------

def forest_class_from_text(text):
    """Riduce la descrizione regionale alle classi usate dal modello."""
    t = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    if not t:
        return "Non nota"
    if any(k in t for k in ["fagget", "fagus", "faggio"]):
        return "Faggio"
    if any(k in t for k in ["castagnet", "castanea", "castagno"]):
        return "Castagno"
    if any(k in t for k in ["querc", "rover", "cerret", "farnia", "leccio", "quercus"]):
        return "Quercia"
    if any(k in t for k in ["conifer", "peccet", "abiet", "laric", "pineta", "pino", "abete", "larice", "picea"]):
        return "Conifere"
    if any(k in t for k in ["misto", "mista", "latifoglie", "bosco", "forest"]):
        return "Bosco misto"
    return "Non nota"


def best_forest_description(properties):
    """Sceglie il campo descrittivo senza dipendere dai nomi degli attributi regionali."""
    if not isinstance(properties, dict):
        return ""
    preferred = ["descr", "tipo", "tipologia", "categoria", "formazione", "veget", "habitat", "nome", "label", "classe"]
    candidates = []
    for key, value in properties.items():
        if value is None or isinstance(value, (dict, list)):
            continue
        value = str(value).strip()
        if not value or value.lower() in {"null", "none", "-"}:
            continue
        key_l = str(key).lower()
        priority = next((i for i, word in enumerate(preferred) if word in key_l), 99)
        forest_match = forest_class_from_text(value) != "Non nota"
        candidates.append((0 if forest_match else 1, priority, -len(value), value))
    return sorted(candidates)[0][3] if candidates else ""


@st.cache_data(ttl=86400, show_spinner=False)
def emilia_feature_types():
    r = requests.get(EMILIA_VEGETATION_WFS, params={"service": "WFS", "request": "GetCapabilities"}, timeout=45)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    names = []
    for el in root.iter():
        if el.tag.split("}")[-1] == "FeatureType":
            for child in el:
                if child.tag.split("}")[-1] == "Name" and child.text:
                    names.append(child.text.strip())
                    break
    preferred = [n for n in names if any(k in n.lower() for k in ["veget", "forest", "bosco"])]
    return preferred + [n for n in names if n not in preferred]


@st.cache_data(ttl=86400, show_spinner=False)
def emilia_forest_lookup(lat, lon):
    eps = 0.00012
    bbox = f"{lon-eps},{lat-eps},{lon+eps},{lat+eps},EPSG:4326"
    errors = []
    for type_name in emilia_feature_types()[:12]:
        params = {"service": "WFS", "version": "2.0.0", "request": "GetFeature",
                  "typeNames": type_name, "outputFormat": "application/json", "count": 1,
                  "srsName": "EPSG:4326", "bbox": bbox}
        try:
            r = requests.get(EMILIA_VEGETATION_WFS, params=params, timeout=35)
            if r.status_code >= 400:
                errors.append(f"{type_name}: HTTP {r.status_code}")
                continue
            payload = r.json()
            features = payload.get("features", [])
            if not features:
                continue
            props = features[0].get("properties", {})
            raw = best_forest_description(props)
            return {"forest_raw": raw or "Vegetazione cartografata", "forest_class": forest_class_from_text(raw),
                    "source": "Carta della vegetazione Emilia-Romagna", "layer": type_name, "automatic": True}
        except (requests.RequestException, ValueError) as exc:
            errors.append(str(exc))
    return {"forest_raw": "Nessun poligono forestale trovato nel punto", "forest_class": "Non nota",
            "source": "Carta della vegetazione Emilia-Romagna", "layer": "", "automatic": True,
            "warning": errors[-1] if errors else "Punto non classificato"}


@st.cache_data(ttl=86400, show_spinner=False)
def lombardia_wms_layers():
    r = requests.get(LOMBARDIA_FOREST_WMS,
                     params={"service": "WMS", "request": "GetCapabilities", "version": "1.3.0"}, timeout=45)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    layers = []
    for layer in root.iter():
        if layer.tag.split("}")[-1] != "Layer":
            continue
        name = title = ""
        for child in layer:
            tag = child.tag.split("}")[-1]
            if tag == "Name" and child.text:
                name = child.text.strip()
            elif tag == "Title" and child.text:
                title = child.text.strip()
        if name:
            layers.append((name, title))
    preferred = [x for x in layers if any(k in (x[0] + " " + x[1]).lower() for k in ["forest", "bosco", "tipo"])]
    return preferred + [x for x in layers if x not in preferred]


def parse_feature_info(response):
    ctype = response.headers.get("content-type", "").lower()
    if "json" in ctype or response.text.lstrip().startswith(("{", "[")):
        payload = response.json()
        features = payload.get("features", []) if isinstance(payload, dict) else []
        if features:
            return features[0].get("properties", {}) or features[0].get("attributes", {})
    try:
        root = ET.fromstring(response.content)
        props = {}
        for el in root.iter():
            if len(el) == 0 and el.text and el.text.strip():
                props[el.tag.split("}")[-1]] = el.text.strip()
        return props
    except ET.ParseError:
        return {}


@st.cache_data(ttl=86400, show_spinner=False)
def lombardia_forest_lookup(lat, lon):
    delta = 0.002
    bbox = f"{lat-delta},{lon-delta},{lat+delta},{lon+delta}"  # WMS 1.3.0 + EPSG:4326: lat,lon
    errors = []
    for layer_name, layer_title in lombardia_wms_layers()[:15]:
        base = {"service": "WMS", "version": "1.3.0", "request": "GetFeatureInfo",
                "layers": layer_name, "query_layers": layer_name, "styles": "",
                "crs": "EPSG:4326", "bbox": bbox, "width": 101, "height": 101,
                "i": 50, "j": 50, "feature_count": 1}
        for info_format in ["application/json", "text/xml", "text/plain"]:
            try:
                r = requests.get(LOMBARDIA_FOREST_WMS, params={**base, "info_format": info_format}, timeout=35)
                if r.status_code >= 400:
                    continue
                props = parse_feature_info(r)
                raw = best_forest_description(props)
                if raw:
                    return {"forest_raw": raw, "forest_class": forest_class_from_text(raw),
                            "source": "Carta forestale Regione Lombardia", "layer": layer_title or layer_name,
                            "automatic": True}
            except (requests.RequestException, ValueError) as exc:
                errors.append(str(exc))
    return {"forest_raw": "Nessun poligono forestale trovato nel punto", "forest_class": "Non nota",
            "source": "Carta forestale Regione Lombardia", "layer": "", "automatic": True,
            "warning": errors[-1] if errors else "Punto non classificato"}


@st.cache_data(ttl=86400, show_spinner=False)
def detect_forest_type(lat, lon, region):
    region = normalize_region(region)
    if region == "Lombardia":
        return lombardia_forest_lookup(lat, lon)
    if region == "Emilia-Romagna":
        return emilia_forest_lookup(lat, lon)
    return {"forest_raw": "Selezione manuale", "forest_class": "Non nota",
            "source": "Inserimento utente", "layer": "", "automatic": False}


def build_forecast(raw, observed_rain=None, forest_type="Non nota", exposure_bonus_value=0):
    daily = pd.DataFrame(raw["daily"])
    daily["time"] = pd.to_datetime(daily["time"])
    daily["date"] = daily["time"].dt.date
    hourly = pd.DataFrame(raw["hourly"])
    hourly["time"] = pd.to_datetime(hourly["time"])
    hourly["date"] = hourly["time"].dt.date
    avg = hourly.groupby("date", as_index=False).agg(humidity=("relative_humidity_2m", "mean"),
        soil=("soil_moisture_9_to_27cm", "mean"), temp=("temperature_2m", "mean"))
    daily = daily.merge(avg, on="date", how="left")
    rain = daily.set_index("date")["precipitation_sum"].fillna(0).astype(float).to_dict()
    if observed_rain is not None and not observed_rain.empty:
        rain.update(observed_rain.set_index("date")["rain"].astype(float).to_dict())
    future = daily[daily["date"] >= date.today()].head(7).copy()
    forest_bonus = {"Non nota": 0, "Faggio": 5, "Castagno": 4, "Quercia": 2,
                    "Conifere": 1, "Bosco misto": 4}.get(forest_type, 0)
    rows = []
    for _, r in future.iterrows():
        day = r["date"]
        rain7 = sum(rain.get(day - timedelta(days=i), 0) for i in range(1, 8))
        last_heavy = next((i for i in range(41) if rain.get(day - timedelta(days=i), 0) >= 10), None)
        humidity = float(r["humidity"] if pd.notna(r["humidity"]) else 60)
        soil = float(r["soil"] if pd.notna(r["soil"]) else 0.2)
        temp = float(r["temp"] if pd.notna(r["temp"]) else 16)
        wind = float(r["wind_speed_10m_max"] if pd.notna(r["wind_speed_10m_max"]) else 10)
        wait_score = 18 if last_heavy is not None and 5 <= last_heavy <= 12 else 8
        score = clamp(rain7/50*32, 0, 32) + wait_score + clamp((humidity-45)/40*18, 0, 18)
        score += clamp((soil-.10)/.28*16, 0, 16) + clamp(14-abs(temp-16)*1.8, 0, 14)
        score -= clamp((wind-12)*.8, 0, 10)
        score += forest_bonus + exposure_bonus_value
        rows.append({"Data": pd.Timestamp(day), "Indice": round(clamp(score)),
            "Pioggia prevista (mm)": round(float(r["precipitation_sum"] or 0), 1),
            "Pioggia 7 gg precedenti (mm)": round(rain7, 1), "Giorni da pioggia >10 mm": last_heavy,
            "T min (°C)": round(float(r["temperature_2m_min"]), 1),
            "T max (°C)": round(float(r["temperature_2m_max"]), 1),
            "Umidità aria (%)": round(humidity), "Umidità suolo": round(soil, 3),
            "Vento max (km/h)": round(wind, 1)})
    return pd.DataFrame(rows)


# ---------------- INTERFACCIA ----------------
st.title("🍄 Fungo Predictor")
st.caption("Seleziona un punto: quota, versante e tipo di bosco vengono rilevati automaticamente dove sono disponibili dati regionali.")

with st.sidebar:
    st.header("Criteri centralina")
    reasonable_km = st.slider("Raggio preferenziale", 5, 40, 15, 5)
    max_km = st.slider("Raggio massimo di ricerca", 20, 100, 50, 5)
    max_alt_diff = st.slider("Differenza quota preferenziale", 100, 1000, 250, 50)
    st.caption("Il bosco è automatico in Lombardia ed Emilia-Romagna; resta manuale in Piemonte e Liguria.")
    if st.button("Cancella punto", use_container_width=True):
        for key in ["point", "source_choice", "station_id"]:
            st.session_state.pop(key, None)
        st.rerun()

m = folium.Map(location=[44.75, 9.10], zoom_start=7, tiles="OpenStreetMap")
if "point" in st.session_state:
    p = st.session_state["point"]
    folium.Marker([p["lat"], p["lon"]], tooltip="Punto selezionato",
        icon=folium.Icon(color="red", icon="info-sign")).add_to(m)
map_data = st_folium(m, height=480, use_container_width=True, returned_objects=["last_clicked"])
if map_data and map_data.get("last_clicked"):
    new_point = {"lat": round(map_data["last_clicked"]["lat"], 5), "lon": round(map_data["last_clicked"]["lng"], 5)}
    if st.session_state.get("point") != new_point:
        st.session_state["point"] = new_point
        st.rerun()
if "point" not in st.session_state:
    st.info("Tocca la mappa nel punto del bosco da analizzare.")
    st.stop()

p = st.session_state["point"]
try:
    with st.spinner("Recupero coordinate, quota, meteo e cartografia forestale..."):
        locality, region, address = reverse_geocode(p["lat"], p["lon"])
        raw = open_meteo(p["lat"], p["lon"])
        terrain = terrain_analysis(p["lat"], p["lon"])
except (requests.RequestException, ValueError) as exc:
    st.error(f"Errore nel recupero dei dati di base: {exc}")
    st.stop()

if region not in REGIONI:
    st.error(f"Il punto risulta in {region or 'una regione non identificata'}. Seleziona Piemonte, Liguria, Lombardia o Emilia-Romagna.")
    st.stop()

# Il lookup automatico non blocca l'intera app se il geoportale è temporaneamente indisponibile.
forest_info = detect_forest_type(p["lat"], p["lon"], region)
if forest_info["automatic"]:
    forest_type = forest_info["forest_class"]
else:
    forest_type = st.sidebar.selectbox("Prevalenza del bosco", ["Non nota", "Faggio", "Castagno", "Quercia", "Conifere", "Bosco misto"])
    forest_info = {**forest_info, "forest_raw": forest_type, "forest_class": forest_type}

elevation = round(float(terrain.get("elevation", raw.get("elevation", 0))))
auto_exposure = terrain["aspect"]
auto_exposure_bonus = terrain_bonus(auto_exposure, terrain["slope_deg"])

st.subheader(locality)
a, b, c, d = st.columns(4)
a.metric("Regione", region); b.metric("Quota punto", f"{elevation} m")
c.metric("Latitudine", p["lat"]); d.metric("Longitudine", p["lon"])
st.caption(address)

t1, t2, t3, t4 = st.columns(4)
t1.metric("Esposizione automatica", auto_exposure)
t2.metric("Pendenza stimata", f"{terrain['slope_deg']:.1f}° / {terrain['slope_pct']:.0f}%")
t3.metric("Bosco", forest_type)
t4.metric("Correttivo bosco", f"{ {'Faggio':5,'Castagno':4,'Quercia':2,'Conifere':1,'Bosco misto':4}.get(forest_type,0):+d} punti")
if forest_info["automatic"]:
    st.info(f"Bosco rilevato automaticamente: **{forest_info['forest_raw']}** → categoria modello **{forest_type}**. Fonte: {forest_info['source']}.")
    if forest_info.get("warning"):
        st.warning(f"Cartografia forestale: {forest_info['warning']}. Il modello prosegue senza bonus bosco.")
else:
    st.info("Per Piemonte e Liguria il tipo di bosco resta selezionabile manualmente.")

candidates = pd.DataFrame(); connector_message = None
if region == "Lombardia":
    try:
        candidates = nearest_candidates(lombardia_stations(), p["lat"], p["lon"], elevation, max_km)
    except requests.RequestException as exc:
        connector_message = f"Servizio ARPA Lombardia momentaneamente non raggiungibile: {exc}"
else:
    connector_message = f"Connettore pluviometrico automatico {region} non disponibile: uso Open-Meteo sul punto."

observed = None
if not candidates.empty:
    options = {}
    for _, r in candidates.head(10).iterrows():
        label = f"{r['station']} | {r['distance_km']:.1f} km | quota {r['elevation']:.0f} m | Δ {r['elevation_diff_m']:+.0f} m"
        options[label] = r
    label = st.selectbox("Centraline pluviometriche ufficiali disponibili", list(options))
    selected_station = options[label]
    recommended = selected_station["distance_km"] <= reasonable_km and abs(selected_station["elevation_diff_m"]) <= max_alt_diff
    source = st.radio("Fonte per la pioggia storica", ["Centralina reale", "Open-Meteo sul punto"],
                      index=0 if recommended else 1, horizontal=True)
    if source == "Centralina reale":
        try:
            observed = lombardia_rain(selected_station["sensor_id"], date.today()-timedelta(days=40), date.today()-timedelta(days=1))
            if observed.empty:
                st.warning("La centralina non ha restituito dati validi. Uso Open-Meteo.")
                observed = None
            else:
                st.info(f"Fonte pioggia: {selected_station['network']} | {selected_station['station']} | distanza {selected_station['distance_km']:.1f} km")
        except requests.RequestException as exc:
            st.warning(f"Dati centralina non disponibili: {exc}. Uso Open-Meteo.")
elif connector_message:
    st.warning(connector_message)

forecast = build_forecast(raw, observed, forest_type, auto_exposure_bonus)
st.subheader("Indice per i prossimi 7 giorni")
fig = px.line(forecast, x="Data", y="Indice", markers=True, range_y=[0, 100])
fig.update_traces(line_color="#16825d", line_width=4, marker_size=10, fill="tozeroy", fillcolor="rgba(22,130,93,.16)")
fig.update_layout(xaxis_title=None, yaxis_title="Indice (%)", hovermode="x unified")
st.plotly_chart(fig, use_container_width=True)


def probability_style(value):
    if value >= 75: return "#166534", "#dcfce7", "Molto alta"
    if value >= 60: return "#15803d", "#ecfccb", "Alta"
    if value >= 45: return "#a16207", "#fef9c3", "Media"
    if value >= 30: return "#c2410c", "#ffedd5", "Bassa"
    return "#b91c1c", "#fee2e2", "Molto bassa"

best_index = forecast["Indice"].idxmax()
cols = st.columns(7)
for position, (_, row) in enumerate(forecast.iterrows()):
    fg, bg, label = probability_style(int(row["Indice"]))
    border = "3px solid #166534" if row.name == best_index else "1px solid rgba(15,23,42,.10)"
    badge = "<div style='font-size:.72rem;font-weight:800;margin-top:5px'>GIORNO MIGLIORE</div>" if row.name == best_index else ""
    with cols[position]:
        st.markdown(f"<div style='background:{bg};color:{fg};border:{border};border-radius:14px;padding:10px 4px;text-align:center;min-height:112px'>"
                    f"<div style='font-size:.78rem;font-weight:700'>{row['Data'].strftime('%a %d/%m')}</div>"
                    f"<div style='font-size:1.65rem;font-weight:900;line-height:1.25'>{int(row['Indice'])}%</div>"
                    f"<div style='font-size:.72rem;font-weight:700'>{label}</div>{badge}</div>", unsafe_allow_html=True)

st.caption(f"Correttivi applicati: bosco **{forest_type}**, esposizione **{auto_exposure}**, pendenza **{terrain['slope_deg']:.1f}°**.")
st.subheader("Dettaglio giornaliero")
for _, r in forecast.iterrows():
    with st.expander(f"{r['Data'].strftime('%d/%m/%Y')} | indice {int(r['Indice'])}%"):
        x1, x2, x3, x4 = st.columns(4)
        x1.metric("Pioggia prevista", f"{r['Pioggia prevista (mm)']} mm")
        x2.metric("Pioggia 7 giorni", f"{r['Pioggia 7 gg precedenti (mm)']} mm")
        x3.metric("Ultima pioggia >10 mm", "Non trovata" if pd.isna(r["Giorni da pioggia >10 mm"]) else f"{int(r['Giorni da pioggia >10 mm'])} giorni fa")
        x4.metric("Temperatura", f"{r['T min (°C)']} / {r['T max (°C)']} °C")
        y1, y2, y3 = st.columns(3)
        y1.metric("Umidità aria", f"{int(r['Umidità aria (%)'])}%")
        y2.metric("Umidità suolo", r["Umidità suolo"])
        y3.metric("Vento massimo", f"{r['Vento max (km/h)']} km/h")
        st.write(f"**Bosco:** {forest_type} | **Classe cartografica:** {forest_info['forest_raw']} | **Esposizione:** {auto_exposure}")

st.divider()
st.caption("L'indice è sperimentale. Se la cartografia regionale non restituisce un poligono, il modello prosegue con bosco 'Non nota' e senza relativo bonus.")
