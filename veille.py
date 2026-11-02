#!/usr/bin/python3
"""veille.py — l'écran en ÉVÉNEMENTS, sans image (PLAN-VOIR niveau 1, 09/10/2026).

Service utilisateur `pilote-ecran-veille` : une ligne JSON par événement dans
`/run/user/<uid>/pilote-ecran/veille.jsonl` ; lecture : `ecran veille [N] [--suivre]`.
Chaque ligne porte `ts`, `src`, `ev`, `msg` (phrase lisible) et les champs utiles.

Sources, toutes en LECTURE SEULE (aucune image écrite, aucune action) :
- fenetre : X11 (_NET_ACTIVE_WINDOW, _NET_CLIENT_LIST, titres) → activée, ouverte, fermée, titre ;
- atspi   : focus, texte modifié (comptes par champ ; extrait seulement hors champ éditable,
            jamais un mot de passe), alertes/dialogues, pages chargées ;
- proc    : processus apparus (vivants ≥ 2 relevés) et disparus ; utilitaires de shell ignorés ;
- gpu     : NVML → bande de charge (repos/actif/fort/saturé), mémoire (pas de 1 Go), processus ;
- audio   : pactl → flux qui jouent, pause, fin ; micro pris / rendu ;
- pilote  : STOP posé/levé, écran réservé/libéré ;
- ecran   : événements X Damage → calme / activité / vidéo / FIGÉ ; aucune capture par défaut.
- carte   : interfaces AT-SPI/DevTools, identités natives, fraîcheur et deltas SSE sans pixels.
- oeil    : désactivé par défaut (réactivation explicite : PILOTE_VEILLE_IMAGES=1).
            L'ANTI-GRAPHIQUE (Félix, 09/10 soir : « la carte transforme des données en image, utilise-la à
            l'envers ») — à chaque changement notable, Qwen vision sur la 5090 (vLLM local) rend l'écran en
            SCÈNE structurée : type d'écran, éléments (type, libellé, zone, état), événement en cours, alerte.
            Pas d'OCR, pas de description pour un humain : des données pour un agent. `GET /oeil` à la demande.

Python système (gi.Atspi, Xlib, pynvml, PIL via perception.py) : rien à installer.
Tout ce que Claude lit transite par l'API Anthropic : ce bus ne porte jamais de donnée patient.
"""
import ctypes
import ctypes.util
import http.server
import json
import os
import queue
import re
import select
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import commun  # noqa: E402
from commun import chemin  # noqa: E402

DISPLAY = os.environ.get("DISPLAY") or ":0"
_GDM = f"/run/user/{os.getuid()}/gdm/Xauthority"
XAUTH = os.environ.get("XAUTHORITY") or (_GDM if os.path.exists(_GDM) else os.path.expanduser("~/.Xauthority"))
os.environ["DISPLAY"] = DISPLAY
os.environ.setdefault("XAUTHORITY", XAUTH)

FICHIER = "veille.jsonl"
FICHIER_ETAT = "etat.json"      # état instantané, réécrit (atomiquement) au plus une fois par seconde
PORT = 8792                     # pont HTTP local, lecture seule : /etat /texte /evenements /flux
MAX_OCTETS = 4_000_000          # au-delà, le fichier est réduit à ses GARDER dernières lignes
GARDER = 3000
PERIODE_PROC, PERIODE_GPU, PERIODE_PILOTE, PERIODE_ECRAN, PERIODE_TEXTE = 2, 5, 1, 2, 2
FIGE_APRES = 30                 # s sans aucun changement à l'écran → « figé »
RAPPEL_FIGE = 300               # s entre deux rappels « toujours figé »
MAX_TEXTE_PAR_S = 50            # événements texte AT-SPI détaillés par seconde ; au-delà, comptés seulement
REPETITIONS_RESUME = 30         # un champ réécrit à l'identique : une ligne toutes les 30 répétitions (≈ 1 min)
SEUIL_PIXEL = 16                # écart de gris (sur 255) à partir duquel un pixel réduit « a changé »
LLM_URL = os.environ.get("PILOTE_LLM_URL", "http://127.0.0.1:8016/v1")   # même moteur que `ecran decrire`
OEIL_INTERVALLE = 5             # s au moins entre deux regards déclenchés par un changement
OEIL_RAFRAICHIR = 60            # s : nouveau regard sans changement quand l'écran bouge (jeu, vidéo)
OEIL_REESSAI = 300              # s avant de retenter un moteur indisponible
OEIL_MAX_COTE = 1280            # px : côté max de l'image envoyée (2560×1440 → 1280×720)
IMAGES_AUTORISEES = os.environ.get("PILOTE_VEILLE_IMAGES", "0") == "1"
IGNORER_PROC = frozenset("""sh bash dash zsh grep egrep fgrep sed awk gawk mawk cat ls sleep cut head tail tr sort uniq wc
date stat du df find xargs env dirname basename readlink mkdir rm mv cp touch tee ps pgrep pkill id whoami hostname uname
git timeout flock nice ionice nvidia-smi xprop xdotool xwininfo xdpyinfo systemctl journalctl gsettings pactl tesseract
lsof ss ip tput stty less more file which column sha256sum md5sum gzip tar zstd xz curl wget ping getent dbus-send gio
gdbus realpath printf echo true false test expr seq sync cmp diff maim import convert""".split())

LIBELLES = {"push button": "bouton", "toggle button": "bouton", "button": "bouton", "text": "champ", "entry": "champ",
            "terminal": "terminal", "link": "lien", "menu item": "menu", "menu": "menu", "page tab": "onglet",
            "list item": "élément", "tree item": "élément", "combo box": "liste", "check box": "case",
            "radio button": "radio", "table cell": "cellule", "password text": "mot de passe", "dialog": "dialogue",
            "alert": "alerte", "notification": "notification", "document web": "page", "heading": "titre",
            "label": "texte", "static": "texte", "search box": "recherche", "spin button": "compteur"}


def libelle(role):
    return LIBELLES.get(role, role or "?")


def _nettoyer(s, n=120):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s[:n]


def _valeur(v):
    """`Atspi.Event.any_data` arrive parfois emballé dans un GObject.Value."""
    try:
        if hasattr(v, "get_value") and not hasattr(v, "get_role_name"):
            return v.get_value()
    except Exception:
        pass
    return v


# ───────────────────────── l'état instantané ─────────────────────────
class Etat:
    """Ce que l'écran EST maintenant, reconstruit depuis les événements : la réponse de `ecran veille --etat`
    et de `GET /etat`, en quelques millisecondes, sans capture ni OCR."""

    def __init__(self):
        self.verrou = threading.Lock()
        self.d = {"fenetre_active": None, "fenetres": {}, "focus": None, "pages": {}, "texte_recent": [],
                  "gpu": None, "audio": {}, "micro": [], "pilote": {"stop": None, "agent": None}, "ecran": None,
                  "scene": None, "dessin": {}, "derniers": []}
        self.sale = False

    def maj(self, e):
        src, ev, d = e.get("src"), e.get("ev"), self.d
        with self.verrou:
            if src == "fenetre":
                if ev == "activee":
                    if (d["fenetre_active"] or {}).get("wid") != e.get("wid"):
                        d["focus"] = None
                    d["fenetre_active"] = ({k: e.get(k) for k in ("app", "titre", "pid", "wid")}
                                           if (e.get("titre") or e.get("app")) else None)
                elif ev == "ouverte":
                    d["fenetres"][e.get("wid")] = {"app": e.get("app"), "titre": e.get("titre"), "pid": e.get("pid")}
                elif ev == "fermee":
                    d["fenetres"].pop(e.get("wid"), None)
                elif ev == "titre":
                    if d["fenetre_active"] and d["fenetre_active"].get("wid") == e.get("wid"):
                        d["fenetre_active"]["titre"] = e.get("titre")
                    if e.get("wid") in d["fenetres"]:
                        d["fenetres"][e["wid"]]["titre"] = e.get("titre")
            elif src == "atspi":
                if ev == "focus":
                    d["focus"] = {"app": e.get("app"), "role": e.get("role"), "nom": e.get("nom"), "ts": e.get("ts")}
                elif ev == "page":
                    d["pages"][e.get("app") or "?"] = e.get("nom")
                elif ev == "texte" and e.get("extrait"):
                    d["texte_recent"] = (d["texte_recent"] + [{"ts": e.get("ts"), "app": e.get("app"), "extrait": e["extrait"]}])[-10:]
                elif ev == "alerte":
                    d["texte_recent"] = (d["texte_recent"] + [{"ts": e.get("ts"), "app": e.get("app"), "alerte": e.get("texte")}])[-10:]
            elif src == "gpu":
                g = d["gpu"] or {"bande": None, "util": None, "mem": None, "procs": {}}
                if ev in ("initial", "charge"):
                    g.update(bande=e.get("bande"), util=e.get("util"), mem=e.get("mem"))
                elif ev == "memoire":
                    g["mem"] = e.get("mem")
                elif ev == "plus":
                    g["procs"][str(e.get("pid"))] = e.get("comm")
                elif ev == "moins":
                    g["procs"].pop(str(e.get("pid")), None)
                d["gpu"] = g
            elif src == "audio":
                if ev in ("joue", "reprise"):
                    d["audio"][e.get("app")] = "joue"
                elif ev in ("pret", "pause"):
                    d["audio"][e.get("app")] = "pause"
                elif ev == "fin":
                    d["audio"].pop(e.get("app"), None)
            elif src == "micro":
                if ev in ("joue", "reprise"):
                    if e.get("app") not in d["micro"]:
                        d["micro"].append(e.get("app"))
                elif ev in ("fin", "pause") and e.get("app") in d["micro"]:
                    d["micro"].remove(e.get("app"))
            elif src == "pilote":
                if ev in ("stop", "reprise"):
                    d["pilote"]["stop"] = e.get("stop")
                elif ev in ("reserve", "libre"):
                    d["pilote"]["agent"] = e.get("agent")
            elif src == "ecran":
                if ev == "etat":
                    d["ecran"] = {"etat": e.get("etat"), "pct": e.get("pct"), "fige_depuis_s": None}
                elif ev == "fige":
                    d["ecran"] = dict(d["ecran"] or {"etat": "calme", "pct": 0}, fige_depuis_s=e.get("depuis"),
                                      suspect=bool(e.get("suspect")))
                elif ev == "reprise" and d["ecran"]:
                    d["ecran"]["fige_depuis_s"] = None
            elif src == "oeil" and ev == "scene":
                d["scene"] = dict(e.get("scene") or {}, ts=e.get("ts"))
            elif src == "dessin" and e.get("wid"):
                if ev in ("debut", "zone", "continu", "repete"):
                    d["dessin"][e["wid"]] = {"app": e.get("app"), "titre": e.get("titre"), "etat": "continu" if ev == "continu" else "actif",
                                             "taux": e.get("taux"), "ts": e.get("ts")}
                elif ev == "arret":
                    d["dessin"].pop(e["wid"], None)
            if src != "veille" and e.get("msg"):
                d["derniers"] = (d["derniers"] + [{"ts": e.get("ts"), "src": src, "msg": e.get("msg")}])[-50:]
            self.sale = True

    def instantane(self):
        with self.verrou:
            d = json.loads(json.dumps(self.d))
        d["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return d


# ───────────────────────── le bus ─────────────────────────
class Bus:
    """Une ligne JSON par événement, ajoutée au fichier ; rotation quand il dépasse MAX_OCTETS.
    Chaque événement met aussi à jour l'état instantané et part vers les abonnés (flux SSE du pont)."""

    def __init__(self, fichier=None):
        self.fichier = fichier or chemin(FICHIER)
        self.verrou = threading.Lock()
        self.n = 0
        self.etat = Etat()
        self.abonnes = []
        self.declencheurs = []          # fonctions appelées à chaque événement (l'œil s'en sert)
        self.annonces = {}              # ce que nos propres applis déclarent afficher (POST /annonce), par titre ou app

    def abonner(self):
        q = queue.Queue(maxsize=500)
        with self.verrou:
            self.abonnes.append(q)
        return q

    def desabonner(self, q):
        with self.verrou:
            if q in self.abonnes:
                self.abonnes.remove(q)

    def emettre(self, src, ev, msg, **champs):
        d = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "src": src, "ev": ev, "msg": msg}
        d.update({k: v for k, v in champs.items() if v not in (None, "", [], {})})
        ligne = json.dumps(d, ensure_ascii=False)
        with self.verrou:
            with open(self.fichier, "a", encoding="utf-8") as f:
                f.write(ligne + "\n")
            self.n += 1
            if self.n % 200 == 0:
                self.tourner()
            abonnes = list(self.abonnes)
        self.etat.maj(d)
        for q in abonnes:
            try:
                q.put_nowait(ligne)
            except queue.Full:
                pass
        for f in list(self.declencheurs):
            try:
                f(d)
            except Exception:
                pass
        return d

    def tourner(self):
        try:
            if os.path.getsize(self.fichier) < MAX_OCTETS:
                return False
            with open(self.fichier, encoding="utf-8") as f:
                lignes = f.read().splitlines()[-GARDER:]
        except OSError:
            return False
        tmp = f"{self.fichier}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lignes) + "\n")
        os.replace(tmp, self.fichier)
        return True


# ───────────────────────── fenêtres X11 ─────────────────────────
def _cadre(titre):
    return "pilote l'écran" in (titre or "").lower()


