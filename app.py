import math
from datetime import date, datetime

import folium
import pandas as pd
import plotly.express as px
import requests
import streamlit as st
from streamlit_folium import st_folium

st.set_page_config(page_title="Previsione funghi", page_icon="🍄", layout="wide")

REGIONI_AMMESSE = {"Piemonte", "Liguria", "Lombardia", "Emilia-Romagna", "Emilia Romagna"}
CENTRO_MAPPA = [44.75, 9.10]


def clamp(x, minimo=0, massimo=100):
    return max(minimo, min(massimo, x))


def fase_lunare(data):
    riferimento = date(2000, 1, 6)
    eta = ((data - riferimento).days % 29.53058867)
    if eta < 1.85 or eta >= 27.68:
        return "Luna nuova"
    if eta < 7.38:
        return "Crescente"
    if eta < 9.23:
        return "Primo quarto"
    if eta < 14.77:
        return "Gibbosa crescente"
    if eta < 16.61:
        return "Luna piena"
    if eta < 22.15:
        return "Gibbosa calante"
    if eta < 24.00:
        return "Ultimo quarto"
    return "Calante"


@st.cache_data(ttl=3600, show_spinner=False)
def reverse_geocode(lat, lon):
    url = "https://nominatim.openstreetmap.org/reverse"
    params = {"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 10, "addressdetails": 1}
    headers = {"User-Agent": "previsione-funghi-streamlit/1.0"}
    response = requests.get(url, params=params, headers=headers, timeout=15)
    response.raise_for_status()
    data = response.json()
    address = data.get("address", {})
    regione = address.get("state", "")
    localita = (
        address.get("village") or address.get("town") or address.get("city")
        or address.get("municipality") or address.get("county") or "Punto selezionato"
    )
    return localita, regione, data.get("display_name", localita)


@st.cache_data(ttl=1800, show_spinner=False)
def scarica_meteo(lat, lon):
    url = "https://api.open-meteo.com/v1/forecast"
    hourly = [
        "temperature_2m", "relative_humidity_2m", "precipitation",
        "soil_moisture_9_to_27cm", "wind_speed_10m"
    ]
    daily = [
        "temperature_2m_max", "temperature_2m_min", "precipitation_sum",
        "precipitation_probability_max", "wind_speed_10m_max"
    ]
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join(hourly),
        "daily": ",".join(daily),
        "past_days": 30,
        "forecast_days": 7,
        "timezone": "Europe/Rome",
    }
    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def prepara_dati(raw, tipo_bosco, esposizione):
    h = pd.DataFrame(raw["hourly"])
    h["time"] = pd.to_datetime(h["time"])
    h["data"] = h["time"].dt.date

    d = pd.DataFrame(raw["daily"])
    d["time"] = pd.to_datetime(d["time"])
    d["data"] = d["time"].dt.date

    oggi = date.today()
    storico = d[d["data"] < oggi].copy()
    futuro = d[d["data"] >= oggi].head(7).copy()

    medie = h.groupby("data", as_index=False).agg(
        umidita_aria=("relative_humidity_2m", "mean"),
        umidita_suolo=("soil_moisture_9_to_27cm", "mean"),
        temp_media=("temperature_2m", "mean"),
    )
    futuro = futuro.merge(medie, on="data", how="left")

    piogge_storiche = storico.set_index("data")["precipitation_sum"].to_dict()
    tutte_piogge = d.set_index("data")["precipitation_sum"].to_dict()

    bonus_bosco = {"Faggio": 6, "Castagno": 5, "Quercia": 3, "Conifere": 2, "Misto": 5}.get(tipo_bosco, 0)
    bonus_esposizione = {"Nord": 4, "Nord-est": 3, "Est": 2, "Ovest": 0, "Sud": -5, "Non nota": 0}.get(esposizione, 0)

    risultati = []
    for _, r in futuro.iterrows():
        giorno = r["data"]
        giorni_precedenti = [giorno - pd.Timedelta(days=i) for i in range(1, 8)]
        pioggia7 = sum(float(tutte_piogge.get(g.date() if hasattr(g, "date") else g, 0) or 0) for g in giorni_precedenti)

        ultima_data = None
        for i in range(0, 31):
            gd = giorno - pd.Timedelta(days=i)
            gd = gd.date() if hasattr(gd, "date") else gd
            if float(tutte_piogge.get(gd, 0) or 0) >= 10:
                ultima_data = gd
                break
        giorni_ultima = (giorno - ultima_data).days if ultima_data else 99

        temp = float(r.get("temp_media", 18) or 18)
        rh = float(r.get("umidita_aria", 60) or 60)
        soil = float(r.get("umidita_suolo", 0.20) or 0.20)
        vento = float(r.get("wind_speed_10m_max", 10) or 10)

        score_pioggia = clamp(pioggia7 / 50 * 32, 0, 32)
        score_attesa = 18 if 5 <= giorni_ultima <= 12 else (10 if 3 <= giorni_ultima <= 16 else 3)
        score_umidita = clamp((rh - 45) / 40 * 16, 0, 16)
        score_suolo = clamp((soil - 0.10) / 0.28 * 14, 0, 14)
        score_temp = clamp(14 - abs(temp - 16) * 1.8, 0, 14)
        penalita_vento = clamp((vento - 12) * 0.8, 0, 10)
        indice = round(clamp(score_pioggia + score_attesa + score_umidita + score_suolo + score_temp + bonus_bosco + bonus_esposizione - penalita_vento))

        risultati.append({
            "Data": pd.Timestamp(giorno),
            "Indice": indice,
            "Pioggia prevista (mm)": round(float(r["precipitation_sum"] or 0), 1),
            "Prob. pioggia (%)": int(r["precipitation_probability_max"] or 0),
            "Pioggia 7 gg precedenti (mm)": round(pioggia7, 1),
            "Giorni da pioggia >10 mm": None if giorni_ultima == 99 else giorni_ultima,
            "T min (°C)": round(float(r["temperature_2m_min"]), 1),
            "T max (°C)": round(float(r["temperature_2m_max"]), 1),
            "Umidità aria (%)": round(rh),
            "Umidità suolo (m³/m³)": round(soil, 3),
            "Vento max (km/h)": round(vento, 1),
            "Luna": fase_lunare(giorno),
        })

    return pd.DataFrame(risultati), storico.tail(30)


