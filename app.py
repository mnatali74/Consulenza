import os
import re
import shutil
import zipfile
import requests
import pdfplumber
import pandas as pd
import geopandas as gpd
import matplotlib.pyplot as plt
import matplotlib.patheffects as pe
import contextily as ctx
from shapely.geometry import box
from fpdf import FPDF
import gradio as gr

# ==============================================================================
# VERIFICA MODULI ALL'AVVIO
# ==============================================================================
def verifica_import_moduli():
    """Verifica che tutti i moduli necessari siano disponibili all'avvio"""
    moduli_necessari = {
        'pdfplumber': 'pdfplumber',
        'pandas': 'pandas',
        'geopandas': 'geopandas',
        'matplotlib': 'matplotlib',
        'contextily': 'contextily',
        'shapely': 'shapely',
        'fpdf': 'fpdf',
        'gradio': 'gradio',
        'requests': 'requests'
    }
    
    for nome, modulo in moduli_necessari.items():
        try:
            __import__(modulo)
            print(f"✓ Modulo '{nome}' caricato correttamente")
        except ImportError:
            raise ImportError(f"Modulo '{nome}' non trovato. Installalo con: pip install {nome}")

# Esegui la verifica all'avvio
verifica_import_moduli()

# ==============================================================================
# CONFIGURAZIONE GIS E COSTANTI
# ==============================================================================
# URL base per il download della cartografia dell'Agenzia delle Entrate
URL_BASE_CARTOGRAFIA = "https://wfs.cartografia.agenziaentrate.gov.it/inspire/wfs/GetDataset.php?dataset="

# Cartella per il caching delle mappe scaricate
CARTELLA_CACHE_MAPPE = "./cache_mappe"

# Cartella temporanea per l'estrazione dei file GML
CARTELLA_ESTRAZIONE_GIS = "./cartografia_estratta_temp"

# Lista di tutte le regioni italiane per il download
REGIONI_ITALIANE = [
    "ABRUZZO", "BASILICATA", "CALABRIA", "CAMPANIA", "EMILIA-ROMAGNA",
    "FRIULI-VENEZIA-GIULIA", "LAZIO", "LIGURIA", "LOMBARDIA", "MARCHE",
    "MOLISE", "PIEMONTE", "PUGLIA", "SARDEGNA", "SICILIA", "TOSCANA",
    "TRENTINO-ALTO-ADIGE", "UMBRIA", "VALLE-D-AOSTA", "VENETO"
]
HEADER_PIANO = "PIANO DI COLTIVAZIONE - PARTICELLE CATASTALI"
HEADER_ZOOTECNICA = "COMPOSIZIONE ZOOTECNICA"

# ==============================================================================
# 1. PARSING DEL FASCICOLO AZIENDALE (PDFPLUMBER)
# ==============================================================================

def pulisci_stringa(txt):
    if not txt:
        return ""
    return re.sub(r"\s+", " ", txt.replace("\n", " ")).strip()

def pulisci_e_tronca_testo_zootecnia(testo_riga):
    if "LE INFORMAZIONI RIPORTATE" in testo_riga.upper():
        testo_riga = re.split(r"LE INFORMAZIONI RIPORTATE", testo_riga, flags=re.IGNORECASE)[0]
    return re.split(r"\b\d{11}\b", testo_riga)[0].strip()

def estrai_dettaglio_capi_analitico(testo_dettaglio):
    dettagli_capi = []
    matches = re.findall(r"(N\.?\s*di\s*capi[^:]+):\s*(\d+)", testo_dettaglio, re.IGNORECASE)
    for desc, qta in matches:
        dettagli_capi.append({"categoria_capo": re.sub(r"\s+", " ", desc).strip(), "quantita": int(qta)})
    return dettagli_capi

def estrai_anagrafica(pdf):
    pag1 = pdf.pages[0]
    testo = pag1.extract_text() or ""
    def trova_campo(pattern, default="N/D"):
        match = re.search(pattern, testo, re.IGNORECASE)
        return match.group(1).strip() if match else default
    return {
        "cuaa": trova_campo(r"CUAA\s*:\s*([A-Z0-9]+)"),
        "partita_iva": trova_campo(r"Partita IVA\s*:\s*(\d+)"),
        "denominazione": trova_campo(r"Denominazione\s*:\s*(.*?)(?=\n|Forma giuridica)"),
        "indirizzo": trova_campo(r"Indirizzo\s*:\s*(.*?)(?=\n|PEC|Mail|Telefono)"),
        "comune": trova_campo(r"COMUNE\s*\n(.*?)\n") 
    }

def estrai_composizione_zootecnica(pdf):
    dataset_zootecnia = []
    estrazione_attiva = False
    for pagina in pdf.pages:
        testo = pagina.extract_text() or ""
        testo_upper = testo.upper()

        if "COMPOSIZIONE ZOOTECNICA" in testo_upper:
            estrazione_attiva = True
        if not estrazione_attiva:
            continue
        if "FABBRICATI" in testo_upper:
            testo = testo.split("FABBRICATI")[0]

        righe = testo.split("\n")
        blocchi_righe, blocco_curr = [], ""

        for riga in righe:
            riga_s = riga.strip()
            if not riga_s or "COMPOSIZIONE ZOOTECNICA" in riga_s.upper():
                continue
            if re.match(r"^\d+\)", riga_s):
                if blocco_curr:
                    blocchi_righe.append(blocco_curr)
                blocco_curr = riga_s
            else:
                if blocco_curr:
                    blocco_curr += " " + riga_s

        if blocco_curr:
            blocchi_righe.append(blocco_curr)

        for t_riga in blocchi_righe:
            t_riga_pulita = pulisci_e_tronca_testo_zootecnia(t_riga)
            t_upper = t_riga_pulita.upper()
            
            match_specie = re.search(r"\b(BOVINI|BUFALINI|OVINI|CAPRINI|SUINI|AVICOLI|EQUINI|APICOLTURA)\b", t_upper)
            specie = match_specie.group(1) if match_specie else "N/D"
            
            match_prod = re.search(r"\b(LATTE|CARNE|RIPRODUZIONE|MISTO|INGRASSO)\b", t_upper)
            tipo_produzione = match_prod.group(1) if match_prod else "N/D"

            dettaglio_capi_lista = estrai_dettaglio_capi_analitico(t_riga_pulita)
            str_dettaglio_capi = (
                " | ".join([f"{item['categoria_capo']}: {item['quantita']}" for item in dettaglio_capi_lista])
                if dettaglio_capi_lista else "Nessun dettaglio specificato"
            )

            dataset_zootecnia.append({
                "specie_allevata": specie,
                "tipo_produzione": tipo_produzione,
                "dettaglio_capi_str": str_dettaglio_capi,
            })

        if "FABBRICATI" in testo_upper:
            break

    return dataset_zootecnia

