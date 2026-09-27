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
# CONFIGURAZIONE GIS E COSTANTI
# ==============================================================================
PATH_ZIP_CARTOGRAFIA = "MARCHE.zip"
URL_ZIP_MARCHE = "https://wfs.cartografia.agenziaentrate.gov.it/inspire/wfs/GetDataset.php?dataset=MARCHE.zip"
CARTELLA_ESTRAZIONE_GIS = "./cartografia_estratta_temp"
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

def assicura_presenza_zip():
    if not os.path.exists(PATH_ZIP_CARTOGRAFIA):
        try:
            print("Download cartografia WFS regionale in corso...")
            response = requests.get(URL_ZIP_MARCHE, stream=True, timeout=120)
            with open(PATH_ZIP_CARTOGRAFIA, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk: f.write(chunk)
        except Exception as e:
            print(f"Errore download WFS: {e}")

def estrai_gml_ricorsivamente(zip_path, cartella_dest, comuni_target):
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
                    if any(com in base_name for com in comuni_target):
                        gml_files.append(z.extract(member, cartella_dest))
    except: pass
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
    assicura_presenza_zip()
    
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
    gml_files = estrai_gml_ricorsivamente(PATH_ZIP_CARTOGRAFIA, CARTELLA_ESTRAZIONE_GIS, comuni_target)
    
    mappe_generate = {} # {(comune, foglio): [lista_immagini]}
    
    for gml_path in gml_files:
        if not gml_path.endswith("_ple.gml"): continue
        try:
            gdf = gpd.read_file(gml_path)
            col_ref = "NATIONALCADASTRALREFERENCE" if "NATIONALCADASTRALREFERENCE" in gdf.columns else "NATIONALCADASTRALZONINGREFERENCE"
            
            for comune_t, fogli_dict in target_data.items():
                if comune_t not in os.path.basename(gml_path).upper(): continue
                
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

def elabora_tutto_ui(file_pdf):
    if file_pdf is None: 
        return "⚠️ Errore: Nessun file caricato.", None
        
    try:
        with pdfplumber.open(file_pdf.name) as pdf:
            anag = estrai_anagrafica(pdf)
            zoo = estrai_composizione_zootecnica_textmode(pdf)
            appz = estrai_piano_coltivazione(pdf)
        
        mappe = genera_mappe_fogli(appz)
        
        out_name = "Report_Campo_Pascoli_Connessi.pdf"
        crea_report(anag, zoo, appz, mappe, out_name)
        
        return "✅ **Elaborazione completata con successo!** Scarica il PDF dal riquadro sottostante.", out_name
    except Exception as e:
        return f"❌ **Errore durante l'elaborazione:** {str(e)}", None

with gr.Blocks(title="Generatore Report Pascoli Laga", theme=gr.themes.Soft()) as app:
    gr.Markdown("### 📄 Generazione Automatica Report di Campo (Pascoli Connessi)")
    gr.Markdown("L'applicazione estrae le informazioni dal Fascicolo Aziendale[span_0](start_span)[span_0](end_span), calcola il **riepilogo degli utilizzi per ogni foglio** e genera le **mappe satellitari georeferenziate** a larghezza piena con dettagli a riquadro.")
    
    with gr.Row():
        input_pdf = gr.File(label="1. Carica Fascicolo Aziendale (PDF)", file_types=[".pdf"])
    
    btn = gr.Button("2. Genera Report Completo", variant="primary")
    
    gr.Markdown("---")
    
    status_testo = gr.Markdown("⏳ *In attesa di caricamento del file...*")
    output_pdf = gr.File(label="3. 📥 Link per il Download del Report PDF")
    
    btn.click(fn=elabora_tutto_ui, inputs=input_pdf, outputs=[status_testo, output_pdf])

if __name__ == "__main__":
    app.launch()