class Fenetres(threading.Thread):
    """PropertyNotify sur la racine (_NET_ACTIVE_WINDOW, _NET_CLIENT_LIST) et sur chaque fenêtre (titre)."""

    def __init__(self, bus):
        super().__init__(daemon=True, name="fenetres")
        self.bus = bus
        self.titre_actif = ""
        self.noms = {}
        self.pids = {}             # wid -> pid (_NET_WM_PID) : la carte relie fenêtre X11 et application AT-SPI par là
        self.geometries = {}       # wid -> (x, y, l, h) écran, tenu à jour par ConfigureNotify
        self.actif = 0
        self.empilement = []
        self.visibles = {}
        self.pointeur = None

    def run(self):
        while True:
            try:
                self.boucle()
            except Exception as e:
                self.bus.emettre("fenetre", "erreur", f"fenêtres X11 : {e}"[:200])
                time.sleep(5)

    def boucle(self):
        from Xlib import X, Xatom, display
        d = display.Display(DISPLAY)
        root = d.screen().root
        self.largeur, self.hauteur = d.screen().width_in_pixels, d.screen().height_in_pixels
        NET_ACTIVE, NET_CLIENTS = d.intern_atom("_NET_ACTIVE_WINDOW"), d.intern_atom("_NET_CLIENT_LIST")
        NET_STACK = d.intern_atom("_NET_CLIENT_LIST_STACKING")
        NET_NAME, NET_PID, UTF8 = d.intern_atom("_NET_WM_NAME"), d.intern_atom("_NET_WM_PID"), d.intern_atom("UTF8_STRING")
        root.change_attributes(event_mask=X.PropertyChangeMask)

        def prop(w, atom, typ):
            try:
                p = w.get_full_property(atom, typ)
                return p.value if p else None
            except Exception:
                return None

        def infos(wid):
            w = d.create_resource_object("window", wid)
            nom = prop(w, NET_NAME, UTF8) or prop(w, Xatom.WM_NAME, X.AnyPropertyType) or b""
            if isinstance(nom, bytes):
                nom = nom.decode("utf-8", "replace")
            try:
                cls = w.get_wm_class() or ("", "")
            except Exception:
                cls = ("", "")
            pid = prop(w, NET_PID, Xatom.CARDINAL)
            return _nettoyer(nom, 100), (cls[1] or cls[0] or ""), (int(pid[0]) if pid else None)

        def suivre(wid):
            try:
                d.create_resource_object("window", wid).change_attributes(event_mask=X.PropertyChangeMask | X.StructureNotifyMask)
            except Exception:
                pass

        geometries = self.geometries

        def geometrie(wid):
            """(x, y, l, h) en coordonnées écran du coin haut gauche de la fenêtre cliente."""
            try:
                w = d.create_resource_object("window", wid)
                g = w.get_geometry()
                t = w.translate_coords(root, 0, 0)
                return (-t.x, -t.y, g.width, g.height)
            except Exception:
                return None

        def actif_courant():
            v = prop(root, NET_ACTIVE, Xatom.WINDOW)
            return int(v[0]) if v else 0

        clients = set(int(x) for x in (prop(root, NET_CLIENTS, Xatom.WINDOW) or []))
        self.empilement = [int(x) for x in (prop(root, NET_STACK, Xatom.WINDOW) or [])]
        for wid in clients:
            suivre(wid)
            nom, cls, pid = infos(wid)
            self.noms[wid] = (nom, cls)
            self.pids[wid] = pid
            geometries[wid] = geometrie(wid)         # sinon le premier déplacement ne fait qu'initialiser (vu le 09/10 à 19 h 20)
            if not _cadre(nom):                      # l'état instantané connaît les fenêtres déjà ouvertes, sans événement
                self.bus.etat.maj({"src": "fenetre", "ev": "ouverte", "app": cls, "titre": nom, "pid": pid, "wid": hex(wid), "msg": ""})
        actif = actif_courant()
        self.actif = actif
        self.annoncer_actif(actif, infos, initial=True)
        dernier_titre = {}
        dernier_releve = 0.0
        while True:
            # Lecture X11 uniquement : ni image, ni mouvement de souris.
            try:
                if time.monotonic() - dernier_releve >= 0.25:
                    dernier_releve = time.monotonic()
                    p = root.query_pointer()
                    self.pointeur = {"x": p.root_x, "y": p.root_y}
                    for w in list(self.noms):
                        try:
                            self.visibles[w] = d.create_resource_object("window", w).get_attributes().map_state == X.IsViewable
                        except Exception:
                            self.visibles[w] = False
            except Exception:
                self.pointeur = None
            if not d.pending_events() and not select.select([d.fileno()], [], [], 0.25)[0]:
                continue
            ev = d.next_event()
            if ev.type == X.ConfigureNotify and ev.window.id in self.noms:
                wid = ev.window.id
                g = geometrie(wid)
                avant = geometries.get(wid)
                if g and avant is None:
                    geometries[wid] = g
                elif g and g != avant:
                    geometries[wid] = g
                    nom, cls = self.noms.get(wid, ("", ""))
                    if not _cadre(nom):
                        quoi = ("déplacée" if g[2:] == avant[2:] else "redimensionnée" if g[:2] == avant[:2] else "déplacée et redimensionnée")
                        self.bus.emettre("fenetre", "geometrie", f"fenêtre {quoi} : {cls or '?'} « {nom} » — x {avant[0]}→{g[0]}, y {avant[1]}→{g[1]}, "
                                         f"{g[2]}×{g[3]}" + (f" (était {avant[2]}×{avant[3]})" if g[2:] != avant[2:] else ""),
                                         app=cls, titre=nom, wid=hex(wid), x=g[0], y=g[1], l=g[2], h=g[3], avant=list(avant))
                continue
            if ev.type != X.PropertyNotify:
                continue
            if ev.window.id == root.id and ev.atom == NET_ACTIVE:
                wid = actif_courant()
                if wid != actif:
                    actif = wid
                    self.actif = wid
                    self.annoncer_actif(wid, infos)
            elif ev.window.id == root.id and ev.atom == NET_STACK:
                self.empilement = [int(x) for x in (prop(root, NET_STACK, Xatom.WINDOW) or [])]
            elif ev.window.id == root.id and ev.atom == NET_CLIENTS:
                nouveaux = set(int(x) for x in (prop(root, NET_CLIENTS, Xatom.WINDOW) or []))
                for wid in nouveaux - clients:
                    suivre(wid)
                    nom, cls, pid = infos(wid)
                    self.noms[wid] = (nom, cls)
                    self.pids[wid] = pid
                    geometries[wid] = geometrie(wid)
                    if not _cadre(nom):
                        self.bus.emettre("fenetre", "ouverte", f"fenêtre ouverte : {cls or '?'} « {nom} »",
                                         app=cls, titre=nom, pid=pid, wid=hex(wid))
                for wid in clients - nouveaux:
                    self.pids.pop(wid, None)
                    self.geometries.pop(wid, None)
                    self.visibles.pop(wid, None)
                    nom, cls = self.noms.pop(wid, ("", ""))
                    if not _cadre(nom):
                        self.bus.emettre("fenetre", "fermee", f"fenêtre fermée : {cls or '?'} « {nom} »",
                                         app=cls, titre=nom, wid=hex(wid))
                clients = nouveaux
            elif ev.window.id in self.noms and ev.atom in (NET_NAME, Xatom.WM_NAME):
                wid = ev.window.id
                nom, cls, _ = infos(wid)
                if nom and nom != dernier_titre.get(wid) and not _cadre(nom):
                    dernier_titre[wid] = nom
                    if wid == actif:
                        self.titre_actif = nom
                    self.noms[wid] = (nom, cls)
                    self.bus.emettre("fenetre", "titre", f"titre : {cls or '?'} « {nom} »", app=cls, titre=nom, wid=hex(wid))

    def annoncer_actif(self, wid, infos, initial=False):
        if not wid:
            self.titre_actif = ""
            self.bus.emettre("fenetre", "activee", "fenêtre active : aucune")
            return
        nom, cls, pid = infos(wid)
        if _cadre(nom):
            return
        self.titre_actif = nom
        self.bus.emettre("fenetre", "activee", ("au démarrage, " if initial else "") + f"fenêtre active : {cls or '?'} « {nom} »",
                         app=cls, titre=nom, pid=pid, wid=hex(wid))


# ───────────────────────── AT-SPI ─────────────────────────
class Accessibilite:
    ROLES_ALERTE = ("alert", "dialog", "notification")
    ROLES_FOCUS_IGNORES = ("frame", "window", "panel", "filler", "application", "desktop frame", "section",
                           "unknown", "invalid", "scroll pane", "viewport", "document web", "document frame",
                           "internal frame", "canvas", "embedded", "redundant object")

    def __init__(self, bus):
        self.bus = bus
        self.seaux = {}
        self.ignores = 0
        self.precedents, self.repetes = {}, Counter()   # un libellé réécrit à l'identique toutes les 2 s n'est pas un événement
        self.verrou = threading.Lock()
        self.dernier_focus = None
        self.derniere_page = (None, 0.0)
        self.compte_s = [0, 0.0]
        self.Atspi = None
        self.ecouteur = None
        self.modifications = {}

    def demarrer(self):
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi, GLib
        Atspi.set_timeout(400, 1500)          # ms : une appli figée ne bloque pas la veille
        self.Atspi = Atspi
        self.ecouteur = Atspi.EventListener.new(self._sur_evenement)
        for t in ("object:state-changed", "object:text-changed", "object:children-changed",
                  "object:property-change", "object:bounds-changed", "document:load-complete"):
            self.ecouteur.register(t)
        GLib.timeout_add_seconds(PERIODE_TEXTE, self._vider_glib)
        GLib.timeout_add(250, self._vider_modifications)

    def _vider_modifications(self):
        modifications, self.modifications = self.modifications, {}
        for pid, app in modifications.items():
            self.bus.emettre("atspi", "modifie", f"interface modifiée : {app}", pid=pid, app=app)
        return True

    def _sur_evenement(self, ev):
        try:
            self.traiter(ev)
        except Exception:
            pass

    def _app(self, acc):
        try:
            a = acc.get_application()
            return _nettoyer(a.get_name() if a else "", 40) or "?"
        except Exception:
            return "?"

    def traiter(self, ev):
        src = ev.source
        if src is None:
            return
        try:
            pid = src.get_process_id()
            if pid not in self.modifications:
                self.modifications[pid] = self._app(src)
        except Exception:
            pass
        t = ev.type
        if t.startswith("object:text-changed"):
            now = time.monotonic()
            if now - self.compte_s[1] >= 1:
                self.compte_s = [0, now]
            self.compte_s[0] += 1
            if self.compte_s[0] > MAX_TEXTE_PAR_S:      # un terminal qui compile : des milliers d'événements/s
                with self.verrou:
                    self.ignores += 1
                return
            role = src.get_role_name()
            if role == "password text":
                return
            editable = src.get_state_set().contains(self.Atspi.StateType.EDITABLE)
            cle = (self._app(src), role, _nettoyer(src.get_name(), 60), bool(editable))
            n = int(ev.detail2 or 0)
            extrait = ""
            if t.endswith(":insert") and (not editable or role == "terminal"):
                extrait = _nettoyer(_valeur(ev.any_data), 120)
            self.compter(cle, n if t.endswith(":insert") else 0, 0 if t.endswith(":insert") else n, extrait)
        elif t.startswith("object:state-changed:focused"):
            if int(ev.detail1 or 0) != 1:
                return
            role = src.get_role_name()
            if role in self.ROLES_FOCUS_IGNORES:
                return
            nom, app = _nettoyer(src.get_name(), 80), self._app(src)
            cle = (app, role, nom)
            if cle == self.dernier_focus:
                return
            self.dernier_focus = cle
            self.bus.emettre("atspi", "focus", f"focus : {libelle(role)} « {nom} » ({app})", app=app, role=role, nom=nom)
        elif t.startswith("object:children-changed:add"):
            enfant = _valeur(ev.any_data)
            if enfant is None or not hasattr(enfant, "get_role_name"):
                return
            role = enfant.get_role_name()
            if role not in self.ROLES_ALERTE:
                return
            texte, app = self._texte_de(enfant), self._app(src)
            self.bus.emettre("atspi", "alerte", f"{libelle(role)} : « {texte} » ({app})", app=app, role=role, texte=texte)
        elif t == "document:load-complete":
            nom, app, now = _nettoyer(src.get_name(), 100), self._app(src), time.monotonic()
            if nom == self.derniere_page[0] and now - self.derniere_page[1] < 5:
                return
            self.derniere_page = (nom, now)
            self.bus.emettre("atspi", "page", f"page : « {nom} » chargée ({app})", app=app, nom=nom)

    def compter(self, cle, ins, sup, extrait=""):
        with self.verrou:
            s = self.seaux.setdefault(cle, {"ins": 0, "sup": 0, "extrait": ""})
            s["ins"] += ins
            s["sup"] += sup
            if extrait:
                s["extrait"] = extrait

    def _texte_de(self, acc, prof=0):
        try:
            nom = _nettoyer(acc.get_name(), 120)
            if nom:
                return nom
            if prof < 2:
                morceaux = []
                for i in range(min(acc.get_child_count(), 12)):
                    e = acc.get_child_at_index(i)
                    x = self._texte_de(e, prof + 1) if e else ""
                    if x:
                        morceaux.append(x)
                return _nettoyer(" / ".join(morceaux), 120)
        except Exception:
            pass
        return ""

    def _vider_glib(self):
        try:
            self.vider()
        except Exception:
            pass
        return True

    def vider(self):
        """Toutes les PERIODE_TEXTE s : un événement par champ modifié, pas un par caractère."""
        with self.verrou:
            seaux, self.seaux = self.seaux, {}
            ignores, self.ignores = self.ignores, 0
        sortie = []
        for cle, s in seaux.items():
            app, role, nom, editable = cle
            empreinte = (s["ins"], s["sup"], s["extrait"])
            if self.precedents.get(cle) == empreinte:
                self.repetes[cle] += 1
                if self.repetes[cle] % REPETITIONS_RESUME == 0:
                    sortie.append(self.bus.emettre("atspi", "repete", f"texte : {app} {libelle(role)}"
                                                   + (f" « {nom} »" if nom else "") + f" réécrit à l'identique ×{self.repetes[cle]}",
                                                   app=app, role=role, nom=nom, repetitions=self.repetes[cle]))
                continue
            self.precedents[cle] = empreinte
            self.repetes[cle] = 0
            msg = f"texte : +{s['ins']}/−{s['sup']} car. dans {app} {libelle(role)}" + (f" « {nom} »" if nom else "")
            if s["extrait"]:
                msg += f" : « {s['extrait']} »"
            sortie.append(self.bus.emettre("atspi", "texte", msg, app=app, role=role, nom=nom, ins=s["ins"], sup=s["sup"],
                                           extrait=s["extrait"] or None, editable=editable))
        if ignores:
            sortie.append(self.bus.emettre("atspi", "flot", f"texte : {ignores} événements non détaillés (flot > {MAX_TEXTE_PAR_S}/s)",
                                           ignores=ignores))
        return sortie


