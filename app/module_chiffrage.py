"""
=============================================================================
 MODULE CHIFFRAGE CVC — SCAFFOLD
=============================================================================
 pip install streamlit pandas fpdf2 openpyxl sqlalchemy psycopg2-binary
 streamlit run module_chiffrage.py

 - Bibliothèque de prix éditable (+ import/export, recherche, taux par ouvrage)
 - Ouvrages composés (ensembles)
 - Base clients
 - Devis : numérotation auto sans doublon, sauvegarde, duplication, statut
 - Suivi des devis (statut En attente / Accepté / Refusé) + export Excel
 - Bloc "Mon entreprise" + logo sur le PDF
 - PDF groupé par ensemble avec sous-totaux, mentions légales BTP

 STOCKAGE : base Postgres persistante hébergée sur Supabase, atteinte via
 SQLAlchemy. La chaîne de connexion vit dans st.secrets["DB_URL"], jamais
 dans le code ni sur GitHub.
=============================================================================
"""

import json
from collections import OrderedDict
from datetime import date
from io import BytesIO
from pathlib import Path

import pandas as pd
import streamlit as st
from fpdf import FPDF
from sqlalchemy import create_engine, text

DB_PATH = Path("chiffrage.db")
ENTREPRISE_PATH = Path("entreprise.json")
LOGO_PATH = Path("logo.png")

COLONNES = ["code", "designation", "unite", "prix_fourniture", "temps_mo", "nb_poseurs", "taux_horaire"]
UNITES = ["u", "ml", "m2", "m3", "h", "ens", "forfait", "kg"]
CLIENT_COLS = ["nom", "adresse", "telephone", "email", "siret"]
DEVIS_COLS = ["numero", "date_devis", "client_nom", "client_adresse", "date_debut",
              "duree", "marge_pct", "tva_pct", "lignes_json", "statut"]
STATUTS = ["En attente", "Accepté", "Refusé"]
ENTREPRISE_CHAMPS = [
    "raison_sociale", "forme_juridique", "adresse", "siret", "tva_intra",
    "assurance_nom", "assurance_police", "assurance_couverture",
    "conditions_reglement", "validite_jours",
]


# ----------------------------------------------------------------------------
# STOCKAGE  (Supabase / Postgres via SQLAlchemy)
# ----------------------------------------------------------------------------
@st.cache_resource
def get_engine():
    db = st.secrets["db"]
    url = (
        f"postgresql+psycopg2://{db['user']}:{db['password']}"
        f"@{db['host']}:{db['port']}/{db['dbname']}"
    )
    return create_engine(url, pool_pre_ping=True)

def init_db():
    eng = get_engine()
    with eng.begin() as con:
        con.execute(text("""CREATE TABLE IF NOT EXISTS ouvrages (
            code TEXT, designation TEXT, unite TEXT,
            prix_fourniture DOUBLE PRECISION, temps_mo DOUBLE PRECISION,
            nb_poseurs DOUBLE PRECISION, taux_horaire DOUBLE PRECISION)"""))
        con.execute(text("""CREATE TABLE IF NOT EXISTS ensembles (
            nom TEXT, designation TEXT, quantite DOUBLE PRECISION)"""))
        con.execute(text("""CREATE TABLE IF NOT EXISTS clients (
            nom TEXT, adresse TEXT, telephone TEXT, email TEXT, siret TEXT)"""))
        con.execute(text("""CREATE TABLE IF NOT EXISTS devis (
            numero TEXT, date_devis TEXT, client_nom TEXT, client_adresse TEXT,
            date_debut TEXT, duree TEXT, marge_pct DOUBLE PRECISION,
            tva_pct DOUBLE PRECISION, lignes_json TEXT, statut TEXT)"""))


def _lire(table, cols):
    eng = get_engine()
    try:
        df = pd.read_sql_query(f"SELECT * FROM {table}", eng)
    except Exception:
        return pd.DataFrame(columns=cols)
    return df if not df.empty else pd.DataFrame(columns=cols)


def _ecrire(table, df):
    eng = get_engine()
    df.to_sql(table, eng, if_exists="replace", index=False)


def charger_bibliotheque():
    return _lire("ouvrages", COLONNES)