def colore_indice(v):
    if v >= 70:
        return "🟢 Alta"
    if v >= 45:
        return "🟡 Media"
    return "🔴 Bassa"


st.title("🍄 Previsione micologica")
st.caption("Seleziona un punto sulla mappa. L'app calcola un indice sperimentale per i successivi 7 giorni.")

with st.sidebar:
    st.header("Parametri del bosco")
    tipo_bosco = st.selectbox("Bosco prevalente", ["Faggio", "Castagno", "Quercia", "Conifere", "Misto", "Non noto"])
    esposizione = st.selectbox("Esposizione", ["Non nota", "Nord", "Nord-est", "Est", "Ovest", "Sud"])
    st.info("La copertura forestale e l'esposizione sono per ora inserite manualmente. Il punto geografico, la quota e il meteo sono automatici.")
    if st.button("Cancella punto selezionato", use_container_width=True):
        st.session_state.pop("punto", None)
        st.rerun()

m = folium.Map(location=CENTRO_MAPPA, zoom_start=7, tiles="OpenStreetMap")
folium.TileLayer("CartoDB positron", name="Mappa chiara").add_to(m)
if "punto" in st.session_state:
    p = st.session_state["punto"]
    folium.Marker([p["lat"], p["lon"]], tooltip="Punto selezionato", icon=folium.Icon(color="green", icon="tree", prefix="fa")).add_to(m)
folium.LayerControl().add_to(m)

mappa = st_folium(m, height=500, use_container_width=True, returned_objects=["last_clicked"])
if mappa and mappa.get("last_clicked"):
    nuovo = {"lat": round(mappa["last_clicked"]["lat"], 5), "lon": round(mappa["last_clicked"]["lng"], 5)}
    if st.session_state.get("punto") != nuovo:
        st.session_state["punto"] = nuovo
        st.rerun()

if "punto" not in st.session_state:
    st.warning("Tocca la mappa nel punto del bosco che vuoi analizzare.")
    st.stop()

punto = st.session_state["punto"]
try:
    with st.spinner("Recupero località, quota e dati meteorologici..."):
        localita, regione, indirizzo = reverse_geocode(punto["lat"], punto["lon"])
        raw = scarica_meteo(punto["lat"], punto["lon"])
        previsione, storico = prepara_dati(raw, tipo_bosco, esposizione)