# ───────────────────────── processus ─────────────────────────
class Processus:
    """Naissance annoncée au 2e relevé où le processus est vivant (≥ PERIODE_PROC s), mort pour tout processus
    vu au moins deux fois ; fils du noyau et utilitaires de shell ignorés."""

    def __init__(self, bus, lire=None, periode=PERIODE_PROC):
        self.bus = bus
        self.lire = lire or self.lire_proc
        self.periode = periode
        initial = self.lire()
        self.age = {pid: 2 for pid in initial}            # déjà là au démarrage : stables, jamais « nés »
        self.infos = dict(initial)
        self.initiaux = set(initial)                      # leur durée de vie est inconnue

    @staticmethod
    def lire_proc():
        d = {}
        for p in os.listdir("/proc"):
            if not p.isdigit():
                continue
            try:
                with open(f"/proc/{p}/stat") as f:
                    stat = f.read()
            except OSError:
                continue
            i, j = stat.find("("), stat.rfind(")")
            comm, champs = stat[i + 1:j], stat[j + 2:].split()
            d[int(p)] = (comm, int(champs[1]))
        return d

    def pas(self):
        now = self.lire()
        naissances, morts = [], []
        for pid, (comm, ppid) in now.items():
            self.infos[pid] = (comm, ppid)
            a = self.age.get(pid, 0) + 1
            self.age[pid] = a
            if a == 2 and ppid != 2 and pid != 2 and comm not in IGNORER_PROC:
                parent = now.get(ppid, self.infos.get(ppid, ("?", 0)))[0]
                naissances.append((pid, comm, parent))
        for pid in [p for p in self.age if p not in now]:
            a = self.age.pop(pid)
            comm, ppid = self.infos.pop(pid, ("?", 0))
            initial = pid in self.initiaux
            self.initiaux.discard(pid)
            if a >= 2 and ppid != 2 and pid != 2 and comm not in IGNORER_PROC:
                morts.append((pid, comm, None if initial else a * self.periode))
        return naissances, morts

    def tic(self):
        naissances, morts = self.pas()
        self.annoncer(naissances, morts)
        return True

    def annoncer(self, naissances, morts):
        for liste, ev, signe in ((naissances, "plus", "+"), (morts, "moins", "−")):
            if len(liste) > 6:
                c = Counter(x[1] for x in liste)
                self.bus.emettre("proc", ev, f"processus : {signe} {len(liste)} ("
                                 + ", ".join(f"{k} ×{v}" if v > 1 else k for k, v in c.most_common(8)) + ")", n=len(liste))
                continue
            for pid, comm, extra in liste:
                if ev == "plus":
                    self.bus.emettre("proc", ev, f"processus : + {comm} (pid {pid}, lancé par {extra})", pid=pid, comm=comm, parent=extra)
                else:
                    vie = "présent au démarrage" if extra is None else f"vécu ≈ {extra} s"
                    self.bus.emettre("proc", ev, f"processus : − {comm} (pid {pid}, {vie})", pid=pid, comm=comm, duree=extra)


# ───────────────────────── GPU ─────────────────────────
def bande_gpu(util):
    return "saturé" if util >= 90 else "fort" if util >= 60 else "actif" if util >= 10 else "repos"


def _comm(pid):
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except OSError:
        return "?"


class Gpu:
    def __init__(self, bus):
        self.bus = bus
        self.nvml = None
        self.bande, self.candidat, self.mem, self.procs = None, (None, 0), None, {}
        try:
            import pynvml
            pynvml.nvmlInit()
            self.nvml, self.h = pynvml, pynvml.nvmlDeviceGetHandleByIndex(0)
        except Exception as e:
            bus.emettre("gpu", "erreur", f"GPU : NVML indisponible ({e})"[:160])

    def mesurer(self):
        n = self.nvml
        util = n.nvmlDeviceGetUtilizationRates(self.h).gpu
        mem = n.nvmlDeviceGetMemoryInfo(self.h).used // 2 ** 20
        procs = {}
        for f in (n.nvmlDeviceGetGraphicsRunningProcesses, n.nvmlDeviceGetComputeRunningProcesses):
            try:
                for p in f(self.h):
                    procs[p.pid] = (_comm(p.pid), (p.usedGpuMemory or 0) // 2 ** 20)
            except Exception:
                pass
        return util, mem, procs

    def tic(self):
        if not self.nvml:
            return False
        try:
            util, mem, procs = self.mesurer()
        except Exception:
            return True
        b = bande_gpu(util)
        if self.bande is None:
            self.bande, self.mem, self.procs = b, mem, procs
            gros = sorted(procs.items(), key=lambda kv: -kv[1][1])[:4]
            self.bus.emettre("gpu", "initial", f"GPU : {b} ({util} %), {mem} Mo, {len(procs)} processus ("
                             + ", ".join(f"{c} {m} Mo" for _, (c, m) in gros) + ")", util=util, mem=mem, bande=b)
            for pid, (c, m) in procs.items():
                self.bus.etat.maj({"src": "gpu", "ev": "plus", "pid": pid, "comm": c, "msg": ""})
            return True
        if b != self.bande:
            self.candidat = (b, self.candidat[1] + 1) if self.candidat[0] == b else (b, 1)
            if self.candidat[1] >= 2:                 # deux relevés d'affilée (10 s) avant de changer de bande
                self.bande, self.candidat = b, (None, 0)
                self.bus.emettre("gpu", "charge", f"GPU : {b} ({util} %, {mem} Mo)", util=util, mem=mem, bande=b)
        else:
            self.candidat = (None, 0)
        if abs(mem - self.mem) >= 1024:
            self.bus.emettre("gpu", "memoire", f"GPU : mémoire {mem} Mo ({'+' if mem > self.mem else '−'}{abs(mem - self.mem)} Mo)", mem=mem)
            self.mem = mem
        for pid in procs.keys() - self.procs.keys():
            c, m = procs[pid]
            self.bus.emettre("gpu", "plus", f"GPU : + {c} (pid {pid}, {m} Mo)", pid=pid, comm=c, mem=m)
        for pid in self.procs.keys() - procs.keys():
            self.bus.emettre("gpu", "moins", f"GPU : − {self.procs[pid][0]} (pid {pid})", pid=pid, comm=self.procs[pid][0])
        self.procs = procs
        return True


# ───────────────────────── audio (pactl) ─────────────────────────
def parse_pactl(texte):
    """`LC_ALL=C pactl list sink-inputs|source-outputs` → [{index, app, media, corked}]."""
    blocs = []
    for ligne in texte.splitlines():
        m = re.match(r"^(Sink Input|Source Output) #(\d+)", ligne)
        if m:
            blocs.append({"index": int(m.group(2)), "app": "", "media": "", "binaire": "", "corked": False})
            continue
        if not blocs:
            continue
        l = ligne.strip()
        if l.startswith("Corked:"):
            blocs[-1]["corked"] = l.split(":", 1)[1].strip().lower() == "yes"
        elif l.startswith("application.name ="):
            blocs[-1]["app"] = l.split("=", 1)[1].strip().strip('"')
        elif l.startswith("application.process.binary ="):
            blocs[-1]["binaire"] = l.split("=", 1)[1].strip().strip('"')
        elif l.startswith("media.name ="):
            blocs[-1]["media"] = l.split("=", 1)[1].strip().strip('"')
    return blocs


def etat_audio(blocs):
    """Clé (application, flux, index) ; sans application.name (parec, pw-cat…), le binaire puis le nom du flux."""
    return {(b["app"] or b["binaire"] or b["media"] or "?", b["media"], b["index"]): b["corked"] for b in blocs}


def diff_audio(avant, apres):
    """→ [(type, app, media)] : joue / pret (nouveau mais en pause) / pause / reprise / fin."""
    evts = []
    for cle, corked in apres.items():
        if cle not in avant:
            evts.append(("pret" if corked else "joue", cle[0], cle[1]))
        elif avant[cle] != corked:
            evts.append(("pause" if corked else "reprise", cle[0], cle[1]))
    for cle in avant:
        if cle not in apres:
            evts.append(("fin", cle[0], cle[1]))
    return evts


class Audio(threading.Thread):
    MSG = {"joue": "audio : {app} joue", "pret": "audio : {app} ouvre un flux (en pause)", "pause": "audio : {app} en pause",
           "reprise": "audio : {app} reprend", "fin": "audio : fin {app}"}
    MSG_MICRO = {"joue": "micro : pris par {app}", "pret": "micro : ouvert par {app} (en pause)", "pause": "micro : {app} en pause",
                 "reprise": "micro : {app} reprend", "fin": "micro : rendu par {app}"}

    def __init__(self, bus):
        super().__init__(daemon=True, name="audio")
        self.bus = bus
        self.sorties, self.entrees = {}, {}
        self.minuteur = None
        self.verrou = threading.Lock()

    def lister(self, quoi):
        r = subprocess.run(["pactl", "list", quoi], capture_output=True, text=True, timeout=10,
                           env={**os.environ, "LC_ALL": "C"})
        return etat_audio(parse_pactl(r.stdout))

    def rafraichir(self, initial=False):
        with self.verrou:
            s, e = self.lister("sink-inputs"), self.lister("source-outputs")
            if initial:                       # l'état de départ passe par les mêmes événements (l'état instantané en vit)
                self.sorties, self.entrees = {}, {}
            self.annoncer(diff_audio(self.sorties, s), self.MSG, "audio")
            self.annoncer(diff_audio(self.entrees, e), self.MSG_MICRO, "micro")
            self.sorties, self.entrees = s, e

    def annoncer(self, evts, msgs, src):
        groupes = Counter((t, app) for t, app, _ in evts)
        medias = {(t, app): media for t, app, media in evts}
        for (t, app), n in groupes.items():
            msg = msgs[t].format(app=app) + (f" ({n} flux)" if n > 1 else (f" « {medias[(t, app)]} »" if medias[(t, app)] and src == "audio" else ""))
            self.bus.emettre(src, t, msg, app=app, flux=n)

    def planifier(self):
        with self.verrou:
            if self.minuteur:
                return
            self.minuteur = threading.Timer(0.4, self._declencher)
            self.minuteur.start()

    def _declencher(self):
        self.minuteur = None
        try:
            self.rafraichir()
        except Exception:
            pass

    def run(self):
        while True:
            try:
                self.rafraichir(initial=True)
                p = subprocess.Popen(["pactl", "subscribe"], stdout=subprocess.PIPE, text=True, env={**os.environ, "LC_ALL": "C"})
                for ligne in p.stdout:
                    if "sink-input" in ligne or "source-output" in ligne:
                        self.planifier()
                p.wait()
            except Exception as e:
                self.bus.emettre("audio", "erreur", f"audio : {e}"[:160])
            time.sleep(5)


# ───────────────────────── pilote (STOP, réservation) ─────────────────────────
class Pilote:
    def __init__(self, bus):
        self.bus = bus
        self.stop, self.agent = self.lire_stop(), self.lire_agent()
        if self.stop:
            bus.emettre("pilote", "stop", f"pilote : STOP en vigueur ({self.stop})", stop=self.stop)
        if self.agent:
            bus.emettre("pilote", "reserve", f"pilote : écran réservé par {self.agent}", agent=self.agent)

    @staticmethod
    def lire_stop():
        try:
            with open(chemin("stop")) as f:
                return _nettoyer(f.read(), 100) or "sans motif"
        except OSError:
            return None

    @staticmethod
    def lire_agent():
        try:
            with open(chemin("pilote")) as f:
                return json.load(f).get("agent") or None
        except (OSError, ValueError, AttributeError):
            return None

    def tic(self):
        s, a = self.lire_stop(), self.lire_agent()
        if s != self.stop:
            if s:
                self.bus.emettre("pilote", "stop", f"pilote : STOP posé ({s})", stop=s)
            else:
                self.bus.emettre("pilote", "reprise", "pilote : STOP levé")
            self.stop = s
        if a != self.agent:
            if a:
                self.bus.emettre("pilote", "reserve", f"pilote : écran réservé par {a}", agent=a)
            else:
                self.bus.emettre("pilote", "libre", f"pilote : écran libéré (était {self.agent})")
            self.agent = a
        return True


# ───────────────────────── écran : bouge / figé ─────────────────────────
class MachineEcran:
    """calme (< 1 %), activité (1-40 %), vidéo (> 40 %) ; changement d'état confirmé par deux relevés.
    FIGÉ : aucun pixel réduit changé depuis FIGE_APRES s (rappel toutes les RAPPEL_FIGE s)."""

    def __init__(self):
        self.etat, self.candidat = None, (None, 0)
        self.dernier_changement = None
        self.fige_annonce = None
        self.dernier_actif = None       # « vidéo » juste avant l'immobilité = un jeu ou une vidéo qui s'est arrêté : suspect

    def bande(self, pct):
        """Seuils avec hystérésis : on quitte « vidéo » sous 30 % (entrée > 40), « activité » sous 0,5 % (entrée ≥ 1)."""
        if pct > 40 or (self.etat == "vidéo" and pct > 30):
            return "vidéo"
        if pct >= 1 or (self.etat == "activité" and pct >= 0.5):
            return "activité"
        return "calme"

    def observer(self, pct, t, indices_jeu=False):
        """`indices_jeu` : ce que le bus sait par ailleurs (carte chargée, audio en cours) ; avec la mémoire de
        l'état « vidéo » juste avant l'arrêt, ça distingue un écran FIGÉ d'un bureau simplement immobile."""
        evts = []
        if pct > 0 or self.dernier_changement is None:
            self.dernier_changement = t
        b = self.bande(pct)
        if b in ("activité", "vidéo"):
            self.dernier_actif = b
        if b != self.etat:
            self.candidat = (b, self.candidat[1] + 1) if self.candidat[0] == b else (b, 1)
            if self.candidat[1] >= 2 or self.etat is None:
                self.etat, self.candidat = b, (None, 0)
                evts.append(("etat", b, round(pct, 1)))
        else:
            self.candidat = (None, 0)
        immobile = t - self.dernier_changement
        if pct > 0 and self.fige_annonce is not None:
            self.fige_annonce = None
            evts.append(("reprise", round(pct, 1)))
        elif immobile >= FIGE_APRES and (self.fige_annonce is None or t - self.fige_annonce >= RAPPEL_FIGE):
            self.fige_annonce = t
            evts.append(("fige", int(immobile), bool(indices_jeu or self.dernier_actif == "vidéo")))
        return evts


# ───────────── dessin : X Damage, le moment exact où quelque chose s'affiche ─────────────
class _XRectangle(ctypes.Structure):
    _fields_ = [("x", ctypes.c_short), ("y", ctypes.c_short), ("width", ctypes.c_ushort), ("height", ctypes.c_ushort)]


class _XDamageNotifyEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
                ("drawable", ctypes.c_ulong), ("damage", ctypes.c_ulong), ("level", ctypes.c_int), ("more", ctypes.c_int),
                ("timestamp", ctypes.c_ulong), ("area", _XRectangle), ("geometry", _XRectangle)]


