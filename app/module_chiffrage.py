"""
=============================================================================
 MODULE CHIFFRAGE CVC — SCAFFOLD
=============================================================================
"""

import json
import hashlib
import secrets as pysecrets
from collections import OrderedDict
from datetime import date, datetime, timedelta
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
    init_utilisateurs()


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


def generer_excel_devis(entreprise, infos, lignes, totaux) -> bytes:
    """Devis Excel mis en forme (en-tête société, destinataire, tableau, totaux),
    calibré pour tenir sur une seule page A4 à l'impression / l'export PDF."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.properties import PageSetupProperties

    NAVY = "1F3864"
    HEADER_FILL = "2E5B8C"
    BOX_FILL = "DCE6F1"
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    wb = Workbook()
    ws = wb.active
    ws.title = "Devis"
    for col, w in {"A": 46, "B": 9, "C": 9, "D": 14, "E": 16}.items():
        ws.column_dimensions[col].width = w

    def sc(ref, value, *, font=None, fill=None, align=None, border_=None, wrap=False):
        c = ws[ref]
        c.value = value
        if font:
            c.font = font
        if fill:
            c.fill = PatternFill("solid", fgColor=fill)
        if align or wrap:
            c.alignment = Alignment(horizontal=align, vertical="center", wrap_text=wrap)
        if border_:
            c.border = border_
        return c

    r = 1
    sc(f"A{r}", entreprise.get("raison_sociale") or "Mon entreprise",
       font=Font(bold=True, size=15, color=NAVY))
    sc(f"D{r}", "DEVIS", font=Font(bold=True, size=22, color=NAVY), align="right")
    ws.merge_cells(f"D{r}:E{r}")
    r += 1

    gauche = []
    for champ in [entreprise.get("forme_juridique"), entreprise.get("adresse"),
                  f"SIRET : {entreprise.get('siret')}" if entreprise.get("siret") else None,
                  f"TVA : {entreprise.get('tva_intra')}" if entreprise.get("tva_intra") else None]:
        if champ:
            gauche.extend(str(champ).split("\n"))
    droite = [x for x in [
        f"N° {infos.get('numero', '')}",
        f"Date d'émission : {infos.get('date', '')}",
        (f"Valable jusqu'au : {infos.get('validite')}" if infos.get("validite")
         else (f"Validité : {infos.get('validite_jours')} jours" if infos.get("validite_jours") else None)),
    ] if x]
    for i in range(max(len(gauche), len(droite))):
        if i < len(gauche):
            sc(f"A{r}", gauche[i], font=Font(size=9, color="404040"))
        if i < len(droite):
            sc(f"D{r}", droite[i], font=Font(size=9, color="404040"), align="right")
            ws.merge_cells(f"D{r}:E{r}")
        r += 1

    r += 1
    deb = r
    sc(f"A{r}", "DESTINATAIRE", font=Font(bold=True, italic=True, size=10, color=NAVY))
    r += 1
    sc(f"A{r}", infos.get("client") or "", font=Font(bold=True, size=10))
    r += 1
    if infos.get("client_adresse"):
        sc(f"A{r}", infos.get("client_adresse"), font=Font(size=9), wrap=True)
        r += 1
    sc(f"A{r}", f"Tél : {infos.get('client_tel') or ''}", font=Font(size=9))
    r += 1
    for rr in range(deb, r):
        for col in "ABCDE":
            ws[f"{col}{rr}"].fill = PatternFill("solid", fgColor=BOX_FILL)
            ws[f"{col}{rr}"].border = border

    r += 1
    if infos.get("objet"):
        sc(f"A{r}", "Objet :", font=Font(bold=True, size=10))
        sc(f"B{r}", infos.get("objet"), font=Font(size=10))
        ws.merge_cells(f"B{r}:E{r}")
        r += 2
    else:
        r += 1

    for j, titre in enumerate(["Désignation", "Qté", "Unité", "PU HT (€)", "Total HT (€)"]):
        sc(f"{get_column_letter(j + 1)}{r}", titre,
           font=Font(bold=True, size=10, color="FFFFFF"), fill=HEADER_FILL,
           align="center" if j else "left", border_=border)
    r += 1

    for lg in lignes:
        sc(f"A{r}", lg.get("designation") or "", font=Font(size=9), border_=border, wrap=True)
        sc(f"B{r}", lg.get("quantite") or 0, font=Font(size=9), align="center", border_=border)
        sc(f"C{r}", lg.get("unite") or "", font=Font(size=9), align="center", border_=border)
        d = sc(f"D{r}", lg.get("prix_unitaire") or 0, font=Font(size=9), align="right", border_=border)
        e = sc(f"E{r}", lg.get("montant_ht") or 0, font=Font(size=9), align="right", border_=border)
        d.number_format = '#,##0.00'
        e.number_format = '#,##0.00'
        r += 1

    r += 1

    def total(label, valeur, *, bold=False, size=10, fill=None):
        nonlocal r
        sc(f"D{r}", label, font=Font(bold=bold, size=size), align="right")
        c = sc(f"E{r}", valeur, align="right", fill=fill, border_=border,
               font=Font(bold=bold, size=size, color="FFFFFF" if fill else "000000"))
        c.number_format = '#,##0.00 "€"'
        r += 1

    total("Sous-total HT", totaux.get("sous_total_ht", 0))
    if totaux.get("marge_pct"):
        total(f"Marge ({totaux['marge_pct']:g} %)",
              totaux.get("total_ht", 0) - totaux.get("sous_total_ht", 0))
    total("Total HT", totaux.get("total_ht", 0), bold=True)
    total(f"TVA ({totaux.get('tva_pct', 0):g} %)", totaux.get("montant_tva", 0))
    total("TOTAL TTC", totaux.get("total_ttc", 0), bold=True, size=12, fill=HEADER_FILL)

    ws.print_area = f"A1:E{r}"
    ws.page_setup.orientation = "portrait"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 1
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.page_margins.left = ws.page_margins.right = 0.4
    ws.page_margins.top = ws.page_margins.bottom = 0.5
    ws.sheet_view.showGridLines = False

    buf = BytesIO()
    wb.save(buf)
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


# -----------------------------------------------------------------------------
# AUTH — hachage des mots de passe (PBKDF2)
# -----------------------------------------------------------------------------
_ITERATIONS = 200_000


def hacher_mdp(mot_de_passe: str) -> str:
    sel = pysecrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", mot_de_passe.encode(), sel.encode(), _ITERATIONS)
    return f"{sel}${h.hex()}"


def verifier_mdp(mot_de_passe: str, empreinte: str) -> bool:
    try:
        sel, attendu = empreinte.split("$", 1)
    except (ValueError, AttributeError):
        return False
    h = hashlib.pbkdf2_hmac("sha256", mot_de_passe.encode(), sel.encode(), _ITERATIONS)
    return pysecrets.compare_digest(h.hex(), attendu)


def generer_mdp(longueur: int = 12) -> str:
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(pysecrets.choice(alphabet) for _ in range(longueur))


# -----------------------------------------------------------------------------
# AUTH — gestion des comptes en base
# -----------------------------------------------------------------------------
def init_utilisateurs():
    eng = get_engine()
    with eng.begin() as con:
        con.execute(text("""CREATE TABLE IF NOT EXISTS utilisateurs (
            identifiant TEXT PRIMARY KEY,
            mdp_hash TEXT NOT NULL,
            actif BOOLEAN DEFAULT TRUE,
            date_creation TEXT)"""))


def lister_utilisateurs():
    eng = get_engine()
    try:
        with eng.begin() as con:
            rows = con.execute(text(
                "SELECT identifiant, actif, date_creation FROM utilisateurs ORDER BY identifiant"
            )).fetchall()
        return [{"identifiant": r[0], "actif": r[1], "date_creation": r[2]} for r in rows]
    except Exception:
        return []


def creer_utilisateur(identifiant: str, mot_de_passe: str) -> bool:
    identifiant = (identifiant or "").strip().lower()
    if not identifiant or not mot_de_passe:
        return False
    eng = get_engine()
    with eng.begin() as con:
        existe = con.execute(
            text("SELECT 1 FROM utilisateurs WHERE identifiant = :i"),
            {"i": identifiant}).fetchone()
        if existe:
            return False
        con.execute(text("""INSERT INTO utilisateurs
            (identifiant, mdp_hash, actif, date_creation)
            VALUES (:i, :h, TRUE, :d)"""),
            {"i": identifiant, "h": hacher_mdp(mot_de_passe),
             "d": datetime.now().strftime("%Y-%m-%d %H:%M")})
    return True


def reinitialiser_mdp(identifiant: str):
    identifiant = (identifiant or "").strip().lower()
    nouveau = generer_mdp()
    eng = get_engine()
    with eng.begin() as con:
        res = con.execute(text("UPDATE utilisateurs SET mdp_hash = :h WHERE identifiant = :i"),
                          {"h": hacher_mdp(nouveau), "i": identifiant})
        if res.rowcount == 0:
            return None
    return nouveau


def changer_mdp(identifiant: str, ancien: str, nouveau: str) -> bool:
    identifiant = (identifiant or "").strip().lower()
    eng = get_engine()
    with eng.begin() as con:
        row = con.execute(text("SELECT mdp_hash FROM utilisateurs WHERE identifiant = :i"),
                          {"i": identifiant}).fetchone()
        if not row or not verifier_mdp(ancien, row[0]):
            return False
        con.execute(text("UPDATE utilisateurs SET mdp_hash = :h WHERE identifiant = :i"),
                    {"h": hacher_mdp(nouveau), "i": identifiant})
    return True


def desactiver_utilisateur(identifiant: str, actif: bool):
    eng = get_engine()
    with eng.begin() as con:
        con.execute(text("UPDATE utilisateurs SET actif = :a WHERE identifiant = :i"),
                    {"a": actif, "i": (identifiant or "").strip().lower()})


def authentifier(identifiant: str, mot_de_passe: str) -> bool:
    identifiant = (identifiant or "").strip().lower()
    eng = get_engine()
    try:
        with eng.begin() as con:
            row = con.execute(
                text("SELECT mdp_hash, actif FROM utilisateurs WHERE identifiant = :i"),
                {"i": identifiant}).fetchone()
    except Exception:
        return False
    if not row or not row[1]:
        return False
    return verifier_mdp(mot_de_passe, row[0])


def check_password():
    def credentials_entered():
        u = st.session_state.get("login_user", "")
        p = st.session_state.get("login_pass", "")
        if u == st.secrets["APP_USER"] and p == st.secrets["APP_PASSWORD"]:
            st.session_state["auth_ok"] = True
            st.session_state["is_admin"] = True
            st.session_state["user"] = u
            st.session_state.pop("login_pass", None)
            return
        if authentifier(u, p):
            st.session_state["auth_ok"] = True
            st.session_state["is_admin"] = False
            st.session_state["user"] = u.strip().lower()
            st.session_state.pop("login_pass", None)
            return
        st.session_state["auth_ok"] = False

    if st.session_state.get("auth_ok"):
        return True

    st.subheader("Connexion")
    st.text_input("Identifiant", key="login_user")
    st.text_input("Mot de passe", type="password", key="login_pass")
    st.button("Se connecter", on_click=credentials_entered)
    if st.session_state.get("auth_ok") is False:
        st.error("Identifiant ou mot de passe incorrect.")
    st.caption("Mot de passe oublié ? Contacte l'administrateur pour le réinitialiser.")
    return False


def panneau_comptes():
    if not st.session_state.get("is_admin"):
        with st.sidebar.expander("🔑 Changer mon mot de passe"):
            a = st.text_input("Mot de passe actuel", type="password", key="chg_old")
            n1 = st.text_input("Nouveau mot de passe", type="password", key="chg_n1")
            n2 = st.text_input("Confirmer", type="password", key="chg_n2")
            if st.button("Valider le changement"):
                if len(n1) < 8:
                    st.error("8 caractères minimum.")
                elif n1 != n2:
                    st.error("Les deux saisies ne correspondent pas.")
                elif changer_mdp(st.session_state.get("user"), a, n1):
                    st.success("Mot de passe modifié.")
                else:
                    st.error("Mot de passe actuel incorrect.")
        return

    with st.sidebar.expander("👤 Gestion des comptes (admin)"):
        st.caption("Crée un compte par client. Le mot de passe n'est affiché qu'une fois.")
        st.markdown("**Créer un compte**")
        new_id = st.text_input("Identifiant du client", key="adm_id")
        if st.button("Créer le compte"):
            if not new_id.strip():
                st.error("Donne un identifiant.")
            else:
                mdp = generer_mdp()
                if creer_utilisateur(new_id, mdp):
                    st.success(f"Compte « {new_id.strip().lower()} » créé.")
                    st.code(mdp, language=None)
                    st.warning("Note ce mot de passe MAINTENANT et transmets-le au client "
                               "par un canal sûr. Il ne sera plus affiché.")
                else:
                    st.error("Cet identifiant existe déjà.")

        st.divider()
        st.markdown("**Réinitialiser un mot de passe**")
        comptes = [u["identifiant"] for u in lister_utilisateurs()]
        if not comptes:
            st.caption("Aucun compte client pour l'instant.")
        else:
            cible = st.selectbox("Compte", comptes, key="adm_reset")
            if st.button("Générer un nouveau mot de passe"):
                nouveau = reinitialiser_mdp(cible)
                if nouveau:
                    st.success(f"Nouveau mot de passe pour « {cible} » :")
                    st.code(nouveau, language=None)
                    st.warning("Transmets-le au client. Il ne sera plus affiché.")
                else:
                    st.error("Compte introuvable.")
            st.divider()
            st.markdown("**Comptes existants**")
            st.dataframe(pd.DataFrame(lister_utilisateurs()), use_container_width=True,
                         hide_index=True)


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
        panneau_comptes()
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
        objet = st.text_input("Objet du devis", key="f_objet",
                              placeholder="Ex. Chiffrage réseau de gaines (CVC)")

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
                    fourn = o.get("prix_fourniture") or 0
                    mo = prix_mo(o.get("temps_mo"), o.get("nb_poseurs"),
                                 o.get("taux_horaire") or taux_horaire)
                    pu = fourn + mo
                    st.session_state.devis_lignes.append(
                        {"groupe": "", "designation": o["designation"], "unite": o["unite"],
                         "quantite": qte, "prix_fourniture": fourn, "mo_unit": mo, "mo_active": True,
                         "prix_unitaire": pu, "montant_ht": pu * qte})
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
                        fourn = o.get("prix_fourniture") or 0
                        mo = prix_mo(o.get("temps_mo"), o.get("nb_poseurs"),
                                     o.get("taux_horaire") or taux_horaire)
                        pu = fourn + mo
                        q = (c["quantite"] or 0) * qte_ens
                        st.session_state.devis_lignes.append(
                            {"groupe": nom_ens, "designation": o["designation"], "unite": o["unite"],
                             "quantite": q, "prix_fourniture": fourn, "mo_unit": mo, "mo_active": True,
                             "prix_unitaire": pu, "montant_ht": pu * q})
                    st.rerun()

        if st.session_state.devis_lignes:
            st.subheader("Lignes du devis (modifiables, supprimables)")
            mo_generale = st.checkbox(
                "Appliquer la main-d'œuvre sur ce devis", value=True,
                help="Décoché = fourniture seule sur tout le devis (aucune MO comptée).")

            cols_ordre = ["groupe", "designation", "unite", "quantite",
                          "prix_fourniture", "mo_unit", "mo_active", "prix_unitaire", "montant_ht"]
            dfl = pd.DataFrame(st.session_state.devis_lignes)
            for c in cols_ordre:
                if c not in dfl.columns:
                    if c == "mo_active":
                        dfl[c] = True
                    elif c in ("prix_fourniture", "mo_unit"):
                        dfl[c] = 0.0
                    else:
                        dfl[c] = None
            dff = dfl[cols_ordre]

            edited = st.data_editor(
                dff, num_rows="dynamic", use_container_width=True,
                column_config={
                    "groupe": st.column_config.TextColumn("Ensemble", disabled=True),
                    "designation": st.column_config.TextColumn("Désignation"),
                    "unite": st.column_config.TextColumn("Unité"),
                    "quantite": st.column_config.NumberColumn("Qté"),
                    "prix_fourniture": st.column_config.NumberColumn("Fourniture (€)", format="%.2f"),
                    "mo_unit": st.column_config.NumberColumn("MO unit. (€)", format="%.2f",
                                                             help="Coût main-d'œuvre d'une unité de cette ligne."),
                    "mo_active": st.column_config.CheckboxColumn("MO",
                                                                 help="Coché = la main-d'œuvre est comptée sur cette ligne."),
                    "prix_unitaire": st.column_config.NumberColumn("PU HT", format="%.2f", disabled=True),
                    "montant_ht": st.column_config.NumberColumn("Montant HT", format="%.2f", disabled=True)},
                key="editeur_devis")

            edited = edited[edited["designation"].notna()].copy()
            edited["quantite"] = edited["quantite"].fillna(0)
            edited["prix_fourniture"] = edited["prix_fourniture"].fillna(0)
            edited["mo_unit"] = edited["mo_unit"].fillna(0)
            edited["mo_active"] = edited["mo_active"].fillna(True)
            mo_appliquee = edited["mo_active"] & mo_generale
            edited["prix_unitaire"] = edited["prix_fourniture"] + edited["mo_unit"].where(mo_appliquee, 0)
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
            vj = str(ent.get("validite_jours") or "").strip()
            validite = ((date_devis + timedelta(days=int(vj))).strftime("%d/%m/%Y")
                        if vj.isdigit() else None)
            infos = {"numero": st.session_state.numero_courant, "client": client_nom,
                     "client_adresse": client_adresse, "client_tel": "",
                     "date": date_devis.strftime("%d/%m/%Y"), "validite": validite,
                     "validite_jours": ent.get("validite_jours"), "objet": objet,
                     "date_debut": date_debut, "duree": duree}

            colA, colB, colC = st.columns(3)
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

            # Export Excel du devis mis en forme (une page) — le client le retravaille de son côté
            xls_bytes = generer_excel_devis(ent, infos, st.session_state.devis_lignes, totaux)
            colC.download_button("📊 Exporter ce devis (Excel)", xls_bytes,
                                 f"{infos['numero']}.xlsx",
                                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
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

            st.caption(
                f"En attente : {(saved['statut']=='En attente').sum()}  |  "
                f"Acceptés : {(saved['statut']=='Accepté').sum()}  |  "
                f"Refusés : {(saved['statut']=='Refusé').sum()}")


if __name__ == "__main__":
    main()
