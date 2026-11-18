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
TVA_AUTORISEES = [0.0, 5.5, 10.0, 20.0]
# Table annexe (remise, mentions, date de modif) : la table `devis` reste inchangée.
COMPLEMENT_COLS = ["numero", "remise_type", "remise_valeur", "mentions", "date_modif",
                   "mise_en_forme", "options_variantes"]
# Colonnes de l'éditeur de lignes (les 3 dernières sont conservées mais masquées)
COLS_EDITION = ["ordre", "groupe", "designation", "unite", "quantite", "prix_unitaire",
                "prix_fourniture", "mo_unit", "mo_active"]
ENTREPRISE_CHAMPS = [
    "raison_sociale", "forme_juridique", "adresse", "siret", "tva_intra",
    "assurance_nom", "assurance_police", "assurance_couverture",
    "conditions_reglement", "validite_jours",
    # en-tête PDF (tous facultatifs)
    "capital", "telephone", "email", "site_web", "code_ape",
    "rc_pro_nom", "rc_pro_police", "qualifications",
    # mentions du devis (la mise en forme vit sous la clé « mise_en_forme »)
    "acompte_pct", "mention_devis",
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
        con.execute(text("""CREATE TABLE IF NOT EXISTS devis_complements (
            numero TEXT PRIMARY KEY, remise_type TEXT, remise_valeur DOUBLE PRECISION,
            mentions TEXT, date_modif TEXT)"""))
        con.execute(text("CREATE TABLE IF NOT EXISTS favoris (designation TEXT PRIMARY KEY)"))
    # migration douce : colonnes ajoutées après coup (jamais supprimées)
    _ajouter_colonnes("devis_complements", {"mise_en_forme": "TEXT", "options_variantes": "TEXT"})
    init_utilisateurs()


def _ajouter_colonnes(table, colonnes: dict):
    """Ajoute les colonnes manquantes (Postgres comme SQLite)."""
    from sqlalchemy import inspect
    eng = get_engine()
    existantes = {c["name"] for c in inspect(eng).get_columns(table)}
    with eng.begin() as con:
        for nom, type_sql in colonnes.items():
            if nom not in existantes:
                con.execute(text(f"ALTER TABLE {table} ADD COLUMN {nom} {type_sql}"))


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


def charger_favoris() -> list:
    """Désignations des ouvrages marqués « postes fréquents »."""
    return [d for d in _lire("favoris", ["designation"])["designation"].tolist() if not _vide(d)]


def enregistrer_favoris(designations):
    with get_engine().begin() as con:
        con.execute(text("DELETE FROM favoris"))
        for d in dict.fromkeys(designations):
            con.execute(text("INSERT INTO favoris (designation) VALUES (:d)"), {"d": d})


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


def mettre_a_jour_devis(numero: str, champs: dict) -> bool:
    """Réécrit les champs d'un devis existant (même numéro). Même mécanique que maj_statuts."""
    df = charger_devis()
    masque = df["numero"] == numero
    if not masque.any():
        return False
    for col, val in champs.items():
        if col in DEVIS_COLS and col != "numero":
            df.loc[masque, col] = val
    _ecrire("devis", df)
    return True


def charger_complements():
    return _lire("devis_complements", COMPLEMENT_COLS)


def complement_devis(numero: str) -> dict:
    """Remise / mentions / date de modif d'un devis (valeurs neutres si absent)."""
    res = {"remise_type": "%", "remise_valeur": 0.0, "mentions": "", "date_modif": "",
           "mise_en_forme": None, "options_variantes": ""}
    df = charger_complements()
    sel = df[df["numero"] == numero]
    if not sel.empty:
        r = sel.iloc[0]
        res["remise_type"] = "€" if r.get("remise_type") == "€" else "%"
        res["remise_valeur"] = _num(r.get("remise_valeur"))
        res["mentions"] = _texte(r.get("mentions"))
        res["date_modif"] = _texte(r.get("date_modif"))
        res["mise_en_forme"] = surcharge_mise_en_forme(r.get("mise_en_forme"))
        res["options_variantes"] = _texte(r.get("options_variantes"))
    return res


def surcharge_mise_en_forme(valeur):
    """JSON stocké avec le devis -> dict, ou None (= préférences de l'entreprise)."""
    if _vide(valeur):
        return None
    try:
        d = json.loads(valeur) if isinstance(valeur, str) else valeur
    except ValueError:
        return None
    return d if isinstance(d, dict) and d else None


def enregistrer_mise_en_forme(numero: str, surcharge, options: str):
    """Mise en forme propre à un devis (None = suit l'entreprise) + options non chiffrées."""
    with get_engine().begin() as con:
        con.execute(text("""INSERT INTO devis_complements (numero, mise_en_forme, options_variantes)
            VALUES (:n, :f, :o)
            ON CONFLICT (numero) DO UPDATE SET
              mise_en_forme = EXCLUDED.mise_en_forme,
              options_variantes = EXCLUDED.options_variantes"""),
            {"n": numero, "f": json.dumps(surcharge, ensure_ascii=False) if surcharge else None,
             "o": (options or "").strip() or None})


def enregistrer_complement(numero: str, remise_type: str, remise_valeur: float, mentions: str):
    """Upsert de la ligne annexe + horodatage de la modification."""
    with get_engine().begin() as con:
        con.execute(text("""INSERT INTO devis_complements
            (numero, remise_type, remise_valeur, mentions, date_modif)
            VALUES (:n, :t, :v, :m, :d)
            ON CONFLICT (numero) DO UPDATE SET
              remise_type = EXCLUDED.remise_type, remise_valeur = EXCLUDED.remise_valeur,
              mentions = EXCLUDED.mentions, date_modif = EXCLUDED.date_modif"""),
            {"n": numero, "t": remise_type, "v": float(remise_valeur), "m": mentions,
             "d": datetime.now().strftime("%Y-%m-%d %H:%M")})


def prochain_numero() -> str:
    annee = date.today().year
    prefixe = f"DEV-{annee}-"
    df = charger_devis()
    nums = [int(n[len(prefixe):]) for n in df["numero"]
            if isinstance(n, str) and n.startswith(prefixe) and n[len(prefixe):].isdigit()]
    return f"{prefixe}{(max(nums) + 1) if nums else 1:03d}"


def charger_entreprise():
    # Valeurs par défaut = migration douce : un ancien entreprise.json sans
    # les nouveaux champs reste valide, ils prennent simplement ces valeurs.
    base = {c: "" for c in ENTREPRISE_CHAMPS}
    base["validite_jours"] = "30"
    base["mention_devis"] = MENTION_DEVIS_DEFAUT
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
              totaux.get("total_ht_brut", totaux.get("total_ht", 0)) - totaux.get("sous_total_ht", 0))
    if totaux.get("remise_montant"):
        total("Remise", -totaux["remise_montant"])
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


def _vide(x) -> bool:
    if x is None or (isinstance(x, str) and not x.strip()):
        return True
    try:
        return bool(pd.isna(x))
    except (TypeError, ValueError):
        return False


def _num(x, defaut=0.0) -> float:
    """Nombre propre : None / NaN / texte invalide -> défaut."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return defaut
    return defaut if v != v else v


def _texte(x) -> str:
    return "" if _vide(x) else str(x).strip()


def montant_ligne(ligne) -> float:
    """Total HT d'une ligne, toujours recalculé (qté × PU). Repli sur montant_ht
    uniquement pour d'anciennes lignes sans PU."""
    if _vide(ligne.get("prix_unitaire")):
        return _num(ligne.get("montant_ht"))
    return _num(ligne.get("quantite")) * _num(ligne.get("prix_unitaire"))


def calculer_totaux(lignes, marge_pct=0, tva_pct=0, remise_type="%", remise_valeur=0) -> dict:
    """Sous-total -> marge -> remise globale (% ou €) -> total HT -> TVA -> TTC.
    Clés compatibles avec generer_pdf_devis."""
    marge, tva, rv = _num(marge_pct), _num(tva_pct), max(_num(remise_valeur), 0.0)
    remise_type = "€" if remise_type == "€" else "%"
    sous = sum(montant_ligne(l) for l in lignes)
    brut = sous * (1 + marge / 100)
    remise = min(rv, brut) if remise_type == "€" else brut * min(rv, 100.0) / 100
    total_ht = brut - remise
    montant_tva = total_ht * tva / 100
    return {"sous_total_ht": sous, "marge_pct": marge, "total_ht_brut": brut,
            "remise_type": remise_type, "remise_valeur": rv, "remise_montant": remise,
            "total_ht": total_ht, "tva_pct": tva, "montant_tva": montant_tva,
            "total_ttc": total_ht + montant_tva}


def total_devis_ttc(rec) -> float:
    try:
        lignes = json.loads(rec["lignes_json"]) if rec.get("lignes_json") else []
    except Exception:
        lignes = []
    return calculer_totaux(lignes, rec.get("marge_pct"), rec.get("tva_pct"),
                           rec.get("remise_type"), rec.get("remise_valeur"))["total_ttc"]


# ----------------------------------------------------------------------------
# PDF
# ----------------------------------------------------------------------------
# --- Mise en forme du devis PDF ----------------------------------------------
# Paramètres choisis par l'utilisateur. Ils vivent à deux niveaux :
#   - entreprise.json["mise_en_forme"]        : préférences par défaut ;
#   - devis_complements.mise_en_forme (JSON)  : surcharge d'UN devis (ou NULL).
# Effectif = MISE_EN_FORME_DEFAUT < préférences entreprise < surcharge devis.
COULEUR_ACCENT_DEFAUT = "#1F3864"
MENTION_DEVIS_DEFAUT = "Devis gratuit, établi avant tout commencement des travaux."
MENTION_ACCORD = "« Devis reçu avant l'exécution des travaux, bon pour accord »"

MODELES = {"classique": "Classique épuré", "moderne": "Moderne coloré", "compact": "Compact"}
DESCRIPTION_MODELES = {
    "classique": "Sobre, noir et gris, sans aplat de couleur : l'accent ne sert qu'aux filets.",
    "moderne": "Bandeau, cartouche, titres de colonnes et total TTC sur fond d'accent, lignes zébrées.",
    "compact": "Police et interlignes réduits, logo plus petit : idéal pour les devis à nombreuses lignes.",
}
PALETTE_ACCENT = {"Bleu marine": "#1F3864", "Bleu": "#2E75B6", "Vert": "#2E7D32",
                  "Rouge brique": "#B03A2E", "Orange": "#C4501E", "Anthracite": "#3C3C3C"}
POSITIONS_LOGO = {"gauche": "Gauche", "centre": "Centre", "droite": "Droite"}
DEVISES = ["EUR", "CHF"]   # en toutes lettres : la police PDF intégrée n'a pas le signe €
SECTIONS_DEVIS = {
    "section_validite": "Durée de validité (cartouche)",
    "section_accord": "Zone « Bon pour accord » et signature",
    "section_options": "Options / variantes non chiffrées",
    "section_paiement": "Conditions de paiement et acompte",
    "section_mentions": "Mentions légales BTP détaillées",
}
MISE_EN_FORME_DEFAUT = {
    "modele": "classique", "couleur_accent": COULEUR_ACCENT_DEFAUT,
    "logo_afficher": True, "logo_position": "gauche",
    "section_validite": True, "section_accord": True, "section_options": False,
    "section_paiement": True, "section_mentions": True,
    "tva_defaut": 10.0, "devise": "EUR",
}

# Un seul moteur : un modèle n'est qu'un jeu de réglages lu par les fonctions de rendu.
_STYLE_BASE = {"bandeau": False, "fond_cartouche": False, "fond_entete": False,
               "fond_section": False, "fond_ttc": False, "cadre_client_accent": False,
               "zebre": None, "echelle": 1.0, "interligne": 4.4, "marge_ligne": 3.0,
               "logo": (55.0, 24.0)}
STYLES = {
    "classique": dict(_STYLE_BASE),
    "moderne": dict(_STYLE_BASE, bandeau=True, fond_cartouche=True, fond_entete=True,
                    fond_section=True, fond_ttc=True, cadre_client_accent=True,
                    zebre=(245, 246, 248)),
    "compact": dict(_STYLE_BASE, zebre=(247, 247, 247), echelle=0.88, interligne=3.6,
                    marge_ligne=1.6, logo=(40.0, 16.0)),
}

GRIS_TEXTE = (95, 95, 95)
GRIS_FILET = (200, 200, 200)
GRIS_LEGER = (228, 228, 228)
LARGEUR_UTILE = 180.0                     # A4 avec marges de 15 mm
COL_GAUCHE, COL_DROITE = 95.0, 80.0       # en-tête : entreprise | cartouche + client
COLONNES_PDF = (("Désignation", 88, "L"), ("Unité", 14, "C"), ("Qté", 18, "R"),
            ("P.U. HT", 28, "R"), ("Montant HT", 32, "R"))