def enregistrer_bibliotheque(df):
    df = df.copy()
    for c in COLONNES:
        if c not in df.columns:
            df[c] = None
    _ecrire("ouvrages", df[COLONNES])


def charger_ensembles():
    return _lire("ensembles", ["nom", "designation", "quantite"])


def enregistrer_ensemble(nom, composants):
    tous = charger_ensembles()
    tous = tous[tous["nom"] != nom]
    nouveaux = composants.copy()
    nouveaux = nouveaux[nouveaux["designation"].notna() & (nouveaux["designation"] != "")]
    nouveaux["nom"] = nom
    _ecrire("ensembles", pd.concat([tous, nouveaux[["nom", "designation", "quantite"]]], ignore_index=True))


def charger_clients():
    return _lire("clients", CLIENT_COLS)


def enregistrer_clients(df):
    df = df.copy()
    for c in CLIENT_COLS:
        if c not in df.columns:
            df[c] = None
    df = df[df["nom"].notna() & (df["nom"] != "")]
    _ecrire("clients", df[CLIENT_COLS])


def charger_devis():
    df = _lire("devis", DEVIS_COLS)
    if "statut" not in df.columns:
        df["statut"] = "En attente"
    df["statut"] = df["statut"].fillna("En attente")
    return df


def sauvegarder_devis(record) -> bool:
    df = charger_devis()
    if record["numero"] in df["numero"].values:
        return False
    _ecrire("devis", pd.concat([df, pd.DataFrame([record])], ignore_index=True))
    return True


def maj_statuts(numero_to_statut: dict):
    df = charger_devis()
    df["statut"] = df["numero"].map(numero_to_statut).fillna(df["statut"])
    _ecrire("devis", df)


def prochain_numero() -> str:
    annee = date.today().year
    prefixe = f"DEV-{annee}-"
    df = charger_devis()
    nums = [int(n[len(prefixe):]) for n in df["numero"]
            if isinstance(n, str) and n.startswith(prefixe) and n[len(prefixe):].isdigit()]
    return f"{prefixe}{(max(nums) + 1) if nums else 1:03d}"


