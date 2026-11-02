"""perception.py — ce que « voit » `ecran`, pour toute appli, comme par un câble HDMI.

La capture pleine résolution (2560×1440) est la source de vérité ; l'arbre
AT-SPI n'est qu'un bonus. D'une même capture on tire :

- `ocr()`      : les mots à l'écran avec leur boîte en pixels ÉCRAN (tesseract fra) ;
- `lignes()`   : les mots regroupés en lignes lisibles ;
- `chercher()` : où se trouve « Nouveau passage » (centre cliquable), ou rien ;
- `diff()`     : ce qui a changé entre deux captures (pourcentage + zones) ;
- `stable()`   : attendre que l'écran cesse de bouger.

Python système (PIL, numpy, cv2), tesseract 5 en ligne de commande : rien à
installer. Mesures du 26/09/2026 sur le comptoir (thème sombre, texte ~10 px) :
gris brut 475 mots / inversé ×2 520 mots en 1,0 s ; la réduction ×0,5 de
l'ancien `voir` n'en laissait que 10. Les boutons colorés (texte blanc sur
vert…) échappent au seuillage global : seconde passe par zone colorée, polarité
remise à « sombre sur clair », toutes les zones dans UNE mosaïque (un seul
appel tesseract).
"""
import csv
import difflib
import io
import os
import re
import subprocess
import time
import unicodedata

import cv2
import numpy as np
from PIL import Image, ImageOps

CONF_MIN = 60              # confiance tesseract en dessous de laquelle un mot est ignoré
AGRANDIR = 2               # le texte d'interface fait ~10 px : ×2 avant OCR
DELAI_OCR = 30             # s max pour un appel tesseract
# 28/10/2026 (Félix : « installe Indra pour le pilote écran ») : Indra OCR (datalab, 1 M de paramètres, ONNX sur CPU,
# venv séparé ~/jarvis/ocr-indra) lit la capture en ~0,2 s (+1 s de lancement du processus). Mesuré sur une capture
# 2560×1440 : 93 lignes / ~400 mots en 1,2 s contre 353 mots en 1,5 s pour tesseract. Il ne donne que des LIGNES (pas de
# boîte par mot) et colle parfois deux onglets voisins : il COMPLÈTE tesseract (mots que celui-ci rate), sans le
# remplacer. PILOTE_OCR = fusion (défaut) | tesseract | indra.
INDRA_PY = os.path.expanduser("~/jarvis/ocr-indra/venv/bin/python")
MOTEUR_OCR = os.environ.get("PILOTE_OCR", "fusion")
INDRA_COEURS = "8"


# ───────────────────────── capture ─────────────────────────
def capturer(zone=None, display=None):
    """Capture en pleine résolution (RGB). `zone` = (x, y, l, h) en pixels écran.
    PIL/xcb : 19 ms pour 2560×1440 ; maim en secours."""
    display = display or os.environ.get("DISPLAY") or ":0"
    bbox = None if zone is None else (zone[0], zone[1], zone[0] + zone[2], zone[1] + zone[3])
    try:
        from PIL import ImageGrab
        return ImageGrab.grab(bbox=bbox, xdisplay=display).convert("RGB")
    except Exception:
        cmd = ["maim", "--hidecursor", "-f", "png"]
        if zone is not None:
            cmd += ["-g", f"{zone[2]}x{zone[3]}+{zone[0]}+{zone[1]}"]
        r = subprocess.run(cmd, capture_output=True, timeout=20,
                           env={**os.environ, "DISPLAY": display})
        if r.returncode != 0:
            raise RuntimeError(f"capture impossible : {r.stderr.decode().strip()}")
        return Image.open(io.BytesIO(r.stdout)).convert("RGB")


# ───────────────────────── OCR ─────────────────────────
def _tesseract(img, psm=11):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    r = subprocess.run(["tesseract", "stdin", "stdout", "-l", "fra", "--psm", str(psm), "tsv"],
                       input=buf.getvalue(), capture_output=True, timeout=DELAI_OCR)
    if r.returncode != 0:
        raise RuntimeError(f"tesseract : {r.stderr.decode().strip()[:200]}")
    lecteur = csv.DictReader(io.StringIO(r.stdout.decode("utf-8", "replace")),
                             delimiter="\t", quoting=csv.QUOTE_NONE)
    return [l for l in lecteur if (l.get("text") or "").strip()]