H_ACCORD, H_LIGNE_TOTAL = 34.0, 5.5       # cadre signature, ligne du bloc totaux


def _hexa(valeur, defaut=COULEUR_ACCENT_DEFAUT) -> str:
    """Couleur « #RRGGBB » valide (sinon la couleur par défaut)."""
    h = _texte(valeur).lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        int(h, 16)
    except ValueError:
        return defaut
    return f"#{h.upper()}" if len(h) == 6 else defaut


def _rgb(valeur):
    h = _hexa(valeur).lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _teinte(rgb, force):
    """Éclaircit une couleur vers le blanc (force 0 = couleur, 1 = blanc)."""
    return tuple(round(c + (255 - c) * force) for c in rgb)


def normaliser_mise_en_forme(*couches) -> dict:
    """Fusionne les couches (dict ou JSON, None ignoré) sur les défauts et
    valide chaque valeur : un fichier ancien ou abîmé ne fait jamais planter."""
    mef = dict(MISE_EN_FORME_DEFAUT)
    for couche in couches:
        if isinstance(couche, str):
            try:
                couche = json.loads(couche)
            except ValueError:
                couche = None
        if isinstance(couche, dict):
            mef.update({k: v for k, v in couche.items()
                        if k in MISE_EN_FORME_DEFAUT and not _vide(v)})
    d = MISE_EN_FORME_DEFAUT
    if mef["modele"] not in MODELES:
        mef["modele"] = d["modele"]
    if mef["logo_position"] not in POSITIONS_LOGO:
        mef["logo_position"] = d["logo_position"]
    if mef["devise"] not in DEVISES:
        mef["devise"] = d["devise"]
    mef["couleur_accent"] = _hexa(mef["couleur_accent"])
    tva = _num(mef["tva_defaut"], d["tva_defaut"])
    mef["tva_defaut"] = tva if tva in TVA_AUTORISEES else d["tva_defaut"]
    for k in ["logo_afficher", *SECTIONS_DEVIS]:
        v = mef[k]
        mef[k] = v.strip().lower() in ("1", "true", "oui") if isinstance(v, str) else bool(v)
    return mef


def mise_en_forme_entreprise(ent) -> dict:
    """Préférences de l'entreprise (reprend l'ancienne clé « couleur_accent » à plat)."""
    ancienne = {"couleur_accent": ent.get("couleur_accent")} if ent.get("couleur_accent") else None
    return normaliser_mise_en_forme(ancienne, ent.get("mise_en_forme"))


def _qte(x) -> str:
    return f"{_num(x):g}".replace(".", ",")


def _joindre(*morceaux, sep="  ·  ") -> str:
    return sep.join(m for m in morceaux if m)


def _txt(s):
    s = str(s)
    for a, b in {"€": "EUR", "œ": "oe", "Œ": "OE", "’": "'", "–": "-",
                 "—": "-", "•": "-", "…": "..."}.items():
        s = s.replace(a, b)
    return s.encode("latin-1", "replace").decode("latin-1")


def _tronquer(pdf, texte, largeur) -> str:
    if pdf.get_string_width(texte) <= largeur:
        return texte
    while texte and pdf.get_string_width(texte + "...") > largeur:
        texte = texte[:-1]
    return texte + "..."


def _validite(infos, ent) -> str:
    jours = infos.get("validite_jours") or ent.get("validite_jours") or ""
    jours = f"{jours} jours" if jours.isdigit() else jours
    jusqu = infos.get("validite") or ""
    if jours and jusqu:
        return f"{jours} (jusqu'au {jusqu})"
    return jours or (f"jusqu'au {jusqu}" if jusqu else "")


def _taille_logo(l_max, h_max):
    """(largeur, hauteur) du logo ramené dans sa boîte, ratio conservé ; None si absent."""
    if not LOGO_PATH.exists():
        return None
    try:
        from PIL import Image
        with Image.open(LOGO_PATH) as im:
            lw, lh = im.size
        k = min(l_max / lw, h_max / lh)
        return lw * k, lh * k
    except Exception:
        return None  # logo illisible : en-tête sans logo plutôt qu'un crash


class _PDFDevis(FPDF):
    """FPDF + réglages de mise en forme ; en-tête allégé (pages 2 et suivantes),
    bandeau éventuel et pied répétés automatiquement à chaque saut de page."""

    def __init__(self, ent, infos, mef):
        super().__init__(format="A4")
        self.ent, self.infos, self.mef = ent, infos, mef
        self.style = STYLES[mef["modele"]]
        self.accent = _rgb(mef["couleur_accent"])
        self.set_margins(15, 12, 15)
        self.set_auto_page_break(True, margin=22)

    def police(self, style, taille):
        self.set_font("Helvetica", style, taille * self.style["echelle"])

    def euro(self, x) -> str:
        return f"{_num(x):,.2f}".replace(",", " ").replace(".", ",") + f" {self.mef['devise']}"

    def filet(self, y, couleur=GRIS_FILET, epaisseur=0.3, x=None, largeur=LARGEUR_UTILE):
        x = self.l_margin if x is None else x
        self.set_draw_color(*couleur)
        self.set_line_width(epaisseur)
        self.line(x, y, x + largeur, y)
        self.set_draw_color(0, 0, 0)
        self.set_line_width(0.2)

    def header(self):
        if self.style["bandeau"]:
            self.set_fill_color(*self.accent)
            self.rect(0, 0, self.w, 3, "F")
        if self.page_no() == 1:
            self.set_y(12)
            return
        # en-tête allégé : raison sociale | rappel du cartouche
        self.set_xy(self.l_margin, 8)
        self.police("B", 10)
        self.set_text_color(0, 0, 0)
        raison = _txt(self.ent.get("raison_sociale") or "Mon entreprise")
        self.cell(105, 6, _tronquer(self, raison, 103))
        self.police("B", 9)
        self.set_text_color(*self.accent)
        rappel = _joindre(f"DEVIS n° {self.infos.get('numero')}", self.infos.get("date"), sep=" du ")
        self.cell(75, 6, _tronquer(self, _txt(rappel), 74), align="R")
        self.set_text_color(0, 0, 0)
        self.filet(15.5)
        self.set_y(19)

    def footer(self):
        e = self.ent
        self.filet(self.h - 15)
        self.set_xy(self.l_margin, self.h - 13.5)
        self.set_font("Helvetica", "", 6.5)
        self.set_text_color(*GRIS_TEXTE)
        ident = _joindre(e.get("raison_sociale"),
                         f"SIRET {e['siret']}" if e.get("siret") else "",
                         f"TVA intra. {e['tva_intra']}" if e.get("tva_intra") else "",
                         f"Tél. {e['telephone']}" if e.get("telephone") else "",
                         e.get("email"))
        self.cell(160, 4, _tronquer(self, _txt(ident), 158))
        self.cell(20, 4, f"Page {self.page_no()}/{{nb}}", align="R")
        self.set_text_color(0, 0, 0)


def _bloc_texte(pdf, x, largeur, texte, taille, style="", couleur=(0, 0, 0), interligne=None):
    """Paragraphe à gauche, ignoré s'il est vide (jamais de trou ni de libellé seul)."""
    if not texte:
        return
    pdf.police(style, taille)
    pdf.set_text_color(*couleur)
    pdf.set_x(x)
    pdf.multi_cell(largeur, (interligne or taille * 0.45) * pdf.style["echelle"], _txt(texte),
                   align="L", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)


def _nb_lignes(pdf, texte, largeur, taille, style="") -> int:
    if not texte:
        return 0
    pdf.police(style, taille)
    return len(pdf.multi_cell(largeur, 4, _txt(texte), dry_run=True, output="LINES"))


def _entete_entreprise(pdf, e, x, largeur) -> float:
    """Colonne identité : raison sociale > statut > coordonnées > identifiants
    légaux (le logo est placé par l'appelant). Retourne le bas du bloc."""
    _bloc_texte(pdf, x, largeur, e.get("raison_sociale") or "Mon entreprise", 14, "B", interligne=6.3)
    fj, capital = e.get("forme_juridique"), e.get("capital")
    statut = (f"{fj} au capital de {capital}" if fj and capital
              else fj or (f"Capital social : {capital}" if capital else ""))
    _bloc_texte(pdf, x, largeur, statut, 8.5, "I", GRIS_TEXTE)
    pdf.ln(0.8)
    _bloc_texte(pdf, x, largeur, e.get("adresse"), 9, interligne=4.3)
    _bloc_texte(pdf, x, largeur, _joindre(f"Tél. {e['telephone']}" if e.get("telephone") else "",
                                          e.get("email")), 9, interligne=4.3)
    _bloc_texte(pdf, x, largeur, e.get("site_web"), 9, interligne=4.3)
    legal = _joindre(f"SIRET {e['siret']}" if e.get("siret") else "",
                     f"TVA intra. {e['tva_intra']}" if e.get("tva_intra") else "",
                     f"APE {e['code_ape']}" if e.get("code_ape") else "")
    if legal:
        pdf.ln(1.2)
        _bloc_texte(pdf, x, largeur, legal, 7.5, couleur=GRIS_TEXTE, interligne=3.5)
    return pdf.get_y()


def _cartouche(pdf, infos, ent, x, largeur) -> float:
    """Cartouche devis : titre « DEVIS » + n°, date d'émission, validité (si activée)."""
    st_, haut = pdf.style, pdf.get_y()
    h_titre = 10 * st_["echelle"]
    pdf.set_xy(x + 1, haut)
    pdf.police("B", 15)
    if st_["fond_cartouche"]:
        pdf.set_fill_color(*pdf.accent)
        pdf.rect(x, haut, largeur, h_titre, "F")
        pdf.set_text_color(255, 255, 255)
    else:
        pdf.set_text_color(0, 0, 0)
    pdf.cell(largeur - 2, h_titre, "DEVIS")
    if not st_["fond_cartouche"]:
        pdf.filet(haut + h_titre, pdf.accent, 0.5, x=x, largeur=largeur)
    lignes = [("N°", infos.get("numero")), ("Date d'émission", infos.get("date"))]
    if pdf.mef["section_validite"]:
        lignes.append(("Validité", _validite(infos, ent)))
    y = haut + h_titre + 2
    for libelle, valeur in lignes:
        if not valeur:
            continue
        pdf.set_xy(x + 1, y)
        pdf.police("", 8.5)
        pdf.set_text_color(*GRIS_TEXTE)
        pdf.cell(29, 5, _txt(libelle))
        pdf.police("B", 9)
        pdf.set_text_color(0, 0, 0)
        pdf.multi_cell(largeur - 31, 5 * st_["echelle"], _txt(valeur), align="L")
        y = pdf.get_y() + 0.5
    bas = y + 1.5
    pdf.set_draw_color(*(_teinte(pdf.accent, 0.5) if st_["fond_cartouche"] else GRIS_FILET))
    pdf.rect(x, haut, largeur, bas - haut, "D")
    pdf.set_draw_color(0, 0, 0)
    return bas


def _bloc_client(pdf, infos, x, largeur) -> float:
    """Bloc client encadré (omis entièrement s'il n'y a aucune info client)."""
    haut = pdf.get_y()
    nom, adresse, tel = infos.get("client"), infos.get("client_adresse"), infos.get("client_tel")
    if not (nom or adresse or tel):
        return haut
    accent = pdf.style["cadre_client_accent"]
    pdf.set_xy(x + 1, haut + 2)
    pdf.police("B", 7.5)
    pdf.set_text_color(*(pdf.accent if accent else GRIS_TEXTE))
    pdf.cell(largeur - 2, 4, "CLIENT")
    pdf.set_y(haut + 7 * pdf.style["echelle"])
    _bloc_texte(pdf, x + 1, largeur - 2, nom, 10, "B", interligne=5)
    _bloc_texte(pdf, x + 1, largeur - 2, adresse, 9, interligne=4.4)
    _bloc_texte(pdf, x + 1, largeur - 2, f"Tél. {tel}" if tel else "", 9, interligne=4.4)
    bas = pdf.get_y() + 2.5
    pdf.set_draw_color(*(pdf.accent if accent else GRIS_FILET))
    pdf.set_line_width(0.35 if accent else 0.3)
    pdf.rect(x, haut, largeur, bas - haut, "D")
    pdf.set_draw_color(0, 0, 0)
    pdf.set_line_width(0.2)
    return bas


