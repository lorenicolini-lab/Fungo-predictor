# funghi_ios_v2.py
# Indicatore probabilita raccolta funghi
# Versione iOS per Pyto / Pythonista
# Usa solo librerie standard Python

import urllib.request
import urllib.parse
import json
import math
from datetime import datetime, date, timedelta
import unicodedata


# ============================================================
# CONFIGURAZIONE
# ============================================================

USE_AVAILABLE_METEO_LOCATION = True
COUNTRY_CODE = "IT"

# Database iniziale localita meteo disponibili.
# Puoi aggiungere qui localita disponibili da ilmeteo.it o altre fonti.
AVAILABLE_LOCATIONS = [
    {
        "name": "Brallo di Pregola",
        "province": "PV",
        "region": "Lombardia",
        "lat": 44.7376,
        "lon": 9.2802,
        "elevation": 950,
        "source": "database_locale"
    },
    {
        "name": "Varzi",
        "province": "PV",
        "region": "Lombardia",
        "lat": 44.8226,
        "lon": 9.1973,
        "elevation": 416,
        "source": "database_locale"
    },
    {
        "name": "Bobbio",
        "province": "PC",
        "region": "Emilia-Romagna",
        "lat": 44.7693,
        "lon": 9.3861,
        "elevation": 272,
        "source": "database_locale"
    },
    {
        "name": "Cabella Ligure",
        "province": "AL",
        "region": "Piemonte",
        "lat": 44.6755,
        "lon": 9.0967,
        "elevation": 510,
        "source": "database_locale"
    },
    {
        "name": "Ottone",
        "province": "PC",
        "region": "Emilia-Romagna",
        "lat": 44.6231,
        "lon": 9.3327,
        "elevation": 510,
        "source": "database_locale"
    },
    {
        "name": "Zavattarello",
        "province": "PV",
        "region": "Lombardia",
        "lat": 44.8673,
        "lon": 9.2656,
        "elevation": 550,
        "source": "database_locale"
    },
    {
        "name": "Romagnese",
        "province": "PV",
        "region": "Lombardia",
        "lat": 44.8402,
        "lon": 9.3293,
        "elevation": 630,
        "source": "database_locale"
    },
    {
        "name": "Santa Margherita di Staffora",
        "province": "PV",
        "region": "Lombardia",
        "lat": 44.7710,
        "lon": 9.2400,
        "elevation": 550,
        "source": "database_locale"
    },
    {
        "name": "Passo Penice",
        "province": "PV",
        "region": "Lombardia",
        "lat": 44.7860,
        "lon": 9.3160,
        "elevation": 1149,
        "source": "database_locale"
    }
]


# ============================================================
# UTILITY
# ============================================================

def normalize_text(s):
    s = str(s).strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.replace("'", " ")
    s = " ".join(s.split())
    return s


def fetch_json(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "fungo-indicator-ios/2.0"}
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw)


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lam = math.radians(lon2 - lon1)

    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2) ** 2
    )

    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def clamp(x, lo=0, hi=100):
    return max(lo, min(hi, x))


def score_range(value, ideal_min, ideal_max, hard_min, hard_max):
    if value is None:
        return 50

    if ideal_min <= value <= ideal_max:
        return 100

    if value < hard_min or value > hard_max:
        return 0

    if value < ideal_min:
        return 100 * (value - hard_min) / (ideal_min - hard_min)

    return 100 * (hard_max - value) / (hard_max - ideal_max)


def safe_round(value, digits=1):
    if value is None:
        return None
    return round(value, digits)


# ============================================================
# GEOLOCALIZZAZIONE E MATCH LOCALITA
# ============================================================

def geocode_location(query):
    encoded = urllib.parse.quote(query)
    url = (
        "https://geocoding-api.open-meteo.com/v1/search"
        f"?name={encoded}"
        f"&count=5"
        f"&language=it"
        f"&format=json"
        f"&countryCode={COUNTRY_CODE}"
    )

    data = fetch_json(url)
    results = data.get("results", [])

    if not results:
        return None

    r = results[0]

    return {
        "name": r.get("name", query),
        "lat": r.get("latitude"),
        "lon": r.get("longitude"),
        "elevation": r.get("elevation"),
        "admin1": r.get("admin1"),
        "admin2": r.get("admin2"),
        "country": r.get("country"),
        "timezone": r.get("timezone", "Europe/Rome")
    }