def converti_superficie(sup_str):
    if not sup_str or sup_str == "N/D": return 0.0
    parti = sup_str.split(",")
    if len(parti) == 3 and all(p.isdigit() for p in parti):
        return int(parti[0]) + (int(parti[1]) / 100.0) + (int(parti[2]) / 10000.0)
    return 0.0

def mappatura_gerarchica_1to1(cod_uso_list, desc_uso_list):
    parti_desc = []
    for d in desc_uso_list:
        sub = [p.strip() for p in d.split(" - ") if p.strip()]
        parti_desc.extend(sub)
    return {
        "uso": parti_desc[2] if len(parti_desc) > 2 else (parti_desc[0] if len(parti_desc) > 0 else "N/D")
    }

def estrai_piano_coltivazione(pdf):
    dataset_appezzamenti = []
    in_piano_coltivazione = False

    for num_pag, pagina in enumerate(pdf.pages, 1):
        testo_pagina = pagina.extract_text() or ""
        if HEADER_PIANO in testo_pagina.upper():
            in_piano_coltivazione = True
        if not in_piano_coltivazione:
            continue

        words_all = pagina.extract_words()
        parole_header = [w for w in words_all if w["text"].upper() in ["PIANO", "COLTIVAZIONE", "PARTICELLE", "CATASTALI"]]
        y_limite_superiore = max(w["bottom"] for w in parole_header) + 2 if parole_header else 0

        progressivi = sorted(
            [w for w in words_all if w["x0"] < 60 and w["top"] >= y_limite_superiore and re.match(r"^\d+\)$", w["text"].strip())],
            key=lambda x: x["top"]
        )
        if not progressivi:
            continue

        linee = sorted(list(set([l["top"] for l in pagina.lines if abs(l["top"] - l["bottom"]) < 2])))
        y_limite_inferiore = max(linee) if (linee and max(linee) > y_limite_superiore) else pagina.height - 20

        for prog in progressivi:
            y_prog = prog["top"]
            if y_prog > y_limite_inferiore:
                continue

            l_sopra = [l for l in linee if l < y_prog]
            y_top = max(l_sopra) if l_sopra else y_limite_superiore
            l_sotto = [l for l in linee if l > y_prog]
            y_bottom = min(l_sotto) if l_sotto else y_limite_inferiore

            crop_part = pagina.crop((180, y_top, 215, y_bottom))
            part_txt = pulisci_stringa(crop_part.extract_text() or "")
            words_part = re.findall(r"\b\d{5}\b", part_txt)

            crop_sup = pagina.crop((500, y_top, 600, y_bottom))
            sup_txt = pulisci_stringa(crop_sup.extract_text() or "")
            words_sup = re.findall(r"\b\d{2},\d{2},\d{2}\b", sup_txt)

            if not words_part or not words_sup:
                continue

            particella = words_part[0]
            raw_sup = words_sup[0]

            crop_comune = pagina.crop((30, y_top, 180, y_bottom))
            comune_txt = pulisci_stringa(crop_comune.extract_text() or "")
            comune = " ".join([w for w in comune_txt.split() if w.isalpha()])

            crop_foglio = pagina.crop((215, y_top, 232, y_bottom))
            foglio_txt = pulisci_stringa(crop_foglio.extract_text() or "")
            words_foglio = [w for w in foglio_txt.split() if w.isdigit() and w != "000"]
            foglio = words_foglio[0] if words_foglio else "N/D"

            sup_ha = converti_superficie(raw_sup)

            words_uso = [w for w in words_all if 232 <= w["x0"] < 500 and y_top - 2 <= w["top"] and w["bottom"] <= y_bottom + 2]
            words_uguale = sorted([w for w in words_uso if w["text"].strip() == "="], key=lambda w: w["top"])

            desc_uso_list = []
            for u_obj in words_uguale:
                y_uguale = u_obj["top"]
                crop_desc = pagina.crop((u_obj["x1"] + 2, y_uguale - 4, 500, y_uguale + 8))
                desc_linea = pulisci_stringa(crop_desc.extract_text() or "")
                if desc_linea:
                    desc_uso_list.append(desc_linea)

            dettaglio_uso = mappatura_gerarchica_1to1([], desc_uso_list)

            dataset_appezzamenti.append({
                "comune": comune,
                "foglio": foglio,
                "particella": particella,
                "superficie_ha": sup_ha,
                "uso": dettaglio_uso.get("uso", "N/D")
            })

    return dataset_appezzamenti

# ==============================================================================
# 2. GENERAZIONE MAPPE GIS (CONTORNI BIANCHI + NUMERO CENTRALE + RIQUADRI)
# ==============================================================================

def verifica_import_moduli():
    """Verifica che tutti i moduli necessari siano disponibili all'avvio"""
    moduli_necessari = {
        'pdfplumber': 'pdfplumber',
        'pandas': 'pandas',
        'geopandas': 'geopandas',
        'matplotlib': 'matplotlib',
        'contextily': 'contextily',
        'shapely': 'shapely',
        'fpdf': 'fpdf',
        'gradio': 'gradio',
        'requests': 'requests'
    }
    
    for nome, modulo in moduli_necessari.items():
        try:
            __import__(modulo)
            print(f"✓ Modulo '{nome}' caricato correttamente")
        except ImportError:
            raise ImportError(f"Modulo '{nome}' non trovato. Installalo con: pip install {nome}")

