from datetime import date, datetime, timedelta
import csv, math, os, re, uuid, xml.etree.ElementTree as ET
import folium, pandas as pd, plotly.express as px, requests, streamlit as st
from streamlit_folium import st_folium

st.set_page_config(page_title="Fungo Predictor", page_icon="🍄", layout="wide")
OPEN_METEO="https://api.open-meteo.com/v1/forecast"
ELEVATION="https://api.open-meteo.com/v1/elevation"
NOMINATIM="https://nominatim.openstreetmap.org/reverse"
LOMB_WMS="https://www.cartografia.servizirl.it/arcgis2/services/agricoltura/carta_forestale/MapServer/WMSServer"
EMILIA_WFS="https://servizigis.regione.emilia-romagna.it/wfs/carta_della_vegetazione"
HEADERS={"User-Agent":"fungo-predictor/4.0"}
OBS="fungo_observations.csv"; HIST="fungo_forecast_history.csv"
FORESTS=["Non nota","Faggio","Castagno","Quercia","Conifere","Bosco misto"]


def clamp(x,a=0,b=100): return max(a,min(b,x))
def haversine(a,b,c,d):
    r=6371.0088; p1,p2=math.radians(a),math.radians(c); dp=math.radians(c-a); dl=math.radians(d-b)
    return 2*r*math.asin(math.sqrt(math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2))

def append_csv(path,row,cols):
    exists=os.path.exists(path) and os.path.getsize(path)>0
    with open(path,"a",newline="",encoding="utf-8-sig") as f:
        w=csv.DictWriter(f,fieldnames=cols,extrasaction="ignore")
        if not exists: w.writeheader()
        w.writerow({c:row.get(c,"") for c in cols})

def load_csv(path,cols):
    if not os.path.exists(path): return pd.DataFrame(columns=cols)
    try: return pd.read_csv(path)
    except Exception: return pd.DataFrame(columns=cols)

@st.cache_data(ttl=3600,show_spinner=False)
def geocode(lat,lon):
    r=requests.get(NOMINATIM,params={"lat":lat,"lon":lon,"format":"jsonv2","zoom":10,"addressdetails":1},headers=HEADERS,timeout=20); r.raise_for_status()
    j=r.json(); a=j.get("address",{}); reg=a.get("state","").replace("Emilia Romagna","Emilia-Romagna")
    loc=a.get("village") or a.get("town") or a.get("city") or a.get("municipality") or a.get("county") or "Punto selezionato"
    return loc,reg,j.get("display_name",loc)

@st.cache_data(ttl=1800,show_spinner=False)
def meteo(lat,lon):
    q={"latitude":lat,"longitude":lon,"hourly":"temperature_2m,relative_humidity_2m,precipitation,soil_moisture_9_to_27cm,wind_speed_10m","daily":"temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max","past_days":40,"forecast_days":7,"timezone":"Europe/Rome"}
    r=requests.get(OPEN_METEO,params=q,timeout=30); r.raise_for_status(); return r.json()