def find_exact_available_location(query):
    q = normalize_text(query)

    for loc in AVAILABLE_LOCATIONS:
        if normalize_text(loc["name"]) == q:
            return loc

    return None


def find_nearest_available_location(lat, lon, elevation=None):
    best = None

    for loc in AVAILABLE_LOCATIONS:
        dist = haversine_km(lat, lon, loc["lat"], loc["lon"])

        if elevation is not None and loc.get("elevation") is not None:
            elevation_delta = abs(elevation - loc["elevation"])
        else:
            elevation_delta = 0

        # Peso quota: 100 m di quota valgono circa 1 km virtuale.
        combined = dist + (elevation_delta / 100.0)

        candidate = {
            "location": loc,
            "distance_km": dist,
            "elevation_delta_m": elevation_delta,
            "combined_score": combined
        }

        if best is None or candidate["combined_score"] < best["combined_score"]:
            best = candidate

    return best


def resolve_weather_location(query):
    exact = find_exact_available_location(query)

    if exact:
        return {
            "searched": query,
            "match_type": "esatto_database_locale",
            "searched_geo": exact,
            "weather_location": exact,
            "distance_km": 0.0,
            "elevation_delta_m": 0,
            "confidence_location": 100
        }

    searched_geo = geocode_location(query)

    if searched_geo is None:
        return None

    if USE_AVAILABLE_METEO_LOCATION and AVAILABLE_LOCATIONS:
        nearest = find_nearest_available_location(
            searched_geo["lat"],
            searched_geo["lon"],
            searched_geo.get("elevation")
        )

        weather_loc = nearest["location"]
        dist = nearest["distance_km"]
        elev_delta = nearest["elevation_delta_m"]

        confidence = 100
        confidence -= min(45, dist * 3)
        confidence -= min(35, elev_delta / 20)
        confidence = int(clamp(confidence, 15, 95))

        return {
            "searched": query,
            "match_type": "nearest_available",
            "searched_geo": searched_geo,
            "weather_location": weather_loc,
            "distance_km": dist,
            "elevation_delta_m": elev_delta,
            "confidence_location": confidence
        }

    return {
        "searched": query,
        "match_type": "coordinate_esatte_open_meteo",
        "searched_geo": searched_geo,
        "weather_location": {
            "name": searched_geo["name"],
            "lat": searched_geo["lat"],
            "lon": searched_geo["lon"],
            "elevation": searched_geo.get("elevation"),
            "source": "open_meteo_geocoding"
        },
        "distance_km": 0.0,
        "elevation_delta_m": 0,
        "confidence_location": 85
    }


# ============================================================
# METEO OPEN-METEO
# ============================================================

def get_weather(lat, lon):
    hourly_vars = ",".join([
        "temperature_2m",
        "relative_humidity_2m",
        "precipitation",
        "precipitation_probability",
        "wind_speed_10m",
        "wind_gusts_10m",
        "soil_moisture_1_to_3cm",
        "soil_moisture_3_to_9cm",
        "soil_temperature_6cm",
        "vapour_pressure_deficit",
        "et0_fao_evapotranspiration"
    ])

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}"
        f"&longitude={lon}"
        f"&hourly={hourly_vars}"
        f"&forecast_days=4"
        f"&past_days=30"
        f"&timezone=auto"
    )

    return fetch_json(url)


def group_hourly_by_day(weather):
    hourly = weather.get("hourly", {})
    times = hourly.get("time", [])

    days = {}

    variables = [
        "temperature_2m",
        "relative_humidity_2m",
        "precipitation",
        "precipitation_probability",
        "wind_speed_10m",
        "wind_gusts_10m",
        "soil_moisture_1_to_3cm",
        "soil_moisture_3_to_9cm",
        "soil_temperature_6cm",
        "vapour_pressure_deficit",
        "et0_fao_evapotranspiration"
    ]

    for i, t in enumerate(times):
        d = t[:10]

        if d not in days:
            days[d] = {var: [] for var in variables}

        for var in variables:
            values = hourly.get(var)

            if values is not None and i < len(values):
                v = values[i]

                if v is not None:
                    days[d][var].append(v)

    return days


def avg(values):
    if not values:
        return None
    return sum(values) / len(values)


def total(values):
    if not values:
        return 0
    return sum(values)


def max_or_none(values):
    if not values:
        return None
    return max(values)


def min_or_none(values):
    if not values:
        return None
    return min(values)