class AgregeDessin:
    """Par fenêtre : redessins et union des zones sur une période (250 ms), et transitions
    calme → actif (« s'affiche ») → continu (≥ CONTINU/s : vidéo, jeu) → arrêt (SILENCE s sans redessin)."""
    CONTINU = 10
    SILENCE = 2.0
    RAPPEL = 30.0

    def __init__(self):
        self.f = {}

    def noter(self, wid, x, y, w, h, t):
        f = self.f.setdefault(wid, {"n": 0, "bbox": None, "dernier": None, "etat": "calme", "depuis": t, "hist": [], "annonce": 0.0})
        f["n"] += 1
        f["dernier"] = t
        b = f["bbox"]
        f["bbox"] = (x, y, x + w, y + h) if b is None else (min(b[0], x), min(b[1], y), max(b[2], x + w), max(b[3], y + h))

    def oublier(self, wid):
        self.f.pop(wid, None)

    def vider(self, t):
        """→ ([(wid, genre, info)], aire) avec genre ∈ debut, zone, continu, arret ; `aire` = somme des boîtes
        redessinées sur la période (une par fenêtre) ; remet les compteurs de la période."""
        sorties, aire = [], 0
        for wid, f in list(self.f.items()):
            n, bbox = f["n"], f["bbox"]
            f["n"], f["bbox"] = 0, None
            if bbox:
                aire += (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
            f["hist"] = [(tt, nn) for tt, nn in f["hist"] if t - tt <= 1.0] + ([(t, n)] if n else [])
            taux = sum(nn for _, nn in f["hist"])
            if n:
                if f["etat"] == "calme":
                    f["etat"], f["depuis"], f["annonce"] = "actif", t, t
                    sorties.append((wid, "debut", {"bbox": bbox, "n": n}))
                elif f["etat"] == "actif" and taux >= self.CONTINU and t - f["depuis"] >= 1.0:
                    f["etat"], f["annonce"] = "continu", t
                    sorties.append((wid, "continu", {"bbox": bbox, "taux": taux}))
                elif f["etat"] == "actif" and taux < self.CONTINU // 2:
                    sorties.append((wid, "zone", {"bbox": bbox, "n": n}))
                elif f["etat"] == "continu" and t - f["annonce"] >= self.RAPPEL:
                    f["annonce"] = t
                    sorties.append((wid, "continu", {"bbox": bbox, "taux": taux}))
            elif f["etat"] != "calme" and f["dernier"] is not None and t - f["dernier"] >= self.SILENCE:
                etait, f["etat"] = f["etat"], "calme"
                sorties.append((wid, "arret", {"etait": etait, "duree": int(t - f["depuis"])}))
        return sorties, aire


class Dessin(threading.Thread):
    """Chaque fenêtre signale au serveur X la zone qu'elle vient de redessiner (extension Damage, ce qu'écoute le
    compositeur) : on l'entend à la milliseconde, sans capture. Quand le rythme est lent, on lit DANS la zone ce que
    l'application y déclare (AT-SPI) : ce qui vient de s'afficher, en données. Remplace la capture périodique."""

    def __init__(self, bus, fenetres=None, lire_zone=None):
        super().__init__(daemon=True, name="dessin")
        self.bus, self.fenetres, self.lire_zone = bus, fenetres, lire_zone
        self.agrege = AgregeDessin()
        self.verrou = threading.Lock()
        self.aires = []
        self.actif = False
        self.largeur, self.hauteur = 2560, 1440
        self.signatures = {}
        self.derniere_lecture = 0.0
        self.sans_arbre = {}

    def pct_recent(self, fenetre_s=2.0):
        """Part de l'écran redessinée : la plus grande période (250 ms) des `fenetre_s` dernières secondes."""
        now = time.monotonic()
        with self.verrou:
            self.aires = [(t, a) for t, a in self.aires if now - t <= fenetre_s]
            pire = max((a for _, a in self.aires), default=0)
        return min(100.0, 100.0 * pire / float(self.largeur * self.hauteur))

    def run(self):
        try:
            self.boucle()
        except Exception as e:
            self.actif = False
            self.bus.emettre("dessin", "erreur", f"dessin : Damage indisponible ({e}) — retour à la capture périodique"[:200])

    def boucle(self):
        X = ctypes.CDLL(ctypes.util.find_library("X11"))
        XD = ctypes.CDLL(ctypes.util.find_library("Xdamage"))
        X.XOpenDisplay.restype, X.XOpenDisplay.argtypes = ctypes.c_void_p, [ctypes.c_char_p]
        d = X.XOpenDisplay(DISPLAY.encode())
        if not d:
            raise RuntimeError("XOpenDisplay a échoué")
        GESTIONNAIRE = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        self._gestionnaire = GESTIONNAIRE(lambda dpy, ev: 0)       # une fenêtre disparue ne doit jamais tuer le service
        X.XSetErrorHandler.argtypes = [GESTIONNAIRE]
        X.XSetErrorHandler(self._gestionnaire)
        base, erreurs = ctypes.c_int(), ctypes.c_int()
        XD.XDamageQueryExtension.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
        if not XD.XDamageQueryExtension(d, ctypes.byref(base), ctypes.byref(erreurs)):
            raise RuntimeError("extension Damage absente")
        XD.XDamageCreate.restype, XD.XDamageCreate.argtypes = ctypes.c_ulong, [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int]
        XD.XDamageSubtract.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        XD.XDamageDestroy.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        for f in (X.XPending, X.XFlush, X.XConnectionNumber, X.XDefaultScreen):
            f.argtypes = [ctypes.c_void_p]
        X.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        X.XRootWindow.restype, X.XRootWindow.argtypes = ctypes.c_ulong, [ctypes.c_void_p, ctypes.c_int]
        X.XDisplayWidth.argtypes = X.XDisplayHeight.argtypes = [ctypes.c_void_p, ctypes.c_int]
        X.XTranslateCoordinates.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                                            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ulong)]
        ecran_n = X.XDefaultScreen(d)
        racine = X.XRootWindow(d, ecran_n)
        self.largeur, self.hauteur = X.XDisplayWidth(d, ecran_n), X.XDisplayHeight(d, ecran_n)
        fd = X.XConnectionNumber(d)
        suivis, creation, buf = {}, {}, ctypes.create_string_buffer(192)
        self.actif = True
        self.bus.emettre("dessin", "pret", "dessin : chaque fenêtre signale ses zones redessinées (X Damage) ; plus de capture périodique")
        prochain = time.monotonic() + 0.25
        while True:
            noms = dict(self.fenetres.noms) if self.fenetres else {}
            for wid, (nom, _) in noms.items():
                if wid not in suivis and not _cadre(nom):
                    suivis[wid] = XD.XDamageCreate(d, wid, 1)              # 1 = XDamageReportBoundingBox
                    creation[wid] = time.monotonic()                       # le premier rapport couvre toute la fenêtre : à ignorer
            for wid in [w for w in suivis if w not in noms]:
                XD.XDamageDestroy(d, suivis.pop(wid))
                creation.pop(wid, None)
                with self.verrou:
                    self.agrege.oublier(wid)
                self.signatures.pop(wid, None)
            X.XFlush(d)
            select.select([fd], [], [], 0.1)
            while X.XPending(d):
                X.XNextEvent(d, buf)
                if ctypes.cast(buf, ctypes.POINTER(ctypes.c_int)).contents.value == base.value:
                    e = ctypes.cast(buf, ctypes.POINTER(_XDamageNotifyEvent)).contents
                    now = time.monotonic()
                    if now - creation.get(int(e.drawable), 0.0) >= 0.5:
                        with self.verrou:
                            self.agrege.noter(int(e.drawable), e.area.x, e.area.y, e.area.width, e.area.height, now)
                    if not e.more:
                        XD.XDamageSubtract(d, e.damage, 0, 0)
            if time.monotonic() >= prochain:
                prochain = time.monotonic() + 0.25
                with self.verrou:
                    sorties, aire = self.agrege.vider(time.monotonic())
                    self.aires.append((time.monotonic(), aire))
                for wid, genre, info in sorties:
                    try:
                        self.publier(wid, genre, info, noms, X, d, racine)
                    except Exception:
                        pass

    def publier(self, wid, genre, info, noms, X, d, racine):
        app, titre = noms.get(wid, ("", ""))
        qui = f"{app or '?'} « {titre} »"
        bbox, zone, ecran_bbox = info.get("bbox"), "", None
        if bbox:
            rx, ry, enfant = ctypes.c_int(), ctypes.c_int(), ctypes.c_ulong()
            X.XTranslateCoordinates(d, wid, racine, bbox[0], bbox[1], ctypes.byref(rx), ctypes.byref(ry), ctypes.byref(enfant))
            l, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            ecran_bbox = (rx.value, ry.value, rx.value + l, ry.value + h)
            zone = f"zone {l}×{h} en ({rx.value},{ry.value})"
        if genre == "continu":
            self.bus.emettre("dessin", "continu", f"en continu : {qui} — {info['taux']} redessins/s, {zone}",
                             app=app, titre=titre, wid=hex(wid), taux=info["taux"], bbox=ecran_bbox)
        elif genre == "arret":
            self.bus.emettre("dessin", "arret", f"s'arrête : {qui} après {info['duree']} s ({info['etait']})",
                             app=app, titre=titre, wid=hex(wid), duree=info["duree"], etait=info["etait"])
        else:
            lignes = self.lire(wid, ecran_bbox)
            signature = (ecran_bbox, tuple(lignes or []))
            sig, rep = self.signatures.get(wid, (None, 0))
            if genre == "zone" and signature == sig:
                rep += 1
                self.signatures[wid] = (sig, rep)
                if rep % REPETITIONS_RESUME == 0:
                    self.bus.emettre("dessin", "repete", f"redessine la même zone ×{rep} : {qui} — {zone}",
                                     app=app, titre=titre, wid=hex(wid), repetitions=rep, bbox=ecran_bbox)
                return
            self.signatures[wid] = (signature, 0)
            msg = ("s'affiche : " if genre == "debut" else "redessine : ") + f"{qui} — {zone}" + (" : " + " | ".join(lignes) if lignes else "")
            self.bus.emettre("dessin", genre, msg, app=app, titre=titre, wid=hex(wid), bbox=ecran_bbox, lignes=lignes or None)

    def lire(self, wid, bbox):
        """Ce que l'application déclare dans la zone (AT-SPI), au plus une lecture par seconde."""
        if not self.lire_zone or bbox is None:
            return None
        now = time.monotonic()
        if now - self.derniere_lecture < 1.0 or now - self.sans_arbre.get(wid, -1e9) < 60:
            return None
        self.derniere_lecture = now
        try:
            lignes = self.lire_zone(wid, bbox)
        except Exception:
            return None
        if lignes is None:
            self.sans_arbre[wid] = now
            return None
        return lignes[:8]


class Ecran:
    def __init__(self, bus, fenetres=None, dessin=None):
        self.bus = bus
        self.fenetres = fenetres
        self.dessin = dessin
        self.machine = MachineEcran()
        self.prec = None
        self.erreur_dite = False

    def mesurer(self):
        import numpy as np
        from perception import capturer
        a = np.asarray(capturer().convert("L").resize((64, 36)), dtype=np.int16)
        pct = None if self.prec is None else float((np.abs(a - self.prec) > SEUIL_PIXEL).mean() * 100)
        self.prec = a
        return pct

    def tic(self):
        if self.dessin and self.dessin.actif:        # les fenêtres disent elles-mêmes ce qu'elles redessinent : aucune capture
            pct = self.dessin.pct_recent(2.0)
        else:
            if not IMAGES_AUTORISEES:
                if not self.erreur_dite:
                    self.erreur_dite = True
                    self.bus.emettre("ecran", "indisponible", "activité visuelle inconnue : X Damage absent, captures désactivées")
                return True
            try:
                pct = self.mesurer()
            except Exception as e:
                if not self.erreur_dite:
                    self.erreur_dite = True
                    self.bus.emettre("ecran", "erreur", f"écran : capture impossible ({e})"[:160])
                return True
            if pct is None:                   # première capture : rien à comparer
                return True
        titre = self.fenetres.titre_actif if self.fenetres else ""
        d = self.bus.etat.d
        indices_jeu = (d.get("gpu") or {}).get("bande") in ("fort", "saturé") or bool(d.get("audio"))
        for e in self.machine.observer(pct, time.time(), indices_jeu):
            if e[0] == "etat":
                self.bus.emettre("ecran", "etat", f"écran : {e[1]} ({e[2]} % de la surface change)", etat=e[1], pct=e[2], fenetre=titre)
            elif e[0] == "fige":
                if e[2]:
                    self.bus.emettre("ecran", "fige", f"écran : FIGÉ depuis {e[1]} s (fenêtre active « {titre} »)",
                                     depuis=e[1], fenetre=titre, suspect=True)
                else:
                    self.bus.emettre("ecran", "fige", f"écran : immobile depuis {e[1]} s, rien ne se passe (fenêtre active « {titre} »)",
                                     depuis=e[1], fenetre=titre, suspect=False)
            else:
                self.bus.emettre("ecran", "reprise", f"écran : bouge à nouveau ({e[1]} %)", pct=e[1], fenetre=titre)
        return True