def charger_entreprise():
    base = {c: "" for c in ENTREPRISE_CHAMPS}
    base["validite_jours"] = "30"
    if ENTREPRISE_PATH.exists():
        try:
            base.update(json.loads(ENTREPRISE_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return base


def enregistrer_entreprise(d):
    ENTREPRISE_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def to_excel_bytes(df) -> bytes:
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False)
    return buf.getvalue()


# ----------------------------------------------------------------------------
# CALCUL
# ----------------------------------------------------------------------------
def prix_mo(temps_mo, nb_poseurs, taux):
    return (temps_mo or 0) * (nb_poseurs or 1) * taux


def prix_unitaire(ligne, taux_global):
    taux = ligne.get("taux_horaire") or taux_global
    return (ligne.get("prix_fourniture") or 0) + prix_mo(ligne.get("temps_mo"), ligne.get("nb_poseurs"), taux)


def total_devis_ttc(rec) -> float:
    try:
        lignes = json.loads(rec["lignes_json"]) if rec.get("lignes_json") else []
    except Exception:
        lignes = []
    sous = sum((l.get("montant_ht") or 0) for l in lignes)
    total_ht = sous * (1 + (rec.get("marge_pct") or 0) / 100)
    return total_ht * (1 + (rec.get("tva_pct") or 0) / 100)


# ----------------------------------------------------------------------------
# PDF
# ----------------------------------------------------------------------------
def _txt(s):
    s = str(s)
    for a, b in {"€": "EUR", "œ": "oe", "Œ": "OE", "’": "'", "–": "-",
                 "—": "-", "•": "-", "…": "..."}.items():
        s = s.replace(a, b)
    return s.encode("latin-1", "replace").decode("latin-1")


def _ligne_tableau(pdf, designation, unite, qte, pu, montant, euro):
    pdf.cell(70, 7, _txt(str(designation)[:42]), border=1)
    pdf.cell(15, 7, _txt(unite), border=1, align="C")
    pdf.cell(20, 7, f"{qte:g}", border=1, align="C")
    pdf.cell(35, 7, euro(pu), border=1, align="R")
    pdf.cell(40, 7, euro(montant), border=1, align="R")
    pdf.ln()


def generer_pdf_devis(entreprise, infos, lignes, totaux):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    def euro(x):
        return f"{x:,.2f} EUR".replace(",", " ")

    # Logo (haut droite) si présent
    if LOGO_PATH.exists():
        try:
            pdf.image(str(LOGO_PATH), x=150, y=8, w=45)
        except Exception:
            pass

    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 7, _txt(entreprise.get("raison_sociale") or "Mon entreprise"), ln=True)
    pdf.set_font("Helvetica", "", 9)
    for info in [entreprise.get("forme_juridique"), entreprise.get("adresse"),
                 f"SIRET : {entreprise.get('siret')}" if entreprise.get("siret") else None,
                 f"TVA intra : {entreprise.get('tva_intra')}" if entreprise.get("tva_intra") else None]:
        if info:
            pdf.cell(0, 5, _txt(info), ln=True)
    pdf.ln(3)

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 9, "DEVIS", ln=True)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 5, _txt(f"Devis n° {infos.get('numero', '')}"), ln=True)
    pdf.cell(0, 5, _txt(f"Date : {infos.get('date', '')}"), ln=True)
    if infos.get("validite_jours"):
        pdf.cell(0, 5, _txt(f"Validité de l'offre : {infos['validite_jours']} jours"), ln=True)
    pdf.ln(2)

    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 5, "Client", ln=True)
    pdf.set_font("Helvetica", "", 10)
    if infos.get("client"):
        pdf.cell(0, 5, _txt(infos["client"]), ln=True)
    if infos.get("client_adresse"):
        pdf.multi_cell(0, 5, _txt(infos["client_adresse"]))
    if infos.get("date_debut") or infos.get("duree"):
        pdf.cell(0, 5, _txt(f"Début des travaux : {infos.get('date_debut') or '-'}   "
                            f"Durée estimée : {infos.get('duree') or '-'}"), ln=True)
    pdf.ln(2)

    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(230, 230, 230)
    pdf.cell(70, 7, "Désignation", border=1, fill=True)
    pdf.cell(15, 7, "Unité", border=1, fill=True, align="C")
    pdf.cell(20, 7, "Qté", border=1, fill=True, align="C")
    pdf.cell(35, 7, "P.U. HT", border=1, fill=True, align="R")
    pdf.cell(40, 7, "Montant HT", border=1, fill=True, align="R")
    pdf.ln()

    groupes = OrderedDict()
    for lg in lignes:
        groupes.setdefault(lg.get("groupe") or "", []).append(lg)
    pdf.set_font("Helvetica", "", 9)
    for nom_groupe, items in groupes.items():
        if nom_groupe:
            pdf.set_font("Helvetica", "B", 9)
            pdf.set_fill_color(245, 245, 245)
            pdf.cell(180, 7, _txt(nom_groupe), border=1, fill=True, ln=True)
            pdf.set_font("Helvetica", "", 9)
        for lg in items:
            _ligne_tableau(pdf, lg["designation"], lg["unite"], lg["quantite"],
                           lg["prix_unitaire"], lg["montant_ht"], euro)
        if nom_groupe:
            sous = sum(lg["montant_ht"] for lg in items)
            pdf.set_font("Helvetica", "I", 9)
            pdf.cell(140, 6, _txt(f"Sous-total {nom_groupe}"), align="R")
            pdf.cell(40, 6, euro(sous), align="R", ln=True)
            pdf.set_font("Helvetica", "", 9)

    pdf.ln(2)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(140, 7, "Sous-total HT", align="R")
    pdf.cell(40, 7, euro(totaux["sous_total_ht"]), align="R", ln=True)
    if totaux.get("marge_pct"):
        pdf.cell(140, 7, _txt(f"Marge ({totaux['marge_pct']:g} %)"), align="R")
        pdf.cell(40, 7, euro(totaux["total_ht"] - totaux["sous_total_ht"]), align="R", ln=True)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(140, 7, "Total HT", align="R")
    pdf.cell(40, 7, euro(totaux["total_ht"]), align="R", ln=True)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(140, 7, _txt(f"TVA ({totaux['tva_pct']:g} %)"), align="R")
    pdf.cell(40, 7, euro(totaux["montant_tva"]), align="R", ln=True)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(140, 8, "TOTAL TTC", align="R")
    pdf.cell(40, 8, euro(totaux["total_ttc"]), align="R", ln=True)

    if entreprise.get("conditions_reglement"):
        pdf.ln(3)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(0, 5, "Conditions de règlement", ln=True)
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(0, 5, _txt(entreprise["conditions_reglement"]))
    if entreprise.get("assurance_nom"):
        pdf.ln(2)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(0, 5, "Assurance décennale", ln=True)
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(0, 5, _txt(f"{entreprise.get('assurance_nom')} - Police n° "
                                  f"{entreprise.get('assurance_police', '')} - Couverture : "
                                  f"{entreprise.get('assurance_couverture', '')}"))
    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 8)
    pdf.multi_cell(0, 4, _txt("Devis reçu avant l'exécution des travaux, lu et accepté, bon pour accord."))
    pdf.ln(8)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, "Date et signature du client :", ln=True)
    return bytes(pdf.output())