def _indra(img, origine=(0, 0), conf_min=50):
    """Mots lus par Indra OCR, boîtes en pixels ÉCRAN. Indra ne donne qu'une boîte par ligne : elle est découpée en
    mots au prorata des caractères. Jamais d'exception : [] si le venv manque ou si Indra échoue (le pilote continue
    avec tesseract seul)."""
    if MOTEUR_OCR == "tesseract" or not os.path.exists(INDRA_PY):
        return []
    import json
    import tempfile
    chemin = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False,
                                         dir="/dev/shm" if os.path.isdir("/dev/shm") else None) as f:
            chemin = f.name
        img.save(chemin, "PNG")
        r = subprocess.run([INDRA_PY, "-m", "indra_ocr", chemin, "--format", "json", "--cores", INDRA_COEURS],
                           capture_output=True, timeout=DELAI_OCR, env={**os.environ, "HF_HUB_OFFLINE": "1"})
        if r.returncode != 0:
            return []
        mots = []
        for ligne in r.stdout.decode("utf-8", "replace").splitlines():
            if not ligne.strip():
                continue
            for l in json.loads(ligne).get("lines", []):
                conf, texte = round(float(l.get("conf", 0)) * 100), l.get("text", "")
                if conf < conf_min or not texte.strip():
                    continue
                x0, y0, x1, y1 = l["box"]
                par_car, pos = (x1 - x0) / max(1, len(texte)), 0
                for mot in texte.split(" "):
                    if mot:
                        mots.append(_mot(mot, conf, origine[0] + int(x0 + pos * par_car), origine[1] + int(y0),
                                         max(1, int(len(mot) * par_car)), max(1, int(y1 - y0)), "indra"))
                    pos += len(mot) + 1
        return mots
    except Exception:
        return []
    finally:
        if chemin:
            try:
                os.unlink(chemin)
            except OSError:
                pass