# ───────────────────────── l'œil : l'anti-graphique ─────────────────────────
def extraire_json(texte):
    """Le modèle entoure parfois sa réponse de ``` , d'un bloc <think> ou de texte : on prend le premier objet { … } complet."""
    t = re.sub(r"<think>.*?</think>", "", texte or "", flags=re.S)
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t.strip())
    try:
        return json.loads(t)
    except ValueError:
        pass
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(t[i:j + 1])
        except ValueError:
            pass
    return None


class Oeil:
    """La carte graphique à l'envers : l'image redevient des données. À chaque changement notable (fenêtre, état
    d'écran, alerte, processus GPU, audio) et au plus une fois toutes les OEIL_INTERVALLE s, Qwen vision (vLLM local
    sur la 5090, le moteur d'`ecran decrire`) rend une SCÈNE structurée, publiée sur le bus et dans l'état."""

    CONSIGNE = (
        "Tu es l'ANTI-GRAPHIQUE d'un agent logiciel. La carte graphique a transformé des données en image pour un "
        "humain ; toi tu refais le chemin inverse : l'image redevient des données. Réponds UNIQUEMENT par un objet JSON, "
        "sans texte autour, avec ces clés :\n"
        '"scene" : une phrase factuelle (quelle application, quel écran, ce qui s\'y passe) ;\n'
        '"type" : "bureau" | "application" | "page web" | "jeu" | "video" | "dialogue" | "terminal" | "autre" ;\n'
        '"elements" : au plus 12 éléments, les plus importants, chacun {"type": "fenetre|bouton|champ|menu|onglet|liste|'
        'texte|image|personnage|objet|dialogue|alerte|indicateur|autre", "libelle": "nom ou contenu court", '
        '"zone": "haut-gauche|haut|haut-droite|gauche|centre|droite|bas-gauche|bas|bas-droite", '
        '"etat": "actif|inactif|selectionne|chargement|erreur|…" ou null} ;\n'
        '"evenement" : ce qui est en train de se passer (chargement, erreur, attente d\'une saisie, partie en cours…) ou null ;\n'
        '"alerte" : un message d\'erreur ou une boîte qui attend une réponse, ou null ;\n'
        '"texte_cle" : au plus 5 textes importants (titre, montant, message), jamais une transcription.\n'
        "Ne décris pas l'esthétique. Ne lis pas tout le texte. N'invente rien qui ne soit pas visible.\n"
        "Tu ne transcris JAMAIS lettre à lettre un nom propre, une adresse, un identifiant, un numéro ou un code : "
        "écris leur nature entre crochets, par exemple [adresse mail], [identifiant], [numéro]. Les textes clés sont des "
        "titres ou des messages courts, clairement lisibles ; en cas de doute, ne les cite pas.\n"
        "Les FAITS CERTAINS fournis avec l'image viennent du système (fenêtre active, bureau, état) : ils priment sur ce "
        "que tu crois voir et tu ne les contredis jamais."
    )
    _RE_MAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
    _RE_NUM = re.compile(r"(?<!\d)\d(?: ?\d){4,}(?!\d)")              # ≥ 5 chiffres (espaces simples admis) ; 12,50 et 18:42 restent

    @classmethod
    def masquer(cls, s):
        """Ce que l'œil n'a pas le droit de lire lettre à lettre, même s'il essaie."""
        return cls._RE_NUM.sub("[numéro]", cls._RE_MAIL.sub("[adresse mail]", s or ""))

    def faits(self):
        """Les données sûres du bus, écrites pour le modèle : elles priment sur l'image."""
        d = self.bus.etat.d
        lignes = ["système : Ubuntu 24.04, bureau GNOME sur X11 (ce n'est ni Windows ni macOS)", f"heure : {time.strftime('%H:%M')}"]
        fa = d.get("fenetre_active") or {}
        if fa:
            lignes.append(f"fenêtre active (X11) : application « {fa.get('app')} », titre « {fa.get('titre')} »")
        e = d.get("ecran") or {}
        if e:
            lignes.append(f"écran : {e.get('etat')}" + (f", immobile depuis {e['fige_depuis_s']} s" if e.get("fige_depuis_s") else ""))
        g = d.get("gpu") or {}
        if g:
            lignes.append(f"carte graphique : {g.get('bande')} ({g.get('util')} %)")
        if d.get("audio"):
            lignes.append("audio : " + ", ".join(f"{k} {v}" for k, v in d["audio"].items()))
        if d.get("micro"):
            lignes.append("micro utilisé par : " + ", ".join(d["micro"]))
        f = d.get("focus") or {}
        if f:
            lignes.append(f"focus (AT-SPI) : {f.get('role')} « {f.get('nom')} » dans {f.get('app')}")
        return "\n".join("- " + l for l in lignes)
    DECLENCHEURS = {("fenetre", "activee"), ("fenetre", "ouverte"), ("ecran", "etat"), ("ecran", "fige"),
                    ("ecran", "reprise"), ("atspi", "alerte"), ("gpu", "plus"), ("gpu", "moins"),
                    ("audio", "joue"), ("audio", "fin"), ("pilote", "stop")}

    def __init__(self, bus, url=LLM_URL, capturer=None):
        self.bus, self.url, self.capturer = bus, url, capturer
        self.verrou = threading.Lock()
        self.demande, self.dernier, self.en_cours, self.indispo = None, 0.0, False, None

    @classmethod
    def interesse(cls, e):
        return (e.get("src"), e.get("ev")) in cls.DECLENCHEURS

    def sur_evenement(self, e):
        if e.get("ev") == "fige" and not e.get("suspect"):      # un bureau immobile n'est pas un blocage : pas de regard
            return
        if self.interesse(e):
            with self.verrou:
                self.demande = e.get("msg") or f"{e.get('src')}:{e.get('ev')}"

    def tic(self):
        now = time.monotonic()
        with self.verrou:
            if self.en_cours or (self.indispo and now - self.indispo < OEIL_REESSAI):
                return True
            raison = None
            if self.demande and now - self.dernier >= OEIL_INTERVALLE:
                raison, self.demande = self.demande, None
            elif now - self.dernier >= OEIL_RAFRAICHIR and (self.bus.etat.d.get("ecran") or {}).get("etat") in ("activité", "vidéo"):
                raison = "rafraîchissement : l'écran bouge"
            if not raison:
                return True
            self.en_cours = True
        threading.Thread(target=self._regarder_puis_liberer, args=(raison,), daemon=True, name="oeil").start()
        return True

    def _regarder_puis_liberer(self, raison):
        try:
            self.regarder(raison)
        finally:
            with self.verrou:
                self.en_cours, self.dernier = False, time.monotonic()

    def regarder_maintenant(self, raison="demande"):
        """Pour `GET /oeil` : synchrone, refuse si un regard est déjà en cours."""
        with self.verrou:
            if self.en_cours:
                return {"erreur": "un regard est déjà en cours, réessaie dans quelques secondes"}
            self.en_cours = True
        try:
            return self.regarder(raison)
        finally:
            with self.verrou:
                self.en_cours, self.dernier = False, time.monotonic()

    def image_b64(self):
        import base64
        import io
        from PIL import Image
        from perception import capturer
        img = (self.capturer or capturer)()
        img.thumbnail((OEIL_MAX_COTE, OEIL_MAX_COTE), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode(), img.width, img.height

    def regarder(self, raison="demande"):
        import urllib.request
        t0 = time.monotonic()
        try:
            b64, w, h = self.image_b64()
            corps = {"model": "jarvis-llm", "temperature": 0, "max_tokens": 1500,
                     "chat_template_kwargs": {"enable_thinking": False},      # Qwen3 : pas de bloc de réflexion avant le JSON
                     "messages": [{"role": "system", "content": self.CONSIGNE},
                                  {"role": "user", "content": [
                                      {"type": "text", "text": f"Image de {w}×{h} pixels. Raison du regard : {raison}.\n"
                                                               "FAITS CERTAINS (ils priment sur l'image, ne les contredis pas) :\n"
                                                               f"{self.faits()}\nRéponds par le JSON seul."},
                                      {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}}]}]}
            req = urllib.request.Request(f"{self.url}/chat/completions", data=json.dumps(corps).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                texte = json.loads(r.read())["choices"][0]["message"]["content"]
        except Exception as e:
            with self.verrou:
                premiere, self.indispo = self.indispo is None, time.monotonic()
            if premiere:
                self.bus.emettre("oeil", "erreur", f"œil : moteur Qwen injoignable ({self.url}) : {type(e).__name__} — "
                                 f"nouvel essai dans {OEIL_REESSAI // 60} min", url=self.url)
            return {"erreur": f"moteur injoignable : {type(e).__name__}"}
        with self.verrou:
            self.indispo = None
        scene = extraire_json(texte)
        if not isinstance(scene, dict):
            self.bus.emettre("oeil", "erreur", "œil : réponse non structurée du modèle", brut=(texte or "")[:200])
            return {"erreur": "réponse non structurée", "brut": (texte or "")[:200]}
        scene = self.normaliser(scene)
        scene["raison"], scene["duree_ms"] = _nettoyer(raison, 120), int((time.monotonic() - t0) * 1000)
        msg = (f"œil : {scene['scene']}" + (f" — {scene['evenement']}" if scene.get("evenement") else "")
               + (f" — ALERTE : {scene['alerte']}" if scene.get("alerte") else "")
               + f" ({len(scene['elements'])} éléments, {scene['duree_ms']} ms)")
        self.bus.emettre("oeil", "scene", msg, scene=scene)
        return scene

    @classmethod
    def normaliser(cls, s):
        m = lambda v, n: cls.masquer(_nettoyer(v, n))  # noqa: E731
        els = s.get("elements") if isinstance(s.get("elements"), list) else []
        propres = [{"type": str(e.get("type") or "autre")[:20], "libelle": m(e.get("libelle"), 80),
                    "zone": str(e.get("zone") or "?")[:12], "etat": _nettoyer(e.get("etat"), 30) or None}
                   for e in els[:12] if isinstance(e, dict)]
        return {"scene": m(s.get("scene"), 200) or "?", "type": str(s.get("type") or "autre")[:16],
                "elements": propres, "evenement": m(s.get("evenement"), 160) or None,
                "alerte": m(s.get("alerte"), 160) or None,
                "texte_cle": [m(t, 80) for t in (s.get("texte_cle") or [])[:5] if isinstance(t, str)]}


# ───────────────────────── le pont : HTTP local, lecture seule ─────────────────────────
def compacter(e):
    """Un élément AT-SPI du pilote → une ligne courte pour un agent : `bouton « Envoyer » [focus]`."""
    ligne = e.get("role", "?") + (f" « {e['nom']} »" if e.get("nom") else "")
    if e.get("texte"):
        ligne += f" = {e['texte']}"
    if e.get("etats"):
        ligne += " [" + ", ".join(e["etats"]) + "]"
    return ligne


class Pont(threading.Thread):
    """`GET /etat` (état instantané), `/texte` (fenêtre active en lignes AT-SPI, lue dans la boucle GLib),
    `/evenements?n=50&src=gpu,ecran`, `/flux?src=…` (SSE : chaque événement dès qu'il arrive), `/` (aide).
    127.0.0.1 seulement ; aucune action possible par ce pont."""

    def __init__(self, bus, port=PORT, lecteur_texte=None, oeil=None, carte=None):
        super().__init__(daemon=True, name="pont")
        self.bus = bus
        self.lecteur_texte = lecteur_texte
        self.oeil = oeil
        self.carte = carte
        pont = self

        class Requete(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def _json(self, code, obj):
                corps = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(corps)))
                self.end_headers()
                self.wfile.write(corps)

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(u.query)
                srcs = {s for s in q.get("src", [""])[0].split(",") if s}
                if u.path == "/etat":
                    self._json(200, pont.bus.etat.instantane())
                elif u.path == "/evenements":
                    try:
                        n = max(1, min(int(q.get("n", ["50"])[0]), 2000))
                    except ValueError:
                        n = 50
                    self._json(200, pont.evenements(n, srcs))
                elif u.path == "/texte":
                    if not pont.lecteur_texte:
                        self._json(503, {"erreur": "lecture AT-SPI indisponible"})
                    else:
                        self._json(200, pont.lecteur_texte(tout=q.get("tout", ["0"])[0] == "1"))
                elif u.path == "/carte":
                    if not pont.carte:
                        self._json(503, {"erreur": "carte indisponible"})
                    else:
                        self._json(200, pont.carte.instantane(q.get("fenetre", [""])[0]))
                elif u.path == "/carte/flux":
                    if not pont.carte:
                        self._json(503, {"erreur": "carte indisponible"})
                    else:
                        try:
                            duree = float(q["duree"][0]) if "duree" in q else None
                            if duree is not None and not 0 < duree <= 600:
                                raise ValueError()
                        except (ValueError, TypeError):
                            self._json(400, {"erreur": "duree doit être comprise entre 0 et 600 secondes"})
                            return
                        self.flux_carte(q.get("fenetre", [""])[0], duree)
                elif u.path == "/carte/cible":
                    if not pont.carte:
                        self._json(503, {"erreur": "carte indisponible"})
                    else:
                        code, cible = pont.carte.cible(q.get("id", [""])[0])
                        self._json(code, cible)
                elif u.path == "/oeil":
                    if not pont.oeil:
                        self._json(503, {"erreur": "œil indisponible"})
                    else:
                        self._json(200, pont.oeil.regarder_maintenant(q.get("raison", ["demande via le pont"])[0]))
                elif u.path == "/flux":
                    self.flux(srcs)
                elif u.path == "/":
                    self._json(200, {"pont": "veille", "lecture_seule": True,
                                     "images_autorisees": IMAGES_AUTORISEES,
                                     "routes": ["/etat", "/carte?fenetre=MOTIF", "/carte/flux?fenetre=MOTIF&duree=60 (SSE)",
                                                "/carte/cible?id=ID", "/texte?tout=1", "/oeil", "/evenements?n=50&src=a,b",
                                                "/flux?src=a,b (SSE)", "POST /annonce {app, titre, lignes[], etat{}}"]})
                else:
                    self._json(404, {"erreur": "route inconnue"})

            def do_POST(self):
                """`POST /annonce` : une appli maison déclare ce qu'elle affiche (JSON : app, titre, lignes[], etat{}).
                Le seul écrit du pont, et il ne commande rien : il décrit."""
                u = urllib.parse.urlparse(self.path)
                if u.path != "/annonce":
                    self._json(404, {"erreur": "route inconnue"})
                    return
                try:
                    n = min(int(self.headers.get("Content-Length", 0)), 65536)
                    d = json.loads(self.rfile.read(n).decode("utf-8"))
                    app, titre = _nettoyer(d.get("app"), 60), _nettoyer(d.get("titre"), 100)
                    lignes = [_nettoyer(l, 160) for l in (d.get("lignes") or [])][:40]
                    etat = d.get("etat") if isinstance(d.get("etat"), dict) else {}
                    if not app:
                        raise ValueError("app manquante")
                except (ValueError, UnicodeDecodeError) as e:
                    self._json(400, {"erreur": f"annonce invalide : {e}"[:120]})
                    return
                self._json(200, pont.annoncer(app, titre, lignes, etat))

            def flux(self, srcs):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                q = pont.bus.abonner()
                try:
                    self.wfile.write(b": pont veille\n\n")
                    self.wfile.flush()
                    while True:
                        try:
                            ligne = q.get(timeout=15)
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                            continue
                        if srcs and json.loads(ligne).get("src") not in srcs:
                            continue
                        self.wfile.write(f"data: {ligne}\n\n".encode("utf-8"))
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    pont.bus.desabonner(q)

            def flux_carte(self, motif, duree):
                """Abonnement avant snapshot : aucune fenêtre de perte entre les deux.
                Après débordement, le snapshot remplace entièrement l'état client.
                """
                q, initial = pont.carte.abonner(motif)
                fin = time.monotonic() + duree if duree is not None else None
                self.close_connection = True
                self.connection.settimeout(5)
                derniere = initial["revision"]
                retenues = {f["wid"] for f in initial["fenetres"]}

                def envoyer(nom, d):
                    identifiant = f"{d['session']}:{d['revision']}"
                    self.wfile.write((f"id: {identifiant}\nevent: {nom}\ndata: "
                                      + json.dumps(d, ensure_ascii=False) + "\n\n").encode("utf-8"))
                    self.wfile.flush()

                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    envoyer("instantane", initial)
                    while fin is None or time.monotonic() < fin:
                        attente = min(1.0, max(0.001, fin - time.monotonic())) if fin else 1.0
                        try:
                            d = q.get(timeout=attente)
                        except queue.Empty:
                            if fin is not None and time.monotonic() >= fin:
                                break
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                            continue
                        if d.get("type") == "resync":
                            pont.carte.desabonner(q)
                            q, initial = pont.carte.abonner(motif)
                            initial["resynchronise"] = True
                            retenues = {f["wid"] for f in initial["fenetres"]}
                            derniere = initial["revision"]
                            envoyer("instantane", initial)
                            continue
                        if d["revision"] <= derniere:
                            continue
                        wid = d.get("wid")
                        if motif and d["type"] == "fermeture" and wid not in retenues:
                            continue
                        if motif and d["type"] == "fenetre":
                            correspond = pont.carte.correspond(d["fenetre"], motif)
                            if not correspond and wid not in retenues:
                                continue
                            if not correspond:
                                d = dict(d, type="fermeture")
                                retenues.discard(wid)
                            elif wid not in retenues:
                                # Un renommage peut faire entrer une fenêtre dans le filtre.
                                # Resynchroniser inclut ses éléments, absents du seul delta de titre.
                                pont.carte.desabonner(q)
                                q, initial = pont.carte.abonner(motif)
                                initial["resynchronise"] = True
                                retenues = {f["wid"] for f in initial["fenetres"]}
                                derniere = initial["revision"]
                                envoyer("instantane", initial)
                                continue
                        if d["type"] == "fermeture":
                            retenues.discard(wid)
                        # La chaîne porte les révisions livrées, même en présence d'un filtre.
                        d = dict(d, precedent=derniere)
                        envoyer("changement", d)
                        derniere = d["revision"]
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    pont.carte.desabonner(q)

        self.serveur = http.server.ThreadingHTTPServer(("127.0.0.1", port), Requete)
        self.serveur.daemon_threads = True
        self.port = self.serveur.server_address[1]

    def annoncer(self, app, titre, lignes, etat):
        """Range l'annonce (par titre ET par app), publie un événement si le contenu change, met la carte à jour."""
        annonce = {"app": app, "titre": titre, "lignes": lignes, "etat": etat, "ts": time.strftime("%H:%M:%S")}
        annonce["observe_a"] = time.time()
        precedente = self.bus.annonces.get(titre or app)
        change = not precedente or precedente.get("lignes") != lignes or precedente.get("etat") != etat
        for cle in {app, titre} - {""}:
            self.bus.annonces[cle] = annonce
        if change:
            contenu = " / ".join(l for l in lignes if l)[:160]
            self.bus.emettre("annonce", "affiche", f"{app} affiche : {contenu}" + (f" — {json.dumps(etat, ensure_ascii=False)[:80]}" if etat else ""),
                             app=app, titre=titre, lignes=lignes, etat=etat)
            with self.bus.etat.verrou:
                for f in self.bus.etat.d["fenetres"].values():
                    if titre and f.get("titre") == titre:
                        f["deduction"] = "déclare : " + contenu
                        self.bus.etat.sale = True
            if self.carte:
                for w, (nom, _cls) in list(self.carte.fenetres.noms.items()) if self.carte.fenetres else []:
                    if titre and nom == titre:
                        self.carte._programmer(w)
        return {"ok": True, "change": change}

    def evenements(self, n, srcs):
        try:
            with open(self.bus.fichier, encoding="utf-8") as f:
                lignes = f.read().splitlines()
        except OSError:
            return []
        sortie = []
        for l in reversed(lignes):
            try:
                d = json.loads(l)
            except ValueError:
                continue
            if not srcs or d.get("src") in srcs:
                sortie.append(d)
                if len(sortie) >= n:
                    break
        return sortie[::-1]

    def run(self):
        self.serveur.serve_forever()