def check_password():
    def password_entered():
        if st.session_state["password"] == st.secrets["ACCESS_CODE"]:
            st.session_state["password_ok"] = True
            del st.session_state["password"]
        else:
            st.session_state["password_ok"] = False

    if st.session_state.get("password_ok"):
        return True
    st.text_input("Code d'accès", type="password",
                  on_change=password_entered, key="password")
    if st.session_state.get("password_ok") is False:
        st.error("Code incorrect.")
    return False


# ----------------------------------------------------------------------------
# INTERFACE
# ----------------------------------------------------------------------------
def main():
    st.set_page_config(page_title="Chiffrage CVC", layout="wide")
    if not check_password():
        st.stop()
    init_db()
    if "devis_lignes" not in st.session_state:
        st.session_state.devis_lignes = []
    if "numero_courant" not in st.session_state:
        st.session_state.numero_courant = prochain_numero()

    st.title("Module chiffrage CVC")

    with st.sidebar:
        st.header("Réglages généraux")
        taux_horaire = st.number_input("Taux horaire par défaut (€/h)", min_value=0.0, value=45.0, step=1.0)
        st.caption("Taux par défaut. Tu peux en fixer un par ouvrage dans la biblio. "
                   "Coût MO = temps pose × nb poseurs × taux.")
        ent = charger_entreprise()
        with st.expander("🏢 Mon entreprise (à remplir une fois)"):
            if LOGO_PATH.exists():
                st.image(str(LOGO_PATH), width=120, caption="Logo actuel")
            logo_file = st.file_uploader("Logo (PNG / JPG)", type=["png", "jpg", "jpeg"], key="logo_up")
            ent["raison_sociale"] = st.text_input("Raison sociale", ent["raison_sociale"])
            ent["forme_juridique"] = st.text_input("Forme juridique", ent["forme_juridique"])
            ent["adresse"] = st.text_area("Adresse", ent["adresse"])
            ent["siret"] = st.text_input("SIRET", ent["siret"])
            ent["tva_intra"] = st.text_input("N° TVA intracommunautaire", ent["tva_intra"])
            ent["assurance_nom"] = st.text_input("Assureur décennale", ent["assurance_nom"])
            ent["assurance_police"] = st.text_input("N° police décennale", ent["assurance_police"])
            ent["assurance_couverture"] = st.text_input("Couverture géographique", ent["assurance_couverture"])
            ent["conditions_reglement"] = st.text_area("Conditions de règlement", ent["conditions_reglement"])
            ent["validite_jours"] = st.text_input("Validité du devis (jours)", ent["validite_jours"] or "30")
            if st.button("💾 Enregistrer mon entreprise"):
                enregistrer_entreprise(ent)
                if logo_file is not None:
                    LOGO_PATH.write_bytes(logo_file.getvalue())
                st.success("Infos entreprise enregistrées.")
                st.rerun()

    t_biblio, t_ens, t_clients, t_devis, t_suivi = st.tabs(
        ["📚 Bibliothèque", "🧩 Ouvrages composés", "👥 Clients", "🧾 Nouveau devis", "📊 Suivi"])

    # ---- BIBLIOTHÈQUE ----
    with t_biblio:
        st.subheader("Mes ouvrages et prix")
        with st.expander("📥 Importer / exporter"):
            modele = pd.DataFrame([["P001", "Radiateur acier 1000W", "u", 120.0, 1.5, 1, None]], columns=COLONNES)
            st.download_button("Télécharger le modèle CSV", modele.to_csv(index=False).encode("utf-8"),
                               "modele_bibliotheque_prix.csv", "text/csv")
            st.download_button("⬇️ Exporter la biblio (Excel)", to_excel_bytes(charger_bibliotheque()),
                               "bibliotheque.xlsx",
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            fichier = st.file_uploader("Importer (.csv ou .xlsx)", type=["csv", "xlsx"])
            remplacer = st.checkbox("Remplacer la bibliothèque existante", value=False)
            if fichier and st.button("Importer"):
                df_import = pd.read_csv(fichier) if fichier.name.endswith(".csv") else pd.read_excel(fichier)
                essentielles = ["code", "designation", "unite", "prix_fourniture", "temps_mo"]
                manquantes = [c for c in essentielles if c not in df_import.columns]
                if manquantes:
                    st.error(f"Colonnes manquantes : {manquantes}")
                else:
                    if "nb_poseurs" not in df_import.columns:
                        df_import["nb_poseurs"] = 1
                    if "taux_horaire" not in df_import.columns:
                        df_import["taux_horaire"] = None
                    base = pd.DataFrame(columns=COLONNES) if remplacer else charger_bibliotheque()
                    enregistrer_bibliotheque(pd.concat([base, df_import[COLONNES]], ignore_index=True))
                    st.success(f"{len(df_import)} ouvrage(s) importé(s).")
                    st.rerun()

        recherche = st.text_input("🔍 Rechercher un ouvrage (désignation ou code)")
        biblio_full = charger_bibliotheque()
        if recherche:
            f = biblio_full[
                biblio_full["designation"].str.contains(recherche, case=False, na=False)
                | biblio_full["code"].str.contains(recherche, case=False, na=False)]
            st.caption(f"{len(f)} résultat(s) :")
            st.dataframe(f, use_container_width=True)

        edite = st.data_editor(
            biblio_full, num_rows="dynamic", use_container_width=True,
            column_config={
                "code": "Code", "designation": "Désignation",
                "unite": st.column_config.SelectboxColumn("Unité", options=UNITES),
                "prix_fourniture": st.column_config.NumberColumn("Prix fourniture (€)", format="%.2f"),
                "temps_mo": st.column_config.NumberColumn("Temps pose (h)", format="%.2f"),
                "nb_poseurs": st.column_config.NumberColumn("Nb poseurs", format="%d", default=1),
                "taux_horaire": st.column_config.NumberColumn("Taux horaire (€/h)", format="%.2f",
                                                              help="Vide = taux par défaut")},
            key="editeur_biblio")
        if st.button("💾 Enregistrer la bibliothèque"):
            enregistrer_bibliotheque(edite)
            st.success("Bibliothèque enregistrée.")

    # ---- OUVRAGES COMPOSÉS ----
    with t_ens:
        st.subheader("Ouvrages composés (ensembles)")
        biblio = charger_bibliotheque()
        if biblio.empty:
            st.warning("Ajoute d'abord des ouvrages dans la bibliothèque.")
        else:
            designations = biblio["designation"].dropna().tolist()
            ens_all = charger_ensembles()
            noms = sorted(ens_all["nom"].unique().tolist())
            choix = st.selectbox("Ensemble", ["+ Nouvel ensemble"] + noms)
            nom = st.text_input("Nom de l'ensemble", value="" if choix == "+ Nouvel ensemble" else choix)
            comps = (pd.DataFrame(columns=["designation", "quantite"]) if choix == "+ Nouvel ensemble"
                     else ens_all[ens_all["nom"] == choix][["designation", "quantite"]].reset_index(drop=True))
            comps_edit = st.data_editor(
                comps, num_rows="dynamic", use_container_width=True,
                column_config={
                    "designation": st.column_config.SelectboxColumn("Ouvrage", options=designations),
                    "quantite": st.column_config.NumberColumn("Quantité", format="%.2f", default=1.0)},
                key="editeur_ensemble")
            if st.button("💾 Enregistrer l'ensemble"):
                if not nom.strip():
                    st.error("Donne un nom à l'ensemble.")
                else:
                    enregistrer_ensemble(nom.strip(), comps_edit)
                    st.success(f"Ensemble « {nom} » enregistré.")
                    st.rerun()

    # ---- CLIENTS ----
    with t_clients:
        st.subheader("Base clients")
        edite_c = st.data_editor(
            charger_clients(), num_rows="dynamic", use_container_width=True,
            column_config={
                "nom": "Nom / Raison sociale", "adresse": "Adresse",
                "telephone": "Téléphone", "email": "Email", "siret": "SIRET (si pro)"},
            key="editeur_clients")
        if st.button("💾 Enregistrer les clients"):
            enregistrer_clients(edite_c)
            st.success("Clients enregistrés.")

    # ---- DEVIS ----
    with t_devis:
        biblio = charger_bibliotheque()
        if biblio.empty:
            st.warning("La bibliothèque est vide.")
            st.stop()

        saved = charger_devis()
        with st.expander("📋 Repartir d'un devis existant (dupliquer)"):
            if saved.empty:
                st.caption("Aucun devis enregistré.")
            else:
                libelles = {r["numero"]: f"{r['numero']} — {r['client_nom'] or ''} — {r['date_devis']}"
                            for _, r in saved.iterrows()}
                src = st.selectbox("Devis à dupliquer", saved["numero"].tolist(),
                                   format_func=lambda n: libelles.get(n, n))
                if st.button("Dupliquer ce devis"):
                    rec = saved[saved["numero"] == src].iloc[0]
                    st.session_state.devis_lignes = json.loads(rec["lignes_json"]) if rec["lignes_json"] else []
                    st.session_state["f_client_nom"] = rec["client_nom"] or ""
                    st.session_state["f_client_adresse"] = rec["client_adresse"] or ""
                    st.session_state["f_date_debut"] = rec["date_debut"] or ""
                    st.session_state["f_duree"] = rec["duree"] or ""
                    st.session_state.numero_courant = prochain_numero()
                    st.rerun()

        clients = charger_clients()
        if not clients.empty:
            cc = st.columns([3, 1])
            sel_cli = cc[0].selectbox("Pré-remplir depuis la base clients", ["—"] + clients["nom"].tolist())
            if cc[1].button("Utiliser", use_container_width=True) and sel_cli != "—":
                row = clients[clients["nom"] == sel_cli].iloc[0]
                st.session_state["f_client_nom"] = sel_cli
                st.session_state["f_client_adresse"] = row["adresse"] or ""
                st.rerun()

        col1, col2, col3 = st.columns(3)
        col1.text_input("N° de devis (auto)", value=st.session_state.numero_courant, disabled=True)
        date_devis = col2.date_input("Date", value=date.today())
        col3.empty()
        col4, col5, col6 = st.columns(3)
        client_nom = col4.text_input("Nom du client", key="f_client_nom")
        client_adresse = col4.text_area("Adresse du client", key="f_client_adresse", height=80)
        date_debut = col5.text_input("Début des travaux", key="f_date_debut")
        duree = col6.text_input("Durée estimée", key="f_duree")

        st.divider()
        type_ajout = st.radio("Ajouter :", ["Un ouvrage", "Un ensemble"], horizontal=True)
        if type_ajout == "Un ouvrage":
            rech = st.text_input("🔍 Filtrer les ouvrages", key="rech_devis")
            options = [i for i in biblio.index
                       if not rech or rech.lower() in str(biblio.loc[i, "designation"]).lower()]
            if not options:
                st.caption("Aucun ouvrage ne correspond.")
            else:
                c1, c2, c3 = st.columns([3, 1, 1])
                choix = c1.selectbox("Ouvrage", options=options,
                                     format_func=lambda i: f"{biblio.loc[i,'designation']} ({biblio.loc[i,'unite']})")
                qte = c2.number_input("Quantité", min_value=0.0, value=1.0, step=1.0)
                if c3.button("➕ Ajouter", use_container_width=True):
                    o = biblio.loc[choix].to_dict()
                    pu = prix_unitaire(o, taux_horaire)
                    st.session_state.devis_lignes.append(
                        {"groupe": "", "designation": o["designation"], "unite": o["unite"],
                         "quantite": qte, "prix_unitaire": pu, "montant_ht": pu * qte})
                    st.rerun()
        else:
            ens_all = charger_ensembles()
            noms = sorted(ens_all["nom"].unique().tolist())
            if not noms:
                st.info("Aucun ensemble créé.")
            else:
                c1, c2, c3 = st.columns([3, 1, 1])
                nom_ens = c1.selectbox("Ensemble", options=noms)
                qte_ens = c2.number_input("Quantité", min_value=0.0, value=1.0, step=1.0)
                if c3.button("➕ Ajouter l'ensemble", use_container_width=True):
                    for _, c in ens_all[ens_all["nom"] == nom_ens].iterrows():
                        match = biblio[biblio["designation"] == c["designation"]]
                        if match.empty:
                            continue
                        o = match.iloc[0].to_dict()
                        pu = prix_unitaire(o, taux_horaire)
                        q = (c["quantite"] or 0) * qte_ens
                        st.session_state.devis_lignes.append(
                            {"groupe": nom_ens, "designation": o["designation"], "unite": o["unite"],
                             "quantite": q, "prix_unitaire": pu, "montant_ht": pu * q})
                    st.rerun()

        if st.session_state.devis_lignes:
            st.subheader("Lignes du devis (modifiables, supprimables)")
            df = pd.DataFrame(st.session_state.devis_lignes)[
                ["groupe", "designation", "unite", "quantite", "prix_unitaire", "montant_ht"]]
            edited = st.data_editor(
                df, num_rows="dynamic", use_container_width=True,
                column_config={
                    "groupe": st.column_config.TextColumn("Ensemble", disabled=True),
                    "designation": st.column_config.TextColumn("Désignation", disabled=True),
                    "unite": st.column_config.TextColumn("Unité", disabled=True),
                    "quantite": st.column_config.NumberColumn("Qté"),
                    "prix_unitaire": st.column_config.NumberColumn("PU HT", format="%.2f", disabled=True),
                    "montant_ht": st.column_config.NumberColumn("Montant HT", format="%.2f", disabled=True)},
                key="editeur_devis")
            edited = edited[edited["designation"].notna()].copy()
            edited["quantite"] = edited["quantite"].fillna(0)
            edited["montant_ht"] = edited["quantite"] * edited["prix_unitaire"]
            st.session_state.devis_lignes = edited.to_dict("records")

            cc1, cc2 = st.columns(2)
            marge_pct = cc1.number_input("Marge (%)", min_value=0.0, value=0.0, step=1.0)
            tva_pct = cc2.selectbox("TVA (%)", options=[5.5, 10.0, 20.0], index=1)

            sous_total_ht = float(edited["montant_ht"].sum())
            total_ht = sous_total_ht * (1 + marge_pct / 100)
            montant_tva = total_ht * tva_pct / 100
            total_ttc = total_ht + montant_tva

            m1, m2, m3 = st.columns(3)
            m1.metric("Sous-total HT", f"{sous_total_ht:,.2f} €".replace(",", " "))
            m2.metric("Total HT", f"{total_ht:,.2f} €".replace(",", " "))
            m3.metric("Total TTC", f"{total_ttc:,.2f} €".replace(",", " "))

            totaux = {"sous_total_ht": sous_total_ht, "marge_pct": marge_pct, "total_ht": total_ht,
                      "tva_pct": tva_pct, "montant_tva": montant_tva, "total_ttc": total_ttc}
            infos = {"numero": st.session_state.numero_courant, "client": client_nom,
                     "client_adresse": client_adresse, "date": str(date_devis),
                     "validite_jours": ent.get("validite_jours"), "date_debut": date_debut, "duree": duree}

            colA, colB = st.columns(2)
            if colA.button("💾 Enregistrer ce devis"):
                record = {"numero": st.session_state.numero_courant, "date_devis": str(date_devis),
                          "client_nom": client_nom, "client_adresse": client_adresse,
                          "date_debut": date_debut, "duree": duree, "marge_pct": marge_pct,
                          "tva_pct": tva_pct,
                          "lignes_json": json.dumps(st.session_state.devis_lignes, ensure_ascii=False),
                          "statut": "En attente"}
                if sauvegarder_devis(record):
                    ancien = st.session_state.numero_courant
                    st.session_state.numero_courant = prochain_numero()
                    st.success(f"Devis {ancien} enregistré. Prochain n° : {st.session_state.numero_courant}.")
                else:
                    st.error("Ce numéro existe déjà — enregistrement refusé (pas de doublon).")

            pdf_bytes = generer_pdf_devis(ent, infos, st.session_state.devis_lignes, totaux)
            colB.download_button("📄 Télécharger le devis (PDF)", pdf_bytes,
                                 f"{infos['numero']}.pdf", "application/pdf")
            if st.button("🗑️ Vider le devis"):
                st.session_state.devis_lignes = []
                st.rerun()
        else:
            st.info("Aucune ligne. Ajoute un ouvrage ou un ensemble ci-dessus.")

    # ---- SUIVI ----
    with t_suivi:
        st.subheader("Suivi des devis")
        saved = charger_devis()
        if saved.empty:
            st.info("Aucun devis enregistré pour l'instant.")
        else:
            vue = pd.DataFrame({
                "numero": saved["numero"],
                "date_devis": saved["date_devis"],
                "client_nom": saved["client_nom"],
                "total_ttc": saved.apply(total_devis_ttc, axis=1).round(2),
                "statut": saved["statut"],
            })
            edited_s = st.data_editor(
                vue, use_container_width=True, hide_index=True,
                column_config={
                    "numero": st.column_config.TextColumn("N°", disabled=True),
                    "date_devis": st.column_config.TextColumn("Date", disabled=True),
                    "client_nom": st.column_config.TextColumn("Client", disabled=True),
                    "total_ttc": st.column_config.NumberColumn("Total TTC (€)", format="%.2f", disabled=True),
                    "statut": st.column_config.SelectboxColumn("Statut", options=STATUTS)},
                key="editeur_suivi")
            c1, c2 = st.columns(2)
            if c1.button("💾 Enregistrer les statuts"):
                maj_statuts(dict(zip(edited_s["numero"], edited_s["statut"])))
                st.success("Statuts mis à jour.")
                st.rerun()
            c2.download_button("⬇️ Exporter les devis (Excel)", to_excel_bytes(vue),
                               "suivi_devis.xlsx",
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

            # Re-télécharger le PDF d'un devis enregistré
            st.divider()
            st.markdown("**Re-télécharger un devis en PDF**")
            libelles_s = {r["numero"]: f"{r['numero']} — {r['client_nom'] or ''} — {r['date_devis']}"
                          for _, r in saved.iterrows()}
            sel_pdf = st.selectbox("Devis", saved["numero"].tolist(),
                                   format_func=lambda n: libelles_s.get(n, n), key="suivi_pdf_sel")
            rec = saved[saved["numero"] == sel_pdf].iloc[0]
            try:
                lignes_rec = json.loads(rec["lignes_json"]) if rec["lignes_json"] else []
            except Exception:
                lignes_rec = []
            sous_rec = sum((l.get("montant_ht") or 0) for l in lignes_rec)
            total_ht_rec = sous_rec * (1 + (rec.get("marge_pct") or 0) / 100)
            tva_rec = rec.get("tva_pct") or 0
            totaux_rec = {"sous_total_ht": sous_rec, "marge_pct": rec.get("marge_pct") or 0,
                          "total_ht": total_ht_rec, "tva_pct": tva_rec,
                          "montant_tva": total_ht_rec * tva_rec / 100,
                          "total_ttc": total_ht_rec * (1 + tva_rec / 100)}
            infos_rec = {"numero": rec["numero"], "client": rec["client_nom"],
                         "client_adresse": rec["client_adresse"], "date": rec["date_devis"],
                         "validite_jours": charger_entreprise().get("validite_jours"),
                         "date_debut": rec["date_debut"], "duree": rec["duree"]}
            pdf_rec = generer_pdf_devis(charger_entreprise(), infos_rec, lignes_rec, totaux_rec)
            st.download_button("📄 Télécharger ce devis (PDF)", pdf_rec, f"{sel_pdf}.pdf",
                               "application/pdf", key="suivi_pdf_dl")

            # Petit récap
            st.caption(
                f"En attente : {(saved['statut']=='En attente').sum()}  |  "
                f"Acceptés : {(saved['statut']=='Accepté').sum()}  |  "
                f"Refusés : {(saved['statut']=='Refusé').sum()}")


if __name__ == "__main__":
    main()