def daily_summary(day_values):
    temp_list = day_values["temperature_2m"]

    sm1 = avg(day_values["soil_moisture_1_to_3cm"])
    sm2 = avg(day_values["soil_moisture_3_to_9cm"])

    if sm1 is not None and sm2 is not None:
        soil_moisture = (sm1 + sm2) / 2
    elif sm1 is not None:
        soil_moisture = sm1
    elif sm2 is not None:
        soil_moisture = sm2
    else:
        soil_moisture = None

    return {
        "temp_avg": avg(temp_list),
        "temp_min": min_or_none(temp_list),
        "temp_max": max_or_none(temp_list),
        "rh_avg": avg(day_values["relative_humidity_2m"]),
        "rain_mm": total(day_values["precipitation"]),
        "rain_prob_max": max_or_none(day_values["precipitation_probability"]),
        "wind_avg": avg(day_values["wind_speed_10m"]),
        "gust_max": max_or_none(day_values["wind_gusts_10m"]),
        "soil_moisture": soil_moisture,
        "soil_temp": avg(day_values["soil_temperature_6cm"]),
        "vpd": avg(day_values["vapour_pressure_deficit"]),
        "et0": total(day_values["et0_fao_evapotranspiration"])
    }


# ============================================================
# LUNA
# ============================================================

def moon_phase_info(d):
    known_new_moon = date(2000, 1, 6)
    synodic_month = 29.53058867

    days = (d - known_new_moon).days
    age = days % synodic_month

    if age < 1.8:
        label = "luna nuova"
    elif age < 7.4:
        label = "crescente"
    elif age < 9.2:
        label = "primo quarto"
    elif age < 14.8:
        label = "gibbosa crescente"
    elif age < 16.6:
        label = "luna piena"
    elif age < 22.1:
        label = "gibbosa calante"
    elif age < 23.9:
        label = "ultimo quarto"
    else:
        label = "calante"

    # Peso basso nel modello.
    if label in ["crescente", "gibbosa crescente", "luna piena"]:
        score = 65
    else:
        score = 50

    return age, label, score


# ============================================================
# INDICE FUNGHI
# ============================================================

def rainfall_score(rain_7d):
    if rain_7d < 3:
        return 5
    if rain_7d < 8:
        return 25
    if rain_7d < 15:
        return 50
    if rain_7d < 35:
        return 85
    if rain_7d < 70:
        return 95
    if rain_7d < 110:
        return 70
    return 45


def soil_moisture_score(soil_moisture, rain_7d, rh_avg):
    if soil_moisture is not None:
        if soil_moisture < 0.12:
            return 15
        if soil_moisture < 0.18:
            return 45
        if soil_moisture < 0.28:
            return 80
        if soil_moisture < 0.40:
            return 95
        return 70

    proxy = (
        0.7 * rainfall_score(rain_7d)
        + 0.3 * score_range(rh_avg, 75, 95, 40, 100)
    )

    return proxy


def season_score(d):
    m = d.month

    if m in [9, 10]:
        return 100
    if m in [8, 11]:
        return 75
    if m in [6, 7]:
        return 45
    if m in [4, 5]:
        return 35

    return 15


def altitude_score(elevation):
    if elevation is None:
        return 65

    if 700 <= elevation <= 1500:
        return 100
    if 400 <= elevation < 700:
        return 75
    if 1500 < elevation <= 1800:
        return 70
    if 200 <= elevation < 400:
        return 50

    return 35


def wind_penalty(wind_avg, gust_max):
    penalty = 0

    if wind_avg is not None and wind_avg > 15:
        penalty += min(15, (wind_avg - 15) * 0.8)

    if gust_max is not None and gust_max > 35:
        penalty += min(15, (gust_max - 35) * 0.6)

    return penalty


def heat_dry_penalty(temp_max, rh_avg, vpd):
    penalty = 0

    if temp_max is not None and temp_max > 27:
        penalty += min(20, (temp_max - 27) * 2)

    if rh_avg is not None and rh_avg < 55:
        penalty += min(15, (55 - rh_avg) * 0.5)

    if vpd is not None and vpd > 1.2:
        penalty += min(15, (vpd - 1.2) * 12)

    return penalty


def frost_penalty(temp_min):
    if temp_min is None:
        return 0

    if temp_min < -1:
        return 25

    if temp_min < 2:
        return 12

    return 0