except requests.RequestException as exc:
    st.error(f"Impossibile recuperare i dati online: {exc}")
    st.stop()

regione_ok = regione in REGIONI_AMMESSE
if not regione_ok:
    st.error(f"Il punto risulta in '{regione or 'regione non identificata'}'. Seleziona un punto in Piemonte, Liguria, Lombardia o Emilia-Romagna.")
    st.stop()

quota = round(float(raw.get("elevation", 0)))
c1, c2, c3, c4 = st.columns(4)
c1.metric("Località", localita)
c2.metric("Regione", regione)
c3.metric("Quota modello", f"{quota} m")
c4.metric("Coordinate", f"{punto['lat']}, {punto['lon']}")
st.caption(indirizzo)

st.subheader("Probabilità nei prossimi 7 giorni")
fig = px.line(previsione, x="Data", y="Indice", markers=True, range_y=[0, 100])
fig.update_traces(line_color="#16825d", line_width=4, marker_size=10, fill="tozeroy", fillcolor="rgba(22,130,93,0.16)")
fig.update_layout(xaxis_title=None, yaxis_title="Indice (%)", hovermode="x unified", margin=dict(l=10, r=10, t=10, b=10))
st.plotly_chart(fig, use_container_width=True)

migliore = previsione.loc[previsione["Indice"].idxmax()]
a, b, c = st.columns(3)
a.metric("Giorno migliore", migliore["Data"].strftime("%d/%m/%Y"))
b.metric("Indice massimo", f"{int(migliore['Indice'])}%")
c.metric("Valutazione", colore_indice(migliore["Indice"]))

st.subheader("Dettaglio dei 7 giorni")
for _, r in previsione.iterrows():
    etichetta = f"{r['Data'].strftime('%A %d/%m')} | {int(r['Indice'])}% | {colore_indice(r['Indice'])}"
    with st.expander(etichetta, expanded=r["Data"] == migliore["Data"]):
        x1, x2, x3, x4 = st.columns(4)
        x1.metric("Pioggia prevista", f"{r['Pioggia prevista (mm)']} mm")
        x2.metric("Pioggia 7 gg precedenti", f"{r['Pioggia 7 gg precedenti (mm)']} mm")
        ultima = r["Giorni da pioggia >10 mm"]
        x3.metric("Ultima pioggia >10 mm", "Non trovata" if pd.isna(ultima) else f"{int(ultima)} giorni fa")
        x4.metric("Probabilità pioggia", f"{r['Prob. pioggia (%)']}%")
        y1, y2, y3, y4 = st.columns(4)
        y1.metric("Temperatura", f"{r['T min (°C)']} / {r['T max (°C)']} °C")
        y2.metric("Umidità aria", f"{int(r['Umidità aria (%)'])}%")
        y3.metric("Umidità suolo", f"{r['Umidità suolo (m³/m³)']} m³/m³")
        y4.metric("Vento massimo", f"{r['Vento max (km/h)']} km/h")
        st.write(f"**Fase lunare:** {r['Luna']}  |  **Bosco:** {tipo_bosco}  |  **Esposizione:** {esposizione}")

with st.expander("Storico meteorologico degli ultimi 30 giorni"):
    storico_plot = storico[["time", "precipitation_sum", "temperature_2m_max", "temperature_2m_min"]].copy()
    storico_plot = storico_plot.rename(columns={"time": "Data", "precipitation_sum": "Precipitazione (mm)", "temperature_2m_max": "T max (°C)", "temperature_2m_min": "T min (°C)"})
    fig2 = px.bar(storico_plot, x="Data", y="Precipitazione (mm)")
    fig2.update_traces(marker_color="#3b82f6")
    fig2.update_layout(margin=dict(l=10, r=10, t=10, b=10))
    st.plotly_chart(fig2, use_container_width=True)
    st.dataframe(storico_plot.sort_values("Data", ascending=False), use_container_width=True, hide_index=True)

st.divider()
st.caption("Indice sperimentale e non garanzia di presenza di funghi. Verifica sempre permessi, limiti di raccolta, accessibilità e regole locali. Dati meteo ed elevazione: Open-Meteo. Cartografia: OpenStreetMap.")