def _entete_page1(pdf, ent, infos):
    """Logo (gauche / centre / droite ou masqué), colonne entreprise à gauche,
    cartouche + client à droite. La suite démarre sous le plus bas des blocs."""
    x, haut = pdf.l_margin, pdf.get_y()
    x_droite = x + LARGEUR_UTILE - COL_DROITE
    y_gauche = y_droite = haut
    taille = _taille_logo(*pdf.style["logo"]) if pdf.mef["logo_afficher"] else None
    if taille:
        w, h = taille
        position = pdf.mef["logo_position"]
        if position == "centre":
            x_logo = x + (LARGEUR_UTILE - w) / 2
            y_gauche = y_droite = haut + h + 4
        elif position == "droite":
            x_logo = x + LARGEUR_UTILE - w
            y_droite = haut + h + 3
        else:
            x_logo = x + pdf.c_margin
            y_gauche = haut + h + 3
        try:
            pdf.image(str(LOGO_PATH), x=x_logo, y=haut, w=w, h=h)
        except Exception:
            y_gauche = y_droite = haut
    pdf.set_y(y_gauche)
    bas_gauche = _entete_entreprise(pdf, ent, x, COL_GAUCHE)
    pdf.set_y(y_droite)
    bas_cartouche = _cartouche(pdf, infos, ent, x_droite, COL_DROITE)
    pdf.set_y(bas_cartouche + 4)
    bas_droite = _bloc_client(pdf, infos, x_droite, COL_DROITE)
    pdf.set_y(max(bas_gauche, bas_droite) + 6 * pdf.style["echelle"])


def _objet_chantier(pdf, infos):
    """Lignes « Objet » et « Chantier » sous l'en-tête (chacune omise si vide)."""
    chantier = _joindre(f"début des travaux : {infos['date_debut']}" if infos.get("date_debut") else "",
                        f"durée estimée : {infos['duree']}" if infos.get("duree") else "")
    h = 5 * pdf.style["echelle"]
    for libelle, valeur in (("Objet", infos.get("objet")), ("Chantier", chantier)):
        if not valeur:
            continue
        pdf.set_x(pdf.l_margin + pdf.c_margin)
        pdf.police("B", 9.5)
        pdf.write(h, _txt(f"{libelle} : "))
        pdf.police("", 9.5)
        pdf.write(h, _txt(valeur))
        pdf.ln(h + 0.5)


def _entete_tableau(pdf):
    st_ = pdf.style
    h = 7.5 * st_["echelle"]
    pdf.set_x(pdf.l_margin)
    pdf.police("B", 8.5)
    if st_["fond_entete"]:
        pdf.set_fill_color(*pdf.accent)
        pdf.set_text_color(255, 255, 255)
    y = pdf.get_y()
    for titre, largeur, align in COLONNES_PDF:
        pdf.cell(largeur, h, _txt(titre), fill=st_["fond_entete"], align=align)
    pdf.ln(h)
    if not st_["fond_entete"]:
        pdf.filet(y + h, pdf.accent, 0.5)
    pdf.set_text_color(0, 0, 0)


def _saut_de_page_tableau(pdf):
    pdf.add_page()
    _entete_tableau(pdf)


def _ligne_tableau(pdf, lg, zebre):
    """Une ligne : désignation sur plusieurs lignes si besoin, montants à droite ;
    zébrage ou filet fin selon le modèle ; ne se coupe jamais entre deux pages."""
    st_ = pdf.style
    inter = st_["interligne"]
    l_desig = COLONNES_PDF[0][1]
    pdf.police("", 9)
    designation = _txt(_texte(lg.get("designation")))
    n = max(1, len(pdf.multi_cell(l_desig, inter, designation, dry_run=True, output="LINES")))
    h = n * inter + st_["marge_ligne"]
    if pdf.will_page_break(h):
        _saut_de_page_tableau(pdf)
        pdf.police("", 9)  # l'en-tête de page laisse une police grasse
    x, y = pdf.l_margin, pdf.get_y()
    if zebre and st_["zebre"]:
        pdf.set_fill_color(*st_["zebre"])
        pdf.rect(x, y, LARGEUR_UTILE, h, "F")
    pdf.set_xy(x, y + st_["marge_ligne"] / 2)
    pdf.multi_cell(l_desig, inter, designation, align="L")
    pdf.set_xy(x + l_desig, y + st_["marge_ligne"] / 2)
    valeurs = (_txt(_texte(lg.get("unite"))), _qte(lg.get("quantite")),
               pdf.euro(lg.get("prix_unitaire")), pdf.euro(montant_ligne(lg)))
    for (_, largeur, align), valeur in zip(COLONNES_PDF[1:], valeurs):
        pdf.cell(largeur, inter, valeur, align=align)
    if not st_["zebre"]:
        pdf.filet(y + h, GRIS_LEGER, 0.1)
    pdf.set_xy(x, y + h)