def last_rain_over_threshold(
    summaries,
    target_date,
    threshold_mm=10,
    lookback_days=30
):
    """
    Cerca l'ultima giornata con pioggia cumulata giornaliera superiore alla soglia.
    Include il giorno target, utile in caso di previsione.
    """

    for k in range(0, lookback_days + 1):
        check_date = target_date - timedelta(days=k)
        check_str = check_date.isoformat()

        if check_str in summaries:
            rain = summaries[check_str]["rain_mm"]

            if rain is not None and rain > threshold_mm:
                return {
                    "date": check_str,
                    "days_ago": k,
                    "rain_mm": round(rain, 1)
                }

    return None


def rain_timing_score(last_rain_10):
    """
    Converte la distanza dall'ultima piovuta > 10 mm in bonus/penalita.
    Valori indicativi:
    0-1 giorni: presto, lieve penalita
    2-7 giorni: finestra interessante
    8-12 giorni: ancora valida
    13-18 giorni: neutra
    >18 giorni: in calo
    nessuna pioggia: penalita
    """

    if last_rain_10 is None:
        return -8

    days_ago = last_rain_10["days_ago"]

    if days_ago <= 1:
        return -2
    if 2 <= days_ago <= 7:
        return 6
    if 8 <= days_ago <= 12:
        return 3
    if 13 <= days_ago <= 18:
        return 0

    return -6


def compute_index(target_date, summaries, elevation):
    d_str = target_date.isoformat()

    if d_str not in summaries:
        return None

    current = summaries[d_str]

    # Pioggia cumulata dei 7 giorni precedenti.
    # Dato mantenuto separato, come richiesto.
    rain_7d = 0

    for k in range(1, 8):
        check_date = (target_date - timedelta(days=k)).isoformat()

        if check_date in summaries:
            rain_7d += summaries[check_date]["rain_mm"]

    # Pioggia utile per il modello.
    # Mantiene il dato 7 giorni puro e aggiunge una piccola quota della pioggia del giorno target.
    rain_useful = rain_7d + 0.35 * current["rain_mm"]

    # Ultima giornata con pioggia > 10 mm negli ultimi 30 giorni.
    last_rain_10 = last_rain_over_threshold(
        summaries,
        target_date,
        threshold_mm=10,
        lookback_days=30
    )

    rain_s = rainfall_score(rain_useful)

    soil_s = soil_moisture_score(
        current["soil_moisture"],
        rain_useful,
        current["rh_avg"]
    )

    temp_s = score_range(current["temp_avg"], 11, 20, 3, 28)
    rh_s = score_range(current["rh_avg"], 75, 95, 45, 100)
    seas_s = season_score(target_date)
    alt_s = altitude_score(elevation)
    moon_age, moon_label, moon_s = moon_phase_info(target_date)

    raw = (
        0.30 * rain_s
        + 0.24 * soil_s
        + 0.18 * temp_s
        + 0.10 * rh_s
        + 0.10 * seas_s
        + 0.05 * alt_s
        + 0.03 * moon_s
    )

    penalties = (
        wind_penalty(current["wind_avg"], current["gust_max"])
        + heat_dry_penalty(current["temp_max"], current["rh_avg"], current["vpd"])
        + frost_penalty(current["temp_min"])
    )

    timing_bonus = rain_timing_score(last_rain_10)

    index = int(clamp(raw - penalties + timing_bonus))

    return {
        "date": d_str,
        "index": index,

        "rain_7d_mm": round(rain_7d, 1),
        "rain_useful_mm": round(rain_useful, 1),
        "rain_day_mm": round(current["rain_mm"], 1),

        "last_rain_10": last_rain_10,
        "rain_timing_bonus": timing_bonus,

        "temp_avg": safe_round(current["temp_avg"], 1),
        "temp_min": safe_round(current["temp_min"], 1),
        "temp_max": safe_round(current["temp_max"], 1),
        "rh_avg": safe_round(current["rh_avg"], 0),
        "soil_moisture": safe_round(current["soil_moisture"], 3),
        "soil_temp": safe_round(current["soil_temp"], 1),
        "wind_avg": safe_round(current["wind_avg"], 1),
        "gust_max": safe_round(current["gust_max"], 1),
        "vpd": safe_round(current["vpd"], 2),
        "et0": safe_round(current["et0"], 1),

        "moon": moon_label,
        "moon_age": safe_round(moon_age, 1),
        "penalties": safe_round(penalties, 1)
    }


def qualitative_label(index):
    if index >= 81:
        return "molto buona"
    if index >= 61:
        return "buona"
    if index >= 41:
        return "discreta"
    if index >= 21:
        return "bassa/moderata"

    return "bassa"