@st.cache_data(ttl=86400,show_spinner=False)
def terrain(lat,lon,step=120):
    dy=step/111320; dx=step/(111320*math.cos(math.radians(lat)))
    pts=[(lat+y*dy,lon+x*dx) for y in (-1,0,1) for x in (-1,0,1)]
    r=requests.get(ELEVATION,params={"latitude":','.join(f'{x[0]:.6f}' for x in pts),"longitude":','.join(f'{x[1]:.6f}' for x in pts)},timeout=30); r.raise_for_status(); z=r.json()["elevation"]
    west,east=(z[0]+2*z[3]+z[6])/4,(z[2]+2*z[5]+z[8])/4; south,north=(z[0]+2*z[1]+z[2])/4,(z[6]+2*z[7]+z[8])/4
    zx,zy=(east-west)/(2*step),(north-south)/(2*step); sd=math.degrees(math.atan(math.sqrt(zx*zx+zy*zy))); ad=(math.degrees(math.atan2(-zx,-zy))+360)%360
    names=["Nord","Nord-est","Est","Sud-est","Sud","Sud-ovest","Ovest","Nord-ovest"]; asp="Pianeggiante" if sd<2 else names[int((ad+22.5)//45)%8]
    return {"elevation":z[4],"slope_deg":sd,"slope_pct":math.tan(math.radians(sd))*100,"aspect":asp}

def forest_class(text):
    t=re.sub(r"\s+"," ",str(text or "")).lower()
    for keys,val in [(["fagget","fagus","faggio"],"Faggio"),(["castagnet","castanea","castagno"],"Castagno"),(["querc","rover","cerret","farnia","leccio"],"Quercia"),(["conifer","peccet","abiet","laric","pineta","pino","abete","larice"],"Conifere"),(["misto","mista","latifoglie","bosco","forest"],"Bosco misto")]:
        if any(k in t for k in keys): return val
    return "Non nota"

def best_desc(props):
    if not isinstance(props,dict): return ""
    vals=[]
    for k,v in props.items():
        if v is None or isinstance(v,(dict,list)): continue
        v=str(v).strip()
        if v: vals.append((0 if forest_class(v)!="Non nota" else 1,0 if any(x in k.lower() for x in ["descr","tipo","categoria","veget","nome","classe"]) else 1,-len(v),v))
    return sorted(vals)[0][3] if vals else ""

@st.cache_data(ttl=86400,show_spinner=False)
def emilia_types():
    r=requests.get(EMILIA_WFS,params={"service":"WFS","request":"GetCapabilities"},timeout=40); r.raise_for_status(); root=ET.fromstring(r.content); out=[]
    for e in root.iter():
        if e.tag.split("}")[-1]=="FeatureType":
            n=next((c.text.strip() for c in e if c.tag.split("}")[-1]=="Name" and c.text),None)
            if n: out.append(n)
    return sorted(out,key=lambda n:0 if any(k in n.lower() for k in ["veget","forest","bosco"]) else 1)

@st.cache_data(ttl=86400,show_spinner=False)
def forest_lookup(lat,lon,region):
    if region=="Emilia-Romagna":
        eps=.00015; bbox=f"{lon-eps},{lat-eps},{lon+eps},{lat+eps},EPSG:4326"
        for layer in emilia_types()[:15]:
            try:
                r=requests.get(EMILIA_WFS,params={"service":"WFS","version":"2.0.0","request":"GetFeature","typeNames":layer,"outputFormat":"application/json","count":1,"srsName":"EPSG:4326","bbox":bbox},timeout=30)
                if r.ok:
                    fs=r.json().get("features",[])
                    if fs:
                        raw=best_desc(fs[0].get("properties",{})); return {"raw":raw or "Vegetazione cartografata","type":forest_class(raw),"source":"Carta vegetazione Emilia-Romagna","auto":True}
            except Exception: pass
        return {"raw":"Nessun poligono rilevato","type":"Non nota","source":"Carta vegetazione Emilia-Romagna","auto":True}
    if region=="Lombardia":
        try:
            cap=requests.get(LOMB_WMS,params={"service":"WMS","request":"GetCapabilities","version":"1.3.0"},timeout=35); cap.raise_for_status(); root=ET.fromstring(cap.content); layers=[]
            for e in root.iter():
                if e.tag.split("}")[-1]=="Layer":
                    name=next((c.text.strip() for c in e if c.tag.split("}")[-1]=="Name" and c.text),None)
                    if name: layers.append(name)
            d=.002; bbox=f"{lat-d},{lon-d},{lat+d},{lon+d}"
            for layer in layers[:15]:
                q={"service":"WMS","version":"1.3.0","request":"GetFeatureInfo","layers":layer,"query_layers":layer,"styles":"","crs":"EPSG:4326","bbox":bbox,"width":101,"height":101,"i":50,"j":50,"feature_count":1,"info_format":"application/json"}
                r=requests.get(LOMB_WMS,params=q,timeout=30)
                if r.ok:
                    try: fs=r.json().get("features",[])
                    except Exception: fs=[]
                    if fs:
                        raw=best_desc(fs[0].get("properties",{})); return {"raw":raw,"type":forest_class(raw),"source":"Carta forestale Lombardia","auto":True}
        except Exception: pass
        return {"raw":"Nessun poligono rilevato","type":"Non nota","source":"Carta forestale Lombardia","auto":True}
    return {"raw":"Selezione manuale","type":"Non nota","source":"Utente","auto":False}

def build_forecast(raw,forest,aspect,slope):
    d=pd.DataFrame(raw["daily"]); d["date"]=pd.to_datetime(d["time"]).dt.date
    h=pd.DataFrame(raw["hourly"]); h["date"]=pd.to_datetime(h["time"]).dt.date
    a=h.groupby("date",as_index=False).agg(humidity=("relative_humidity_2m","mean"),soil=("soil_moisture_9_to_27cm","mean"),temp=("temperature_2m","mean")); d=d.merge(a,on="date")
    rain=d.set_index("date")["precipitation_sum"].fillna(0).to_dict(); future=d[d.date>=date.today()].head(7); fb={"Faggio":5,"Castagno":4,"Quercia":2,"Conifere":1,"Bosco misto":4}.get(forest,0)
    ab={"Nord":4,"Nord-est":3,"Nord-ovest":3,"Est":2,"Sud-est":-3,"Sud-ovest":-3,"Sud":-4}.get(aspect,0)*clamp(slope/18,.25,1.5) if slope>=2 else 0
    rows=[]
    for _,r in future.iterrows():
        day=r.date; r7=sum(rain.get(day-timedelta(days=i),0) for i in range(1,8)); heavy=next((i for i in range(41) if rain.get(day-timedelta(days=i),0)>=10),None); hum=float(r.humidity); soil=float(r.soil); temp=float(r.temp); wind=float(r.wind_speed_10m_max)
        score=clamp(r7/50*32,0,32)+(18 if heavy is not None and 5<=heavy<=12 else 8)+clamp((hum-45)/40*18,0,18)+clamp((soil-.1)/.28*16,0,16)+clamp(14-abs(temp-16)*1.8,0,14)-clamp((wind-12)*.8,0,10)+fb+ab
        rows.append({"Data":pd.Timestamp(day),"Indice":round(clamp(score)),"Pioggia prevista (mm)":round(float(r.precipitation_sum),1),"Pioggia 7 gg (mm)":round(r7,1),"T min":round(float(r.temperature_2m_min),1),"T max":round(float(r.temperature_2m_max),1),"Umidità (%)":round(hum),"Suolo":round(soil,3),"Vento":round(wind,1)})
    return pd.DataFrame(rows)

HIST_COLS=["saved_at","forecast_date","lat","lon","locality","region","forecast_score","rain_7d_mm","forest_type","aspect","slope_deg"]
OBS_COLS=["id","created_at","outing_date","time_slot","lat","lon","locality","region","elevation_m","forest_type","forest_raw","aspect","slope_deg","ground_condition","mushrooms_found","species","quantity","quantity_unit","maturity","notes","forecast_score","forecast_source","actual_score","prediction_error","absolute_error","assessment"]

def save_snapshot(df,lat,lon,loc,reg,forest,t):
    old=load_csv(HIST,HIST_COLS); now=datetime.now().isoformat(timespec="seconds"); rows=[]
    for _,r in df.iterrows(): rows.append({"saved_at":now,"forecast_date":r.Data.date().isoformat(),"lat":lat,"lon":lon,"locality":loc,"region":reg,"forecast_score":r.Indice,"rain_7d_mm":r["Pioggia 7 gg (mm)"],"forest_type":forest,"aspect":t["aspect"],"slope_deg":round(t["slope_deg"],1)})
    pd.concat([old,pd.DataFrame(rows)]).drop_duplicates(["saved_at","forecast_date","lat","lon"]).to_csv(HIST,index=False,encoding="utf-8-sig")

def saved_prediction(day,lat,lon):
    h=load_csv(HIST,HIST_COLS)
    if h.empty:return None
    h=h[h.forecast_date.astype(str)==day.isoformat()].copy()
    if h.empty:return None
    h["distance"]=h.apply(lambda x:haversine(lat,lon,float(x.lat),float(x.lon)),axis=1); h=h[h.distance<=2]
    if h.empty:return None
    return h.sort_values("saved_at").iloc[-1]

def actual_score(found,q,state):
    if not found:return 0
    return round(clamp(min(70,20+10*math.log1p(float(q)))+{"Nascita":15,"Maturi":10,"Misti":8,"Marci":-15}.get(state,0)))

def diary(p,loc,reg,elev,fi,forest,t,fc):
    st.divider(); st.header("📚 Diario uscite e verifica del modello")
    with st.expander("➕ Registra un'uscita reale"):
        with st.form("outing"):
            a,b=st.columns(2); day=a.date_input("Data dell'uscita",date.today(),max_value=date.today()); slot=b.radio("Momento",["Mattina","Pomeriggio"],horizontal=True)
            a,b=st.columns(2); ground=a.selectbox("Condizioni bosco/terreno",["Secco","Umido","Molto umido","Bagnato"]); found=b.radio("Funghi trovati?",["Sì","No"],horizontal=True)=="Sì"
            species=st.multiselect("Quali",["Porcino edulis","Porcino estivo","Porcino nero","Porcino pinicolo","Finferlo/Gallinaccio","Ovulo","Mazza di tamburo","Chiodino","Altro"],disabled=not found)
            a,b,c=st.columns(3); q=a.number_input("Quanti",0.0,step=1.0,disabled=not found); unit=b.selectbox("Unità",["Esemplari","Grammi","Chilogrammi"],disabled=not found); state=c.selectbox("Stato",["Nascita","Maturi","Misti","Marci"],disabled=not found)
            notes=st.text_area("Note"); submit=st.form_submit_button("Salva uscita",use_container_width=True)
        if submit:
            if found and (not species or q<=0): st.error("Indica almeno una specie e una quantità maggiore di zero.")
            else:
                snap=saved_prediction(day,p["lat"],p["lon"]); current=fc[fc.Data.dt.date==day]
                pred=int(float(snap.forecast_score)) if snap is not None else (int(current.iloc[0].Indice) if not current.empty else None); source="Snapshot salvato" if snap is not None else ("Previsione corrente" if pred is not None else "Non disponibile")
                real=actual_score(found,q,state if found else ""); err=real-pred if pred is not None else None; assess="Non confrontabile" if err is None else ("Previsione rispettata" if abs(err)<=10 else ("Risultato migliore del previsto" if err>0 else "Risultato peggiore del previsto"))
                row={"id":str(uuid.uuid4()),"created_at":datetime.now().isoformat(timespec="seconds"),"outing_date":day.isoformat(),"time_slot":slot,"lat":p["lat"],"lon":p["lon"],"locality":loc,"region":reg,"elevation_m":elev,"forest_type":forest,"forest_raw":fi["raw"],"aspect":t["aspect"],"slope_deg":round(t["slope_deg"],1),"ground_condition":ground,"mushrooms_found":found,"species":"; ".join(species) if found else "Nessuno","quantity":q if found else 0,"quantity_unit":unit if found else "Esemplari","maturity":state if found else "Nessuno","notes":notes,"forecast_score":"" if pred is None else pred,"forecast_source":source,"actual_score":real,"prediction_error":"" if err is None else err,"absolute_error":"" if err is None else abs(err),"assessment":assess}
                append_csv(OBS,row,OBS_COLS); st.success(f"Uscita salvata. {assess}.")
                if pred is not None: st.write(f"Previsione **{pred}%** | esito reale normalizzato **{real}%** | scostamento **{err:+d} punti**")
    obs=load_csv(OBS,OBS_COLS)
    if obs.empty: st.info("Nessuna uscita registrata."); return
    for c in ["forecast_score","actual_score","absolute_error"]: obs[c]=pd.to_numeric(obs[c],errors="coerce")
    comp=obs.dropna(subset=["forecast_score","actual_score"]); a,b,c,d=st.columns(4); a.metric("Uscite",len(obs)); b.metric("Confrontabili",len(comp)); c.metric("Errore medio",f"{comp.absolute_error.mean():.1f} punti" if len(comp) else "n.d."); d.metric("Rispettate ±10",f"{(comp.absolute_error.le(10).mean()*100):.0f}%" if len(comp) else "n.d.")
    if len(comp):
        z=comp.copy(); z["Data"]=pd.to_datetime(z.outing_date); z=z.melt(id_vars="Data",value_vars=["forecast_score","actual_score"],var_name="Serie",value_name="Punteggio"); z.Serie=z.Serie.map({"forecast_score":"Previsione","actual_score":"Esito reale"}); st.plotly_chart(px.line(z,x="Data",y="Punteggio",color="Serie",markers=True,range_y=[0,100]),use_container_width=True)
    st.dataframe(obs[["outing_date","time_slot","locality","ground_condition","species","quantity","quantity_unit","maturity","forecast_score","actual_score","assessment"]].sort_values("outing_date",ascending=False),hide_index=True,use_container_width=True)
    st.download_button("Scarica database CSV",obs.to_csv(index=False).encode("utf-8-sig"),"fungo_observations.csv","text/csv",use_container_width=True)

st.title("🍄 Fungo Predictor")
st.caption("Previsione su mappa, riconoscimento del bosco e diario delle uscite reali.")
with st.sidebar:
    st.header("Impostazioni"); st.caption("Bosco automatico in Lombardia ed Emilia-Romagna.")
    uploaded=st.file_uploader("Ripristina/integra database uscite",type="csv")
    if uploaded and st.button("Importa database"):
        up=pd.read_csv(uploaded); old=load_csv(OBS,OBS_COLS); pd.concat([old,up]).drop_duplicates("id",keep="last").to_csv(OBS,index=False,encoding="utf-8-sig"); st.success("Database importato")
    if st.button("Cancella punto",use_container_width=True): st.session_state.pop("point",None); st.rerun()

m=folium.Map([44.75,9.10],zoom_start=7)
if "point" in st.session_state: folium.Marker([st.session_state.point["lat"],st.session_state.point["lon"]],icon=folium.Icon(color="red",icon="info-sign")).add_to(m)
md=st_folium(m,height=480,use_container_width=True,returned_objects=["last_clicked"])
if md and md.get("last_clicked"):
    np={"lat":round(md["last_clicked"]["lat"],5),"lon":round(md["last_clicked"]["lng"],5)}
    if st.session_state.get("point")!=np: st.session_state.point=np; st.rerun()
if "point" not in st.session_state: st.info("Clicca sulla mappa nel punto da analizzare."); st.stop()
p=st.session_state.point
try:
    with st.spinner("Recupero meteo, quota e vegetazione..."): loc,reg,address=geocode(p["lat"],p["lon"]); raw=meteo(p["lat"],p["lon"]); t=terrain(p["lat"],p["lon"]); fi=forest_lookup(p["lat"],p["lon"],reg)
except Exception as e: st.error(f"Errore recupero dati: {e}"); st.stop()
if reg not in {"Piemonte","Liguria","Lombardia","Emilia-Romagna"}: st.error(f"Punto fuori area: {reg or 'regione non riconosciuta'}"); st.stop()
forest=fi["type"] if fi["auto"] else st.sidebar.selectbox("Tipo di bosco",FORESTS)
if not fi["auto"]: fi={**fi,"raw":forest,"type":forest}
elev=round(float(t["elevation"])); st.subheader(loc); a,b,c,d=st.columns(4); a.metric("Regione",reg); b.metric("Quota",f"{elev} m"); c.metric("Bosco",forest); d.metric("Esposizione",t["aspect"]); st.caption(address); st.info(f"Classe cartografica: **{fi['raw']}** | Fonte: {fi['source']} | Pendenza: **{t['slope_deg']:.1f}°**")
fc=build_forecast(raw,forest,t["aspect"],t["slope_deg"]); save_snapshot(fc,p["lat"],p["lon"],loc,reg,forest,t)
st.subheader("Indice prossimi 7 giorni"); fig=px.line(fc,x="Data",y="Indice",markers=True,range_y=[0,100]); fig.update_traces(line_color="#16825d",line_width=4,fill="tozeroy"); st.plotly_chart(fig,use_container_width=True)
cols=st.columns(7)
for i,(_,r) in enumerate(fc.iterrows()):
    color="#dcfce7" if r.Indice>=60 else ("#fef9c3" if r.Indice>=40 else "#fee2e2")
    cols[i].markdown(f"<div style='background:{color};padding:10px;border-radius:12px;text-align:center'><b>{r.Data.strftime('%d/%m')}</b><br><span style='font-size:1.5rem;font-weight:900'>{r.Indice}%</span></div>",unsafe_allow_html=True)
st.dataframe(fc,hide_index=True,use_container_width=True)
diary(p,loc,reg,elev,fi,forest,t,fc)
st.caption("Nota: su Streamlit Community Cloud i file locali possono essere eliminati a ogni riavvio. Scarica periodicamente il CSV e reimportalo dalla barra laterale.")
