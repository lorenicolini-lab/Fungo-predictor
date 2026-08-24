from datetime import date, timedelta
from io import StringIO
import math

import folium
import pandas as pd
import plotly.express as px
import requests
import streamlit as st
from streamlit_folium import st_folium

st.set_page_config(page_title="Fungo Predictor", page_icon="🍄", layout="wide")

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
NOMINATIM = "https://nominatim.openstreetmap.org/reverse"
LOMBARDIA_STAZIONI = "https://www.dati.lombardia.it/resource/nf78-nj6b.json"
LOMBARDIA_DATI = "https://www.dati.lombardia.it/resource/647i-nhxk.json"
REGIONI = {"Piemonte", "Liguria", "Lombardia", "Emilia-Romagna", "Emilia Romagna"}
HEADERS = {"User-Agent": "fungo-predictor/2.0 (Streamlit; rainfall source selector)"}


def haversine(lat1, lon1, lat2, lon2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def first(row, names, default=None):
    for name in names:
        if name in row and pd.notna(row[name]) and str(row[name]).strip() != "":
            return row[name]
    return default


def clamp(value, low=0, high=100):
    return max(low, min(high, value))


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
    region = address.get("state", "")
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
    """Anagrafica ufficiale ARPA Lombardia via Socrata. Restituisce solo pluviometri."""
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
    params = {"$where": where, "$limit": 50000, "$order": "data ASC"}
    response = requests.get(LOMBARDIA_DATI, params=params, timeout=45)
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
    # ARPA indica idOperatore=4 come cumulata. Per evitare doppi conteggi si usa il massimo giornaliero
    # quando il sensore pubblica cumulate, altrimenti la somma dei passi incrementali.
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


def build_forecast(raw, observed_rain=None):
    daily = pd.DataFrame(raw["daily"])
    daily["time"] = pd.to_datetime(daily["time"])
    daily["date"] = daily["time"].dt.date
    hourly = pd.DataFrame(raw["hourly"])
    hourly["time"] = pd.to_datetime(hourly["time"])
    hourly["date"] = hourly["time"].dt.date
    avg = hourly.groupby("date", as_index=False).agg(
        humidity=("relative_humidity_2m", "mean"), soil=("soil_moisture_9_to_27cm", "mean"),
        temp=("temperature_2m", "mean"))
    daily = daily.merge(avg, on="date", how="left")
    rain = daily.set_index("date")["precipitation_sum"].fillna(0).astype(float).to_dict()
    if observed_rain is not None and not observed_rain.empty:
        rain.update(observed_rain.set_index("date")["rain"].astype(float).to_dict())
    future = daily[daily["date"] >= date.today()].head(7).copy()
    rows = []
    for _, r in future.iterrows():
        day = r["date"]
        rain7 = sum(rain.get(day - timedelta(days=i), 0) for i in range(1, 8))
        last_heavy = None
        for i in range(0, 41):
            if rain.get(day - timedelta(days=i), 0) >= 10:
                last_heavy = i
                break
        humidity = float(r["humidity"] or 60)
        soil = float(r["soil"] or 0.2)
        temp = float(r["temp"] or 16)
        wind = float(r["wind_speed_10m_max"] or 10)
        wait_score = 18 if last_heavy is not None and 5 <= last_heavy <= 12 else 8
        score = clamp(rain7 / 50 * 32, 0, 32) + wait_score + clamp((humidity - 45) / 40 * 18, 0, 18)
        score += clamp((soil - .10) / .28 * 16, 0, 16) + clamp(14 - abs(temp - 16) * 1.8, 0, 14)
        score -= clamp((wind - 12) * .8, 0, 10)
        rows.append({"Data": pd.Timestamp(day), "Indice": round(clamp(score)),
                     "Pioggia prevista (mm)": round(float(r["precipitation_sum"] or 0), 1),
                     "Pioggia 7 gg precedenti (mm)": round(rain7, 1),
                     "Giorni da pioggia >10 mm": last_heavy,
                     "T min (°C)": round(float(r["temperature_2m_min"]), 1),
                     "T max (°C)": round(float(r["temperature_2m_max"]), 1),
                     "Umidità aria (%)": round(humidity), "Umidità suolo": round(soil, 3),
                     "Vento max (km/h)": round(wind, 1)})
    return pd.DataFrame(rows)


st.title("🍄 Fungo Predictor")
st.caption("Seleziona un punto. La pioggia osservata ha priorità quando è disponibile una centralina ufficiale rappresentativa.")

with st.sidebar:
    st.header("Criteri centralina")
    reasonable_km = st.slider("Raggio preferenziale", 5, 40, 15, 5)
    max_km = st.slider("Raggio massimo di ricerca", 20, 100, 50, 5)
    max_alt_diff = st.slider("Differenza quota preferenziale", 100, 1000, 250, 50)
    st.caption("Una centralina è consigliata se rispetta sia il raggio preferenziale sia la differenza di quota.")
    if st.button("Cancella punto", use_container_width=True):
        for key in ["point", "source_choice", "station_id"]:
            st.session_state.pop(key, None)
        st.rerun()

center = [44.75, 9.10]
m = folium.Map(location=center, zoom_start=7, tiles="OpenStreetMap")
if "point" in st.session_state:
    p = st.session_state["point"]
    folium.Marker([p["lat"], p["lon"]], tooltip="Punto selezionato").add_to(m)
map_data = st_folium(m, height=480, use_container_width=True, returned_objects=["last_clicked"])
if map_data and map_data.get("last_clicked"):
    new_point = {"lat": round(map_data["last_clicked"]["lat"], 5), "lon": round(map_data["last_clicked"]["lng"], 5)}
    if st.session_state.get("point") != new_point:
        st.session_state["point"] = new_point
        st.session_state.pop("source_choice", None)
        st.session_state.pop("station_id", None)
        st.rerun()

if "point" not in st.session_state:
    st.info("Tocca la mappa nel punto del bosco da analizzare.")
    st.stop()

p = st.session_state["point"]
try:
    with st.spinner("Recupero coordinate, quota, meteo e centraline..."):
        locality, region, address = reverse_geocode(p["lat"], p["lon"])
        raw = open_meteo(p["lat"], p["lon"])
except requests.RequestException as exc:
    st.error(f"Errore nel recupero dei dati di base: {exc}")
    st.stop()

if region not in REGIONI:
    st.error(f"Il punto risulta in {region or 'una regione non identificata'}. Seleziona Piemonte, Liguria, Lombardia o Emilia-Romagna.")
    st.stop()

elevation = round(float(raw.get("elevation", 0)))
st.subheader(locality)
a, b, c, d = st.columns(4)
a.metric("Regione", region)
b.metric("Quota punto", f"{elevation} m")
c.metric("Latitudine", p["lat"])
d.metric("Longitudine", p["lon"])
st.caption(address)

candidates = pd.DataFrame()
connector_message = None
if region == "Lombardia":
    try:
        candidates = nearest_candidates(lombardia_stations(), p["lat"], p["lon"], elevation, max_km)
    except requests.RequestException as exc:
        connector_message = f"Servizio ARPA Lombardia momentaneamente non raggiungibile: {exc}"
else:
    connector_message = (f"Connettore automatico {region} non ancora disponibile in questa versione. "
                         "Non viene usata una stazione non verificata: è disponibile Open-Meteo sul punto.")

observed = None
selected_station = None
if not candidates.empty:
    options = {}
    for _, r in candidates.head(10).iterrows():
        label = f"{r['station']} | {r['distance_km']:.1f} km | quota {r['elevation']:.0f} m | Δ {r['elevation_diff_m']:+.0f} m"
        options[label] = r
    label = st.selectbox("Centraline pluviometriche ufficiali disponibili", list(options))
    selected_station = options[label]
    recommended = selected_station["distance_km"] <= reasonable_km and abs(selected_station["elevation_diff_m"]) <= max_alt_diff
    if recommended:
        st.success("Centralina entro i criteri preferenziali. Il dato reale è selezionato automaticamente, ma puoi usare Open-Meteo.")
        default_index = 0
    else:
        st.warning("La centralina è fuori dai criteri preferenziali. Scegli consapevolmente quale fonte utilizzare.")
        default_index = 1
    source = st.radio("Fonte per la pioggia storica", ["Centralina reale", "Open-Meteo sul punto"], index=default_index, horizontal=True)
    if source == "Centralina reale":
        try:
            start = date.today() - timedelta(days=40)
            end = date.today() - timedelta(days=1)
            observed = lombardia_rain(selected_station["sensor_id"], start, end)
            completeness = len(observed) / 40 * 100
            if observed.empty:
                st.warning("La centralina non ha restituito dati validi nel periodo. Passaggio automatico a Open-Meteo.")
                observed = None
            else:
                st.info(f"Fonte: {selected_station['network']} | {selected_station['station']} | "
                        f"distanza {selected_station['distance_km']:.1f} km | quota {selected_station['elevation']:.0f} m | "
                        f"differenza quota {selected_station['elevation_diff_m']:+.0f} m | completezza indicativa {completeness:.0f}%")
        except requests.RequestException as exc:
            st.warning(f"Dati della centralina non disponibili: {exc}. Uso Open-Meteo.")
            observed = None
else:
    if connector_message:
        st.warning(connector_message)
    else:
        st.warning(f"Nessuna centralina pluviometrica trovata entro {max_km} km. Uso Open-Meteo sul punto.")

forecast = build_forecast(raw, observed)
st.subheader("Indice per i prossimi 7 giorni")
fig = px.line(forecast, x="Data", y="Indice", markers=True, range_y=[0, 100])
fig.update_traces(line_color="#16825d", line_width=4, marker_size=10, fill="tozeroy", fillcolor="rgba(22,130,93,.16)")
fig.update_layout(xaxis_title=None, yaxis_title="Indice (%)", hovermode="x unified")
st.plotly_chart(fig, use_container_width=True)

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

st.divider()
st.caption("La pioggia osservata viene usata solo quando il connettore ufficiale restituisce dati validi. Le previsioni future, l'umidità del terreno e il fallback storico provengono da Open-Meteo. L'indice è sperimentale e non garantisce la presenza di funghi.")