def timing_label(last_rain_10):
    if last_rain_10 is None:
        return "nessuna piovuta efficace recente"

    days_ago = last_rain_10["days_ago"]

    if days_ago <= 1:
        return "forse ancora presto"
    if 2 <= days_ago <= 4:
        return "finestra molto interessante"
    if 5 <= days_ago <= 7:
        return "finestra buona"
    if 8 <= days_ago <= 12:
        return "ancora possibile"
    if 13 <= days_ago <= 18:
        return "in graduale calo"

    return "probabilita in calo salvo boschi molto umidi"


def species_probability(result, elevation, habitat):
    idx = result["index"]
    rain = result["rain_7d_mm"]
    temp = result["temp_avg"]
    rh = result["rh_avg"]

    habitat_n = normalize_text(habitat)

    species = []

    def add(name, level, note):
        species.append({
            "name": name,
            "level": level,
            "note": note
        })

    # Porcini
    porcini_score = idx

    if elevation is not None and 700 <= elevation <= 1500:
        porcini_score += 10

    if temp is not None and 10 <= temp <= 20:
        porcini_score += 8

    if rain < 10:
        porcini_score -= 20

    if any(x in habitat_n for x in ["faggio", "castagno", "abete", "misto", "quercia"]):
        porcini_score += 8

    if porcini_score >= 75:
        add(
            "Porcini / Boletus gruppo edulis",
            "medio-alta",
            "quota, pioggia e temperatura favorevoli"
        )
    elif porcini_score >= 55:
        add(
            "Porcini / Boletus gruppo edulis",
            "media",
            "condizioni possibili, da verificare nel bosco"
        )
    elif porcini_score >= 40:
        add(
            "Porcini / Boletus gruppo edulis",
            "bassa/media",
            "possibili solo in zone fresche e umide"
        )

    # Finferli
    finferli_score = idx

    if rain >= 20:
        finferli_score += 8

    if rh is not None and rh >= 75:
        finferli_score += 8

    if any(x in habitat_n for x in ["faggio", "abete", "misto", "castagno"]):
        finferli_score += 5

    if finferli_score >= 75:
        add(
            "Finferli / Cantharellus",
            "media/alta",
            "buona umidita persistente"
        )
    elif finferli_score >= 55:
        add(
            "Finferli / Cantharellus",
            "media",
            "possibili dove il suolo resta fresco"
        )

    # Russule
    russula_score = idx

    if any(x in habitat_n for x in ["faggio", "castagno", "quercia", "misto"]):
        russula_score += 8

    if russula_score >= 55:
        add(
            "Russule",
            "media",
            "specie frequenti in boschi misti e latifoglie"
        )

    # Mazze di tamburo
    mazza_score = idx

    if temp is not None and 14 <= temp <= 24:
        mazza_score += 6

    if any(x in habitat_n for x in ["prato", "pascolo", "radura", "misto"]):
        mazza_score += 10

    if mazza_score >= 65:
        add(
            "Mazze di tamburo / Macrolepiota",
            "media",
            "piu probabili in radure, margini e prati"
        )

    # Chiodini
    month = datetime.strptime(result["date"], "%Y-%m-%d").month
    chiodini_score = idx

    if month in [10, 11]:
        chiodini_score += 20
    elif month == 9:
        chiodini_score += 5
    else:
        chiodini_score -= 20

    if chiodini_score >= 65:
        add(
            "Chiodini / Armillaria",
            "media",
            "piu probabili in stagione avanzata, su ceppaie o legno"
        )

    if not species:
        add(
            "Specie generiche di bosco",
            "bassa",
            "condizioni non abbastanza selettive per una specie principale"
        )

    return species


# ============================================================
# REPORT
# ============================================================