def _mot(texte, conf, x, y, l, h, source):
    return {"texte": texte, "conf": round(conf), "x": x, "y": y, "l": l, "h": h,
            "cx": x + l // 2, "cy": y + h // 2, "source": source}


def _gris_sombre_sur_clair(img):
    """Niveaux de gris avec du texte SOMBRE sur fond CLAIR (ce que tesseract lit
    le mieux) : une interface en thème sombre est inversée."""
    g = img.convert("L")
    return ImageOps.invert(g) if np.asarray(g).mean() < 128 else g


def zones_colorees(rgb, mini=(28, 14), maxi=(1400, 90)):
    """Boîtes (x, y, l, h) des aplats de la taille d'un bouton ou d'une pastille
    où le seuillage global perd le texte : aplats saturés (bouton vert…) et, sur
    une page sombre, capsules CLAIRES à texte sombre (thème façon ChatGPT du
    comptoir, 26/09 : « Oui, continuer sans mutuelle », « Nouveau passage »).
    Jusqu'à 1 400 px de large (« Valider → Charli prépare la facture » : 812 px).
    Les 8 px du bord sont ignorés : le cadre orange du pilote y passe, et il
    reliait tous les boutons collés au bord en une seule zone géante."""
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    masque = (hsv[:, :, 1] > 90) & (hsv[:, :, 2] > 90)
    if rgb.mean() < 128:
        masque |= (hsv[:, :, 2] > 200) & (hsv[:, :, 1] < 40)
    masque = masque.astype(np.uint8)
    b = 8
    masque[:b, :] = masque[-b:, :] = 0
    masque[:, :b] = masque[:, -b:] = 0
    # Le texte creuse l'aplat : on referme avant de chercher les composantes.
    masque = cv2.morphologyEx(masque, cv2.MORPH_CLOSE, np.ones((5, 9), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(masque, connectivity=4)
    boites = []
    for i in range(1, n):
        x, y, l, h, aire = stats[i]
        if mini[0] <= l <= maxi[0] and mini[1] <= h <= maxi[1] and aire >= 0.6 * l * h:
            boites.append((int(x), int(y), int(l), int(h)))
    return boites


def _polarite(crop):
    """Crop en niveaux de gris, texte sombre sur clair : Otsu, puis la classe
    minoritaire (le texte) est mise en noir."""
    g = np.asarray(crop.convert("L"))
    _, b = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if (b == 0).mean() > 0.5:                # texte clair sur fond sombre
        b = 255 - b
    return Image.fromarray(b)


def _passe_couleurs(img, origine, conf_min):
    rgb = np.asarray(img)
    boites = zones_colorees(rgb)
    if not boites:
        return []
    marge, ech = 24, 3
    tuiles = [_polarite(img.crop((x, y, x + l, y + h))).resize((l * ech, h * ech), Image.LANCZOS)
              for x, y, l, h in boites]
    largeur = max(t.width for t in tuiles) + 2 * marge
    hauteur = sum(t.height + marge for t in tuiles) + marge
    mosaique = Image.new("L", (largeur, hauteur), 255)
    places, y = [], marge
    for t in tuiles:
        mosaique.paste(t, (marge, y))
        places.append(y)
        y += t.height + marge
    par_boite = {i: [] for i in range(len(boites))}
    for r in _tesseract(mosaique, psm=11):
        conf = float(r["conf"])
        if conf < conf_min:
            continue
        mx, my, ml, mh = (int(r[k]) for k in ("left", "top", "width", "height"))
        centre = my + mh / 2
        for i, ((bx, by, bl, bh), py, t) in enumerate(zip(boites, places, tuiles)):
            if py <= centre < py + t.height:
                par_boite[i].append(_mot(r["text"].strip(), conf,
                                         origine[0] + bx + (mx - marge) // ech,
                                         origine[1] + by + (my - py) // ech,
                                         max(1, ml // ech), max(1, mh // ech), "ocr-couleur"))
                break
    # (boîte écran, mots lus dedans) : `ocr()` choisit ensuite, boîte par boîte.
    return [((origine[0] + b[0], origine[1] + b[1], b[2], b[3]), par_boite[i])
            for i, b in enumerate(boites) if par_boite[i]]


def _recouvre(a, b, taux=0.5):
    ix = max(0, min(a["x"] + a["l"], b["x"] + b["l"]) - max(a["x"], b["x"]))
    iy = max(0, min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"]))
    return ix * iy >= taux * min(a["l"] * a["h"], b["l"] * b["h"])


def _adaptatif(img):
    """Seuillage LOCAL (gaussien, fenêtre 31 px) : un texte gris sur fond
    sombre (texte d'invite « Dépose une ordonnance… ») disparaît au seuillage
    global dès que la zone contient aussi du texte blanc vif — mesuré le 26/09
    sur le comptoir réel, lu seulement si on découpait une petite zone."""
    g = np.asarray(img.convert("L"))
    if g.mean() < 128:
        g = 255 - g
    return Image.fromarray(cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                                 cv2.THRESH_BINARY, 31, 10))


def _passe(base, origine, agrandir, conf_min, psm, source):
    if agrandir != 1:
        base = base.resize((base.width * agrandir, base.height * agrandir), Image.LANCZOS)
    mots = []
    for r in _tesseract(base, psm=psm):
        conf = float(r["conf"])
        if conf < conf_min:
            continue
        x, y, l, h = (int(r[k]) // agrandir for k in ("left", "top", "width", "height"))
        mots.append(_mot(r["text"].strip(), conf, origine[0] + x, origine[1] + y,
                         max(1, l), max(1, h), source))
    return mots


def ocr(img, origine=(0, 0), conf_min=CONF_MIN, psm=11, couleurs=True, agrandir=AGRANDIR):
    """Mots lus sur `img` (capture pleine résolution), boîtes en pixels ÉCRAN
    (`origine` = coin haut-gauche de `img` à l'écran). Refuse implicitement une
    capture réduite : elle ne rend presque rien (c'est voulu, cf. tests).

    Deux lectures EN PARALLÈLE, fusionnées : seuillage global (le meilleur sur
    les boutons et le texte vif) et seuillage adaptatif (le seul qui lit le
    texte gris peu contrasté) ; un mot vu par la seconde seulement est ajouté."""
    from concurrent.futures import ThreadPoolExecutor
    if MOTEUR_OCR == "indra":
        return _indra(img, origine)
    with ThreadPoolExecutor(3) as ex:
        f_glob = ex.submit(_passe, _gris_sombre_sur_clair(img), origine, agrandir, conf_min, psm, "ocr")
        f_adap = ex.submit(_passe, _adaptatif(img), origine, agrandir, conf_min, psm, "ocr")
        f_indra = ex.submit(_indra, img, origine) if MOTEUR_OCR == "fusion" else None   # en parallèle : rien de plus à attendre
        mots, adap = f_glob.result(), f_adap.result()
        indra = f_indra.result() if f_indra else []
    # Le seuillage local fait ressortir les BORDS des boutons, lus « | », « — » :
    # ils recollaient tous les boutons d'un en-tête en une seule fausse ligne.
    # On ne prend de cette passe que des mots qui contiennent une lettre ou un
    # chiffre (la passe globale, elle, garde « = », « × » d'une calculatrice).
    adap = [m for m in adap if re.search(r"[^\W_]", m["texte"])]
    # Même mot lu par les deux passes : leurs boîtes diffèrent (un « 4 » en 10×28 d'un côté, 13×18 de l'autre, 46 %
    # de recouvrement) — le centre de l'un dans la boîte de l'autre suffit, sinon « Compté 4 4 fois ».
    def meme_mot(a, b):
        return _recouvre(a, b, 0.3) or (b["x"] - 2 <= a["cx"] <= b["x"] + b["l"] + 2
                                        and b["y"] - 2 <= a["cy"] <= b["y"] + b["h"] + 2)
    mots += [m for m in adap if not any(meme_mot(m, n) for n in mots)]
    if couleurs:
        # Dans chaque aplat coloré, fusion MOT À MOT : la passe globale lit
        # parfois « Enx » pour « Envoyer » (blanc sur vert), la passe couleur
        # moins bien une pastille ; et une boîte détectée peut réunir un bouton
        # et la pastille voisine (« Reste 2,48 € » + « Carte bleue »). Pour deux
        # mots qui se recouvrent, le plus sûr (confiance × longueur) reste.
        def poids(m):
            return m["conf"] * len(m["texte"])
        for _boite, couleur in _passe_couleurs(img, origine, conf_min):
            for c in couleur:
                rivaux = [n for n in mots if _recouvre(c, n)]
                if all(poids(c) > poids(n) for n in rivaux):
                    mots = [n for n in mots if n not in rivaux] + [c]
    # Indra en complément : seulement des mots avec une lettre ou un chiffre, et que tesseract n'a pas déjà lus.
    indra = [m for m in indra if re.search(r"[^\W_]", m["texte"])]
    mots += [m for m in indra if not any(meme_mot(m, n) for n in mots)]
    return mots


def lignes(mots):
    """Regroupe les mots en lignes : même bande verticale (recouvrement ≥ 50 %)
    et écart horizontal ≤ 1,5 hauteur. Deux colonnes d'un tableau restent
    séparées. Chaque ligne : texte, mots (de gauche à droite), boîte, centre."""
    # De gauche à droite : trier par hauteur d'abord mettait « Vitale » (y 34)
    # avant « Carte » (y 35) et cassait « Carte Vitale » en deux lignes.
    restants = sorted(mots, key=lambda m: (m["x"], m["cy"]))
    resultat = []
    for m in restants:
        place, meilleur = None, None
        for li in resultat:
            d = li["mots"][-1]
            haut = max(d["h"], m["h"])
            vertical = min(d["y"] + d["h"], m["y"] + m["h"]) - max(d["y"], m["y"])
            ecart = m["x"] - (d["x"] + d["l"])
            if vertical >= 0.5 * min(d["h"], m["h"]) and -haut <= ecart <= 1.5 * haut:
                if meilleur is None or ecart < meilleur:
                    place, meilleur = li, ecart
        if place is None:
            resultat.append({"mots": [m]})
        else:
            place["mots"].append(m)
    for li in resultat:
        ms = li["mots"]
        x0, y0 = min(m["x"] for m in ms), min(m["y"] for m in ms)
        x1, y1 = max(m["x"] + m["l"] for m in ms), max(m["y"] + m["h"] for m in ms)
        li.update(texte=" ".join(m["texte"] for m in ms), x=x0, y=y0, l=x1 - x0, h=y1 - y0,
                  cx=(x0 + x1) // 2, cy=(y0 + y1) // 2,
                  conf=round(sum(m["conf"] for m in ms) / len(ms)))
    return sorted(resultat, key=lambda li: (li["y"], li["x"]))


# ───────────────────────── recherche ─────────────────────────
def normaliser(s):
    """« Espèces » = « especes » = « ESPÈCES » ; « Œ » = « oe » ; ponctuation ignorée."""
    s = s.replace("œ", "oe").replace("Œ", "OE").replace("æ", "ae").replace("Æ", "AE")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    return re.sub(r"[^\w€]+", " ", s).strip()


def chercher(mots, motif, exact=False):
    """Occurrences de `motif` dans les mots lus : suite de mots CONSÉCUTIFS
    d'une même ligne (un petit mot de ≤ 3 lettres mal lu peut être sauté).
    Chaque occurrence : texte lu, boîte couvrante, centre (cx, cy) écran, conf."""
    decouper = (lambda s: s.split()) if exact else (lambda s: normaliser(s).split())
    cible = decouper(motif)
    if not cible:
        return []
    trouves = []
    for li in lignes(mots):
        # Jetons de la ligne, chacun rattaché à son mot : « l'ordonnance » lu
        # comme UN mot donne deux jetons, comme le motif « Voir l'ordonnance ».
        jetons = [(j, m) for m in li["mots"] for j in decouper(m["texte"])]
        for i in range(len(jetons)):
            j, k, sautes, pris = i, 0, 0, []
            while k < len(cible) and j < len(jetons):
                if jetons[j][0] == cible[k]:
                    if not pris or pris[-1] is not jetons[j][1]:
                        pris.append(jetons[j][1])
                    j, k = j + 1, k + 1
                elif pris and sautes == 0 and len(jetons[j][0]) <= 3:
                    sautes, j = 1, j + 1
                else:
                    break
            if k == len(cible):
                x0, y0 = min(m["x"] for m in pris), min(m["y"] for m in pris)
                x1 = max(m["x"] + m["l"] for m in pris)
                y1 = max(m["y"] + m["h"] for m in pris)
                trouves.append({"texte": " ".join(m["texte"] for m in pris),
                                "x": x0, "y": y0, "l": x1 - x0, "h": y1 - y0,
                                "cx": (x0 + x1) // 2, "cy": (y0 + y1) // 2,
                                "conf": min(m["conf"] for m in pris),
                                "source": pris[0]["source"]})
    return trouves


def proches(mots, motif, n=3):
    """Les n lignes lues qui ressemblent le plus à `motif` (pour un « introuvable »
    utile plutôt que muet)."""
    cible = normaliser(motif)
    notes = [(difflib.SequenceMatcher(None, cible, normaliser(li["texte"])).ratio(), li)
             for li in lignes(mots)]
    return [li for _, li in sorted(notes, key=lambda t: -t[0])[:n]]


# ───────────────────────── changements ─────────────────────────
def diff(avant, apres, seuil=30, aire_min=60, exclure=(), origine=(0, 0)):
    """Ce qui a changé entre deux captures de même taille : pourcentage de pixels
    changés et zones (x, y, l, h) en pixels ÉCRAN ; seul le bruit (< 60 px²)
    est écarté. Un CHIFFRE qui change (« Compté 3 » → « 4 », un prix) fait
    ~12×18 px : le seuil de 800 px² du 26/09 au matin le rendait invisible.
    Le curseur n'est pas dans les captures xcb. `exclure` : boîtes écran à
    ignorer (barre GNOME)."""
    a = cv2.cvtColor(np.asarray(avant), cv2.COLOR_RGB2GRAY)
    b = cv2.cvtColor(np.asarray(apres), cv2.COLOR_RGB2GRAY)
    if a.shape != b.shape:
        raise ValueError(f"captures de tailles différentes : {a.shape} ≠ {b.shape}")
    masque = (cv2.absdiff(a, b) > seuil).astype(np.uint8)
    for x, y, l, h in exclure:
        x, y = x - origine[0], y - origine[1]
        masque[max(0, y):max(0, y + h), max(0, x):max(0, x + l)] = 0
    pct = float(100.0 * masque.mean())
    # Dilatation pour réunir les pixels voisins d'une même zone, puis boîte
    # ramenée aux vrais pixels changés (la dilatation gonflait chaque zone).
    grossi = cv2.dilate(masque, np.ones((15, 15), np.uint8))
    n, etiquettes, _, _ = cv2.connectedComponentsWithStats(grossi, connectivity=8)
    zones = []
    for i in range(1, n):
        ys, xs = np.nonzero((etiquettes == i) & (masque == 1))
        if not len(xs):
            continue
        x, y = int(xs.min()), int(ys.min())
        l, h = int(xs.max()) - x + 1, int(ys.max()) - y + 1
        if l * h >= aire_min:
            zones.append((x + origine[0], y + origine[1], l, h))
    zones.sort(key=lambda z: -z[2] * z[3])
    return {"pct": round(pct, 2), "zones": zones}


def stable(capture, max_s=1.5, pas=0.15, seuil_pct=0.05):
    """Recapture jusqu'à deux images successives quasi identiques (animation,
    chargement terminés) ou `max_s` écoulées. `capture` : fonction sans argument
    qui rend une image. Retourne (image, stable ?, durée)."""
    debut = time.time()
    prec = capture()
    while True:
        time.sleep(pas)
        cour = capture()
        if diff(prec, cour)["pct"] < seuil_pct:
            return cour, True, time.time() - debut
        if time.time() - debut >= max_s:
            return cour, False, time.time() - debut
        prec = cour