def _tableau_lignes(pdf, lignes):
    st_ = pdf.style
    _entete_tableau(pdf)
    if not lignes:
        pdf.police("I", 9)
        pdf.set_text_color(*GRIS_TEXTE)
        pdf.cell(LARGEUR_UTILE, 9, "Aucune prestation chiffrée sur ce devis.", align="C",
                 new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
    groupes = OrderedDict()
    for lg in lignes:
        groupes.setdefault(_texte(lg.get("groupe")), []).append(lg)
    h_section, h_sous_total = 7 * st_["echelle"], 6.5 * st_["echelle"]
    rang = 0
    for nom_groupe, items in groupes.items():
        if nom_groupe:
            if pdf.will_page_break(h_section + 10):   # pas de titre de section seul en bas de page
                _saut_de_page_tableau(pdf)
            pdf.set_x(pdf.l_margin)
            pdf.police("B", 9)
            y = pdf.get_y()
            if st_["fond_section"]:
                pdf.set_fill_color(*_teinte(pdf.accent, 0.85))
                pdf.set_text_color(*pdf.accent)
            pdf.cell(LARGEUR_UTILE, h_section, _txt(nom_groupe), fill=st_["fond_section"],
                     new_x="LMARGIN", new_y="NEXT")
            if not st_["fond_section"]:
                pdf.filet(y + h_section, GRIS_FILET, 0.2)
            pdf.set_text_color(0, 0, 0)
        for lg in items:
            _ligne_tableau(pdf, lg, zebre=rang % 2 == 1)
            rang += 1
        if nom_groupe:
            if pdf.will_page_break(h_sous_total):
                _saut_de_page_tableau(pdf)
            pdf.set_x(pdf.l_margin)
            pdf.police("I", 8.5)
            pdf.set_text_color(*GRIS_TEXTE)
            pdf.cell(LARGEUR_UTILE - 32, h_sous_total, _txt(f"Sous-total {nom_groupe}"), align="R")
            pdf.police("B", 9)
            pdf.set_text_color(0, 0, 0)
            pdf.cell(32, h_sous_total, pdf.euro(sum(montant_ligne(lg) for lg in items)), align="R",
                     new_x="LMARGIN", new_y="NEXT")
    pdf.filet(pdf.get_y(), pdf.accent, 0.4)


def _options_variantes(pdf, infos):
    """Section « Options / variantes » non chiffrées (une option par ligne)."""
    options = [o.strip(" -•\t") for o in (infos.get("options") or "").splitlines()]
    options = [o for o in options if o]
    if not (pdf.mef["section_options"] and options):
        return
    hauteur = 6 + 4.2 * sum(_nb_lignes(pdf, o, LARGEUR_UTILE - 6, 8.5) for o in options)
    if pdf.will_page_break(min(hauteur, 40)):
        pdf.add_page()
    pdf.ln(4)
    _bloc_texte(pdf, pdf.l_margin, LARGEUR_UTILE, "Options / variantes (non comprises dans le total)",
                9, "B", couleur=pdf.accent, interligne=5)
    for option in options:
        _bloc_texte(pdf, pdf.l_margin + 3, LARGEUR_UTILE - 3, f"- {option}", 8.5, interligne=4.2)


def _mentions_legales(pdf) -> list:
    """Pied du devis selon les sections activées ; champs vides omis."""
    e, mef = pdf.ent, pdf.mef
    mentions = []
    if mef["section_paiement"] and e.get("conditions_reglement"):
        mentions.append(f"Conditions de règlement : {e['conditions_reglement']}")
    if mef["section_mentions"]:
        decennale = ""
        if e.get("assurance_nom") or e.get("assurance_police"):
            decennale = _joindre(e.get("assurance_nom"),
                                 f"police n° {e['assurance_police']}" if e.get("assurance_police") else "",
                                 f"couverture : {e['assurance_couverture']}" if e.get("assurance_couverture") else "")
        rc_pro = _joindre(e.get("rc_pro_nom"),
                          f"police n° {e['rc_pro_police']}" if e.get("rc_pro_police") else "")
        mentions += [m for m in (
            f"Assurance décennale : {decennale}" if decennale else "",
            f"Responsabilité civile professionnelle : {rc_pro}" if rc_pro else "",
            f"Qualifications : {e['qualifications']}" if e.get("qualifications") else "",
            e.get("mention_devis") or MENTION_DEVIS_DEFAUT,
        ) if m]
    return mentions


def _bon_pour_accord(pdf, x_cadre, y, l_cadre=88.0) -> float:
    """Cadre signature client : consigne + mention manuscrite, date, signature."""
    h = H_ACCORD
    fond = pdf.style["fond_section"]
    if fond:
        pdf.set_fill_color(*_teinte(pdf.accent, 0.85))
        pdf.rect(x_cadre, y, l_cadre, 7, "F")
    pdf.set_xy(x_cadre + 1, y)
    pdf.police("B", 8.5)
    pdf.set_text_color(*pdf.accent)
    pdf.cell(l_cadre - 2, 7, "BON POUR ACCORD")
    if not fond:
        pdf.filet(y + 7, GRIS_FILET, 0.2, x=x_cadre, largeur=l_cadre)
    pdf.set_xy(x_cadre + 1, y + 8.5)
    pdf.police("I", 7.5)
    pdf.set_text_color(*GRIS_TEXTE)
    pdf.multi_cell(l_cadre - 2, 3.6, _txt(f"Daté, signé et précédé de la mention manuscrite : "
                                          f"{MENTION_ACCORD}"), align="L")
    yy = pdf.get_y() + 2
    pdf.set_text_color(0, 0, 0)
    pdf.police("", 8.5)
    pdf.set_xy(x_cadre + 1, yy)
    pdf.cell(l_cadre - 2, 4.5, "Date :")
    pdf.set_xy(x_cadre + 1, yy + 6.5)
    pdf.cell(l_cadre - 2, 4.5, "Signature du client :")
    pdf.set_draw_color(*(_teinte(pdf.accent, 0.4) if fond else GRIS_FILET))
    pdf.rect(x_cadre, y, l_cadre, h, "D")
    pdf.set_draw_color(0, 0, 0)
    return y + h


def _bloc_final(pdf, infos, totaux):
    """À gauche : remarques puis « Bon pour accord » (si activé) ; à droite :
    totaux. Dessous : conditions / mentions légales. Le tout reste groupé
    sur la dernière page."""
    mef, ent = pdf.mef, pdf.ent
    x, x_tot, l_tot, l_val = pdf.l_margin, pdf.l_margin + 95, 85.0, 34.0
    t = totaux
    marge, remise = _num(t.get("marge_pct")), _num(t.get("remise_montant"))
    lignes_tot = []
    if marge or remise:
        lignes_tot.append(("Sous-total HT", t["sous_total_ht"], ""))
    if marge:
        lignes_tot.append((f"Marge ({marge:g} %)",
                           t.get("total_ht_brut", t["total_ht"]) - t["sous_total_ht"], ""))
    if remise:
        lib = ("Remise" if t.get("remise_type") == "€"
               else f"Remise ({_num(t.get('remise_valeur')):g} %)")
        lignes_tot.append((lib, -remise, ""))
    lignes_tot.append(("Total HT", t["total_ht"], "B"))
    lignes_tot.append((f"TVA {_num(t.get('tva_pct')):g} %", t["montant_tva"], ""))
    pct = _num(ent.get("acompte_pct")) if mef["section_paiement"] else 0.0
    acompte = t["total_ttc"] * pct / 100 if 0 < pct <= 100 else 0.0
    remarques = infos.get("mentions")
    legales = _mentions_legales(pdf)

    # tout le bloc doit tenir sur la page, sinon on passe à la suivante
    h_tot = H_LIGNE_TOTAL * len(lignes_tot) + 10.5 + (6.5 if acompte else 0)
    h_rem = 5 + 4 * _nb_lignes(pdf, remarques, 88, 8.5) if remarques else 0
    h_gauche = (h_rem + 3 if remarques else 0) + (H_ACCORD if mef["section_accord"] else 0)
    h_leg = 5 + 3.4 * sum(_nb_lignes(pdf, m, LARGEUR_UTILE, 7) for m in legales) if legales else 0
    if pdf.will_page_break(4 + max(h_tot, h_gauche) + 5 + h_leg):
        pdf.add_page()

    haut = pdf.get_y() + 4
    y = haut
    for libelle, valeur, style in lignes_tot:
        pdf.set_xy(x_tot, y)
        pdf.police(style, 9.5)
        pdf.cell(l_tot - l_val, H_LIGNE_TOTAL, _txt(libelle), align="R")
        montant = ("- " + pdf.euro(-valeur)) if valeur < 0 else pdf.euro(valeur)
        pdf.cell(l_val, H_LIGNE_TOTAL, montant, align="R")
        y += H_LIGNE_TOTAL
    y += 1.5
    pdf.set_xy(x_tot, y)
    pdf.police("B", 11)
    if pdf.style["fond_ttc"]:
        pdf.set_fill_color(*pdf.accent)
        pdf.rect(x_tot, y, l_tot, 9, "F")
        pdf.set_text_color(255, 255, 255)
    else:
        pdf.filet(y, pdf.accent, 0.5, x=x_tot, largeur=l_tot)
        pdf.filet(y + 9, pdf.accent, 0.5, x=x_tot, largeur=l_tot)
    pdf.cell(l_tot - l_val, 9, "TOTAL TTC", align="R")
    pdf.cell(l_val, 9, pdf.euro(t["total_ttc"]), align="R")
    pdf.set_text_color(0, 0, 0)
    y += 9
    if acompte:
        pdf.set_xy(x_tot, y + 1)
        pdf.police("I", 9)
        pdf.cell(l_tot - l_val, 5.5, _txt(f"Acompte à la commande ({pct:g} %)"), align="R")
        pdf.police("B", 9)
        pdf.cell(l_val, 5.5, pdf.euro(acompte), align="R")
        y += 6.5
    bas_droite, y_gauche = y, haut
    if remarques:
        pdf.set_xy(x, haut)
        pdf.police("B", 9)
        pdf.cell(88, 5, "Remarques", new_x="LMARGIN", new_y="NEXT")
        _bloc_texte(pdf, x, 88, remarques, 8.5, interligne=4)
        y_gauche = pdf.get_y() + 3
    bas_gauche = _bon_pour_accord(pdf, x, y_gauche) if mef["section_accord"] else y_gauche

    if legales:
        pdf.set_y(max(bas_droite, bas_gauche) + 5)
        pdf.set_x(x)
        pdf.police("B", 7.5)
        pdf.set_text_color(*GRIS_TEXTE)
        titre = "Informations légales" if mef["section_mentions"] else "Conditions de paiement"
        pdf.cell(LARGEUR_UTILE, 4.5, titre, new_x="LMARGIN", new_y="NEXT")
        for mention in legales:
            _bloc_texte(pdf, x, LARGEUR_UTILE, mention, 7, couleur=GRIS_TEXTE, interligne=3.4)


def generer_pdf_devis(entreprise, infos, lignes, totaux, mise_en_forme=None):
    """Devis PDF. Un seul moteur : le modèle (classique / moderne / compact),
    la couleur, le logo et les sections viennent de `mise_en_forme` (complété
    par les préférences de l'entreprise puis les défauts). Les montants
    viennent de `totaux` (calculer_totaux) ; seul le sous-total de section est
    sommé ici. Les coordonnées bancaires ne figurent jamais sur le devis.
    `infos["options"]` : options / variantes non chiffrées, une par ligne."""
    ent = {k: _texte(v) for k, v in (entreprise or {}).items() if not isinstance(v, dict)}
    mef = normaliser_mise_en_forme(mise_en_forme_entreprise(entreprise or {}), mise_en_forme)
    infos = {k: _texte(v) for k, v in (infos or {}).items()}
    pdf = _PDFDevis(ent, infos, mef)
    pdf.add_page()
    _entete_page1(pdf, ent, infos)
    _objet_chantier(pdf, infos)
    pdf.ln(2)
    _tableau_lignes(pdf, lignes or [])
    _options_variantes(pdf, infos)
    _bloc_final(pdf, infos, totaux)
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


def panneau_comptes(zone=None):
    zone = zone or st.sidebar
    if not st.session_state.get("is_admin"):
        with zone.expander("🔑 Changer mon mot de passe", expanded=zone is st):
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

    with zone.expander("👤 Gestion des comptes (admin)", expanded=zone is st):
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
# MISE EN FORME DU DEVIS (panneau de choix + aperçu)
# ----------------------------------------------------------------------------
_EXEMPLE_APERCU = {
    "infos": {"numero": "DEV-EXEMPLE", "date": date.today().strftime("%d/%m/%Y"),
              "client": "M. et Mme Exemple", "client_adresse": "1 rue de l'Exemple\n69000 Lyon",
              "objet": "Remplacement chaudière par pompe à chaleur", "duree": "3 jours",
              "options": "Robinets thermostatiques connectés\nDésembouage du réseau"},
    "lignes": [
        {"groupe": "Dépose", "designation": "Dépose chaudière existante", "unite": "forfait",
         "quantite": 1, "prix_unitaire": 450.0},
        {"groupe": "Installation", "designation": "Pompe à chaleur air/eau 11 kW", "unite": "u",
         "quantite": 1, "prix_unitaire": 7900.0},
        {"groupe": "Installation", "designation": "Tube cuivre 22 mm", "unite": "ml",
         "quantite": 18, "prix_unitaire": 24.0},
        {"groupe": "Installation", "designation": "Mise en service", "unite": "h",
         "quantite": 3, "prix_unitaire": 65.0},
    ],
}


@st.cache_data(show_spinner=False, max_entries=64)
def _apercu_png(ent_json, mef_json, devis_json, signature_logo):
    """Page 1 du PDF en image (None si pypdfium2 n'est pas installé)."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return None
    d = json.loads(devis_json)
    pdf = generer_pdf_devis(json.loads(ent_json), d["infos"], d["lignes"], d["totaux"],
                            mise_en_forme=json.loads(mef_json))
    image = pdfium.PdfDocument(pdf)[0].render(scale=1.3).to_pil()
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def afficher_apercu(ent, mef, infos=None, lignes=None, totaux=None):
    """Aperçu de la page 1 avec les choix courants (devis réel ou exemple)."""
    if not lignes:
        infos, lignes = _EXEMPLE_APERCU["infos"], _EXEMPLE_APERCU["lignes"]
        totaux = calculer_totaux(lignes, 0, mef["tva_defaut"])
    signature_logo = LOGO_PATH.stat().st_mtime if LOGO_PATH.exists() else 0
    png = _apercu_png(json.dumps(ent, ensure_ascii=False, sort_keys=True, default=str),
                      json.dumps(mef, sort_keys=True),
                      json.dumps({"infos": infos, "lignes": lignes, "totaux": totaux},
                                 ensure_ascii=False, sort_keys=True, default=str),
                      signature_logo)
    if png:
        st.image(png, caption="Aperçu — page 1", width="stretch")
    else:
        st.info("Aperçu indisponible : installe le module pypdfium2 (requirements.txt).")


def panneau_mise_en_forme(base: dict, cle: str) -> dict:
    """Choix de mise en forme, préremplis avec `base`. Retourne le dict normalisé.
    `cle` distingue les instances (entreprise, nouveau devis, devis en édition)."""
    modeles = list(MODELES)
    modele = st.radio("Modèle de devis", modeles, index=modeles.index(base["modele"]),
                      format_func=MODELES.get, horizontal=True, key=f"{cle}_modele")
    st.caption(DESCRIPTION_MODELES[modele])

    noms = list(PALETTE_ACCENT) + ["Personnalisée"]
    actuel = next((n for n, h in PALETTE_ACCENT.items() if h == base["couleur_accent"]), "Personnalisée")
    c1, c2 = st.columns([2, 1])
    choix = c1.selectbox("Couleur d'accent", noms, index=noms.index(actuel), key=f"{cle}_palette")
    if choix == "Personnalisée":
        couleur = c2.color_picker("Code couleur", base["couleur_accent"], key=f"{cle}_hex")
    else:
        couleur = PALETTE_ACCENT[choix]
        c2.color_picker("Code couleur", couleur, disabled=True, key=f"{cle}_hex_{choix}")

    c3, c4 = st.columns([1, 2])
    logo = c3.toggle("Afficher le logo", base["logo_afficher"], key=f"{cle}_logo")
    positions = list(POSITIONS_LOGO)
    position = c4.radio("Position du logo", positions, index=positions.index(base["logo_position"]),
                        format_func=POSITIONS_LOGO.get, horizontal=True, disabled=not logo,
                        key=f"{cle}_logo_pos")

    st.markdown("**Sections incluses**")
    sections = {k: st.checkbox(lib, base[k], key=f"{cle}_{k}") for k, lib in SECTIONS_DEVIS.items()}

    c5, c6 = st.columns(2)
    tva = c5.selectbox("Taux de TVA par défaut", TVA_AUTORISEES,
                       index=TVA_AUTORISEES.index(base["tva_defaut"]),
                       format_func=lambda v: f"{v:g} %", key=f"{cle}_tva")
    devise = c6.selectbox("Devise d'affichage", DEVISES, index=DEVISES.index(base["devise"]),
                          key=f"{cle}_devise")
    return normaliser_mise_en_forme({"modele": modele, "couleur_accent": couleur,
                                     "logo_afficher": logo, "logo_position": position,
                                     "tva_defaut": tva, "devise": devise, **sections})


def choix_mise_en_forme_devis(mef_entreprise, surcharge, cle, options_init=""):
    """Bloc « Mise en forme de ce devis » : suit l'entreprise ou personnalise.
    Retourne (mise en forme effective, surcharge à stocker ou None, options)."""
    perso = st.checkbox("Personnaliser la mise en forme pour ce devis",
                        value=surcharge is not None, key=f"{cle}_perso",
                        help="Décoché : le devis suit les préférences de l'entreprise "
                             "(Mon entreprise > Présentation des devis).")
    if perso:
        surcharge = panneau_mise_en_forme(normaliser_mise_en_forme(mef_entreprise, surcharge), cle)
    else:
        surcharge = None
        st.caption(f"Préférences de l'entreprise : modèle {MODELES[mef_entreprise['modele']]}.")
    effective = normaliser_mise_en_forme(mef_entreprise, surcharge)
    options = ""
    if effective["section_options"]:
        options = st.text_area("Options / variantes non chiffrées (une par ligne)",
                               value=options_init, key=f"{cle}_options", height=80)
    return effective, surcharge, options


# ----------------------------------------------------------------------------
# ÉDITION D'UN DEVIS EXISTANT
# ----------------------------------------------------------------------------
def _df_edition(lignes) -> pd.DataFrame:
    """Lignes JSON -> DataFrame pour l'éditeur (colonne « ordre » pour réordonner)."""
    df = pd.DataFrame(lignes)
    for c in COLS_EDITION:
        if c not in df.columns:
            df[c] = None
    df = df[COLS_EDITION].reset_index(drop=True)
    # Types forcés : sur un devis sans ligne, pandas typerait les colonnes texte
    # en float et st.data_editor refuserait de les éditer.
    df["ordre"] = pd.Series([float(i) for i in range(1, len(df) + 1)], index=df.index, dtype=float)
    for c in ("quantite", "prix_unitaire", "prix_fourniture", "mo_unit"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    # anciennes lignes sans PU : PU = fourniture + MO
    sans_pu = df["prix_unitaire"].isna()
    df.loc[sans_pu, "prix_unitaire"] = (df.loc[sans_pu, "prix_fourniture"].fillna(0)
                                        + df.loc[sans_pu, "mo_unit"].fillna(0))
    df["mo_active"] = pd.Series([True if _vide(v) else bool(v) for v in df["mo_active"]],
                                index=df.index, dtype=bool)
    for c in ("groupe", "designation", "unite"):
        df[c] = pd.Series([_texte(v) for v in df[c]], index=df.index, dtype="string")
    return df


def _ligne_blanche(r) -> bool:
    return all(_vide(r.get(c)) for c in ("designation", "unite", "quantite", "prix_unitaire"))


def _ventiler_pu(pu, fourn, mo, mo_active):
    """Garde fourniture / MO cohérents avec le PU saisi : le flux « Dupliquer »
    recalcule PU = fourniture + MO, il doit retrouver le même prix."""
    mo_app = mo if mo_active else 0.0
    if abs(fourn + mo_app - pu) < 0.005:
        return fourn, mo, mo_active
    if pu >= mo_app:
        return pu - mo_app, mo, mo_active
    return pu, mo, False


def lignes_depuis_editeur(df):
    """Nettoie, valide et trie les lignes saisies. Retourne (lignes, erreurs)."""
    enregs = list(enumerate(df.to_dict("records"), start=1))
    enregs.sort(key=lambda p: (_vide(p[1].get("ordre")), _num(p[1].get("ordre")), p[0]))
    lignes, erreurs = [], []
    for n, r in enregs:
        desig, unite = _texte(r.get("designation")), _texte(r.get("unite"))
        q, pu = r.get("quantite"), r.get("prix_unitaire")
        if _ligne_blanche(r):
            continue  # ligne vide ajoutée puis laissée telle quelle : ignorée
        err = []
        if not desig:
            err.append(f"Ligne {n} : désignation obligatoire.")
        if _num(q) < 0:
            err.append(f"Ligne {n} : quantité négative.")
        if _num(pu) < 0:
            err.append(f"Ligne {n} : prix unitaire négatif.")
        if err:
            erreurs.extend(err)
            continue
        q, pu = _num(q), _num(pu)
        mo_active = True if _vide(r.get("mo_active")) else bool(r.get("mo_active"))
        fourn, mo, mo_active = _ventiler_pu(pu, _num(r.get("prix_fourniture")),
                                            _num(r.get("mo_unit")), mo_active)
        lignes.append({"groupe": _texte(r.get("groupe")), "designation": desig, "unite": unite,
                       "quantite": q, "prix_fourniture": fourn, "mo_unit": mo,
                       "mo_active": bool(mo_active), "prix_unitaire": pu, "montant_ht": q * pu})
    return lignes, erreurs


def _figer_edition(etat, df):
    """Remplace la source de l'éditeur et change sa clé (sinon Streamlit
    ré-appliquerait les anciennes modifications sur la nouvelle source)."""
    garder = [not _ligne_blanche(r) for r in df.to_dict("records")]
    etat["df"] = df[garder].reset_index(drop=True)
    etat["version"] += 1


def _fermer_edition(message=None):
    st.session_state.pop("ed_etat", None)
    if message:
        st.session_state["flash"] = message
    st.rerun()  # rerun complet = fermeture du dialog


@st.dialog("✏️ Modifier le devis", width="large")
def dialog_modifier_devis(numero, taux_horaire):
    etat = st.session_state.get("ed_etat")
    if not etat or etat.get("numero") != numero:
        tous = charger_devis()
        sel = tous[tous["numero"] == numero]
        if sel.empty:
            st.error(f"Devis {numero} introuvable.")
            return
        rec = sel.iloc[0].to_dict()
        illisible = False
        try:
            lignes_init = json.loads(rec["lignes_json"]) if not _vide(rec.get("lignes_json")) else []
        except Exception:
            lignes_init, illisible = [], True
        comp = complement_devis(numero)
        tva = _num(rec.get("tva_pct"), 20.0)
        try:
            d = date.fromisoformat(_texte(rec.get("date_devis")))
        except ValueError:
            d = date.today()
        etat = {"numero": numero, "rec": rec, "date_modif": comp["date_modif"],
                "mef": comp["mise_en_forme"], "options": comp["options_variantes"],
                "jeton": pysecrets.token_hex(4), "version": 0, "df": _df_edition(lignes_init),
                "tva_hors_liste": tva not in TVA_AUTORISEES, "lignes_illisibles": illisible}
        st.session_state["ed_etat"] = etat
        st.session_state.update({
            "ed_client_nom": _texte(rec.get("client_nom")),
            "ed_client_adresse": _texte(rec.get("client_adresse")),
            "ed_date": d, "ed_tva": tva if tva in TVA_AUTORISEES else 20.0,
            "ed_date_debut": _texte(rec.get("date_debut")), "ed_duree": _texte(rec.get("duree")),
            "ed_marge": _num(rec.get("marge_pct")), "ed_remise_type": comp["remise_type"],
            "ed_remise_valeur": comp["remise_valeur"], "ed_mentions": comp["mentions"]})

    rec = etat["rec"]
    st.caption(f"Devis **{numero}** — statut : {_texte(rec.get('statut')) or 'En attente'}"
               + (f" — dernière modif : {etat['date_modif']}" if etat["date_modif"] else ""))
    if etat["lignes_illisibles"]:
        st.error("Les lignes enregistrées sont illisibles : enregistrement bloqué pour ne pas les écraser.")
    if etat["tva_hors_liste"]:
        st.warning(f"TVA enregistrée ({_num(rec.get('tva_pct')):g} %) hors liste autorisée : "
                   "remplacée par 20 %, vérifie avant d'enregistrer.")

    # ---- En-tête ----
    c1, c2, c3 = st.columns([2, 1, 1])
    client_nom = c1.text_input("Client", key="ed_client_nom")
    date_devis = c2.date_input("Date", key="ed_date", format="DD/MM/YYYY")
    tva_pct = c3.selectbox("TVA", TVA_AUTORISEES, key="ed_tva", format_func=lambda v: f"{v:g} %")
    client_adresse = st.text_area("Adresse du client", key="ed_client_adresse", height=68)
    c4, c5 = st.columns(2)
    date_debut = c4.text_input("Début des travaux", key="ed_date_debut")
    duree = c5.text_input("Durée estimée", key="ed_duree")
    c6, c7, c8 = st.columns(3)
    marge_pct = c6.number_input("Marge (%)", min_value=0.0, step=1.0, key="ed_marge")
    remise_type = c7.radio("Remise globale en", ["%", "€"], horizontal=True, key="ed_remise_type")
    remise_valeur = c8.number_input(f"Remise ({remise_type})", min_value=0.0, step=1.0,
                                    key="ed_remise_valeur")
    mentions = st.text_area("Mentions libres (imprimées sur le PDF)", key="ed_mentions", height=68)

    # ---- Lignes ----
    st.markdown("**Lignes du devis**")
    st.caption("« + » sous le tableau = ligne vide · cocher une ligne puis 🗑️ = suppression · "
               "pour réordonner, change « Ordre » puis « Appliquer l'ordre ».")
    edite = st.data_editor(
        etat["df"], num_rows="dynamic", use_container_width=True,
        column_order=["ordre", "groupe", "designation", "unite", "quantite", "prix_unitaire"],
        column_config={
            "ordre": st.column_config.NumberColumn("Ordre", step=1, format="%d", width="small"),
            "groupe": st.column_config.TextColumn("Ensemble"),
            "designation": st.column_config.TextColumn("Désignation", width="large"),
            "unite": st.column_config.TextColumn("Unité", width="small"),
            "quantite": st.column_config.NumberColumn("Qté", min_value=0.0, format="%.2f"),
            "prix_unitaire": st.column_config.NumberColumn("PU HT (€)", min_value=0.0, format="%.2f")},
        key=f"ed_lignes_{etat['jeton']}_{etat['version']}")

    if st.button("↕️ Appliquer l'ordre", key="ed_ordre"):
        df = edite.copy()
        df["_pos"] = range(len(df))
        df = df.sort_values(["ordre", "_pos"], na_position="last").drop(columns="_pos")
        df["ordre"] = [float(i) for i in range(1, len(df) + 1)]
        _figer_edition(etat, df)
        st.rerun(scope="fragment")

    with st.expander("📚 Ajouter une ligne depuis la bibliothèque"):
        biblio = charger_bibliotheque()
        rech = st.text_input("🔍 Filtrer", key="ed_rech")
        options = [i for i in biblio.index
                   if not rech or rech.lower() in str(biblio.loc[i, "designation"]).lower()
                   or rech.lower() in str(biblio.loc[i, "code"]).lower()]
        if not options:
            st.caption("Aucun ouvrage." if biblio.empty else "Aucun ouvrage ne correspond.")
        else:
            b1, b2, b3 = st.columns([3, 1, 1])
            choix = b1.selectbox("Ouvrage", options, key="ed_biblio_choix",
                                 format_func=lambda i: f"{biblio.loc[i, 'designation']} ({biblio.loc[i, 'unite']})")
            qte = b2.number_input("Quantité", min_value=0.0, value=1.0, step=1.0, key="ed_biblio_qte")
            if b3.button("➕ Ajouter", key="ed_biblio_ajout", use_container_width=True):
                o = biblio.loc[choix].to_dict()
                fourn = _num(o.get("prix_fourniture"))
                mo = prix_mo(_num(o.get("temps_mo")), _num(o.get("nb_poseurs")),
                             _num(o.get("taux_horaire")) or taux_horaire)
                nouvelle = {"ordre": _num(edite["ordre"].max()) + 1, "groupe": "",
                            "designation": _texte(o.get("designation")), "unite": _texte(o.get("unite")),
                            "quantite": qte, "prix_unitaire": fourn + mo, "prix_fourniture": fourn,
                            "mo_unit": mo, "mo_active": True}
                _figer_edition(etat, pd.concat([edite, pd.DataFrame([nouvelle])], ignore_index=True))
                st.rerun(scope="fragment")

    # ---- Validation + recalcul en direct ----
    lignes, erreurs = lignes_depuis_editeur(edite)
    totaux = calculer_totaux(lignes, marge_pct, tva_pct, remise_type, remise_valeur)
    if remise_type == "%" and remise_valeur > 100:
        erreurs.append("Remise : 100 % maximum.")
    if remise_type == "€" and remise_valeur > totaux["total_ht_brut"] + 1e-9:
        erreurs.append("Remise supérieure au total HT.")
    for e in erreurs:
        st.warning(e)

    if lignes:
        with st.expander("Total HT par ligne (recalculé)", expanded=True):
            st.dataframe(pd.DataFrame([{"Désignation": l["designation"], "Qté": l["quantite"],
                                        "Unité": l["unite"], "PU HT (€)": l["prix_unitaire"],
                                        "Total HT (€)": l["montant_ht"]} for l in lignes]),
                         hide_index=True, use_container_width=True,
                         column_config={"PU HT (€)": st.column_config.NumberColumn(format="%.2f"),
                                        "Total HT (€)": st.column_config.NumberColumn(format="%.2f")})
    else:
        st.info("Aucune ligne : le devis (et son PDF) sera à 0 €.")

    def eur(x):
        return f"{x:,.2f} €".replace(",", " ")

    m = st.columns(3) + st.columns(3)  # 2 rangées : lisible même en fenêtre étroite
    m[0].metric("Sous-total HT", eur(totaux["sous_total_ht"]))
    m[1].metric(f"Marge {totaux['marge_pct']:g} %",
                eur(totaux["total_ht_brut"] - totaux["sous_total_ht"]))
    m[2].metric("Remise", eur(-totaux["remise_montant"]) if totaux["remise_montant"] else "—")
    m[3].metric("Total HT", eur(totaux["total_ht"]))
    m[4].metric(f"TVA {tva_pct:g} %", eur(totaux["montant_tva"]))
    m[5].metric("Total TTC", eur(totaux["total_ttc"]))

    # ---- Mise en forme propre à ce devis ----
    ent = charger_entreprise()
    with st.expander("🎨 Mise en forme de ce devis"):
        mef_devis, surcharge_devis, options_devis = choix_mise_en_forme_devis(
            mise_en_forme_entreprise(ent), etat["mef"], f"ed_mef_{etat['jeton']}", etat["options"])

    # ---- Actions ----
    bloque = bool(erreurs) or etat["lignes_illisibles"]
    vj = str(ent.get("validite_jours") or "").strip()
    infos = {"numero": numero, "client": client_nom, "client_adresse": client_adresse,
             "client_tel": "", "date": date_devis.strftime("%d/%m/%Y"),
             "validite": (date_devis + timedelta(days=int(vj))).strftime("%d/%m/%Y") if vj.isdigit() else None,
             "validite_jours": ent.get("validite_jours"), "date_debut": date_debut,
             "duree": duree, "mentions": mentions.strip(), "options": options_devis}
    champs = {"date_devis": str(date_devis), "client_nom": client_nom.strip(),
              "client_adresse": client_adresse, "date_debut": date_debut, "duree": duree,
              "marge_pct": float(marge_pct), "tva_pct": float(tva_pct),
              "lignes_json": json.dumps(lignes, ensure_ascii=False)}

    a1, a2, a3, a4 = st.columns(4)
    if a1.button("💾 Enregistrer", type="primary", disabled=bloque, use_container_width=True):
        if mettre_a_jour_devis(numero, champs):
            enregistrer_complement(numero, remise_type, remise_valeur, mentions.strip())
            enregistrer_mise_en_forme(numero, surcharge_devis, options_devis)
            _fermer_edition(f"Devis {numero} mis à jour.")
        else:
            st.error("Ce devis n'existe plus en base.")
    if a2.button("🆕 Enregistrer comme nouveau", disabled=bloque, use_container_width=True):
        nouveau = prochain_numero()
        if sauvegarder_devis({"numero": nouveau, **champs, "statut": "En attente"}):
            enregistrer_complement(nouveau, remise_type, remise_valeur, mentions.strip())
            enregistrer_mise_en_forme(nouveau, surcharge_devis, options_devis)
            if not st.session_state.get("nd_enregistre"):  # évite un doublon côté « Nouveau devis »
                st.session_state.numero_courant = prochain_numero()
            _fermer_edition(f"Nouveau devis {nouveau} créé à partir de {numero}.")
        else:
            st.error("Numéro déjà pris, réessaie.")
    pdf_bytes = b""
    if not bloque:
        try:
            pdf_bytes = generer_pdf_devis(ent, infos, lignes, totaux, mise_en_forme=mef_devis)
        except Exception as exc:
            st.error(f"Génération PDF impossible : {exc}")
    a3.download_button("📄 Régénérer le PDF", pdf_bytes, f"{numero}.pdf", "application/pdf",
                       disabled=bloque or not pdf_bytes, use_container_width=True, key="ed_pdf")
    if a4.button("✖️ Annuler", use_container_width=True):
        _fermer_edition()


# ----------------------------------------------------------------------------
# NOUVEAU DEVIS (parcours simplifié : un écran, 4 étapes)
# ----------------------------------------------------------------------------
NOUVEAU_CLIENT = "➕ Nouveau client"
NB_POSTES_FREQUENTS = 8


def _eur(x) -> str:
    return f"{_num(x):,.2f} €".replace(",", " ").replace(".", ",")


def _prix_ouvrage(o, taux_horaire):
    """(fourniture, main-d'œuvre) d'une unité d'un ouvrage de la bibliothèque."""
    fourn = _num(o.get("prix_fourniture"))
    mo = prix_mo(_num(o.get("temps_mo")), _num(o.get("nb_poseurs")),
                 _num(o.get("taux_horaire")) or taux_horaire)
    return fourn, mo


def _nouvelle_ligne(designation="", unite="u", quantite=1.0, fourn=0.0, mo=0.0, groupe="",
                    pu=None, mo_active=True) -> dict:
    """Ligne de devis en cours de saisie (« _id » sert de clé aux champs à l'écran)."""
    return {"_id": pysecrets.token_hex(4), "groupe": groupe, "designation": designation,
            "unite": unite or "u", "quantite": float(quantite), "prix_fourniture": fourn,
            "mo_unit": mo, "mo_active": mo_active,
            "prix_unitaire": fourn + mo if pu is None else pu}


def _ajouter_ouvrage(o, taux_horaire, quantite=1.0, groupe=""):
    fourn, mo = _prix_ouvrage(o, taux_horaire)
    sans_mo = st.session_state.get("nd_sans_mo", False)
    st.session_state.devis_lignes.append(_nouvelle_ligne(
        _texte(o.get("designation")), _texte(o.get("unite")), quantite, fourn, mo, groupe,
        pu=fourn + (0 if sans_mo else mo)))


def lignes_propres(lignes) -> list:
    """Lignes prêtes à enregistrer : sans « _id », fourniture / MO cohérents avec
    le prix unitaire saisi, montant recalculé."""
    propres = []
    for lg in lignes:
        q, pu = _num(lg.get("quantite")), _num(lg.get("prix_unitaire"))
        fourn, mo, mo_active = _ventiler_pu(pu, _num(lg.get("prix_fourniture")), _num(lg.get("mo_unit")),
                                            True if _vide(lg.get("mo_active")) else bool(lg.get("mo_active")))
        propres.append({"groupe": _texte(lg.get("groupe")), "designation": _texte(lg.get("designation")),
                        "unite": _texte(lg.get("unite")), "quantite": q, "prix_fourniture": fourn,
                        "mo_unit": mo, "mo_active": bool(mo_active), "prix_unitaire": pu,
                        "montant_ht": q * pu})
    return propres


def postes_frequents(biblio, devis, favoris, n=NB_POSTES_FREQUENTS) -> list:
    """Index de la bibliothèque à proposer en accès rapide : les favoris choisis,
    complétés par les ouvrages les plus utilisés dans les devis enregistrés."""
    compte = {}
    for brut in devis.get("lignes_json", []):
        try:
            for lg in json.loads(brut) if not _vide(brut) else []:
                d = _texte(lg.get("designation"))
                compte[d] = compte.get(d, 0) + 1
        except (ValueError, AttributeError):
            continue
    par_designation = {}
    for i in biblio.index:
        par_designation.setdefault(_texte(biblio.loc[i, "designation"]), i)
    ordre = list(favoris) + sorted(compte, key=compte.get, reverse=True)
    index = []
    for d in ordre:
        i = par_designation.get(d)
        if i is not None and i not in index:
            index.append(i)
    return index[:n]


TAUX_HORAIRE_DEFAUT = 45.0
_CLES_NON_CONSERVEES = ("nd_fav_", "nd_x_", "nd_pdf", "nd_excel", "nd_recherche", "nd_favoris_sel")


def _conserver_saisie():
    """Streamlit oublie les champs d'un écran dès qu'on en affiche un autre :
    on reconduit la saisie du devis en cours pour pouvoir naviguer sans rien perdre."""
    for cle in list(st.session_state):
        c = str(cle)
        if c.startswith(("nd_", "f_")) and not c.startswith(_CLES_NON_CONSERVEES):
            st.session_state[cle] = st.session_state[cle]


def _reinitialiser_devis(message=None):
    """Repart d'un devis vierge : efface la saisie en cours et prend le numéro suivant."""
    for cle in [c for c in st.session_state if str(c).startswith(("nd_", "f_"))]:
        del st.session_state[cle]
    # Les champs de l'en-tête restent affichés à chaque run : leur widget garde
    # l'ancienne valeur si on se contente de supprimer la clé. On les vide donc
    # explicitement (un callback peut écrire une clé de widget avant son rendu).
    for cle in ("f_client_nom", "f_client_adresse", "f_date_debut", "f_duree",
                "f_objet", "nd_remarques"):
        st.session_state[cle] = ""
    st.session_state["nd_client_sel"] = NOUVEAU_CLIENT
    st.session_state.devis_lignes = []
    st.session_state.numero_courant = prochain_numero()
    if message:
        st.session_state["flash"] = message


def _cb_client(clients):
    nom = st.session_state.get("nd_client_sel")
    if nom and nom != NOUVEAU_CLIENT:
        fiche = clients[clients["nom"] == nom].iloc[0]
        st.session_state["f_client_nom"] = nom
        st.session_state["f_client_adresse"] = _texte(fiche.get("adresse"))
    else:
        st.session_state["f_client_nom"] = ""
        st.session_state["f_client_adresse"] = ""


def _cb_dupliquer(devis):
    numero = st.session_state.get("nd_dup_sel")
    sel = devis[devis["numero"] == numero]
    if sel.empty:
        return
    rec = sel.iloc[0]
    try:
        anciennes = json.loads(rec["lignes_json"]) if not _vide(rec.get("lignes_json")) else []
    except ValueError:
        anciennes = []
    _reinitialiser_devis(f"Devis {numero} repris comme base : vérifie le client et les quantités.")
    st.session_state.devis_lignes = [
        _nouvelle_ligne(_texte(lg.get("designation")), _texte(lg.get("unite")), _num(lg.get("quantite")),
                        _num(lg.get("prix_fourniture")), _num(lg.get("mo_unit")), _texte(lg.get("groupe")),
                        pu=_num(lg.get("prix_unitaire")) if not _vide(lg.get("prix_unitaire")) else None,
                        mo_active=True if _vide(lg.get("mo_active")) else bool(lg.get("mo_active")))
        for lg in anciennes]
    st.session_state["f_client_nom"] = _texte(rec.get("client_nom"))
    st.session_state["f_client_adresse"] = _texte(rec.get("client_adresse"))
    st.session_state["f_date_debut"] = _texte(rec.get("date_debut"))
    st.session_state["f_duree"] = _texte(rec.get("duree"))
    st.session_state["nd_marge"] = _num(rec.get("marge_pct"))
    if _num(rec.get("tva_pct"), -1) in TVA_AUTORISEES:
        st.session_state["nd_tva"] = _num(rec.get("tva_pct"))


def _cb_recherche(biblio, ensembles, taux_horaire):
    """Choisir dans la barre de recherche = ajouter tout de suite (quantité 1)."""
    choix = st.session_state.get("nd_recherche")
    if not choix:
        return
    genre, cle = choix
    if genre == "e":
        nb = 0
        for _, comp in ensembles[ensembles["nom"] == cle].iterrows():
            trouve = biblio[biblio["designation"] == comp["designation"]]
            if not trouve.empty:
                _ajouter_ouvrage(trouve.iloc[0].to_dict(), taux_horaire, _num(comp.get("quantite"), 1.0), cle)
                nb += 1
        st.session_state["flash"] = f"Ensemble « {cle} » ajouté ({nb} ligne(s))."
    else:
        o = biblio.loc[cle].to_dict()
        _ajouter_ouvrage(o, taux_horaire)
        st.session_state["flash"] = f"« {_texte(o.get('designation'))} » ajouté : indique la quantité."
    st.session_state["nd_recherche"] = None


def _cb_favori(o, taux_horaire):
    _ajouter_ouvrage(o, taux_horaire)
    st.session_state["flash"] = f"« {_texte(o.get('designation'))} » ajouté : indique la quantité."


def _cb_ligne_libre():
    st.session_state.devis_lignes.append(_nouvelle_ligne())


def _cb_supprimer(identifiant):
    st.session_state.devis_lignes = [lg for lg in st.session_state.devis_lignes
                                     if lg.get("_id") != identifiant]


def _cb_sans_mo():
    """Bascule « fourniture seule » : recalcule le prix unitaire des lignes
    venues de la bibliothèque (fourniture, avec ou sans main-d'œuvre)."""
    sans_mo = st.session_state.get("nd_sans_mo", False)
    for lg in st.session_state.devis_lignes:
        if _num(lg.get("mo_unit")):
            pu = _num(lg.get("prix_fourniture")) + (0 if sans_mo else _num(lg.get("mo_unit")))
            lg["prix_unitaire"] = pu
            st.session_state[f"nd_pu_{lg['_id']}"] = pu


def onglet_nouveau_devis(ent, mef_entreprise, taux_horaire):
    lignes = st.session_state.devis_lignes
    for lg in lignes:
        lg.setdefault("_id", pysecrets.token_hex(4))
    biblio, clients, devis = charger_bibliotheque(), charger_clients(), charger_devis()
    ensembles = charger_ensembles()
    numero = st.session_state.numero_courant
    deja_enregistre = st.session_state.get("nd_enregistre", False)
    st.session_state.setdefault("nd_tva", mef_entreprise["tva_defaut"])
    st.session_state.setdefault("nd_marge", 0.0)
    st.session_state.setdefault("nd_date", date.today())

    # ---- En-tête : numéro + points de départ ----
    h1, h2, h3 = st.columns([3, 2, 2], vertical_alignment="center")
    h1.subheader(f"Devis n° {numero}")
    with h2.popover("📋 Partir d'un ancien devis", use_container_width=True, disabled=devis.empty):
        if not devis.empty:
            libelles = {r["numero"]: f"{r['numero']} — {_texte(r['client_nom'])} — {r['date_devis']}"
                        for _, r in devis.iterrows()}
            st.selectbox("Devis à reprendre", devis["numero"].tolist()[::-1],
                         format_func=lambda n: libelles.get(n, n), key="nd_dup_sel")
            if lignes:
                st.caption("⚠️ Remplace le devis en cours de saisie.")
            st.button("Utiliser ce devis comme base", type="primary", use_container_width=True,
                      on_click=_cb_dupliquer, args=(devis,))
    with h3.popover("🆕 Nouveau devis vierge", use_container_width=True):
        st.write("La saisie en cours sera effacée." if lignes and not deja_enregistre
                 else "Repartir d'un devis vide.")
        st.button("Oui, repartir de zéro", type="primary", use_container_width=True,
                  on_click=_reinitialiser_devis, args=("Nouveau devis vierge prêt.",))
    if deja_enregistre:
        st.success(f"Devis {numero} enregistré. Si tu le modifies, clique de nouveau sur "
                   "« Enregistrer » pour le mettre à jour. Sinon : « 🆕 Nouveau devis vierge ».")

    # ---- ① Client ----
    st.markdown("#### ① Client")
    noms = sorted({_texte(n) for n in clients["nom"]} - {""})
    if noms:
        st.selectbox("Client déjà enregistré", [NOUVEAU_CLIENT] + noms, key="nd_client_sel",
                     on_change=_cb_client, args=(clients,),
                     help="Tape quelques lettres pour le retrouver : nom et adresse se remplissent.")
    c1, c2 = st.columns(2)
    client_nom = c1.text_input("Nom du client", key="f_client_nom")
    client_adresse = c2.text_area("Adresse", key="f_client_adresse", height=80)
    client_inconnu = bool(client_nom.strip()) and client_nom.strip() not in noms
    st.session_state.setdefault("nd_memoriser", True)
    memoriser = (st.checkbox("Mémoriser ce client pour les prochains devis", key="nd_memoriser")
                 if client_inconnu else False)
    with st.expander("Plus d'infos : date, objet, chantier, remarques"):
        d1, d2, d3 = st.columns(3)
        date_devis = d1.date_input("Date du devis", format="DD/MM/YYYY", key="nd_date")
        date_debut = d2.text_input("Début des travaux", key="f_date_debut")
        duree = d3.text_input("Durée estimée", key="f_duree")
        objet = st.text_input("Objet du devis", key="f_objet",
                              placeholder="Ex. Remplacement chaudière par pompe à chaleur")
        remarques = st.text_area("Remarques imprimées sur le devis", key="nd_remarques", height=68)

    # ---- ② Ajouter des postes ----
    st.markdown("#### ② Ajouter des postes")
    if biblio.empty:
        st.info("Ta bibliothèque de prix est vide : remplis-la dans « Mes prix » (menu de gauche), "
                "ou ajoute des lignes libres ci-dessous.")
    else:
        choix = [("o", i) for i in biblio.index if _texte(biblio.loc[i, "designation"])]
        choix += [("e", nom) for nom in sorted({_texte(n) for n in ensembles["nom"]} - {""})]

        def libelle(c):
            if c[0] == "e":
                return f"🧩 {c[1]} (ensemble)"
            o = biblio.loc[c[1]]
            code = f"  [{_texte(o.get('code'))}]" if _texte(o.get("code")) else ""
            return f"{_texte(o['designation'])} — {_eur(sum(_prix_ouvrage(o, taux_horaire)))} / {_texte(o.get('unite')) or 'u'}{code}"

        st.selectbox("🔍 Chercher dans ma bibliothèque", choix, index=None, format_func=libelle,
                     placeholder="Tape quelques lettres (ex. radiateur, cuivre…) puis choisis : c'est ajouté",
                     key="nd_recherche", on_change=_cb_recherche, args=(biblio, ensembles, taux_horaire))
        favoris = charger_favoris()
        frequents = postes_frequents(biblio, devis, favoris)
        if frequents:
            st.caption("⭐ Postes fréquents — un clic pour ajouter")
            colonnes = st.columns(4)
            for k, i in enumerate(frequents):
                o = biblio.loc[i].to_dict()
                colonnes[k % 4].button(_texte(o.get("designation"))[:45], key=f"nd_fav_{i}",
                                       use_container_width=True, on_click=_cb_favori,
                                       args=(o, taux_horaire))
    b1, b2 = st.columns(2)
    b1.button("➕ Ligne libre (hors bibliothèque)", use_container_width=True, on_click=_cb_ligne_libre)
    with b2.popover("⭐ Choisir mes postes fréquents", use_container_width=True, disabled=biblio.empty):
        if not biblio.empty:
            designations = sorted({_texte(d) for d in biblio["designation"]} - {""})
            choisis = st.multiselect("Ouvrages à garder sous la main", designations,
                                     default=[f for f in favoris if f in designations],
                                     key="nd_favoris_sel")
            st.caption("Sans choix de ta part, l'outil propose les ouvrages que tu utilises le plus.")
            if st.button("Enregistrer mes postes fréquents", use_container_width=True):
                enregistrer_favoris(choisis)
                st.session_state["flash"] = "Postes fréquents enregistrés."
                st.rerun()

    # ---- ③ Lignes du devis ----
    st.markdown("#### ③ Lignes du devis")
    erreurs = []
    if not lignes:
        st.info("Aucune ligne pour l'instant : cherche un ouvrage ou clique sur un poste fréquent.")
    groupe_precedent = ""
    for n, lg in enumerate(lignes, start=1):
        i = lg["_id"]
        groupe = _texte(lg.get("groupe"))
        if groupe and groupe != groupe_precedent:
            st.caption(f"🧩 {groupe}")
        groupe_precedent = groupe
        c = st.columns([5, 1.3, 1.8, 2, 1.8, 0.8], vertical_alignment="bottom")
        # valeurs initiales posées dans l'état (pas de « value= ») : la saisie survit
        # au changement d'écran, voir _conserver_saisie
        st.session_state.setdefault(f"nd_d_{i}", _texte(lg.get("designation")))
        st.session_state.setdefault(f"nd_u_{i}", _texte(lg.get("unite")))
        st.session_state.setdefault(f"nd_q_{i}", max(_num(lg.get("quantite")), 0.0))
        st.session_state.setdefault(f"nd_pu_{i}", max(_num(lg.get("prix_unitaire")), 0.0))
        lg["designation"] = c[0].text_input("Désignation", key=f"nd_d_{i}",
                                            placeholder="Désignation du poste")
        lg["unite"] = c[1].text_input("Unité", key=f"nd_u_{i}")
        lg["quantite"] = c[2].number_input("Quantité", min_value=0.0, step=1.0, key=f"nd_q_{i}")
        lg["prix_unitaire"] = c[3].number_input("Prix unitaire HT (€)", min_value=0.0, step=1.0,
                                                format="%.2f", key=f"nd_pu_{i}")
        c[4].markdown(f"**{_eur(lg['quantite'] * lg['prix_unitaire'])}**")
        c[5].button("🗑️", key=f"nd_x_{i}", help="Supprimer cette ligne",
                    on_click=_cb_supprimer, args=(i,))
        if not lg["designation"].strip():
            erreurs.append(f"Ligne {n} : indique une désignation, ou supprime la ligne.")
    for e in erreurs:
        st.warning(e)

    with st.expander("⚙️ Options : TVA, marge, remise, main-d'œuvre, présentation"):
        o1, o2, o3, o4 = st.columns(4)
        tva_pct = o1.selectbox("TVA", TVA_AUTORISEES, format_func=lambda v: f"{v:g} %", key="nd_tva")
        marge_pct = o2.number_input("Marge (%)", min_value=0.0, step=1.0, key="nd_marge")
        remise_type = o3.radio("Remise en", ["%", "€"], horizontal=True, key="nd_remise_type")
        remise_valeur = o4.number_input(f"Remise ({remise_type})", min_value=0.0, step=1.0,
                                        key="nd_remise_valeur")
        st.checkbox("Fourniture seule (sans main-d'œuvre)", key="nd_sans_mo", on_change=_cb_sans_mo,
                    help="Recalcule le prix des lignes venues de la bibliothèque, avec ou sans pose.")
        st.markdown("**Présentation du devis**")
        mef_devis, surcharge_devis, options_devis = choix_mise_en_forme_devis(
            mef_entreprise, None, "nd_mef")

    propres = lignes_propres(lignes)
    totaux = calculer_totaux(propres, marge_pct, tva_pct, remise_type, remise_valeur)
    if remise_type == "%" and remise_valeur > 100:
        erreurs.append("Remise : 100 % maximum.")
    if remise_type == "€" and remise_valeur > totaux["total_ht_brut"] + 1e-9:
        erreurs.append("La remise dépasse le total HT.")
    m1, m2, m3 = st.columns(3)
    m1.metric("Total HT", _eur(totaux["total_ht"]))
    m2.metric(f"TVA {tva_pct:g} %", _eur(totaux["montant_tva"]))
    m3.metric("Total TTC", _eur(totaux["total_ttc"]))
    details = [f"marge {marge_pct:g} % : + {_eur(totaux['total_ht_brut'] - totaux['sous_total_ht'])}"
               if marge_pct else "",
               f"remise : - {_eur(totaux['remise_montant'])}" if totaux["remise_montant"] else ""]
    if any(details):
        st.caption("Total HT calculé avec " + " et ".join(d for d in details if d) + ".")

    # ---- ④ Terminer ----
    st.markdown("#### ④ Terminer")
    vj = str(ent.get("validite_jours") or "").strip()
    infos = {"numero": numero, "client": client_nom, "client_adresse": client_adresse, "client_tel": "",
             "date": date_devis.strftime("%d/%m/%Y"),
             "validite": (date_devis + timedelta(days=int(vj))).strftime("%d/%m/%Y") if vj.isdigit() else None,
             "validite_jours": ent.get("validite_jours"), "objet": objet, "date_debut": date_debut,
             "duree": duree, "mentions": remarques.strip(), "options": options_devis}
    pret = bool(lignes) and not erreurs

    a1, a2, a3 = st.columns(3)
    if a1.button("💾 Enregistrer le devis", type="primary", use_container_width=True):
        bloquants = (["Indique le nom du client (étape ①)."] if not client_nom.strip() else []) \
            + (["Ajoute au moins une ligne (étape ②)."] if not lignes else []) + erreurs
        if bloquants:
            for b in bloquants:
                st.error(b)
        else:
            champs = {"date_devis": str(date_devis), "client_nom": client_nom.strip(),
                      "client_adresse": client_adresse, "date_debut": date_debut, "duree": duree,
                      "marge_pct": float(marge_pct), "tva_pct": float(tva_pct),
                      "lignes_json": json.dumps(propres, ensure_ascii=False)}
            if deja_enregistre:
                ok = mettre_a_jour_devis(numero, champs)
            else:
                ok = sauvegarder_devis({"numero": numero, **champs, "statut": "En attente"})
                if not ok:  # numéro pris entre-temps (autre poste) : on prend le suivant
                    numero = prochain_numero()
                    st.session_state.numero_courant = numero
                    ok = sauvegarder_devis({"numero": numero, **champs, "statut": "En attente"})
            if not ok:
                st.error("Enregistrement impossible, réessaie.")
            else:
                if deja_enregistre or remise_valeur or remarques.strip():
                    enregistrer_complement(numero, remise_type, remise_valeur, remarques.strip())
                if deja_enregistre or surcharge_devis or options_devis.strip():
                    enregistrer_mise_en_forme(numero, surcharge_devis, options_devis)
                if memoriser:
                    enregistrer_clients(pd.concat(
                        [clients, pd.DataFrame([{"nom": client_nom.strip(), "adresse": client_adresse}])],
                        ignore_index=True))
                st.session_state["nd_enregistre"] = True
                st.session_state["flash"] = (f"Devis {numero} mis à jour." if deja_enregistre
                                             else f"Devis {numero} enregistré.")
                st.rerun()
    voir = a2.toggle("👁️ Voir l'aperçu", key="nd_apercu", disabled=not pret)
    pdf_bytes = b""
    if pret:
        try:
            pdf_bytes = generer_pdf_devis(ent, infos, propres, totaux, mise_en_forme=mef_devis)
        except Exception as exc:
            st.error(f"PDF impossible à générer : {exc}")
    a3.download_button("📄 Télécharger le PDF", pdf_bytes, f"{numero}.pdf", "application/pdf",
                       disabled=not pdf_bytes, use_container_width=True, key="nd_pdf")
    if not pret and lignes:
        st.caption("Corrige les lignes signalées pour débloquer l'aperçu et le PDF.")
    if voir and pret:
        afficher_apercu(ent, mef_devis, infos, propres, totaux)
    if pret:
        with st.expander("Autres exports"):
            st.download_button("📊 Exporter ce devis (Excel)", generer_excel_devis(ent, infos, propres, totaux),
                               f"{numero}.xlsx",
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               key="nd_excel")


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

    flash = st.session_state.pop("flash", None)
    if flash:
        st.toast(flash, icon="✅")
    _conserver_saisie()

    ent = charger_entreprise()
    est_admin = bool(st.session_state.get("is_admin"))
    taux_horaire = _num(ent.get("taux_horaire"), TAUX_HORAIRE_DEFAUT) or TAUX_HORAIRE_DEFAUT
    mef_entreprise = mise_en_forme_entreprise(ent)

    # Chaque écran est une fonction ; le menu (en bas de main) les regroupe par usage.
    # ---- BIBLIOTHÈQUE ----
    def ecran_ouvrages():
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
            st.toast("Bibliothèque enregistrée.", icon="✅")

    # ---- OUVRAGES COMPOSÉS ----
    def ecran_ensembles():
        st.caption("Un ensemble regroupe plusieurs ouvrages à ajouter d'un coup dans un devis.")
        biblio = charger_bibliotheque()
        if biblio.empty:
            st.warning("Ajoute d'abord des ouvrages dans l'onglet Ouvrages.")
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
    def page_clients():
        st.header("👥 Mes clients")
        edite_c = st.data_editor(
            charger_clients(), num_rows="dynamic", use_container_width=True,
            column_config={
                "nom": "Nom / Raison sociale", "adresse": "Adresse",
                "telephone": "Téléphone", "email": "Email", "siret": "SIRET (si pro)"},
            key="editeur_clients")
        if st.button("💾 Enregistrer les clients"):
            enregistrer_clients(edite_c)
            st.toast("Clients enregistrés.", icon="✅")

    # ---- MISE EN FORME (préférences entreprise) ----
    def ecran_presentation():
        st.caption("Préférences par défaut de l'entreprise, appliquées à tous les devis. "
                   "Un devis peut les personnaliser (écran Nouveau devis ou bouton ✏️ Modifier).")
        c_choix, c_apercu = st.columns([1, 1], gap="large")
        with c_choix:
            mef_choisie = panneau_mise_en_forme(mef_entreprise, "mef_ent")
            if st.button("💾 Enregistrer comme préférences de l'entreprise", type="primary"):
                fichier = charger_entreprise()   # ne pas enregistrer les saisies de la barre latérale
                fichier["mise_en_forme"] = mef_choisie
                fichier.pop("couleur_accent", None)   # ancienne clé à plat
                enregistrer_entreprise(fichier)
                st.session_state["flash"] = "Préférences de mise en forme enregistrées."
                st.rerun()
        with c_apercu:
            afficher_apercu(ent, mef_choisie)

    # ---- SUIVI ----
    def page_mes_devis():
        st.header("📋 Mes devis")
        saved = charger_devis()
        if saved.empty:
            st.info("Aucun devis enregistré pour l'instant.")
        else:
            saved = saved.merge(charger_complements(), on="numero", how="left")
            vue = pd.DataFrame({
                "numero": saved["numero"],
                "date_devis": saved["date_devis"],
                "client_nom": saved["client_nom"],
                "total_ttc": saved.apply(total_devis_ttc, axis=1).round(2),
                "statut": saved["statut"],
                "date_modif": [_texte(v) for v in saved["date_modif"]],
            })
            edited_s = st.data_editor(
                vue, use_container_width=True, hide_index=True,
                column_config={
                    "numero": st.column_config.TextColumn("N°", disabled=True),
                    "date_devis": st.column_config.TextColumn("Date", disabled=True),
                    "client_nom": st.column_config.TextColumn("Client", disabled=True),
                    "total_ttc": st.column_config.NumberColumn("Total TTC (€)", format="%.2f", disabled=True),
                    "statut": st.column_config.SelectboxColumn("Statut", options=STATUTS),
                    "date_modif": st.column_config.TextColumn("Modifié le", disabled=True)},
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
            st.markdown("**Modifier ou re-télécharger un devis**")
            libelles_s = {r["numero"]: f"{r['numero']} — {r['client_nom'] or ''} — {r['date_devis']}"
                          for _, r in saved.iterrows()}
            sel_pdf = st.selectbox("Devis", saved["numero"].tolist(),
                                   format_func=lambda n: libelles_s.get(n, n), key="suivi_pdf_sel")
            rec = saved[saved["numero"] == sel_pdf].iloc[0]
            try:
                lignes_rec = json.loads(rec["lignes_json"]) if rec["lignes_json"] else []
            except Exception:
                lignes_rec = []
            totaux_rec = calculer_totaux(lignes_rec, rec.get("marge_pct"), rec.get("tva_pct"),
                                         rec.get("remise_type"), rec.get("remise_valeur"))
            infos_rec = {"numero": rec["numero"], "client": rec["client_nom"],
                         "client_adresse": rec["client_adresse"], "date": rec["date_devis"],
                         "validite_jours": charger_entreprise().get("validite_jours"),
                         "date_debut": rec["date_debut"], "duree": rec["duree"],
                         "mentions": _texte(rec.get("mentions")),
                         "options": _texte(rec.get("options_variantes"))}
            pdf_rec = generer_pdf_devis(charger_entreprise(), infos_rec, lignes_rec, totaux_rec,
                                        mise_en_forme=surcharge_mise_en_forme(rec.get("mise_en_forme")))
            cP, cM = st.columns(2)
            cP.download_button("📄 Télécharger ce devis (PDF)", pdf_rec, f"{sel_pdf}.pdf",
                               "application/pdf", key="suivi_pdf_dl")
            if cM.button("✏️ Modifier ce devis", key="suivi_modifier"):
                st.session_state.pop("ed_etat", None)  # repart toujours de la version en base
                dialog_modifier_devis(sel_pdf, taux_horaire)

            st.caption(
                f"En attente : {(saved['statut']=='En attente').sum()}  |  "
                f"Acceptés : {(saved['statut']=='Accepté').sum()}  |  "
                f"Refusés : {(saved['statut']=='Refusé').sum()}")


    # ---- NOUVEAU DEVIS ----
    def page_nouveau_devis():
        st.header("🧾 Nouveau devis")
        onglet_nouveau_devis(ent, mef_entreprise, taux_horaire)

    # ---- MES PRIX (ouvrages + ensembles) ----
    def page_mes_prix():
        st.header("📚 Mes prix")
        st.caption("Les ouvrages et leurs prix, réutilisés dans tous tes devis.")
        t_ouvrages, t_ensembles = st.tabs(["Ouvrages", "Ensembles (lots tout prêts)"])
        with t_ouvrages:
            ecran_ouvrages()
        with t_ensembles:
            ecran_ensembles()

    # ---- MON ENTREPRISE (identité, présentation des devis, tarif horaire) ----
    def ecran_identite():
        st.caption("Ces informations apparaissent en tête et en pied de chaque devis. "
                   "Un champ laissé vide n'est simplement pas imprimé.")
        g, d = st.columns(2, gap="large")
        with g:
            st.markdown("**Identité**")
            if LOGO_PATH.exists():
                st.image(str(LOGO_PATH), width=160, caption="Logo actuel")
            logo_file = st.file_uploader("Logo (PNG / JPG)", type=["png", "jpg", "jpeg"], key="logo_up")
            ent["raison_sociale"] = st.text_input("Raison sociale", ent["raison_sociale"])
            ent["forme_juridique"] = st.text_input("Forme juridique", ent["forme_juridique"])
            ent["capital"] = st.text_input("Capital social", ent["capital"], placeholder="Ex. 10 000 €")
            ent["adresse"] = st.text_area("Adresse", ent["adresse"])
            ent["telephone"] = st.text_input("Téléphone", ent["telephone"])
            ent["email"] = st.text_input("Email", ent["email"])
            ent["site_web"] = st.text_input("Site web", ent["site_web"])
            ent["siret"] = st.text_input("SIRET", ent["siret"])
            ent["tva_intra"] = st.text_input("N° TVA intracommunautaire", ent["tva_intra"])
            ent["code_ape"] = st.text_input("Code APE / NAF", ent["code_ape"], placeholder="Ex. 4322B")
        with d:
            st.markdown("**Assurances et qualifications**")
            ent["assurance_nom"] = st.text_input("Assureur décennale", ent["assurance_nom"])
            ent["assurance_police"] = st.text_input("N° police décennale", ent["assurance_police"])
            ent["assurance_couverture"] = st.text_input("Couverture géographique", ent["assurance_couverture"])
            ent["rc_pro_nom"] = st.text_input("Assureur RC pro", ent["rc_pro_nom"])
            ent["rc_pro_police"] = st.text_input("N° police RC pro", ent["rc_pro_police"])
            ent["qualifications"] = st.text_input("Qualifications", ent["qualifications"],
                                                  placeholder="Ex. RGE QualiPAC, Qualibat 5211")
            st.markdown("**Conditions du devis**")
            ent["conditions_reglement"] = st.text_area("Conditions de règlement", ent["conditions_reglement"])
            ent["validite_jours"] = st.text_input("Validité du devis (jours)", ent["validite_jours"] or "30")
            ent["acompte_pct"] = st.text_input("Acompte demandé à la commande (%)", ent["acompte_pct"],
                                               placeholder="Vide = pas d'acompte")
            ent["mention_devis"] = st.text_input("Mention en pied de devis",
                                                 ent["mention_devis"] or MENTION_DEVIS_DEFAUT)
        if st.button("💾 Enregistrer mon entreprise", type="primary"):
            enregistrer_entreprise(ent)
            if logo_file is not None:
                LOGO_PATH.write_bytes(logo_file.getvalue())
            st.session_state["flash"] = "Fiche entreprise enregistrée."
            st.rerun()

    def ecran_tarif_horaire():
        st.caption("Sert à calculer la main-d'œuvre : temps de pose × nombre de poseurs × tarif horaire. "
                   "Un ouvrage peut avoir son propre tarif dans « Mes prix ».")
        nouveau = st.number_input("Tarif horaire par défaut (€/h)", min_value=0.0, value=taux_horaire,
                                  step=1.0, key="ent_taux_horaire")
        if st.button("💾 Enregistrer le tarif horaire", type="primary"):
            fichier = charger_entreprise()
            fichier["taux_horaire"] = nouveau
            enregistrer_entreprise(fichier)
            st.session_state["flash"] = "Tarif horaire enregistré."
            st.rerun()

    def page_entreprise():
        st.header("🏢 Mon entreprise")
        t_identite, t_presentation, t_tarif = st.tabs(
            ["Identité et mentions", "Présentation des devis", "Tarif horaire"])
        with t_identite:
            ecran_identite()
        with t_presentation:
            ecran_presentation()
        with t_tarif:
            ecran_tarif_horaire()

    # ---- COMPTES ----
    def page_comptes():
        st.header("🔑 Comptes utilisateurs" if est_admin else "🔑 Mon mot de passe")
        panneau_comptes(st)

    # ---- ACCUEIL ----
    def page_accueil():
        st.header(f"🏠 {_texte(ent.get('raison_sociale')) or 'Bienvenue'}")
        if not _texte(ent.get("raison_sociale")):
            st.info("Première utilisation ? Commence par ta fiche entreprise : "
                    "elle apparaît en tête de chaque devis.")
            if st.button("🏢 Remplir Mon entreprise"):
                st.switch_page(p_entreprise)

        en_cours = len(st.session_state.devis_lignes) if not st.session_state.get("nd_enregistre") else 0
        r1, r2, r3 = st.columns(3)
        libelle = f"🧾 Continuer mon devis ({en_cours} ligne(s))" if en_cours else "🧾 Nouveau devis"
        if r1.button(libelle, type="primary", use_container_width=True):
            st.switch_page(p_nouveau)
        if r2.button("📋 Reprendre un devis", use_container_width=True):
            st.switch_page(p_devis)
        if r3.button("📚 Mes prix", use_container_width=True):
            st.switch_page(p_prix)

        devis = charger_devis()
        if devis.empty:
            st.caption("Aucun devis pour l'instant : clique sur « Nouveau devis » pour faire le premier.")
            return
        devis = devis.merge(charger_complements(), on="numero", how="left")
        devis["total_ttc"] = devis.apply(total_devis_ttc, axis=1)
        attente = devis[devis["statut"] == "En attente"]
        m1, m2, m3 = st.columns(3)
        m1.metric("Devis en attente de réponse", len(attente))
        m2.metric("Montant en attente (TTC)", _eur(attente["total_ttc"].sum()))
        m3.metric("Devis acceptés", int((devis["statut"] == "Accepté").sum()))

        st.subheader("Mes derniers devis")
        for _, r in devis.iloc[::-1].head(5).iterrows():
            c = st.columns([2, 3, 2, 2, 2, 1.5], vertical_alignment="center")
            c[0].markdown(f"**{r['numero']}**")
            c[1].write(_texte(r["client_nom"]) or "—")
            c[2].write(_texte(r["date_devis"]))
            c[3].write(_eur(r["total_ttc"]))
            c[4].write(_texte(r["statut"]))
            if c[5].button("Ouvrir", key=f"acc_ouvrir_{r['numero']}", use_container_width=True):
                st.session_state["suivi_pdf_sel"] = r["numero"]
                st.switch_page(p_devis)

    # ---- MENU ----
    p_accueil = st.Page(page_accueil, title="Accueil", icon="🏠", url_path="accueil", default=True)
    p_nouveau = st.Page(page_nouveau_devis, title="Nouveau devis", icon="🧾", url_path="nouveau-devis")
    p_devis = st.Page(page_mes_devis, title="Mes devis", icon="📋", url_path="mes-devis")
    p_clients = st.Page(page_clients, title="Mes clients", icon="👥", url_path="mes-clients")
    p_prix = st.Page(page_mes_prix, title="Mes prix", icon="📚", url_path="mes-prix")
    p_entreprise = st.Page(page_entreprise, title="Mon entreprise", icon="🏢", url_path="mon-entreprise")
    p_comptes = st.Page(page_comptes, icon="🔑", url_path="comptes",
                        title="Comptes utilisateurs" if est_admin else "Mon mot de passe")
    page = st.navigation({"": [p_accueil],
                          "Devis": [p_nouveau, p_devis],
                          "Carnet": [p_clients, p_prix],
                          "Réglages": [p_entreprise, p_comptes]})
    with st.sidebar:
        st.caption(f"Connecté : {st.session_state.get('user', '')}")
        if st.button("Se déconnecter", use_container_width=True):
            st.session_state.clear()
            st.rerun()
    page.run()


if __name__ == "__main__":
    main()