class LecteurTexte:
    """La fenêtre active en lignes AT-SPI (fonctions du pilote), exécutée DANS la boucle GLib : libatspi n'est
    pas sûre entre fils. Pas d'OCR, pas de capture, pas de réservation : deux secondes au plus."""

    def __init__(self, fenetres=None, delai=2.0):
        self.fenetres = fenetres
        self.delai = delai
        self.racines = {}
        self.couvertures = {}

    def _dans_la_boucle(self, fn, attente):
        from gi.repository import GLib
        fini, annule, res = threading.Event(), threading.Event(), {}

        def job():
            if annule.is_set():
                return False
            try:
                res["valeur"] = fn()
            except Exception as e:
                res["erreur"] = str(e)[:200]
            fini.set()
            return False

        GLib.idle_add(job)
        if not fini.wait(attente):
            annule.set()
            return {"erreur": f"délai dépassé ({attente:.0f} s)"}
        return res

    def __call__(self, tout=False):
        res = self._dans_la_boucle(lambda: self.lire(tout), self.delai + 3)
        return res["valeur"] if "valeur" in res else res

    def zone(self, wid, bbox):
        """Les lignes AT-SPI de la fenêtre X11 `wid` qui touchent `bbox` (écran) ; None = pas d'arbre."""
        res = self._dans_la_boucle(lambda: self.lire_zone(wid, bbox), 1.5)
        return res.get("valeur")

    def _parcourir(self, ecran, Atspi, fenetres, delai):
        largeur, hauteur = ecran.ecran_taille()
        elements, vus = [], set()
        for _ in range(2):                      # Chromium (Chrome, VS Code) ne donne son arbre qu'à la 2e interrogation
            deadline = time.monotonic() + delai
            for _, _, w, wid_w in fenetres:
                ecran.parcourir(Atspi, w, largeur, hauteur, deadline, elements, vus, wid_w)
            if elements:
                break
        return elements

    def lire(self, tout=False):
        import ecran
        Atspi = ecran.atspi()
        fenetres, (wid, nom, pid) = ecran.choisir_fenetres_atspi(Atspi, bool(tout), None)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        if tout:                                # tout l'écran « en mode texte », fenêtre par fenêtre
            sortie = []
            for nom_app, nom_w, w, wid_w in fenetres:
                els = self._parcourir(ecran, Atspi, [(nom_app, nom_w, w, wid_w)], min(self.delai, 1.0))
                sortie.append({"app": nom_app, "titre": nom_w, "active": bool(wid_w and str(wid_w) == str(wid)),
                               "elements": len(els), "lignes": [compacter(e) for e in els]})
            return {"ts": ts, "fenetre_active": nom, "fenetres": sortie}
        elements = self._parcourir(ecran, Atspi, fenetres, self.delai)
        return {"ts": ts, "fenetre": nom, "pid": pid, "applis_atspi": len(fenetres),
                "elements": len(elements), "lignes": [compacter(e) for e in elements]}

    def elements_fenetre(self, wid, delai=0.5):
        """Les éléments AT-SPI (avec x, y, l, h écran) de la fenêtre X11 `wid` ; None = pas d'arbre.
        Correspondance par pid (_NET_WM_PID) puis titre, sans xdotool : Chrome nomme ses cadres AT-SPI tard ou pas du
        tout, et marque « non affichés » ceux qui ne sont pas actifs ; on prend le cadre le plus plausible."""
        import ecran
        Atspi = ecran.atspi()
        wid = int(wid)
        self.racines.pop(wid, None)
        self.couvertures[wid] = {"tronquee": False}
        pid = (self.fenetres.pids.get(wid) if self.fenetres else None)
        titre = (self.fenetres.noms.get(wid, ("", ""))[0] if self.fenetres else "")
        if not pid:
            return None
        bureau = Atspi.get_desktop(0)
        cadres = []
        for i in range(bureau.get_child_count()):
            try:
                app = bureau.get_child_at_index(i)
                if app is None or app.get_process_id() != pid:
                    continue
                for j in range(app.get_child_count()):
                    w = app.get_child_at_index(j)
                    if w is None:
                        continue
                    nom_w, s = (w.get_name() or ""), w.get_state_set()
                    score = (nom_w == titre, s.contains(Atspi.StateType.ACTIVE), bool(nom_w) and nom_w in titre,
                             s.contains(Atspi.StateType.SHOWING), w.get_child_count() > 0)
                    cadres.append((score, j, w))
            except Exception:
                continue
        if not cadres:
            return None
        cadres.sort(key=lambda c: c[0], reverse=True)
        largeur, hauteur = ecran.ecran_taille()
        elements, vus = [], set()
        lisible = False
        for score, _, w in cadres[:2]:              # le meilleur cadre, puis le suivant si le premier est vide
            lisible = lisible or bool(score[3])     # un arbre visible peut légitimement n'avoir aucun contrôle
            for _ in range(2):                      # Chromium : arbre à la seconde interrogation
                _, coupe = ecran.parcourir(Atspi, w, largeur, hauteur, time.monotonic() + delai, elements, vus, str(wid))
                self.couvertures[wid]["tronquee"] |= coupe
                if elements:
                    self.racines[wid] = w
                    break
            if elements:
                break
        return elements if elements or (lisible and not self.couvertures[wid]["tronquee"]) else None

    def verifier_cible(self, wid, element):
        """Hit-test AT-SPI dans la boucle GLib ; aucun déplacement ni capture.
        Un toolkit qui ne répond pas au hit-test ne donne pas d'autorisation.
        """
        import ecran
        racine = self.racines.get(wid)
        if racine is None:
            return None
        try:
            Atspi = ecran.atspi()
            coord, dx, dy = ecran.repere(Atspi, racine, str(wid))
            x = int(element["x"] + element["w"] / 2 - dx)
            y = int(element["y"] + element["h"] / 2 - dy)
            courant, vus = racine, set()
            limite = time.monotonic() + 0.6
            for _ in range(30):
                if time.monotonic() > limite:
                    return None
                comp = courant.get_component_iface()
                suivant = comp.get_accessible_at_point(x, y, coord) if comp else None
                cle = ecran.identite_atspi(suivant) if suivant else None
                if not cle or cle in vus:
                    break
                vus.add(cle)
                courant = suivant
            for _ in range(30):
                if courant is None or time.monotonic() > limite:
                    return None
                if ecran.identite_atspi(courant) == element.get("cle_source"):
                    s = courant.get_state_set()
                    return bool(s.contains(Atspi.StateType.SHOWING) and s.contains(Atspi.StateType.VISIBLE)
                                and s.contains(Atspi.StateType.ENABLED))
                courant = courant.get_parent()
            return False
        except Exception:
            return None

    def elements_application(self, nom_app, delai=1.0):
        """Les éléments AT-SPI d'une application sans fenêtre X11 cliente (gnome-shell : barre du haut, dock,
        notifications) ; None si l'application est absente."""
        import ecran
        Atspi = ecran.atspi()
        bureau = Atspi.get_desktop(0)
        cadres = []
        for i in range(bureau.get_child_count()):
            try:
                app = bureau.get_child_at_index(i)
                if app is not None and (app.get_name() or "") == nom_app:
                    cadres += [app.get_child_at_index(j) for j in range(app.get_child_count())]
            except Exception:
                continue
        cadres = [w for w in cadres if w is not None]
        if not cadres:
            return None
        largeur, hauteur = ecran.ecran_taille()
        elements, vus = [], set()
        for w in cadres:
            ecran.parcourir(Atspi, w, largeur, hauteur, time.monotonic() + delai, elements, vus, None)
        return elements

    def lire_zone(self, wid, bbox):
        elements = self.elements_fenetre(wid, 0.5)
        if not elements:
            return None
        x1, y1, x2, y2 = bbox
        dans = [e for e in elements if e["x"] < x2 and e["x"] + e["w"] > x1 and e["y"] < y2 and e["y"] + e["h"] > y1]
        return [compacter(e) for e in dans]