def scarica_cartografia_regione(nome_regione):
    """Scarica la cartografia di una specifica regione se non è già in cache"""
    nome_file_zip = f"{nome_regione}.zip"
    percorso_file = os.path.join(CARTELLA_CACHE_MAPPE, nome_file_zip)
    
    if os.path.exists(percorso_file):
        print(f"Cartografia per {nome_regione} già presente in cache")
        return percorso_file
    
    try:
        print(f"Download cartografia WFS per {nome_regione} in corso...")
        url_regione = f"{URL_BASE_CARTOGRAFIA}{nome_regione}.zip"
        response = requests.get(url_regione, stream=True, timeout=120)
        response.raise_for_status()
        
        os.makedirs(CARTELLA_CACHE_MAPPE, exist_ok=True)
        with open(percorso_file, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk: f.write(chunk)
        print(f"Cartografia per {nome_regione} scaricata e salvata in cache")
        return percorso_file
    except Exception as e:
        print(f"Errore download WFS per {nome_regione}: {e}")
        return None

def assicura_presenza_cartografia(comuni_target):
    """Assicura che la cartografia per tutti i comuni target sia disponibile"""
    regioni_necessarie = set()
    
    # Mappa provincia -> regione (basato su sigle provinciali ISTAT)
    provincia_a_regione = {
        # Abruzzo
        "AQ": "ABRUZZO", "CH": "ABRUZZO", "PE": "ABRUZZO", "TE": "ABRUZZO",
        # Basilicata
        "MT": "BASILICATA", "PZ": "BASILICATA",
        # Calabria
        "CS": "CALABRIA", "CZ": "CALABRIA", "KR": "CALABRIA", "RC": "CALABRIA", "VV": "CALABRIA",
        # Campania
        "AV": "CAMPANIA", "BN": "CAMPANIA", "CE": "CAMPANIA", "NA": "CAMPANIA", "SA": "CAMPANIA",
        # Emilia-Romagna
        "BO": "EMILIA-ROMAGNA", "FC": "EMILIA-ROMAGNA", "FE": "EMILIA-ROMAGNA", 
        "MO": "EMILIA-ROMAGNA", "PR": "EMILIA-ROMAGNA", "RA": "EMILIA-ROMAGNA",
        "RE": "EMILIA-ROMAGNA", "RN": "EMILIA-ROMAGNA", "PC": "EMILIA-ROMAGNA",
        # Friuli-Venezia Giulia
        "GO": "FRIULI-VENEZIA-GIULIA", "PN": "FRIULI-VENEZIA-GIULIA", 
        "TS": "FRIULI-VENEZIA-GIULIA", "UD": "FRIULI-VENEZIA-GIULIA",
        # Lazio
        "FR": "LAZIO", "LT": "LAZIO", "RI": "LAZIO", "RM": "LAZIO", "VT": "LAZIO",
        # Liguria
        "GE": "LIGURIA", "IM": "LIGURIA", "SP": "LIGURIA", "SV": "LIGURIA",
        # Lombardia
        "BG": "LOMBARDIA", "BS": "LOMBARDIA", "CO": "LOMBARDIA", "CR": "LOMBARDIA",
        "LC": "LOMBARDIA", "LO": "LOMBARDIA", "MN": "LOMBARDIA", "MI": "LOMBARDIA",
        "MB": "LOMBARDIA", "PV": "LOMBARDIA", "SO": "LOMBARDIA", "VA": "LOMBARDIA",
        # Marche
        "AN": "MARCHE", "AP": "MARCHE", "AS": "MARCHE", "FM": "MARCHE",
        "MC": "MARCHE", "PU": "MARCHE",
        # Molise
        "CB": "MOLISE", "IS": "MOLISE",
        # Piemonte
        "AL": "PIEMONTE", "AT": "PIEMONTE", "BI": "PIEMONTE", "CN": "PIEMONTE",
        "NO": "PIEMONTE", "TO": "PIEMONTE", "VC": "PIEMONTE", "VB": "PIEMONTE",
        # Puglia
        "BA": "PUGLIA", "BR": "PUGLIA", "FG": "PUGLIA", "LE": "PUGLIA",
        "TA": "PUGLIA", "BT": "PUGLIA",
        # Sardegna
        "CA": "SARDEGNA", "CI": "SARDEGNA", "NU": "SARDEGNA", "OR": "SARDEGNA",
        "OT": "SARDEGNA", "SS": "SARDEGNA", "VS": "SARDEGNA",
        # Sicilia
        "AG": "SICILIA", "CL": "SICILIA", "CT": "SICILIA", "EN": "SICILIA",
        "ME": "SICILIA", "PA": "SICILIA", "RG": "SICILIA", "SR": "SICILIA", "TP": "SICILIA",
        # Toscana
        "AR": "TOSCANA", "FI": "TOSCANA", "GR": "TOSCANA", "LI": "TOSCANA",
        "LU": "TOSCANA", "MS": "TOSCANA", "PI": "TOSCANA", "PO": "TOSCANA",
        "PT": "TOSCANA", "SI": "TOSCANA",
        # Trentino-Alto Adige
        "BZ": "TRENTINO-ALTO-ADIGE", "TN": "TRENTINO-ALTO-ADIGE",
        # Umbria
        "PG": "UMBRIA", "TR": "UMBRIA",
        # Valle d'Aosta
        "AO": "VALLE-D-AOSTA",
        # Veneto
        "BL": "VENETO", "BS": "VENETO", "PD": "VENETO", "RO": "VENETO",
        "TV": "VENETO", "VE": "VENETO", "VR": "VENETO", "VI": "VENETO"
    }
    
    # Mappa comune -> regione per i capoluoghi e comuni principali
    comune_a_regione = {
        # Abruzzo
        "L'AQUILA": "ABRUZZO", "TERAMO": "ABRUZZO", "PESCARA": "ABRUZZO", "CHIETI": "ABRUZZO",
        "AVEZZANO": "ABRUZZO", "SULMONA": "ABRUZZO", "LANCIANO": "ABRUZZO", "VASTO": "ABRUZZO",
        # Basilicata
        "POTENZA": "BASILICATA", "MATERA": "BASILICATA", "MELFI": "BASILICATA",
        # Calabria
        "CATANZARO": "CALABRIA", "COSENZA": "CALABRIA", "REGGIO CALABRIA": "CALABRIA",
        "CROTONE": "CALABRIA", "VIBO VALENTIA": "CALABRIA",
        # Campania
        "NAPOLI": "CAMPANIA", "SALERNO": "CAMPANIA", "CASERTA": "CAMPANIA",
        "BENEVENTO": "CAMPANIA", "AVELLINO": "CAMPANIA",
        # Emilia-Romagna
        "BOLOGNA": "EMILIA-ROMAGNA", "MODENA": "EMILIA-ROMAGNA", "REGGIO EMILIA": "EMILIA-ROMAGNA",
        "PARMA": "EMILIA-ROMAGNA", "PIACENZA": "EMILIA-ROMAGNA", "RAVENNA": "EMILIA-ROMAGNA",
        "FERRARA": "EMILIA-ROMAGNA", "RIMINI": "EMILIA-ROMAGNA", "FORLI": "EMILIA-ROMAGNA",
        # Friuli-Venezia Giulia
        "TRIESTE": "FRIULI-VENEZIA-GIULIA", "UDINE": "FRIULI-VENEZIA-GIULIA",
        "GORIZIA": "FRIULI-VENEZIA-GIULIA", "PORDENONE": "FRIULI-VENEZIA-GIULIA",
        # Lazio
        "ROMA": "LAZIO", "LATINA": "LAZIO", "FROSINONE": "LAZIO", "VITERBO": "LAZIO",
        "RIETI": "LAZIO", "GUIDONIA MONTECELIO": "LAZIO", "TIVOLI": "LAZIO",
        # Liguria
        "GENOVA": "LIGURIA", "SAVONA": "LIGURIA", "IMPERIA": "LIGURIA", "LA SPEZIA": "LIGURIA",
        # Lombardia
        "MILANO": "LOMBARDIA", "BERGAMO": "LOMBARDIA", "BRESCIA": "LOMBARDIA",
        "COMO": "LOMBARDIA", "CREMONA": "LOMBARDIA", "LECCO": "LOMBARDIA",
        "LODI": "LOMBARDIA", "MANTOVA": "LOMBARDIA", "PAVIA": "LOMBARDIA",
        "SONDRIO": "LOMBARDIA", "VARSESE": "LOMBARDIA",
        # Marche
        "ANCONA": "MARCHE", "PESARO": "MARCHE", "MACERATA": "MARCHE",
        "FERMO": "MARCHE", "ASCOLI PICENO": "MARCHE", "SENIGALLIA": "MARCHE",
        "JESI": "MARCHE", "FABRIANO": "MARCHE", "CIVITANOVA MARCHE": "MARCHE",
        # Molise
        "CAMPBASSO": "MOLISE", "ISERNIA": "MOLISE", "TERMOLI": "MOLISE",
        # Piemonte
        "TORINO": "PIEMONTE", "CUNEO": "PIEMONTE", "NOVARA": "PIEMONTE",
        "ALESSANDRIA": "PIEMONTE", "ASTI": "PIEMONTE", "BIELLA": "PIEMONTE",
        "VERCELLI": "PIEMONTE", "VERBANIA": "PIEMONTE",
        # Puglia
        "BARI": "PUGLIA", "FOGGIA": "PUGLIA", "LECCE": "PUGLIA",
        "TARANTO": "PUGLIA", "BRINDISI": "PUGLIA", "ANDRIA": "PUGLIA",
        "BARLETTA": "PUGLIA", "TRANI": "PUGLIA",
        # Sardegna
        "CAGLIARI": "SARDEGNA", "SASSARI": "SARDEGNA", "NUORO": "SARDEGNA",
        "ORISTANO": "SARDEGNA", "ALGHERO": "SARDEGNA", "OLBIA": "SARDEGNA",
        "QUARTU SANT'ELENA": "SARDEGNA",
        # Sicilia
        "PALERMO": "SICILIA", "CATANIA": "SICILIA", "MESSINA": "SICILIA",
        "SYRACUSE": "SICILIA", "TRAPANI": "SICILIA", "AGRIGENTO": "SICILIA",
        "CALTANISSETTA": "SICILIA", "ENNA": "SICILIA", "RAGUSA": "SICILIA",
        # Toscana
        "FIRENZE": "TOSCANA", "SIENA": "TOSCANA", "PISA": "TOSCANA",
        "AREZZO": "TOSCANA", "GROSSETO": "TOSCANA", "LIVORNO": "TOSCANA",
        "LUCCA": "TOSCANA", "MASSA": "TOSCANA", "PISTOIA": "TOSCANA",
        "PRATO": "TOSCANA",
        # Trentino-Alto Adige
        "TRENTO": "TRENTINO-ALTO-ADIGE", "BOLZANO": "TRENTINO-ALTO-ADIGE",
        "ROVERETO": "TRENTINO-ALTO-ADIGE", "MERANO": "TRENTINO-ALTO-ADIGE",
        # Umbria
        "PERUGIA": "UMBRIA", "TERNI": "UMBRIA",
        "FOLIGNO": "UMBRIA", "SPOLETO": "UMBRIA", "ASSISI": "UMBRIA",
        # Valle d'Aosta
        "AOSTA": "VALLE-D-AOSTA",
        # Veneto
        "VENEZIA": "VENETO", "PADOVA": "VENETO", "VERONA": "VENETO",
        "VICENZA": "VENETO", "TREVISO": "VENETO", "BELLUNO": "VENETO",
        "ROVIGO": "VENETO"
    }
    
    for comune in comuni_target:
        comune_upper = comune.upper().replace("'", "").replace("-", " ")
        
        # 1. Cerca prima per provincia (più veloce e affidabile)
        sigla_provincia = comune[:2].upper() if len(comune) >= 2 else ""
        regione_trovata = provincia_a_regione.get(sigla_provincia)
        
        # 2. Se non trovato, cerca nel nome del comune
        if not regione_trovata:
            for com_key, reg in comune_a_regione.items():
                if com_key.upper() in comune_upper or comune_upper in com_key.upper():
                    regione_trovata = reg
                    break
        
        # 3. Se ancora non trovato, usa Marche come default
        if not regione_trovata:
            regione_trovata = "MARCHE"
            print(f"Attenzione: Non ho trovato la regione per il comune '{comune}'. Usato default: MARCHE")
        
        if regione_trovata:
            regioni_necessarie.add(regione_trovata)
    
    # Scarica la cartografia per tutte le regioni necessarie
    percorsi_zip = []
    for regione in regioni_necessarie:
        percorso = scarica_cartografia_regione(regione)
        if percorso:
            percorsi_zip.append(percorso)
    
    return percorsi_zip

def estrai_gml_ricorsivamente(zip_path, cartella_dest, comuni_target):
    """Estrae ricorsivamente i file GML dai file ZIP, filtrando per comuni target"""
    gml_files = []
    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            for member in z.namelist():
                base_name = os.path.basename(member).upper()
                if member.lower().endswith(".zip"):
                    sub_zip = z.extract(member, cartella_dest)
                    gml_files.extend(estrai_gml_ricorsivamente(sub_zip, cartella_dest, comuni_target))
                    os.remove(sub_zip)
                elif member.lower().endswith(".gml"):
                    # Controlla se il file GML contiene uno dei comuni target
                    if any(com.upper() in base_name for com in comuni_target):
                        gml_files.append(z.extract(member, cartella_dest))
    except Exception as e:
        print(f"Errore estrazione GML da {zip_path}: {e}")
    return gml_files

def plot_gdf_to_file(gdf, out_name, title, focus_bounds=None):
    fig, ax = plt.subplots(figsize=(10, 8))
    
    for _, row in gdf.iterrows():
        geom = row.geometry
        p_clean = str(row['LABEL_CLEAN'])
        
        # Contorno BIANCO per le particelle
        gpd.GeoSeries([geom]).plot(ax=ax, facecolor='none', edgecolor='white', linewidth=2.0)
        
        # Numero al centro BIANCO con ombra NERA
        centroid = geom.centroid
        ax.annotate(p_clean, xy=(centroid.x, centroid.y), color='white', 
                    fontsize=10, ha='center', va='center', fontweight='bold',
                    path_effects=[pe.withStroke(linewidth=3, foreground="black")])
    
    ctx.add_basemap(ax, source=ctx.providers.Esri.WorldImagery)
    
    if focus_bounds:
        ax.set_xlim(focus_bounds[0], focus_bounds[1])
        ax.set_ylim(focus_bounds[2], focus_bounds[3])
        
    ax.set_axis_off()
    ax.set_title(title, color='black', fontweight='bold', fontsize=14)
    plt.tight_layout()
    plt.savefig(out_name, bbox_inches='tight', dpi=150)
    plt.close(fig)

def genera_mappe_fogli(dati_appezzamenti):
    """Genera mappe GIS per tutti i comuni presenti nei dati, supportando multiple regioni"""
    # Organizza i dati per comune e foglio
    target_data = {}
    for app in dati_appezzamenti:
        comune = app['comune'].upper()
        foglio = app['foglio'].lstrip("0")
        part = app['particella'].lstrip("0")
        
        if comune not in target_data: target_data[comune] = {}
        if foglio not in target_data[comune]: target_data[comune][foglio] = set()
        target_data[comune][foglio].add(part)

    if os.path.exists(CARTELLA_ESTRAZIONE_GIS): shutil.rmtree(CARTELLA_ESTRAZIONE_GIS)
    os.makedirs(CARTELLA_ESTRAZIONE_GIS, exist_ok=True)
    
    comuni_target = list(target_data.keys())
    
    # Assicura che la cartografia per tutti i comuni target sia disponibile
    zip_paths = assicura_presenza_cartografia(comuni_target)
    
    # Estrai tutti i file GML dai ZIP scaricati
    all_gml_files = []
    for zip_path in zip_paths:
        if os.path.exists(zip_path):
            all_gml_files.extend(estrai_gml_ricorsivamente(zip_path, CARTELLA_ESTRAZIONE_GIS, comuni_target))
    
    mappe_generate = {} # {(comune, foglio): [lista_immagini]}
    
    for gml_path in all_gml_files:
        if not gml_path.endswith("_ple.gml"): continue
        try:
            gdf = gpd.read_file(gml_path)
            col_ref = "NATIONALCADASTRALREFERENCE" if "NATIONALCADASTRALREFERENCE" in gdf.columns else "NATIONALCADASTRALZONINGREFERENCE"
            
            for comune_t, fogli_dict in target_data.items():
                gml_basename = os.path.basename(gml_path).upper()
                if comune_t not in gml_basename: continue
                
                for foglio_t, particelle_t in fogli_dict.items():
                    foglio_pad = foglio_t.zfill(4)
                    gdf_foglio = gdf[gdf[col_ref].astype(str).str.contains(f"_{foglio_pad}")].copy()
                    if gdf_foglio.empty: continue
                    
                    gdf_foglio["LABEL_CLEAN"] = gdf_foglio["LABEL"].astype(str).str.strip().str.lstrip("0")
                    gdf_part = gdf_foglio[gdf_foglio["LABEL_CLEAN"].isin(particelle_t)]
                    
                    if not gdf_part.empty:
                        gdf_wm = gdf_part.to_crs(epsg=3857)
                        lista_immagini_foglio = []
                        
                        # 1. Mappa Panoramica
                        img_main = f"mappa_{comune_t}_{foglio_t}_main.png".replace(" ", "_")
                        plot_gdf_to_file(gdf_wm, img_main, f"Comune: {comune_t} - Fg: {foglio_t} (Panoramica)")
                        lista_immagini_foglio.append(img_main)
                        
                        # 2. Divisione in Riquadri se l'area e' vasta (>1200m)
                        minx, miny, maxx, maxy = gdf_wm.total_bounds
                        width, height = maxx - minx, maxy - miny
                        
                        if max(width, height) > 1200:
                            midx, midy = minx + width/2, miny + height/2
                            quadrants = [
                                box(minx, midy, midx, maxy), # NW
                                box(midx, midy, maxx, maxy), # NE
                                box(minx, miny, midx, midy), # SW
                                box(midx, miny, maxx, midy)  # SE
                            ]
                            
                            for i, quad in enumerate(quadrants):
                                gdf_sub = gdf_wm[gdf_wm.intersects(quad)]
                                if not gdf_sub.empty:
                                    img_sub = f"mappa_{comune_t}_{foglio_t}_Q{i+1}.png".replace(" ", "_")
                                    fb = [quad.bounds[0]-100, quad.bounds[2]+100, quad.bounds[1]-100, quad.bounds[3]+100]
                                    plot_gdf_to_file(gdf_sub, img_sub, f"Comune: {comune_t} - Fg: {foglio_t} (Riquadro {i+1})", focus_bounds=fb)
                                    lista_immagini_foglio.append(img_sub)
                                    
                        mappe_generate[(comune_t, foglio_t)] = lista_immagini_foglio
        except Exception as e:
            print(f"Errore parsing GML {gml_path}: {e}")

    # Non eliminare la cartella cache, mantieni i file scaricati per il caching
    if os.path.exists(CARTELLA_ESTRAZIONE_GIS): shutil.rmtree(CARTELLA_ESTRAZIONE_GIS)
    return mappe_generate

# ==============================================================================
# 3. COMPILAZIONE REPORT PDF
# ==============================================================================

class ReportCompletoPDF(FPDF):
    def header(self):
        self.set_font('Arial', 'B', 14)
        self.set_text_color(0, 100, 0)
        self.cell(0, 10, 'SCHEDA RILIEVO IN CAMPO - PASCOLI CONNESSI', 0, 1, 'C')
        self.set_draw_color(0, 100, 0)
        self.line(10, 20, 200, 20)
        self.ln(5)

    def footer(self):
        self.set_y(-15)
        self.set_font('Arial', 'I', 8)
        self.set_text_color(128)
        self.cell(0, 10, f'Pagina {self.page_no()}', 0, 0, 'C')

    def titolo_sezione(self, titolo):
        self.set_font('Arial', 'B', 11)
        self.set_fill_color(225, 240, 225) 
        self.set_text_color(0, 0, 0)
        self.cell(0, 10, f" {titolo}", 0, 1, 'L', 1)
        self.ln(4)

def crea_report(dati_anag, dati_zoo, dati_app, mappe_img, out_path="Report_Campo_Pascoli_Laga.pdf"):
    pdf = ReportCompletoPDF()
    pdf.add_page()

    # --- SEZIONE 1: ANAGRAFICA ---
    pdf.titolo_sezione("1. DATI IDENTIFICATIVI DELL'AZIENDA")
    pdf.set_font('Arial', '', 11)
    
    cuaa = dati_anag.get('cuaa', 'N/D')
    if cuaa == "N/D": cuaa = dati_anag.get('partita_iva', 'N/D')
    pdf.cell(0, 8, f"Azienda: {dati_anag.get('denominazione', 'N/D')} - CUAA: {cuaa}", ln=1)
    pdf.ln(2)
    
    # --- SEZIONE 2: ZOOTECNIA ---
    pdf.titolo_sezione("2. CONSISTENZA ZOOTECNICA")
    if not dati_zoo:
        pdf.set_font('Arial', '', 11)
        pdf.cell(0, 8, "Nessun dato zootecnico rilevato nel fascicolo.", ln=1)
    else:
        for z in dati_zoo:
            pdf.set_font('Arial', 'B', 10)
            pdf.cell(0, 6, f"Specie: {z.get('specie_allevata')} ({z.get('tipo_produzione')})", ln=1)
            pdf.set_font('Arial', '', 9)
            pdf.multi_cell(0, 5, f"Fascicolo: {z.get('dettaglio_capi_str')}")
            pdf.set_font('Arial', 'I', 9)
            pdf.cell(0, 6, "Correzione N. Capi Reali: [ ______ ] Note: _________________________________________________", ln=1)
            pdf.ln(3)

    # --- SEZIONE 3: PARTICELLE, RIEPILOGO UTILIZZI E MAPPE ---
    pdf.add_page()
    pdf.titolo_sezione("3. ELENCO PARTICELLE, UTILIZZI E MAPPE (PER COMUNE/FOGLIO)")
    
    mappa_comuni = {}
    for app in dati_app:
        com, fog = app['comune'], app['foglio'].lstrip("0")
        part, sup, uso = app['particella'].lstrip("0"), app['superficie_ha'], app['uso']
        
        if com not in mappa_comuni: mappa_comuni[com] = {}
        if fog not in mappa_comuni[com]: mappa_comuni[com][fog] = {"particelle": [], "riepilogo_uso": {}}
        
        duplicato = False
        for p_esistente in mappa_comuni[com][fog]["particelle"]:
            if p_esistente['part'] == part and p_esistente['uso'] == uso:
                p_esistente['sup'] += sup
                duplicato = True
                break
        
        if not duplicato:
            mappa_comuni[com][fog]["particelle"].append({"part": part, "sup": sup, "uso": uso})
            
        mappa_comuni[com][fog]["riepilogo_uso"][uso] = mappa_comuni[com][fog]["riepilogo_uso"].get(uso, 0.0) + sup

    for comune, fogli in sorted(mappa_comuni.items()):
        for foglio, info in sorted(fogli.items()):
            
            pdf.set_font('Arial', 'B', 12)
            pdf.set_fill_color(240, 240, 240)
            pdf.cell(0, 8, f" COMUNE: {comune} - FOGLIO: {foglio}", border=1, ln=1, fill=True)
            pdf.ln(2)
            
            # Riepilogo Utilizzi del Foglio
            pdf.set_font('Arial', 'B', 10)
            pdf.cell(0, 6, "Riepilogo Utilizzi del Foglio:", ln=1)
            pdf.set_font('Arial', '', 9)
            for u_nome, u_sup in sorted(info["riepilogo_uso"].items()):
                pdf.cell(0, 5, f"- {u_nome}: {u_sup:.4f} Ha", ln=1)
            pdf.ln(2)
            
            # Elenco Particelle
            pdf.set_font('Arial', 'B', 10)
            pdf.cell(0, 6, "Dettaglio Particelle:", ln=1)
            pdf.set_font('Arial', '', 9)
            for p in sorted(info["particelle"], key=lambda x: str(x['part'])):
                pdf.cell(0, 5, f" Particella {p['part']:<8} |  Superficie: {p['sup']:>8.4f} Ha  |  Utilizzo: {p['uso']}", ln=1)
            pdf.ln(4)
            
            # Mappe (A larghezza di pagina)
            imgs_per_foglio = mappe_img.get((comune, foglio), [])
            if not imgs_per_foglio:
                pdf.set_font('Arial', 'I', 9)
                pdf.cell(0, 6, "[Nessuna cartografia GIS trovata per questo foglio]", ln=1)
                pdf.ln(5)
            else:
                for img_path in imgs_per_foglio:
                    if pdf.get_y() > 160: pdf.add_page()
                    pdf.image(img_path, x=10, w=190) # Larghezza piena
                    os.remove(img_path)
                    pdf.ln(5)
            
            pdf.set_draw_color(150, 150, 150)
            pdf.line(10, pdf.get_y(), 200, pdf.get_y())
            pdf.ln(8)

    pdf.output(out_path)
    return out_path

# ==============================================================================
# 4. INTERFACCIA GRADIO
# ==============================================================================


# ==============================================================================
# 4. GESTIONE ERRORI
# ==============================================================================

class ElaborazioneError(Exception):
    """Eccezione personalizzata per errori di elaborazione"""
    pass


class FileInvalidError(ElaborazioneError):
    """File non valido o non supportato"""
    pass


class DatiMancantiError(ElaborazioneError):
    """Dati necessari mancanti nel fascicolo"""
    pass


class DownloadError(ElaborazioneError):
    """Errore nel download della cartografia"""
    pass


class GISError(ElaborazioneError):
    """Errore nell'elaborazione GIS"""
    pass


def valida_file_pdf(file_path):
    """Valida che il file sia un PDF valido e leggibile"""
    if not file_path:
        raise FileInvalidError("Nessun file fornito")
    
    if not file_path.lower().endswith('.pdf'):
        raise FileInvalidError("Il file deve essere in formato PDF")
    
    try:
        with pdfplumber.open(file_path) as pdf:
            if len(pdf.pages) == 0:
                raise FileInvalidError("Il file PDF e vuoto o corrotto")
    except Exception as e:
        raise FileInvalidError(f"Errore nella lettura del PDF: {str(e)}")


def elabora_tutto_ui(file_pdf):
    """Funzione principale per l'elaborazione tramite interfaccia Gradio"""
    if file_pdf is None:
        return "\u26a0\ufe0f **Errore:** Nessun file caricato. Seleziona un file PDF valido.", None
        
    try:
        # Validazione file
        valida_file_pdf(file_pdf.name)
        
        # Estrazione dati
        with pdfplumber.open(file_pdf.name) as pdf:
            if len(pdf.pages) < 1:
                raise FileInvalidError("Il PDF deve contenere almeno una pagina")
            
            anag = estrai_anagrafica(pdf)
            if not anag or anag.get('cuaa') == 'N/D' and anag.get('partita_iva') == 'N/D':
                raise DatiMancantiError("Non sono stati trovati dati anagrafici validi (CUAA o Partita IVA)")
            
            zoo = estrai_composizione_zootecnica(pdf)
            appz = estrai_piano_coltivazione(pdf)
        
        if not appz:
            raise DatiMancantiError("Non sono state trovate particelle catastali nel fascicolo")
        
        # Generazione mappe
        mappe = genera_mappe_fogli(appz)
        
        # Creazione report
        out_name = "Report_Campo_Pascoli_Connessi.pdf"
        crea_report(anag, zoo, appz, mappe, out_name)
        
        # Statistiche finali
        num_comuni = len(set(app['comune'] for app in appz))
        num_particelle = len(appz)
        sup_totale = sum(app['superficie_ha'] for app in appz)
        
        messaggio_successo = (
            f"\u2705 **Elaborazione completata con successo!**\n\n"
            f"\u2139 **Statistiche:**\n"
            f"- Comuni: {num_comuni}\n"
            f"- Particelle: {num_particelle}\n"
            f"- Superficie totale: {sup_totale:.2f} Ha\n\n"
            f"Scarica il PDF dal riquadro sottostante."
        )
        return messaggio_successo, out_name
        
    except FileInvalidError as e:
        return f"\u274c **Errore di file:** {str(e)}", None
    except DatiMancantiError as e:
        return f"\u274c **Dati mancanti:** {str(e)}. Verifica che il fascicolo contenga tutte le sezioni necessarie.", None
    except DownloadError as e:
        return f"\u274c **Errore di download:** {str(e)}. Verifica la connessione internet.", None
    except GISError as e:
        return f"\u274c **Errore GIS:** {str(e)}. Potrebbe essere necessario scaricare manualmente la cartografia.", None
    except Exception as e:
        return f"\u274c **Errore generico:** {str(e)}. Contatta l'amministratore se il problema persiste.", None

with gr.Blocks(title="Generatore Report Pascoli", theme=gr.themes.Soft()) as app:
    gr.Markdown("""
    # \ud83d\udcc4 Generatore Report Pascoli Connessi
    ### Applicazione per l'estrazione automatica di dati dal Fascicolo Aziendale
    """)
    
    with gr.Row():
        with gr.Column(scale=3):
            gr.Markdown("""
            **Funzionalita:**
            - \u2705 Estrazione automatica di dati anagrafici, zootecnici e catastali
            - \u2705 Generazione di mappe GIS georeferenziate per tutti i comuni italiani
            - \u2705 Caching automatico della cartografia per evitare download ripetuti
            - \u2705 Creazione di report PDF professionali con riepiloghi e mappe
            """)
        
        with gr.Column(scale=1):
            gr.Markdown("""
            **Istruzioni:**
            1. Carica il Fascicolo Aziendale in PDF
            2. Clicca su "Genera Report"
            3. Attendi il completamento
            4. Scarica il report generato
            """)
    
    gr.Markdown("---")
    
    with gr.Row():
        input_pdf = gr.File(label="\ud83d\udce5 1. Carica Fascicolo Aziendale (PDF)", file_types=[".pdf"])
    
    with gr.Row():
        btn_genera = gr.Button("\u25b6\ufe0f 2. Genera Report Completo", variant="primary")
        btn_pulisce = gr.Button("\ud83d\udd04 Pulisci", variant="secondary")
    
    gr.Markdown("---")
    
    with gr.Row():
        with gr.Column(scale=2):
            status_testo = gr.Markdown("\u23f3 *In attesa di caricamento del file...*")
        with gr.Column(scale=1):
            log_output = gr.Textbox(label="Log Dettagliato", lines=8, max_lines=10, interactive=False)
    
    with gr.Row():
        output_pdf = gr.File(label="\ud83d\udce5 3. Scarica Report PDF Generato")
    
    # Funzione per pulire i campi
    def pulisci_campi():
        return None, "\u23f3 *Pronto per un nuovo file...", ""
    
    # Funzione wrapper con progress e log
    def elabora_con_progress(file_pdf):
        if file_pdf is None:
            return "\u26a0\ufe0f **Errore:** Nessun file caricato.", None, ""
        
        try:
            # Validazione file
            valida_file_pdf(file_pdf.name)
            log_msg = "\u2713 File PDF valido\n"
            yield 0.2, "File valido. Estrazione dati anagrafici...", log_msg
            
            # Estrazione dati
            with pdfplumber.open(file_pdf.name) as pdf:
                anag = estrai_anagrafica(pdf)
                log_msg += "\u2713 Dati anagrafici estratti\n"
                yield 0.4, "Estrazione composizione zootecnica...", log_msg
                
                if not anag or anag.get('cuaa') == 'N/D' and anag.get('partita_iva') == 'N/D':
                    raise DatiMancantiError("Non sono stati trovati dati anagrafici validi")
                
                zoo = estrai_composizione_zootecnica(pdf)
                log_msg += "\u2713 Dati zootecnici estratti\n"
                yield 0.5, "Estrazione piano di coltivazione...", log_msg
                
                appz = estrai_piano_coltivazione(pdf)
                log_msg += "\u2713 Dati catastali estratti\n"
                yield 0.6, "Generazione mappe GIS...", log_msg
            
            if not appz:
                raise DatiMancantiError("Non sono state trovate particelle catastali")
            
            mappe = genera_mappe_fogli(appz)
            log_msg += "\u2713 Mappe GIS generate\n"
            yield 0.8, "Creazione report PDF...", log_msg
            
            out_name = "Report_Campo_Pascoli_Connessi.pdf"
            crea_report(anag, zoo, appz, mappe, out_name)
            log_msg += "\u2713 Report PDF creato\n"
            yield 0.95, "Finalizzazione...", log_msg
            
            # Statistiche
            num_comuni = len(set(app['comune'] for app in appz))
            num_particelle = len(appz)
            sup_totale = sum(app['superficie_ha'] for app in appz)
            
            messaggio_successo = (
                f"\u2705 **Elaborazione completata con successo!**\n\n"
                f"\u2139 **Statistiche:**\n"
                f"- Comuni: {num_comuni}\n"
                f"- Particelle: {num_particelle}\n"
                f"- Superficie totale: {sup_totale:.2f} Ha\n\n"
                f"Scarica il PDF dal riquadro sottostante."
            )
            
            log_msg += "\u2713 Elaborazione completata\nTutti i dati sono stati processati correttamente"
            yield 1.0, messaggio_successo, log_msg
            return out_name
            
        except FileInvalidError as e:
            err_msg = f"\u274c **Errore di file:** {str(e)}"
            yield 1.0, err_msg, f"ERR: {str(e)}"
            return None
        except DatiMancantiError as e:
            err_msg = f"\u274c **Dati mancanti:** {str(e)}"
            yield 1.0, err_msg, f"ERR: {str(e)}"
            return None
        except DownloadError as e:
            err_msg = f"\u274c **Errore di download:** {str(e)}"
            yield 1.0, err_msg, f"ERR: {str(e)}"
            return None
        except GISError as e:
            err_msg = f"\u274c **Errore GIS:** {str(e)}"
            yield 1.0, err_msg, f"ERR: {str(e)}"
            return None
        except Exception as e:
            err_msg = f"\u274c **Errore generico:** {str(e)}"
            yield 1.0, err_msg, f"ERR: {str(e)}"
            return None
    
    # Event handlers
    btn_genera.click(
        fn=elabora_con_progress,
        inputs=input_pdf,
        outputs=[status_testo, output_pdf, log_output]
    )
    
    btn_pulisce.click(
        fn=pulisci_campi,
        inputs=[],
        outputs=[input_pdf, status_testo, log_output]
    )

if __name__ == "__main__":
    app.launch()