def print_report(resolution, results, habitat):
    weather_loc = resolution["weather_location"]
    searched_geo = resolution["searched_geo"]

    print("\n" + "=" * 60)
    print("INDICATORE FUNGHI - PROSSIMI 3 GIORNI")
    print("=" * 60)

    print(f"\nLocalita cercata: {resolution['searched']}")

    if searched_geo:
        print(
            "Coordinate localita cercata: "
            f"{searched_geo.get('lat')}, {searched_geo.get('lon')}"
        )

    print(f"\nDati meteo usati: {weather_loc['name']}")
    print(f"Tipo match: {resolution['match_type']}")

    if resolution["match_type"] == "nearest_available":
        print(f"Distanza dalla localita cercata: {resolution['distance_km']:.1f} km")
        print(f"Differenza quota stimata: {resolution['elevation_delta_m']:.0f} m")

    print(f"Quota dati meteo: {weather_loc.get('elevation')} m")
    print(f"Confidenza localita: {resolution['confidence_location']}/100")
    print(f"Habitat dichiarato: {habitat}")

    print("\n" + "-" * 60)

    for r in results:
        if r is None:
            continue

        label = qualitative_label(r["index"])
        last_rain = r.get("last_rain_10")

        print(f"\nData: {r['date']}")
        print(f"Indice funghi: {r['index']}/100 - {label}")

        print(f"Pioggia cumulata ultimi 7 giorni: {r['rain_7d_mm']} mm")
        print(f"Pioggia utile modello: {r['rain_useful_mm']} mm")
        print(f"Pioggia prevista/nel giorno: {r['rain_day_mm']} mm")

        if last_rain is not None:
            print(
                "Ultima piovuta > 10 mm: "
                f"{last_rain['date']} "
                f"({last_rain['days_ago']} giorni prima, "
                f"{last_rain['rain_mm']} mm)"
            )
            print(f"Valutazione timing pioggia: {timing_label(last_rain)}")
        else:
            print("Ultima piovuta > 10 mm: non rilevata negli ultimi 30 giorni")
            print("Valutazione timing pioggia: nessuna piovuta efficace recente")

        print(f"Bonus timing pioggia: {r['rain_timing_bonus']} punti")

        print(
            "Temperatura media/min/max: "
            f"{r['temp_avg']} / {r['temp_min']} / {r['temp_max']} °C"
        )

        print(f"Umidita aria media: {r['rh_avg']} %")
        print(f"Umidita terreno modellata: {r['soil_moisture']}")
        print(f"Temperatura terreno 6 cm: {r['soil_temp']} °C")
        print(f"Evapotraspirazione giorno: {r['et0']} mm")
        print(f"VPD medio: {r['vpd']} kPa")
        print(f"Vento medio / raffica max: {r['wind_avg']} / {r['gust_max']} km/h")
        print(f"Luna: {r['moon']}")
        print(f"Penalita meteo: {r['penalties']} punti")

        print("Specie compatibili:")

        elevation = weather_loc.get("elevation")
        species = species_probability(r, elevation, habitat)

        for sp in species:
            print(f" - {sp['name']}: {sp['level']} ({sp['note']})")

        print("-" * 60)

    print("\nNota sicurezza:")
    print("Questo indicatore NON identifica funghi commestibili.")
    print("Per il consumo serve riconoscimento certo o controllo micologico.")
    print("=" * 60 + "\n")


# ============================================================
# MAIN
# ============================================================

def main():
    print("\n🍄 Indicatore probabilita funghi - versione iOS V2")
    print("Esempi localita: Brallo di Pregola, Varzi, Bobbio, Capanne di Cosola")
    print("")

    query = input("Inserisci localita: ").strip()

    if not query:
        print("Localita non valida.")
        return

    habitat = input(
        "Habitat prevalente? "
        "(faggio/castagno/quercia/abete/misto/prato/pascolo/non so): "
    ).strip()

    if not habitat:
        habitat = "non so"

    print("\nRisoluzione localita...")

    try:
        resolution = resolve_weather_location(query)
    except Exception as e:
        print("Errore durante la geocodifica/localizzazione:")
        print(e)
        return

    if resolution is None:
        print("Non sono riuscito a geocodificare la localita.")
        return

    loc = resolution["weather_location"]
    lat = loc["lat"]
    lon = loc["lon"]

    print("Scarico dati meteo Open-Meteo...")
    print("Intervallo: ultimi 30 giorni + prossimi 4 giorni")

    try:
        weather = get_weather(lat, lon)
    except Exception as e:
        print("Errore durante il download dei dati meteo:")
        print(e)
        return

    if "hourly" not in weather:
        print("Risposta meteo non valida.")
        print(weather)
        return

    days_raw = group_hourly_by_day(weather)
    summaries = {}

    for d_str, values in days_raw.items():
        summaries[d_str] = daily_summary(values)

    today = date.today()

    target_days = [
        today,
        today + timedelta(days=1),
        today + timedelta(days=2)
    ]

    results = []

    for d in target_days:
        result = compute_index(
            d,
            summaries,
            loc.get("elevation")
        )
        results.append(result)

    print_report(resolution, results, habitat)


if __name__ == "__main__":
    main()