# ───────────── la carte : ce qu'il y a sur l'écran, où, et ce qu'on en déduit ─────────────
def deduire(app, titre, els):
    """Ce qu'on peut dire d'une fenêtre sans la regarder : genre, ce qu'elle attend, où est le focus."""
    a, t = (app or "").lower(), (titre or "").lower()
    roles = Counter(e.get("role_atspi") for e in els or [])
    if not els:
        genre = "sans arbre lisible (Steam, jeu, vidéo, ou Chrome/Electron en arrière-plan)"
    elif "chrome" in a or "firefox" in a or "chromium" in a or roles.get("document web"):
        genre = "navigateur"
    elif a.startswith("code") or "vscode" in a:
        genre = "éditeur de code"
    elif "terminal" in a or roles.get("terminal"):
        genre = "terminal"
    elif "nautilus" in a:
        genre = "gestionnaire de fichiers"
    elif "steam" in a:
        genre = "Steam"
    elif "mailspring" in a or "mail" in t or "thunderbird" in a:
        genre = "courrier"
    elif "whatsapp" in a or "telegram" in a or "signal" in a:
        genre = "messagerie"
    elif "tk" == a or "charli" in a:
        genre = "application maison"
    else:
        genre = "application"
    focus = next((e for e in els or [] if "focus" in (e.get("etats") or [])), None)
    dialogues = [e for e in els or [] if e.get("role_atspi") in ("dialog", "alert") and (e.get("nom") or e.get("texte"))]
    attend = None
    if dialogues:
        attend = f"une réponse : « {dialogues[0].get('nom') or dialogues[0].get('texte')} »"
    elif focus and focus.get("role") == "champ":
        attend = f"une saisie dans « {focus.get('nom') or 'champ'} »" + (f" (contient « {focus['texte'][:40]} »)" if focus.get("texte") else "")
    boutons = [e["nom"] for e in els or [] if e.get("role") == "bouton" and e.get("nom")][:6]
    champs = sum(1 for e in els or [] if e.get("role") == "champ")
    resume = f"{genre}, {len(els or [])} éléments" + (f", {champs} champ{'s' if champs > 1 else ''}" if champs else "") \
        + (f", boutons : {', '.join(boutons)}" if boutons else "") + (f" ; attend {attend}" if attend else "")
    return {"genre": genre, "attend": attend, "focus": compacter(focus) if focus else None, "resume": resume}


class Carte:
    """Carte versionnée sans pixels : snapshot atomique, puis deltas d'éléments.

    Une identité native suit l'objet exposé par l'application. Sans identité native,
    le rapprochement est explicitement estimé et ne permet pas de clic par ID.
    Les coordonnées ne constituent jamais une preuve de visibilité effective.
    """
    SHELL_PERIODE = 15.0
    SHELL = "gnome-shell"
    PERIODE_ACTIVE = 1.0
    PERIODE_FOND = 15.0
    AGE_MAX = 30.0
    TAILLE_FILE = 128

    def __init__(self, bus, lecteur, fenetres, devtools=None):
        self.bus, self.lecteur, self.fenetres, self.devtools = bus, lecteur, fenetres, devtools
        self.verrou = threading.RLock()
        self.fen, self.a_rafraichir = {}, {}
        self.amorcee = False
        self.essais = Counter()
        self.generations = Counter()
        self.session = uuid.uuid4().hex
        self.revision = 0
        self.numero = 0
        self.abonnes = []
        self.derniers_essais = {}
        self.contexte = self._contexte()

    def _contexte(self):
        fen = self.fenetres
        return {"fenetre_active": hex(getattr(fen, "actif", 0) or 0),
                "pointeur": getattr(fen, "pointeur", None),
                "empilement": [hex(w) for w in list(getattr(fen, "empilement", []))],
                "visibles": {hex(w): v for w, v in list(getattr(fen, "visibles", {}).items())},
                "focus": self.bus.etat.instantane().get("focus"),
                "pilote": self.bus.etat.instantane().get("pilote")}

    def _publier(self, **d):
        # Appelé sous verrou : les révisions et le snapshot initial sont atomiques.
        precedent = self.revision
        self.revision += 1
        d.update(session=self.session, revision=self.revision, precedent=precedent,
                 ts=time.time())
        for q in list(self.abonnes):
            try:
                q.put_nowait(d)
            except queue.Full:
                while True:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break
                q.put_nowait({"type": "resync"})

    def actualiser_contexte(self):
        contexte = self._contexte()
        with self.verrou:
            if contexte != self.contexte:
                self.contexte = contexte
                # L'activité appartient aussi aux métadonnées des fenêtres.
                for w, f in list(self.fen.items()):
                    active = f["wid"] == contexte["fenetre_active"]
                    visible = contexte["visibles"].get(f["wid"])
                    if f.get("active") != active or f.get("visible") != visible:
                        self._enregistrer(w, dict(f, active=active, visible=visible))
                self._publier(type="contexte", contexte=contexte)

    def _programmer(self, w):
        with self.verrou:
            self.generations[w] += 1
            # Ne pas repousser indéfiniment la lecture d'une fenêtre animée.
            self.a_rafraichir.setdefault(w, time.monotonic())
            ancien = self.fen.get(w)
            if ancien and ancien.get("fraicheur", {}).get("etat") == "observe":
                f = dict(ancien, fraicheur=dict(ancien["fraicheur"], etat="a_relire"))
                self._enregistrer(w, f)

    def sur_evenement(self, e):
        src, ev = e.get("src"), e.get("ev")
        if src in ("fenetre", "pilote"):
            self.actualiser_contexte()
        if src == "atspi":
            self.actualiser_contexte()
            if ev in ("modifie", "focus", "texte", "page", "alerte", "repete", "structure", "valeur"):
                for w, (titre, app) in list(getattr(self.fenetres, "noms", {}).items()):
                    pid = getattr(self.fenetres, "pids", {}).get(w)
                    # Un événement sans pid concerne souvent l'application active.
                    meme = e.get("pid") and pid == e["pid"]
                    nom = (e.get("app") or "").lower()
                    if meme or (nom and app and (nom in app.lower() or app.lower() in nom)) or (not e.get("pid") and w == self.fenetres.actif):
                        self._programmer(w)
            return
        wid = e.get("wid")
        if not wid:
            return
        try:
            w = int(wid, 16)
        except (TypeError, ValueError):
            return
        if src == "fenetre" and ev == "fermee":
            with self.verrou:
                ancienne = self.fen.pop(w, None)
                self.a_rafraichir.pop(w, None)
                self.derniers_essais.pop(w, None)
                self.generations[w] += 1
                if ancienne:
                    self._publier(type="fermeture", wid=wid, fenetre={k: v for k, v in ancienne.items() if k != "elements"})
        elif ((src == "fenetre" and ev in ("ouverte", "titre", "geometrie", "activee"))
              or (src == "dessin" and ev in ("debut", "zone", "arret", "continu", "repete"))):
            self._programmer(w)

    def sur_devtools(self, e):
        if e.get("src") != "devtools" or e.get("ev") not in ("chargee", "navigation", "page"):
            return
        titre = (e.get("titre") or "").strip()
        if titre and self.fenetres:
            for w, (nom, _cls) in list(self.fenetres.noms.items()):
                if titre in (nom or ""):
                    self._programmer(w)

    def tic(self):
        self.actualiser_contexte()
        now = time.monotonic()
        with self.verrou:
            for w in list(getattr(self.fenetres, "noms", {})):
                periode = self.PERIODE_ACTIVE if w == self.fenetres.actif else self.PERIODE_FOND
                if now - self.derniers_essais.get(w, 0) >= periode:
                    self.a_rafraichir.setdefault(w, now - 0.11)
            # La seconde place laisse progresser l'arrière-plan même si l'active change sans cesse.
            prets = sorted((w for w, t in self.a_rafraichir.items() if now - t >= 0.1),
                           key=lambda w: (w != self.fenetres.actif, self.derniers_essais.get(w, 0)))[:2]
        for w in prets:
            self.rafraichir(w)
        if now - getattr(self, "shell_lu", 0.0) >= self.SHELL_PERIODE and self.lecteur:
            self.shell_lu = now
            self.rafraichir_shell()
        return True

    def _elements(self, w, elements, ancienne, source):
        precedents = (ancienne or {}).get("elements", [])
        natifs = {e.get("cle_source"): e for e in precedents if e.get("cle_source")}
        utilises, resultat = set(), []
        for brut in elements or []:
            e = {k: v for k, v in brut.items() if k not in ("id", "wid")}
            cle = e.get("cle_source")
            native = bool(cle) and ":structure:" not in cle and e.get("identite") != "estimee"
            avant = natifs.get(cle) if cle else None
            if not cle:
                # Repli conservateur : jamais annoncé comme identité certaine.
                candidats = [p for p in precedents if p["id"] not in utilises and not p.get("cle_source")
                             and p.get("role") == e.get("role")]
                if len(candidats) == 1:
                    avant = candidats[0]
            if avant and avant["id"] not in utilises:
                e["id"] = avant["id"]
            else:
                self.numero += 1
                e["id"] = f"{self.session[:12]}:{w}:e{self.numero}"
            utilises.add(e["id"])
            e["identite"] = "native" if native else "estimee"
            e.setdefault("source", source or "inconnue")
            e.setdefault("visible_source", True)
            e["occlusion"] = "non_evaluee"
            resultat.append(e)
        return resultat

    def _enregistrer(self, w, entree):
        ancienne = self.fen.get(w)
        self.fen[w] = entree
        avant = {e["id"]: e for e in (ancienne or {}).get("elements", [])}
        apres = {e["id"]: e for e in entree.get("elements", [])}
        meta = {k: v for k, v in entree.items() if k != "elements"}
        meta_avant = {k: v for k, v in (ancienne or {}).items() if k != "elements"}
        ordre = [e["id"] for e in entree.get("elements", [])]
        ajoutes = [e for i, e in apres.items() if i not in avant]
        modifies = [e for i, e in apres.items() if i in avant and e != avant[i]]
        retires = [i for i in avant if i not in apres]
        if ajoutes or modifies or retires or meta != meta_avant or ordre != [e["id"] for e in (ancienne or {}).get("elements", [])]:
            self._publier(type="fenetre", wid=entree["wid"], fenetre=meta,
                          ajoutes=ajoutes, modifies=modifies, retires=retires, ordre=ordre)

    def rafraichir_shell(self):
        try:
            els = self.lecteur.elements_application(self.SHELL, 0.4)
        except Exception:
            els = None
        if els is None:
            return
        with self.verrou:
            ancienne = self.fen.get("shell")
            entree = {"wid": "shell", "app": self.SHELL, "titre": "barre du haut, dock, notifications", "x": 0, "y": 0,
                      "l": None, "h": None, "source": "atspi", "arbre": True, "active": False, "visible": None,
                      "elements": self._elements("shell", els, ancienne, "atspi"),
                      "deduction": {"genre": "interface GNOME", "attend": None, "focus": None,
                                    "resume": f"interface GNOME, {len(els)} éléments (barre du haut, dock)"},
                      "ts": time.strftime("%H:%M:%S"), "perime": False,
                      "fraicheur": {"etat": "observe", "observe_a": time.time()},
                      "couverture": {"etat": "partielle", "raison": "éléments exposés par AT-SPI ; images non interprétées"}}
            self._enregistrer("shell", entree)

    def rafraichir(self, w):
        with self.verrou:
            generation = self.generations[w]
            ancienne = self.fen.get(w)
        noms = getattr(self.fenetres, "noms", {})
        if w not in noms:
            with self.verrou:
                self.a_rafraichir.pop(w, None)
            return
        nom, cls = noms[w]
        if _cadre(nom):
            with self.verrou:
                self.a_rafraichir.pop(w, None)
            return
        t0 = time.monotonic()
        observe_a = time.time()
        self.derniers_essais[w] = t0
        try:
            els = self.lecteur.elements_fenetre(w, 0.35) if self.lecteur else None
        except Exception:
            els = None
        geo = getattr(self.fenetres, "geometries", {}).get(w) or (None, None, None, None)
        source = "atspi" if els is not None else None
        couverture = {"etat": "partielle", "raison": "éléments exposés ; images et occlusion non interprétées"}
        couverture["atspi"] = dict(getattr(self.lecteur, "couvertures", {}).get(w, {}))
        page = None
        if self.devtools and (not els or self.devtools.page_pour(nom)):
            try:
                page = self.devtools.lire(nom, geo, delai=0.7)
            except Exception:
                page = None
            if page is not None and "elements" in page:
                # AT-SPI reste la source des contrôles natifs du navigateur.
                natifs = [e for e in (els or []) if (e.get("y") or 0) < (geo[1] or 0) + 140]
                els = natifs + page["elements"]
                source = "devtools" + ("+atspi" if natifs else "")
                couverture["devtools"] = page.get("couverture", {})
                couverture["tronquee"] = page.get("tronquee", False)
        annonce = self.bus.annonces.get(nom) or self.bus.annonces.get(cls)
        with self.verrou:
            # Une fenêtre peut se fermer pendant un appel distant.
            if w not in getattr(self.fenetres, "noms", {}):
                return
            if els is None and ancienne and ancienne.get("elements"):
                entree = dict(ancienne, titre=nom, app=cls, x=geo[0], y=geo[1], l=geo[2], h=geo[3], perime=True)
                entree["fraicheur"] = dict(ancienne.get("fraicheur", {}), etat="perime")
                entree["deduction"] = dict(ancienne["deduction"], resume=ancienne["deduction"]["resume"].split(" (lu à ")[0]
                                            + f" (lu à {ancienne.get('lu') or ancienne['ts']}, en arrière-plan)")
                entree["lu"] = ancienne.get("lu") or ancienne["ts"]
                # Les anciennes coordonnées absolues restent périmées, jamais translatées comme des faits.
            else:
                entree = {"wid": hex(w), "app": cls, "titre": nom, "x": geo[0], "y": geo[1], "l": geo[2], "h": geo[3],
                          "elements": self._elements(w, els, ancienne, source), "arbre": els is not None, "source": source,
                          "deduction": deduire(cls, nom, els), "ts": time.strftime("%H:%M:%S"),
                          "lecture_ms": int((time.monotonic() - t0) * 1000), "perime": False,
                          "fraicheur": {"etat": "observe" if els is not None else "indisponible",
                                        "observe_a": observe_a if els is not None else None},
                          "couverture": couverture if els is not None else {"etat": "indisponible", "raison": "contenu non décrit par les sources disponibles"}}
            if annonce:
                entree["annonce"] = annonce
                if not entree.get("elements"):
                    entree["couverture"] = {"etat": "declaration", "raison": "texte déclaré par l'application, sans positions des contrôles"}
                    entree["deduction"] = dict(entree["deduction"], resume=f"{entree['deduction']['genre']} ; déclare : "
                                               + " / ".join(l for l in annonce.get("lignes", []) if l)[:160])
            entree["active"] = w == getattr(self.fenetres, "actif", 0)
            entree["visible"] = getattr(self.fenetres, "visibles", {}).get(w)
            if self.generations[w] == generation:
                self.a_rafraichir.pop(w, None)
            elif entree["fraicheur"]["etat"] == "observe":
                entree["fraicheur"] = dict(entree["fraicheur"], etat="a_relire")
            self._enregistrer(w, entree)
        with self.bus.etat.verrou:
            f = self.bus.etat.d["fenetres"].get(hex(w))
            if f is not None:
                f["deduction"] = entree["deduction"]["resume"]
                self.bus.etat.sale = True

    @staticmethod
    def correspond(f, motif):
        m = (motif or "").lower()
        return not m or m in (f.get("titre") or "").lower() or m in (f.get("app") or "").lower()

    def instantane(self, motif=None):
        with self.verrou:
            fen = json.loads(json.dumps([f for f in self.fen.values() if self.correspond(f, motif)]))
            now = time.time()
            for f in fen:
                observe = f.get("fraicheur", {}).get("observe_a")
                f["age_ms"] = max(0, int((now - observe) * 1000)) if observe else None
                if observe and now - observe > self.AGE_MAX:
                    f["perime"] = True
                    f["fraicheur"]["etat"] = "perime"
            fen.sort(key=lambda f: (not f.get("active"), f["wid"] == "shell", f.get("app") or ""))
            return {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "session": self.session, "revision": self.revision,
                    "fenetres": fen, "a_rafraichir": len(self.a_rafraichir),
                    "contexte": json.loads(json.dumps(self.contexte)), "sans_capture": True}

    def abonner(self, motif=None):
        with self.verrou:
            q = queue.Queue(maxsize=self.TAILLE_FILE)
            self.abonnes.append(q)
            return q, self.instantane(motif)

    def desabonner(self, q):
        with self.verrou:
            if q in self.abonnes:
                self.abonnes.remove(q)

    def cible(self, identifiant):
        """Relit la fenêtre dans GLib, puis résout une cible native active et fraîche.
        Aucun clic : le pilote garde seul les actions et leurs garde-fous.
        """
        with self.verrou:
            candidat = next(((w, f) for w, f in self.fen.items()
                              if any(e["id"] == identifiant for e in f.get("elements", []))), None)
        if not candidat or candidat[0] == "shell":
            return 409, {"erreur": "cible absente ou sans fenêtre contrôlable"}
        w, f = candidat
        attendu = next(e for e in f["elements"] if e["id"] == identifiant)
        if f.get("perime") or w != getattr(self.fenetres, "actif", 0):
            return 409, {"erreur": "cible périmée ou fenêtre inactive"}
        if self.lecteur and hasattr(self.lecteur, "_dans_la_boucle"):
            res = self.lecteur._dans_la_boucle(lambda: self.rafraichir(w), 3.0)
            if "erreur" in res:
                return 409, {"erreur": "relecture de la cible indisponible"}
        self.actualiser_contexte()
        with self.verrou:
            f = self.fen.get(w)
            e = next((e for e in (f or {}).get("elements", []) if e["id"] == identifiant), None)
            observe = (f or {}).get("fraicheur", {}).get("observe_a")
            if (not f or not e or f.get("perime") or f.get("visible") is False
                    or w != getattr(self.fenetres, "actif", 0) or f.get("fraicheur", {}).get("etat") != "observe"
                    or not observe or time.time() - observe > 2.0):
                return 409, {"erreur": "cible non confirmée par une lecture récente"}
            if any(e.get(k) != attendu.get(k) for k in ("cle_source", "role", "nom", "texte", "etats")):
                return 409, {"erreur": "contenu de la cible changé depuis la carte ; relis-la avant d'agir"}
            if e.get("identite") != "native" or "inactif" in (e.get("etats") or []) or e.get("visible_source") is False:
                return 409, {"erreur": "identité estimée ou élément désactivé"}
            if not all(isinstance(e.get(k), (int, float)) for k in ("x", "y", "w", "h")) or e["w"] <= 0 or e["h"] <= 0:
                return 409, {"erreur": "coordonnées indisponibles"}
            x, y = e["x"] + e["w"] / 2, e["y"] + e["h"] / 2
            geo = [f.get(k) for k in ("x", "y", "l", "h")]
            if (not all(isinstance(v, (int, float)) for v in geo)
                    or not (geo[0] <= x < geo[0] + geo[2] and geo[1] <= y < geo[1] + geo[3])
                    or x < 0 or y < 0 or x >= getattr(self.fenetres, "largeur", float("inf"))
                    or y >= getattr(self.fenetres, "hauteur", float("inf"))):
                return 409, {"erreur": "centre de la cible hors fenêtre ou écran"}
            stack = list(getattr(self.fenetres, "empilement", []))
            for autre in stack[stack.index(w) + 1:] if w in stack else []:
                if getattr(self.fenetres, "visibles", {}).get(autre) is False:
                    continue
                geo = getattr(self.fenetres, "geometries", {}).get(autre)
                if geo and geo[0] <= x < geo[0] + geo[2] and geo[1] <= y < geo[1] + geo[3]:
                    return 409, {"erreur": "centre de la cible potentiellement recouvert"}
            generation = self.generations[w]
            element = json.loads(json.dumps(e))
            geometrie = tuple(f.get(k) for k in ("x", "y", "l", "h"))
            titre = f["titre"]
        # Aucun verrou Carte pendant un aller-retour GLib : la boucle peut la mettre à jour.
        confirme = None
        if element.get("source") == "devtools" and self.devtools:
            try:
                confirme = self.devtools.verifier_cible(titre, geometrie, element)
            except Exception:
                pass
        elif self.lecteur and hasattr(self.lecteur, "verifier_cible"):
            job = lambda: self.lecteur.verifier_cible(w, element)
            if hasattr(self.lecteur, "_dans_la_boucle"):
                confirme = self.lecteur._dans_la_boucle(job, 2.0).get("valeur")
            else:
                confirme = job()
        if confirme is not True:
            return 409, {"erreur": "visibilité de la cible non confirmée au point de clic"}
        with self.verrou:
            f = self.fen.get(w)
            actuel = next((a for a in (f or {}).get("elements", []) if a["id"] == identifiant), None)
            if (actuel != element or self.generations[w] != generation or w != getattr(self.fenetres, "actif", 0)
                    or (f or {}).get("perime") or (f or {}).get("fraicheur", {}).get("etat") != "observe"):
                return 409, {"erreur": "cible modifiée pendant la vérification"}
            return 200, json.loads(json.dumps({"element": actuel, "fenetre": {k: v for k, v in f.items() if k != "elements"},
                                               "session": self.session, "revision": self.revision}))


# ───────────────────────── service ─────────────────────────
def garde(fn):
    def tic():
        try:
            return fn() is not False
        except Exception:
            return True
    return tic


def activer_accessibilite(bus):
    """Même règle qu'`ecran` : GTK/Qt/Electron n'émettent rien tant que le bus ne le demande pas ;
    jamais `screen-reader-enabled` (Orca) ; rien d'écrit sous STOP."""
    if os.path.exists(chemin("stop")):
        return
    r = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "toolkit-accessibility"],
                       capture_output=True, text=True)
    if r.stdout.strip() == "false":
        subprocess.run(["gsettings", "set", "org.gnome.desktop.interface", "toolkit-accessibility", "true"], capture_output=True)
        bus.emettre("veille", "accessibilite", "accessibilité activée (toolkit-accessibility) : les applis déjà ouvertes "
                    "n'émettront qu'après redémarrage")


def adresse_bus_a11y():
    """libatspi lit d'abord la propriété X `AT_SPI_BUS`, souvent périmée sur ce poste (socket disparu, un
    avertissement par démarrage) : on demande l'adresse vivante au lanceur D-Bus et on la lui impose."""
    try:
        r = subprocess.run(["dbus-send", "--session", "--print-reply", "--dest=org.a11y.Bus", "/org/a11y/bus",
                            "org.a11y.Bus.GetAddress"], capture_output=True, text=True, timeout=5)
        m = re.search(r'string "([^"]+)"', r.stdout)
        return m.group(1) if m else None
    except Exception:
        return None


def main():
    os.makedirs(commun.ETAT, exist_ok=True)
    bus = Bus()
    bus.emettre("veille", "demarrage", f"veille démarrée (pid {os.getpid()}) : fenêtres, AT-SPI, processus, GPU, audio, pilote, écran")
    adresse = adresse_bus_a11y()
    if adresse and not os.environ.get("AT_SPI_BUS_ADDRESS"):
        os.environ["AT_SPI_BUS_ADDRESS"] = adresse
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import GLib
    activer_accessibilite(bus)
    fen = Fenetres(bus)
    fen.start()
    Audio(bus).start()
    lecteur = LecteurTexte(fen)
    dessin = Dessin(bus, fen, lecteur.zone)
    dessin.start()
    proc, gpu, pil, ecr = Processus(bus), Gpu(bus), Pilote(bus), Ecran(bus, fen, dessin)
    GLib.timeout_add_seconds(PERIODE_PROC, garde(proc.tic))
    GLib.timeout_add_seconds(PERIODE_GPU, garde(gpu.tic))
    GLib.timeout_add_seconds(PERIODE_PILOTE, garde(pil.tic))
    GLib.timeout_add_seconds(PERIODE_ECRAN, garde(ecr.tic))
    acc = Accessibilite(bus)
    try:
        acc.demarrer()
    except Exception as e:
        bus.emettre("atspi", "erreur", f"AT-SPI indisponible ({e})"[:160])

    def ecrire_etat():
        if bus.etat.sale:
            bus.etat.sale = False
            commun.ecrire(FICHIER_ETAT, json.dumps(bus.etat.instantane(), ensure_ascii=False))
        return True

    GLib.timeout_add_seconds(1, garde(ecrire_etat))
    oeil = Oeil(bus) if IMAGES_AUTORISEES else None
    if oeil:
        bus.declencheurs.append(oeil.sur_evenement)
        GLib.timeout_add_seconds(1, garde(oeil.tic))
    devtools = None
    try:
        import cdp
        devtools = cdp.Devtools(bus)
        devtools.start()
    except Exception as e:
        bus.emettre("devtools", "erreur", f"DevTools : prise indisponible ({e})"[:160])
    carte = Carte(bus, lecteur, fen, devtools)
    bus.declencheurs.append(carte.sur_evenement)
    bus.declencheurs.append(carte.sur_devtools)
    GLib.timeout_add(100, garde(carte.tic))
    pont, erreur = None, None
    for essai in range(10):                   # au redémarrage, l'ancien processus peut tenir le port une seconde
        try:
            pont = Pont(bus, PORT, lecteur, oeil, carte)
            break
        except OSError as e:
            erreur = e
            time.sleep(1)
    if pont:
        pont.start()
        bus.emettre("veille", "pont", f"pont HTTP ouvert sur 127.0.0.1:{pont.port} (/etat /carte /texte /oeil /evenements /flux), lecture seule")
    else:
        bus.emettre("veille", "erreur", f"pont HTTP impossible sur le port {PORT} : {erreur}"[:160])
    boucle = GLib.MainLoop()

    def arret(*_):
        bus.emettre("veille", "arret", "veille arrêtée")
        boucle.quit()
        return False

    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, arret)
    boucle.run()


if __name__ == "__main__":
    main()
