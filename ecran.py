#!/usr/bin/python3
"""ecran — pilotage plein écran de JARVIS par Claude (08/09/2026).

Boucle « computer use » locale : capture d'écran + arbre d'accessibilité
(AT-SPI) numéroté → clic / frappe / touches par xdotool sur X11 (:0).
Pendant le pilotage, un cadre orange (cadre.py) entoure l'écran ; si la
souris bouge sans nous, tout s'arrête (fichier `stop`, cf. `reprendre`).

    ecran regard [--fenetre MOTIF] [--element eN] [--tout] [--lignes N] [--lecteur NOM] [--sans-memoire] [--json]
      (07/11/2026 : l'écran lu SANS capture par le service `regard` — scène, ce qui a changé depuis TON dernier regard,
       fenêtre active en plan avec références eN ; `ecran cliquer e12` clique une référence après vérification fraîche)
    ecran regard --attendre "texte" | --disparu "texte" [--fenetre MOTIF] [--max S] [--pas MS]   (sans OCR ; exit 2 au délai)
    ecran regard --oeil [--fenetre MOTIF]   (fenêtre muette : capture + OCR + Qwen local → éléments positionnés eN, puis la page)
    ecran lire e42            (le texte COMPLET d'une référence : terminal, document, mail, champ — sans image)
    ecran voir [--rapide] [--zone X Y L H] [--plein] [--tout] [--fenetre MOTIF] [--sans-marques] [--sans-ocr]
    ecran texte [--zone X Y L H | --fenetre-active] [--conf N] [--json]
    ecran chercher "texte" ["texte2"…] [--zone X Y L H] [--exact] [--json]
    ecran cliquer <id> | id:<ID_CARTE> | tN | @"texte" [--n K] [--zone X Y L H] | <x> <y>  [--double] [--droit] [--milieu] [--img]
    ecran taper "texte" [--coller] [--stdin] [--dans @"champ" | --dans FENÊTRE] [--entree]
    ecran touche ctrl+l Tab Return [--dans FENÊTRE]
    ecran captcha X Y L H [--message "…"]     (découpe le code à l'écran et l'envoie à Félix par ntfy ; il répond dans la conversation)
    ecran defiler bas|haut [N] [--a X Y | tN | @"texte"]    ecran souris X Y | tN | @"texte"
      (cliquer/taper/touche/defiler : [--verifier "texte"] [--attendre S] [--sans-capture] ; cliquer : [--avec ctrl|shift])
      (cliquer/souris : [--lent], trajet progressif d'environ 2 s)
    ecran attendre S | --texte "…" | --disparu "…" | --stable S  [--zone …] [--max S] [--pas MS]
    ecran diff [--texte] [--zone X Y L H] [--json]
    ecran decrire [--zone X Y L H | --fenetre-active] [--question "…"] [--reflechir]   (Qwen local, jamais de coordonnées)
    ecran lire <id> | tN | --zone X Y L H | @"ancre" [--autour LxH]
    ecran fenetres | fenetre MOTIF|0xID [--deplacer DX DY]        ecran lancer CMD [ARGS…]
    ecran prendre [NOM] | rendre               ecran etat | stop | reprendre
    ecran veille [N] [--src fenetre,atspi,proc,gpu,audio,pilote,ecran] [--suivre [S]] [--etat | --texte | --carte [--fenetre MOTIF]] [--json]
      (l'écran en événements, sans image ; --etat = état instantané, --texte = fenêtre active en lignes AT-SPI)
      (--carte --suivre : carte initiale puis changements ; cliquer id: utilise cette carte sans capture)

Coordonnées : toujours en pixels ÉCRAN (2560×1440), sauf `--img`
(coordonnées relevées sur la capture réduite, converties par l'échelle).

Texte d'abord (26/09/2026) : `texte`, `chercher` et la couche OCR de `voir`
lisent la capture PLEINE RÉSOLUTION de n'importe quelle appli (tesseract),
avec ou sans arbre AT-SPI. Les lignes lues portent un id `tN` cliquable ;
`cliquer @"Nouveau passage"` cherche le texte à l'instant puis clique.

Chaque action (cliquer, taper, touche, defiler) rend un VERDICT : capture
avant, écran stabilisé (≤ 1,5 s), « changement 3,2 % — zone … » ou « aucun
changement visible ». C'est un indice ; `--verifier "texte"` relit le résultat.

Options : --profil affiche les durées (phases inclusives) sur stderr.
--voir sur cliquer/taper/touche/defiler rend aussi l’image après action, sans OCR supplémentaire.

Sortie : `voir`, `texte`, `chercher`, `lire`, `diff` commencent par une ligne `ETAT`.
Codes : 0 ok · 1 erreur/arguments · 2 introuvable ou ambigu · 3 STOP · 4 écran
réservé par un autre agent.

Une seule commande `ecran` à la fois (verrou). Pendant l'approche lente,
STOP et position sont vérifiés à chaque pas ; la vigie surveille les gestes physiques.
"""
import fcntl
import functools
from contextlib import nullcontext
import re
import json
import os
import shutil
import signal
import subprocess
import sys
import time

import commun
from commun import (AGE_OCCUPE, SEUIL, chemin, ecrire, effacer,  # noqa: F401
                    occupe_valide, pid_vivant)

# Python SYSTÈME imposé : `gi` (AT-SPI) et PIL n'existent que là. Un agent
# qui appelle `ecran` depuis un venv (Hermès pour Charli) tombait sinon sur
# « No module named gi » — constaté le 08/09 au premier pilotage de Charli.
PYTHON_SYS = "/usr/bin/python3"

DISPLAY = commun.affichage()   # pas os.environ tel quel : un DISPLAY hérité peut viser une session morte
XAUTH = os.environ.get("XAUTHORITY") or (
    f"/run/user/{os.getuid()}/gdm/Xauthority"
    if os.path.exists(f"/run/user/{os.getuid()}/gdm/Xauthority")
    else os.path.expanduser("~/.Xauthority"))
ETAT = commun.ETAT        # PILOTE_ETAT sinon /run/user/<uid>/pilote-ecran
ICI = os.path.dirname(os.path.realpath(__file__))  # realpath : ecran est un lien dans ~/.local/bin
LANCEUR = os.path.join(ICI, "ecran")
CADRE_UNITE = "pilote-ecran-cadre"
PORT_VEILLE = 8792         # pont HTTP local de veille.py (lecture seule) : /etat /texte /evenements /flux
PORT_REGARD = int(os.environ.get("PILOTE_REGARD_PORT") or 8793)   # service `regard` (Rust) : /regard /carte /carte/cible
PP_UNITE = "pilote-ecran-presse-papiers"   # xclip qui survit à `ecran`
PYTHON_CADRE = os.path.expanduser("~/jarvis/charli-app/venv/bin/python")  # PySide6
SEUIL_VIEUX = 60           # s : au-delà, la liste d'éléments est réputée périmée
LARGEUR_CAPTURE = 1280     # capture réduite par défaut (échelle 0,5 en 2560)
DELAI_ATSPI = 4.0          # s max passées à parcourir l'arbre d'accessibilité
DELAI_SH = 20              # s max pour un sous-processus (xdotool, maim, wmctrl…)
MAX_ELEMENTS = 400
MAX_DEFILEMENT = 50        # crans de molette par commande
from commun import EXPIRATION_PILOTE  # noqa: E402  (30 min : le cadre et la réservation vivent ensemble)
MAX_LIGNES_VOIR = 150      # lignes OCR affichées par `voir` (toutes restent dans texte.json)
# Codes de sortie (les autres erreurs sortent en 1).
INTROUVABLE, ARRETE, RESERVE = 2, 3, 4
# Le cadre écrit son étiquette à l'écran : l'OCR ne doit pas la prendre pour
# du contenu (formes normalisées, cf. perception.normaliser).
SIGNATURES_CADRE = ("pilote l ecran", "bouge la souris pour l arreter",
                    "tu as bouge la souris")

# Rôles AT-SPI retenus (anglais libatspi → libellé court affiché).
ROLES = {
    "push button": "bouton", "toggle button": "bouton", "button": "bouton",
    "check box": "case", "radio button": "radio", "switch": "interrupteur",
    "menu": "menu", "menu item": "menu", "check menu item": "menu",
    "radio menu item": "menu", "menu bar": "barre-menu",
    "combo box": "liste", "list box": "liste", "list": "liste",
    "list item": "élément", "tree item": "élément", "table cell": "cellule",
    "text": "champ", "entry": "champ", "password text": "champ",
    "editbar": "champ", "search box": "champ",
    "link": "lien", "page tab": "onglet", "tab": "onglet",
    "slider": "curseur", "spin button": "compteur", "scroll bar": "ascenseur",
    "label": "texte", "heading": "titre", "static": "texte",
    "image": "image", "icon": "icône", "tool bar": "barre-outils",
    "dialog": "dialogue", "alert": "alerte", "notification": "notif",
    "terminal": "terminal",      # 09/10 : la zone d'un terminal GNOME, avec son texte (invite, dernières lignes)
}
# Rôles conteneurs : jamais listés, mais toujours parcourus.
ROLES_TEXTE = {"text", "entry", "password text", "editbar", "search box", "terminal"}


# Profil optionnel, uniquement durées/compteurs ; jamais le contenu des arguments.
PROFIL = None


def mesurer(phase):
    def decorer(fn):
        @functools.wraps(fn)
        def appel(*args, **kwargs):
            if PROFIL is None:
                return fn(*args, **kwargs)
            categorie = phase
            if phase == "sous_processus" and len(args) > 1 and args[0] == "xdotool" and args[1] in {
                "mousemove", "click", "key", "keydown", "keyup", "type", "windowactivate", "windowraise"}:
                categorie = "action_x11"
            debut = time.monotonic()
            try:
                return fn(*args, **kwargs)
            finally:
                valeur = PROFIL.setdefault(categorie, {"secondes": 0.0, "appels": 0})
                valeur["secondes"] += time.monotonic() - debut
                valeur["appels"] += 1
        return appel
    return decorer


def executer_commande(cmd, args):
    global PROFIL
    actif = "--profil" in args
    args = [a for a in args if a != "--profil"]
    PROFIL = {} if actif else None
    debut = time.monotonic()
    try:
        COMMANDES[cmd](args)
    finally:
        if actif:
            print("PROFIL " + json.dumps({"total_s": time.monotonic() - debut,
                                          "phases_inclusives": PROFIL}), file=sys.stderr)
        PROFIL = None


# ───────────────────────── environnement ─────────────────────────
def deconfiner():
    """Relance hors du bac à sable snap (VS Code) : sinon AppArmor bloque les
    messages AT-SPI vers les applis snap (Firefox, Chromium…).

    `RuntimeMaxSec` borne la vie de l'unité : tuer `systemd-run` (timeout du
    Bash tool, Ctrl-C) ne tue PAS le service, une action bloquée continuerait
    sinon indéfiniment sans personne pour la surveiller."""
    try:
        label = open("/proc/self/attr/current").read().strip()
    except OSError:
        return
    if not label.startswith("snap.") or os.environ.get("PILOTE_DECONFINE"):
        return
    cmd = ["systemd-run", "--user", "--quiet", "--pipe", "--wait", "--collect",
           "--same-dir", "-p", "RuntimeMaxSec=180", "-p", "KillSignal=SIGTERM",
           f"--setenv=DISPLAY={DISPLAY}",
           f"--setenv=XAUTHORITY={XAUTH}", "--setenv=PILOTE_DECONFINE=1"]
    # L'identité est calculée AVANT la relance : les variables du client (CLAUDECODE, CODEX_*…) ne passent pas
    # dans l'unité systemd. PILOTE_SANS_CADRE n'existe plus (lot 5, décision de Félix du 26/09).
    cmd.append(f"--setenv=PILOTE_AGENT={agent_courant()}")
    for var in ("LANG", "LC_ALL", "PILOTE_ETAT"):
        if os.environ.get(var):
            cmd.append(f"--setenv={var}={os.environ[var]}")
    cmd += [PYTHON_SYS, LANCEUR, *sys.argv[1:]]
    os.execvp(cmd[0], cmd)


def env_x():
    e = dict(os.environ)
    e["DISPLAY"] = DISPLAY
    e["XAUTHORITY"] = XAUTH
    return e


@mesurer("sous_processus")
def sh(*args, entree=None, ok=True, delai=DELAI_SH, stderr_aussi=False):
    """Sous-processus borné dans le temps : sans `timeout`, un outil qui garde
    ses tubes ouverts (xclip !) fige `ecran` dans une unité qui survit au
    client, garde-fou désarmé."""
    try:
        r = subprocess.run(args, env=env_x(), input=entree, capture_output=True,
                           text=True, timeout=delai)
    except subprocess.TimeoutExpired:
        raise SystemExit(f"✗ {args[0]} : pas de réponse en {delai} s (abandon)")
    if ok and r.returncode != 0:
        raise SystemExit(f"✗ {' '.join(args[:2])} : {r.stderr.strip() or r.returncode}")
    return (r.stdout.strip(), r.stderr.strip()) if stderr_aussi else r.stdout.strip()


def xdotool(*args, **kw):
    return sh("xdotool", *args, **kw)


def sortir(message, code=1):
    """Message sur stderr + code de sortie distinct (2 introuvable, 3 STOP,
    4 réservé) : un agent peut réagir sans analyser le texte."""
    print(message, file=sys.stderr)
    sys.exit(code)


def entier(valeur, nom, mini=None, maxi=None):
    try:
        n = int(str(valeur), 0) if str(valeur).lower().startswith("0x") else int(valeur)
    except (TypeError, ValueError):
        raise SystemExit(f"✗ {nom} : un entier est attendu, reçu « {valeur} »")
    if mini is not None and n < mini:
        raise SystemExit(f"✗ {nom} : minimum {mini}, reçu {n}")
    if maxi is not None and n > maxi:
        raise SystemExit(f"✗ {nom} : maximum {maxi}, reçu {n}")
    return n


def options_valides(args, connues, commande):
    for a in args:
        if a.startswith("--") and a not in connues:
            raise SystemExit(f"✗ option inconnue pour `{commande}` : {a}"
                             f"\n  connues : {' '.join(sorted(connues))}")


def ecran_taille():
    try:
        w, h = xdotool("getdisplaygeometry").split()
        return int(w), int(h)
    except Exception:
        return 2560, 1440


def pointeur():
    out = xdotool("getmouselocation", "--shell")
    d = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    return int(d["X"]), int(d["Y"])


def verifier_x():
    """X11 injoignable : mieux vaut refuser que lancer des applis dans le vide."""
    r = subprocess.run(["xdotool", "getdisplaygeometry"], env=env_x(),
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"✗ X11 injoignable (DISPLAY={DISPLAY}) : {r.stderr.strip()}")


def bouger_souris(x, y):
    """Déplace le pointeur SANS `--sync` : xdotool 3.2016 attend que le pointeur
    QUITTE sa position, donc `mousemove --sync` vers la position courante ne
    rend jamais la main (et c'est alors le geste de Félix qui débloque… en se
    faisant absorber). On vérifie nous-mêmes l'arrivée."""
    if pointeur() == (x, y):
        return
    xdotool("mousemove", str(x), str(y))
    for _ in range(30):                      # ≤ 300 ms
        if pointeur() == (x, y):
            return
        time.sleep(0.01)
    raise SystemExit(f"✗ la souris n'a pas atteint {x},{y}"
                     " — quelqu'un d'autre la tient ?")


def verifier_position_souris(attendu):
    """Pendant un trajet lent, ne jamais écraser un déplacement humain entre deux pas."""
    verifier_stop()
    courant = pointeur()
    if courant != attendu:
        ecrire("stop", "[ecran] souris déplacée pendant l'approche lente"
                       f" ({attendu[0]},{attendu[1]} → {courant[0]},{courant[1]})")
        sortir("⛔ ARRÊTÉ : souris déplacée pendant l'approche lente ; aucun clic envoyé", ARRETE)


def bouger_souris_lent(x, y, ctx):
    """Trajet de 2 s, accélération/décélération continues, sans hasard ni dépassement.

    Appelé sous agir(bouge=True). La vigie détecte les mouvements physiques
    même sous `occupe` ; le contrôle de position complète cette surveillance.
    """
    depart = ctx.depart
    verifier_position_souris(depart)
    ctx.vers = depart
    if depart == (x, y):
        return
    debut, precedent = time.monotonic(), depart
    for i in range(1, 81):
        pause = debut + i * 0.025 - time.monotonic()
        if pause > 0:
            time.sleep(pause)
        verifier_position_souris(precedent)
        if time.monotonic() - debut > 5:
            raise SystemExit("✗ approche lente trop longue ; aucun clic envoyé")
        t = i / 80
        progression = t * t * (3 - 2 * t)
        point = (round(depart[0] + (x - depart[0]) * progression),
                 round(depart[1] + (y - depart[1]) * progression))
        if point != precedent:
            bouger_souris(*point)
        precedent = point
        ctx.vers = point
        verifier_position_souris(precedent)
        if time.monotonic() - debut > 5:
            raise SystemExit("✗ approche lente trop longue ; aucun clic envoyé")


# ───────────────────────── cadre + garde-fou ─────────────────────────
# chemin, ecrire, effacer, pid_vivant, occupe_valide : voir commun.py (partagés avec cadre.py)


def cadre_actif():
    r = subprocess.run(["systemctl", "--user", "is-active", CADRE_UNITE],
                       capture_output=True, text=True)
    return r.stdout.strip() == "active"


def cadre_pret():
    """Le cadre écrit `pret` (pid) une fois la fenêtre affichée."""
    try:
        pid = int(open(chemin("pret")).read().split()[0])
    except (OSError, ValueError, IndexError):
        return False
    return pid_vivant(pid)


def cadre_demarrer():
    """Démarre le cadre et N'AGIT PAS tant qu'il n'est pas réellement affiché :
    sans lui, Félix n'a aucun signal visible ET aucun kill-switch."""
    if cadre_actif() and cadre_pret():
        return
    if not os.path.exists(PYTHON_CADRE):
        raise SystemExit(f"✗ python du cadre introuvable ({PYTHON_CADRE}) :"
                         " garde-fou indisponible, rien n'est exécuté")
    effacer("pret")
    subprocess.run(["systemctl", "--user", "stop", CADRE_UNITE], capture_output=True)
    subprocess.run(["systemctl", "--user", "reset-failed", CADRE_UNITE],
                   capture_output=True)
    subprocess.run(["systemd-run", "--user", "--quiet", "--collect",
                    f"--unit={CADRE_UNITE}", f"--setenv=DISPLAY={DISPLAY}",
                    f"--setenv=XAUTHORITY={XAUTH}", f"--setenv=PILOTE_ETAT={ETAT}",
                    f"--setenv=PILOTE_AGENT={agent_courant()}",
                    PYTHON_CADRE, os.path.join(ICI, "cadre.py")],
                   capture_output=True)
    for _ in range(30):                      # ≤ 3 s
        if cadre_pret():
            return
        time.sleep(0.1)
    journal = subprocess.run(["journalctl", "--user", "-u", CADRE_UNITE, "-n", "5",
                              "--no-pager", "-o", "cat"], capture_output=True, text=True)
    raise SystemExit("✗ cadre non affiché : garde-fou indisponible, rien n'est"
                     f" exécuté.\n{journal.stdout.strip()}")


def cadre_arreter():
    subprocess.run(["systemctl", "--user", "stop", CADRE_UNITE], capture_output=True)
    effacer("pret", "souris")


def agent_courant():
    """Qui pilote ? `PILOTE_AGENT`, sinon déduit du CLIENT (Claude Code, Codex, Hermès/Charli). Plus jamais
    `anonyme-<ppid>` : après déconfinement le ppid est le gestionnaire systemd, commun à tous (audit bugs-05)."""
    nom = os.environ.get("PILOTE_AGENT", "").strip()
    if nom:
        return nom
    e = os.environ
    if e.get("CLAUDECODE") or e.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude"
    if any(k.startswith("CODEX_") for k in e):
        return "codex"
    if any(k.startswith("HERMES_") for k in e) or "hermes" in (e.get("VIRTUAL_ENV") or ""):
        return "charli"
    return "inconnu"


ETAT_REEL = f"/run/user/{os.getuid()}/pilote-ecran"


def stop_reel():
    """Chemin du VRAI fichier `stop` quand PILOTE_ETAT désigne un autre dossier : un dossier d'état détourné ne
    contourne pas un arrêt posé par Félix (audit securite-pilote-etat-detourne)."""
    if os.path.realpath(ETAT) == os.path.realpath(ETAT_REEL):
        return None
    p = os.path.join(ETAT_REEL, "stop")
    return p if os.path.exists(p) else None


def notifier_felix(titre, texte):
    """Notification ntfy silencieuse (décision de Félix : chaque `reprendre` et `stop --forcer`). Directement à
    ntfy.sh (le pont :9100 est coupé depuis le 25/09). Jamais en test (dossier d'état non réel)."""
    if os.path.realpath(ETAT) != os.path.realpath(ETAT_REEL):
        return
    try:
        import urllib.request
        topic = open(os.path.expanduser("~/.jarvis/ntfy_topic")).read().strip()
        urllib.request.urlopen(urllib.request.Request(
            f"https://ntfy.sh/{topic}", data=texte.encode("utf-8"), method="POST",
            headers={"Title": titre.encode("ascii", "replace").decode(), "Priority": "low", "Tags": "robot"}), timeout=8)
    except Exception:
        pass


def journal_chemin():
    if os.path.realpath(ETAT) != os.path.realpath(ETAT_REEL):
        return chemin("journal.ndjson")
    d = os.path.expanduser("~/.local/state/pilote-ecran")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "journal.ndjson")


def journaliser(**champs):
    """Journal append-only de ce que les agents FONT à l'écran (qui, quoi, où, quand) ; le texte tapé n'y est
    jamais — seulement sa longueur."""
    champs = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "agent": agent_courant(), **champs}
    try:
        with open(journal_chemin(), "a") as f:
            f.write(json.dumps(champs, ensure_ascii=False) + "\n")
    except OSError:
        pass


def pilote_en_cours():
    """(agent, âge en s) du détenteur de la main, ou None.

    Un pilote inactif depuis plus de EXPIRATION_PILOTE secondes est réputé
    parti : sans ça, un agent qui plante bloquerait l'écran pour les autres."""
    try:
        d = json.load(open(chemin("pilote")))
        age = time.time() - os.path.getmtime(chemin("actif"))
    except (OSError, ValueError):
        return None
    except Exception:
        return None
    if age > EXPIRATION_PILOTE and not os.path.exists(chemin("stop")):
        # Sous STOP la réservation est GELÉE : c'est à Félix de dire qui reprend (audit securite-expiration).
        effacer("pilote")
        return None
    return d.get("agent", "?"), age


def verifier_pilote():
    """Refuse d'agir si un AUTRE agent a pris la main.

    Le verrou `flock` sérialise les commandes une par une, mais pas une
    TÂCHE : le 08/09, Charli et Codex ont piloté en même temps et les clics
    de l'une sont partis dans la fenêtre ouverte par l'autre (résultat lu
    31 au lieu de 42). D'où cette réservation explicite."""
    p = pilote_en_cours()
    if p and p[0] != agent_courant():
        sortir(f"⛔ « {p[0]} » pilote l'écran depuis {int(p[1])} s — rien n'est"
               " exécuté.\n   Attends qu'il fasse `ecran stop`, ou demande à"
               " Félix d'arbitrer (`ecran etat` pour voir).", RESERVE)


def ligne_etat():
    """Première ligne des commandes de perception : l'agent sait d'un coup
    d'œil s'il peut agir, sans appeler `etat`."""
    if os.path.exists(chemin("stop")):
        try:
            raison = open(chemin("stop")).read().strip()
        except OSError:
            raison = "?"
        return f"ETAT STOP — {raison} (lecture seule ; `reprendre` seulement sur GO de Félix)"
    p = pilote_en_cours()
    if p and p[0] != agent_courant():
        return f"ETAT réservé par « {p[0]} » (inactif depuis {int(p[1])} s) — lecture seule"
    return "ETAT ok" + (" — écran réservé par toi" if p else "")


def verifier_stop(lecture=False):
    """Un arrêt déclenché par Félix bloque toute action. Les commandes sans
    effet (`voir`, `lire`) restent permises, avec bandeau : il faut pouvoir
    regarder ce qui s'est passé pour le lui raconter — sans être tenté de
    faire `reprendre` juste pour voir."""
    fichier = chemin("stop") if os.path.exists(chemin("stop")) else stop_reel()
    if not fichier:
        return False
    with open(fichier) as f:
        raison = f.read().strip()
    if lecture:
        print(f"⛔ ARRÊTÉ ({raison}) — lecture seule ; `ecran reprendre` seulement"
              " après le GO de Félix", file=sys.stderr)
        return True
    sortir("⛔ ARRÊTÉ : " + raison
           + "\n   (rien n'est exécuté tant que `ecran reprendre` n'a pas été lancé)", ARRETE)


class verrou_exclusif:
    """Verrou de fichier partagé par TOUTES les commandes qui touchent l'état.

    `stop` et `reprendre` en étaient exemptés : ils pouvaient retirer le cadre
    et effacer `occupe` pendant qu'une autre commande injectait encore des
    événements (constat de Codex, 08/09)."""

    def __init__(self, secondes=10):
        self.secondes = secondes
        self.f = None

    def __enter__(self):
        os.makedirs(ETAT, exist_ok=True)
        self.f = open(chemin("verrou"), "w")
        for _ in range(int(self.secondes * 10)):
            try:
                fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                time.sleep(0.1)
        raise SystemExit("✗ une autre commande `ecran` est en cours (verrou) —"
                         " un seul pilote à la fois")

    def __exit__(self, *_):
        try:
            fcntl.flock(self.f, fcntl.LOCK_UN)
            self.f.close()
        except OSError:
            pass
        return False


class agir:
    """Contexte d'une action.

    - verrou exclusif : deux `ecran` en parallèle se marchaient dessus
      (`occupe` effacé par l'un pendant l'action de l'autre → faux arrêt) ;
    - `occupe` (qui rend le cadre aveugle) n'est posé QUE pour les commandes
      qui déplacent la souris, et pour leur durée ;
    - à l'entrée comme à la sortie, on compare la position réelle du pointeur
      à celle attendue : un mouvement de Félix pendant l'action déclenche
      l'arrêt au lieu d'être entériné comme le nôtre.
    """

    def __init__(self, bouge=False, vers=None, lecture=False):
        self.bouge = bouge          # l'action déplace la souris
        self.vers = vers            # position finale attendue
        self.lecture = lecture      # commande sans effet (voir/lire)
        self.verrou = None
        self.depart = None
        self.sous_stop = False

    def _prendre_verrou(self):
        self.verrou = verrou_exclusif().__enter__()

    def pointeur_devie(self):
        """Vrai si la souris a quitté la position attendue — à interroger au
        milieu d'une action longue (série de touches)."""
        if self.bouge or self.depart is None:
            return False
        try:
            x, y = pointeur()
        except SystemExit:
            return False
        return (abs(x - self.depart[0]) > SEUIL or abs(y - self.depart[1]) > SEUIL)

    def _verifier_pointeur(self):
        """Ferme la course de 100 ms entre le dernier tick du cadre et `occupe`."""
        try:
            sx, sy = (int(v) for v in open(chemin("souris")).read().split())
        except (OSError, ValueError):
            return
        x, y = self.depart
        if abs(x - sx) > SEUIL or abs(y - sy) > SEUIL:
            ecrire("stop", f"[ecran] souris bougée par Félix ({sx},{sy} → {x},{y})"
                           f" à {time.strftime('%H:%M:%S')}")
            raise SystemExit(f"⛔ ARRÊTÉ : la souris a bougé ({sx},{sy} → {x},{y})"
                             " avant l'action — rien n'est exécuté")

    def __enter__(self):
        os.makedirs(ETAT, exist_ok=True)
        self._prendre_verrou()
        self.sous_stop = verifier_stop(self.lecture)
        if not self.lecture:
            verifier_pilote()
            if not pilote_en_cours():
                # Réservation IMPLICITE à la première action (décision de Félix §5.1) : l'agent qui agit tient
                # l'écran jusqu'à `ecran stop`/`rendre`, les autres ne peuvent plus agir.
                ecrire("pilote", json.dumps({"agent": agent_courant(), "pid": os.getpid(), "ts": time.time(),
                                             "implicite": True}))
        occupe_valide()                      # retire un `occupe` orphelin
        if not self.sous_stop:
            # `actif` AVANT de (re)lancer le cadre : un cadre relancé après une
            # pause lisait l'ancien `actif`, se croyait inactif et mourait en
            # 200 ms — l'action partait sans cadre ni kill-switch (25/09).
            # Un agent qui ne fait que REGARDER ne prolonge pas la réservation d'un autre (audit bugs-08).
            p = pilote_en_cours()
            if not (self.lecture and p and p[0] != agent_courant()):
                ecrire("actif", str(time.time()))
            cadre_demarrer()
        self.depart = pointeur()
        if not self.sous_stop:
            self._verifier_pointeur()
            if self.bouge:
                ecrire("occupe", str(os.getpid()))
        return self

    def __exit__(self, *exc):
        try:
            if self.sous_stop:
                return False
            attendu = self.vers if (self.bouge and self.vers) else self.depart
            try:
                fin = pointeur()
            except BaseException:
                return False
            if (abs(fin[0] - attendu[0]) > SEUIL or abs(fin[1] - attendu[1]) > SEUIL) \
                    and not os.path.exists(chemin("stop")):
                ecrire("stop", "[ecran] souris bougée par Félix pendant l'action"
                               f" ({attendu[0]},{attendu[1]} → {fin[0]},{fin[1]})"
                               f" à {time.strftime('%H:%M:%S')}")
                print("⛔ la souris a bougé pendant l'action : pilotage arrêté",
                      file=sys.stderr)
            else:
                ecrire("souris", f"{fin[0]} {fin[1]}")
        finally:
            effacer("occupe")
            if self.verrou:
                self.verrou.__exit__()
        return False


# ───────────────────────── accessibilité ─────────────────────────
def atspi():
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi
    Atspi.set_timeout(400, 1500)  # ms : une appli figée ne bloque pas tout
    return Atspi


def activer_accessibilite():
    """GTK/Qt/Electron n'exposent leur arbre que si le bus le demande.
    Jamais `ScreenReaderEnabled` / `screen-reader-enabled` : Orca démarrerait sur le poste de Félix (audit
    atspi-07). Et rien n'est écrit sous STOP : `voir` y est une lecture seule."""
    if os.path.exists(chemin("stop")):
        return
    r = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface",
                        "toolkit-accessibility"], capture_output=True, text=True)
    if r.stdout.strip() == "false":
        subprocess.run(["gsettings", "set", "org.gnome.desktop.interface",
                        "toolkit-accessibility", "true"], capture_output=True)
        print("ℹ accessibilité activée (toolkit-accessibility) — les applis déjà"
              " ouvertes n'exposeront leur arbre qu'après redémarrage",
              file=sys.stderr)


def fenetre_active():
    """(id X11, nom, pid) de la fenêtre active — un pid manquant (vieilles
    applis X sans _NET_WM_PID) ne doit pas faire perdre l'id ni le nom."""
    try:
        wid = xdotool("getactivewindow")
    except SystemExit:
        return None, "", None
    nom = sh("xdotool", "getwindowname", wid, ok=False)
    pid = sh("xdotool", "getwindowpid", wid, ok=False)
    return wid, nom, int(pid) if pid.isdigit() else None


def hors_vue(e, geo, taille_ecran):
    """Vrai si le CENTRE d'un élément AT-SPI tombe hors de la zone visible de sa fenêtre (geo = x, y, l, h) ou hors
    écran. Un élément d'une page web défilée hors du cadre garde des coordonnées AT-SPI valides : le 01/11/2026, le
    bouton « Valider » du CIC était annoncé en y = 1391 sous une fenêtre finissant en 1373 — le clic tombait sur le
    dock GNOME et lançait Zoom à chaque essai."""
    cx, cy = e["x"] + e["w"] // 2, e["y"] + e["h"] // 2
    largeur, hauteur = taille_ecran
    if not (0 <= cx < largeur and 0 <= cy < hauteur):
        return True
    if geo:
        gx, gy, gl, gh = geo
        return not (gx <= cx < gx + gl and gy <= cy < gy + gh)
    return False


def geometrie_x11(wid):
    """Origine et taille VISIBLES d'une fenêtre X11 (ombres CSD déduites via
    _GTK_FRAME_EXTENTS) : (x, y, l, h) ou None."""
    # xwininfo et non `xdotool getwindowgeometry` : pour une fenêtre à barre
    # de titre mutter (Chrome --app, Tk…), xdotool compte la barre deux fois
    # (Y=106 pour une fenêtre réellement en 69 : 69 + 37). Mesuré le 26/09 ;
    # sans barre mutter (VS Code, GTK4) les deux coïncident.
    wid = str(wid)   # les éléments AT-SPI portent un wid numérique (int) : xprop/xwininfo veulent du texte (01/11)
    out = sh("xwininfo", "-id", wid, ok=False)
    g = {}
    for l in out.splitlines():
        if ":" in l:
            k, v = l.split(":", 1)
            g[k.strip()] = v.strip()
    try:
        x, y = int(g["Absolute upper-left X"]), int(g["Absolute upper-left Y"])
        w, h = int(g["Width"]), int(g["Height"])
    except (KeyError, ValueError):
        return None
    ext = sh("xprop", "-id", wid, "_GTK_FRAME_EXTENTS", ok=False)
    if "=" in ext:
        try:
            g_, d_, ht, b = (int(v) for v in ext.split("=")[1].split(","))
            return x + g_, y + ht, w - g_ - d_, h - ht - b
        except ValueError:
            pass
    return x, y, w, h


def fenetres_x11_du_pid(pid):
    out = sh("xdotool", "search", "--onlyvisible", "--pid", str(pid), ok=False)
    return [w for w in out.split() if w.isdigit()]


def fenetres_x():
    """Liste wmctrl : [(id, bureau, nom)] sans le cadre ni le bureau."""
    out = sh("wmctrl", "-l", ok=False)
    res = []
    for l in out.splitlines():
        p = l.split(None, 3)
        if len(p) < 4:
            continue
        wid, bureau, _hote, nom = p
        if nom == "Claude pilote l'écran" or nom.startswith("Desktop Icons"):
            continue
        res.append((wid, bureau, nom))
    return res


def empilement():
    """Ids X11 du bas vers le haut (_NET_CLIENT_LIST_STACKING)."""
    out = sh("xprop", "-root", "_NET_CLIENT_LIST_STACKING", ok=False)
    ids = []
    if "#" in out:
        for m in out.split("#", 1)[1].split(","):
            m = m.strip()
            if m.startswith("0x"):
                ids.append(int(m, 16))
    return ids


def nom_de(acc, Atspi, role):
    nom = (acc.get_name() or "").strip()
    if not nom:
        try:
            nom = (acc.get_description() or "").strip()
        except Exception:
            nom = ""
    texte = ""
    if role in ROLES_TEXTE:
        try:
            # get_text_iface() rend le MÊME objet : `t.get_text(0, n)` tomberait
            # sur Atspi.Accessible.get_text (déprécié, 0 argument) → appel
            # explicite de l'interface Text.
            t = acc.get_text_iface()
            if t:
                n = Atspi.Text.get_character_count(t)
                texte = Atspi.Text.get_text(t, 0, min(n, 80)).replace("\n", "⏎")
                if n > 80:
                    texte += "…"
        except Exception:
            pass
    return nom, texte


def etats_de(acc, Atspi):
    s = acc.get_state_set()
    marques = []
    for st, lib in ((Atspi.StateType.FOCUSED, "focus"),
                    (Atspi.StateType.CHECKED, "coché"),
                    (Atspi.StateType.SELECTED, "sélectionné"),
                    (Atspi.StateType.EXPANDED, "déplié")):
        if s.contains(st):
            marques.append(lib)
    if not s.contains(Atspi.StateType.SENSITIVE):
        marques.append("inactif")
    return marques


@mesurer("atspi")
def choisir_fenetres_atspi(Atspi, tout, motif):
    """Fenêtres AT-SPI à parcourir : active (par pid + nom), toutes, ou motif."""
    bureau = Atspi.get_desktop(0)
    wid, nom_actif, pid_actif = fenetre_active()
    motif_l = motif.lower() if motif else None
    choisies, secours = [], []

    def wid_de(app, nom_w):
        """Fenêtre X11 correspondante : l'active si même pid, sinon par nom."""
        try:
            pid = app.get_process_id()
        except Exception:
            return None
        if pid and pid == pid_actif and nom_w == nom_actif:
            return wid
        for w_ in fenetres_x11_du_pid(pid):
            if sh("xdotool", "getwindowname", w_, ok=False) == nom_w:
                return w_
        cands = fenetres_x11_du_pid(pid)
        return cands[0] if cands else None

    for i in range(bureau.get_child_count()):
        try:
            app = bureau.get_child_at_index(i)
            if app is None:
                continue
            nom_app = app.get_name() or ""
            # Le cadre et les décorations mutter portent le titre de la
            # fenêtre active sans en être le contenu.
            if nom_app in ("cadre.py", "mutter-x11-frames"):
                continue
            for j in range(app.get_child_count()):
                w = app.get_child_at_index(j)
                if w is None:
                    continue
                s = w.get_state_set()
                if not (s.contains(Atspi.StateType.SHOWING)
                        or s.contains(Atspi.StateType.ACTIVE)):
                    continue
                nom_w = w.get_name() or ""
                if tout:
                    choisies.append((nom_app, nom_w, w, wid_de(app, nom_w)))
                elif motif_l:
                    if motif_l in nom_w.lower() or motif_l in nom_app.lower():
                        choisies.append((nom_app, nom_w, w, wid_de(app, nom_w)))
                else:
                    meme_pid = pid_actif and app.get_process_id() == pid_actif
                    if meme_pid and (s.contains(Atspi.StateType.ACTIVE)
                                     or nom_w == nom_actif):
                        choisies.append((nom_app, nom_w, w, wid))
                    elif meme_pid or (nom_w and nom_w == nom_actif):
                        secours.append((nom_app, nom_w, w, wid_de(app, nom_w)))
        except Exception:
            continue
    if not choisies and not tout and not motif_l:
        choisies = secours[:1]
    return choisies, (wid, nom_actif, pid_actif)


def repere(Atspi, racine, wid):
    """(type de coordonnées, dx, dy) fiable pour cette fenêtre.

    GTK3/Qt/Firefox donnent de vraies coordonnées SCREEN. GTK4 (Calculatrice,
    Éditeur de texte, Nautilus 46…) renvoie 0,0 en SCREEN pour tout : on
    prend alors les coordonnées WINDOW (relatives au cadre visible) décalées
    de l'origine X11 visible (getwindowgeometry − _GTK_FRAME_EXTENTS)."""
    try:
        comp = racine.get_component_iface()
        fr = comp.get_extents(Atspi.CoordType.SCREEN) if comp else None
    except Exception:
        fr = None
    geo = geometrie_x11(wid) if wid else None
    if fr and (fr.x, fr.y) != (0, 0):
        return Atspi.CoordType.SCREEN, 0, 0
    if geo and (geo[0], geo[1]) != (0, 0):
        return Atspi.CoordType.WINDOW, geo[0], geo[1]
    return Atspi.CoordType.SCREEN, 0, 0


def identite_atspi(acc):
    """Identité native, stable tant que l'objet vit sur le bus AT-SPI.

    Les champs Object.path et Application.bus_name sont locaux à libatspi :
    aucun texte, géométrie ou appel D-Bus supplémentaire pour cette clé.
    """
    try:
        bus, path = acc.app.bus_name, acc.path
        if isinstance(bus, str) and bus and isinstance(path, str) and path:
            return f"atspi:{bus}:{path}"
    except Exception:
        pass
    return None


def ancre_structure_atspi(racine, wid):
    """Repli identifié comme structurel : stable hors réorganisation de l'arbre."""
    native = identite_atspi(racine)
    if native:
        return native
    if wid:
        return f"x11:{wid}"
    try:
        return f"pid:{racine.get_process_id()}:racine:{racine.get_index_in_parent()}"
    except Exception:
        return "racine-inconnue"


@mesurer("atspi")
def parcourir(Atspi, racine, largeur, hauteur, deadline, elements, vus, wid=None):
    """BFS de l'arbre AT-SPI : ne garde que les rôles utiles, visibles,
    à l'écran ; s'arrête à la deadline (les pages web sont immenses)."""
    coord, dx, dy = repere(Atspi, racine, wid)
    ancre = ancre_structure_atspi(racine, wid)
    file = [(racine, 0, ())]
    noeuds = 0
    coupe = False
    while file:
        if time.monotonic() > deadline or len(elements) >= MAX_ELEMENTS:
            coupe = True
            break
        acc, prof, indices = file.pop(0)
        noeuds += 1
        try:
            role = acc.get_role_name()
            s = acc.get_state_set()
            visible = s.contains(Atspi.StateType.SHOWING) and s.contains(Atspi.StateType.VISIBLE)
            if role in ROLES and visible and prof > 0:
                comp = acc.get_component_iface()
                if comp:
                    r = comp.get_extents(coord)
                    r.x += dx
                    r.y += dy
                    if (r.width > 0 and r.height > 0 and r.x + r.width > 0
                            and r.y + r.height > 0 and r.x < largeur and r.y < hauteur
                            and (role in ROLES_TEXTE     # une zone de texte peut être plein écran
                                 or r.width * r.height < 0.6 * largeur * hauteur)):
                        nom, texte = nom_de(acc, Atspi, role)
                        cle = (r.x, r.y, r.width, r.height, nom)
                        # Étiquette interne d'un bouton déjà listé (même nom,
                        # boîte incluse) : du bruit, on ne la numérote pas.
                        doublon = ROLES[role] == "texte" and any(
                            e["nom"] == nom and e["x"] <= r.x and e["y"] <= r.y
                            and e["x"] + e["w"] >= r.x + r.width
                            and e["y"] + e["h"] >= r.y + r.height
                            for e in elements[-60:])
                        if not doublon and cle not in vus and (
                                nom or texte or role in ROLES_TEXTE
                                or ROLES[role] not in ("texte", "image")):
                            vus.add(cle)
                            elements.append({
                                "id": len(elements) + 1, "role": ROLES[role],
                                "source": "atspi",
                                "cle_source": identite_atspi(acc) or (
                                    f"atspi:structure:{ancre}:" + "/".join(map(str, indices))),
                                "role_atspi": role, "nom": nom, "texte": texte,
                                "x": r.x, "y": r.y, "w": r.width, "h": r.height,
                                "etats": etats_de(acc, Atspi),
                                "wid": int(wid) if wid else None,   # fenêtre de CET élément (audit bugs-02)
                            })
            # On ne descend pas dans les sous-arbres invisibles (menus fermés,
            # onglets cachés) ni dans les zones hors écran : ça coûte des IPC.
            if prof == 0 or s.contains(Atspi.StateType.SHOWING) or role in ("menu", "menu bar"):
                if prof < 30:
                    n = acc.get_child_count()
                    if n > 500:
                        coupe = True
                    for k in range(min(n, 500)):
                        ch = acc.get_child_at_index(k)
                        if ch is not None:
                            file.append((ch, prof + 1, indices + (k,)))
                        else:
                            coupe = True  # enfant annoncé mais inaccessible (Electron peut renvoyer atspi/null)
                elif acc.get_child_count():
                    coupe = True
        except Exception:
            coupe = True
            continue
    return noeuds, coupe


# ───────────────────────── commandes ─────────────────────────
@mesurer("capture")
def capture(zone=None, plein=False):
    """Capture X11 (maim, secours import) → PNG, réduite sauf --plein/zone."""
    from PIL import Image
    brut = chemin("brut.png")
    if shutil.which("maim"):
        args = ["maim", "-u", "-q"]  # -u : sans le curseur
        if zone:
            x, y, w, h = zone
            args += ["-g", f"{w}x{h}+{x}+{y}"]
        args.append(brut)
        sh(*args)
    else:
        args = ["import", "-window", "root"]
        if zone:
            x, y, w, h = zone
            args += ["-crop", f"{w}x{h}+{x}+{y}", "+repage"]
        args.append(brut)
        sh(*args)
    img = Image.open(brut).convert("RGB")
    pleine = img                              # pleine résolution, pour l'OCR
    echelle = 1.0
    if not plein and not zone and img.width > LARGEUR_CAPTURE:
        echelle = LARGEUR_CAPTURE / img.width
        img = img.resize((LARGEUR_CAPTURE, round(img.height * echelle)), Image.LANCZOS)
    return img, echelle, pleine


LARGEUR_IMAGE_MAX = 1920   # strictement < 2000 : l'API refuse les images de 2000 px dans les requêtes à nombreuses images


def cadrer(taille, origine, mode="fenetre", fenetre=None, point=None, autour=(1200, 600), tuile=None):
    """Quelle partie de la capture pleine résolution devient l'IMAGE livrée, et à quelle échelle (lot 2, 26/09).
    `taille`/`origine` : capture pleine (écran entier, ou --zone). Rend (boîte dans la capture, origine écran de
    l'image, échelle). Par défaut la FENÊTRE ACTIVE à l'échelle 1 (réduite seulement au-delà de 1 920 px) : la vue
    ×0,5 d'avant (texte à 5 px) faisait deviner les clics."""
    W, H = taille
    ox, oy = origine
    if mode == "apercu":
        return (0, 0, W, H), (ox, oy), min(1.0, 1280 / W)
    if mode == "plein":
        return (0, 0, W, H), (ox, oy), min(1.0, LARGEUR_IMAGE_MAX / W)
    if mode == "tuile":
        (c, r), k = tuile
        if not (1 <= k <= c * r):
            raise SystemExit(f"✗ --tuile {c}x{r} : numéro 1…{c * r}")
        i, j = (k - 1) % c, (k - 1) // c
        x0, y0, x1, y1 = W * i // c, H * j // r, W * (i + 1) // c, H * (j + 1) // r
        return (x0, y0, x1, y1), (ox + x0, oy + y0), min(1.0, LARGEUR_IMAGE_MAX / (x1 - x0))
    if mode == "autour":
        l, h = min(autour[0], W), min(autour[1], H)
        x0 = min(max(0, point[0] - ox - l // 2), W - l)
        y0 = min(max(0, point[1] - oy - h // 2), H - h)
        return (x0, y0, x0 + l, y0 + h), (ox + x0, oy + y0), min(1.0, LARGEUR_IMAGE_MAX / l)
    if mode == "fenetre" and fenetre:
        fx, fy, fw, fh = fenetre
        x0, y0 = max(0, fx - ox), max(0, fy - oy)
        x1, y1 = min(W, fx - ox + fw), min(H, fy - oy + fh)
        if x1 - x0 >= 50 and y1 - y0 >= 50:
            return (x0, y0, x1, y1), (ox + x0, oy + y0), min(1.0, LARGEUR_IMAGE_MAX / (x1 - x0))
    return (0, 0, W, H), (ox, oy), min(1.0, LARGEUR_IMAGE_MAX / W)


def marquer(img, echelle, elements, decal=(0, 0)):
    from PIL import ImageDraw, ImageFont
    try:
        police = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 12)
    except OSError:
        police = ImageFont.load_default()
    d = ImageDraw.Draw(img)
    for e in elements:
        x = (e["x"] - decal[0]) * echelle
        y = (e["y"] - decal[1]) * echelle
        w, h = e["w"] * echelle, e["h"] * echelle
        d.rectangle([x, y, x + w, y + h], outline=(240, 138, 36), width=1)
        lab = str(e["id"])
        tw = d.textlength(lab, font=police) + 6
        d.rectangle([x, y - 14, x + tw, y], fill=(240, 138, 36))
        d.text((x + 3, y - 14), lab, fill=(0, 0, 0), font=police)
    return img


def invalider_reperes():
    # Ces fichiers décrivent l'ancienne vue. Ne pas laisser cliquer leurs ids.
    for nom in ("elements.json", "texte.json", "ecran.json"):
        effacer(nom)


def publier_vue_action(pleine, contexte):
    """Réutilise la dernière image du verdict, sans nouvelle capture ni OCR."""
    from PIL import Image
    boite, origine, echelle = cadrer(pleine.size, (0, 0), fenetre=contexte["geo"])
    img = pleine.crop(boite)
    if echelle < 1:
        img = img.resize((round(img.width * echelle), round(img.height * echelle)), Image.LANCZOS)
    png = chemin("ecran.png")
    img.save(png)
    ecrire("ecran.json", json.dumps({"ts": contexte["ts"], "origine": list(origine),
                                    "echelle": echelle, "taille": list(img.size), "mode": "apres-action"}))
    print(f"Image après action : {png} ({img.width}×{img.height}, origine {origine[0]},{origine[1]}, "
          f"échelle {echelle:g}) — fenêtre « {contexte['nom']} »")
    print(f"Capture prise il y a {max(0, time.time() - contexte['ts']):.1f} s ; "
          "inspecte cette image avant l’action suivante. Coordonnées image : --img.")


def point_de_reference(ref, elements):
    """Centre écran de `--autour` : tN (dernière lecture), @texte (cherché maintenant), id AT-SPI, ou X,Y."""
    if re.fullmatch(r"\d+,\d+", ref):
        return tuple(int(v) for v in ref.split(","))
    if ref[:1] == "t" and ref[1:].isdigit():
        x, y, _ = resoudre_texte(ref)
        return x, y
    if ref.startswith("@"):
        res, _ = trouver(ref[1:])
        if not res:
            sortir(f"✗ --autour : « {ref[1:]} » introuvable à l'écran", INTROUVABLE)
        return res[0]["cx"], res[0]["cy"]
    e = next((x for x in elements if str(x["id"]) == ref), None)
    if not e:
        raise SystemExit(f"✗ --autour {ref} : ni tN, ni @texte, ni id connu, ni X,Y")
    return e["x"] + e["w"] // 2, e["y"] + e["h"] // 2


def cmd_voir(args):
    options_valides(args, {"--zone", "--plein", "--tout", "--fenetre", "--sans-marques", "--sans-ocr",
                           "--apercu", "--tuile", "--autour", "--rapide"}, "voir")
    rapide = "--rapide" in args
    args = [a for a in args if a != "--rapide"]
    if rapide and ("--fenetre" in args or "--tout" in args):
        raise SystemExit("✗ --rapide ne lit pas les arbres : retire --fenetre/--tout")
    avec_ocr = not rapide and "--sans-ocr" not in args
    args = [a for a in args if a != "--sans-ocr"]
    largeur, hauteur = ecran_taille()
    mode_image, tuile, ref_autour, taille_autour = "fenetre", None, None, (1200, 600)
    if "--apercu" in args:
        mode_image = "apercu"
        args = [a for a in args if a != "--apercu"]
    if "--tuile" in args:
        i = args.index("--tuile")
        try:
            c, r = (int(v) for v in args[i + 1].lower().split("x"))
            tuile = ((c, r), int(args[i + 2]))
        except (IndexError, ValueError):
            raise SystemExit("✗ --tuile attend une grille et un numéro : --tuile 2x2 3")
        mode_image, args = "tuile", args[:i] + args[i + 3:]
    if "--autour" in args:
        i = args.index("--autour")
        if i + 1 >= len(args):
            raise SystemExit('✗ --autour attend tN, @"texte", un id ou X,Y [LxH]')
        ref_autour, fin = args[i + 1], i + 2
        if fin < len(args) and re.fullmatch(r"\d+x\d+", args[fin]):
            taille_autour = tuple(int(v) for v in args[fin].split("x"))
            fin += 1
        mode_image, args = "autour", args[:i] + args[fin:]
    zone = None
    plein = tout = sans_marques = False
    motif = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--zone":
            vals = args[i + 1:i + 5]
            if len(vals) != 4 or any(v.startswith("--") for v in vals):
                raise SystemExit("✗ --zone attend 4 entiers : --zone X Y LARGEUR HAUTEUR")
            zx = entier(vals[0], "--zone X", 0, largeur - 1)
            zy = entier(vals[1], "--zone Y", 0, hauteur - 1)
            zw = entier(vals[2], "--zone LARGEUR", 1, largeur)
            zh = entier(vals[3], "--zone HAUTEUR", 1, hauteur)
            zone = (zx, zy, min(zw, largeur - zx), min(zh, hauteur - zy))
            if (zw, zh) != (zone[2], zone[3]):
                print(f"⚠ zone rognée à l'écran : {zone[2]}×{zone[3]}", file=sys.stderr)
            i += 4
        elif a == "--plein":
            plein = True
            mode_image = "plein"
        elif a == "--tout":
            tout = True
        elif a == "--fenetre":
            if i + 1 >= len(args) or args[i + 1].startswith("--"):
                raise SystemExit("✗ --fenetre attend un motif : --fenetre Calculatrice")
            motif = args[i + 1]
            i += 1
        elif a == "--sans-marques":
            sans_marques = True
        else:
            raise SystemExit(f"✗ argument inattendu : {a}")
        i += 1
    with agir(lecture=True) as ctx:
        if rapide:
            if ref_autour and not re.fullmatch(r"\d+,\d+", ref_autour):
                raise SystemExit("✗ --rapide --autour attend X,Y ; les ids nécessitent une lecture complète")
            invalider_reperes()
            fenetres, elements = [], []
            wid, nom_actif, pid = fenetre_active()
            noter_fenetre_travail(wid, nom_actif, "voir")
            noeuds, coupe, relances, duree_atspi = 0, False, 0, 0.0
        else:
            activer_accessibilite()
            Atspi = atspi()
            t0 = time.monotonic()
            fenetres, (wid, nom_actif, pid) = choisir_fenetres_atspi(Atspi, tout, motif)
            noter_fenetre_travail(wid, nom_actif, "voir")
            elements, vus = [], set()
            deadline = t0 + DELAI_ATSPI
            noeuds, coupe, relances = 0, False, 0
            while True:
                for nom_app, nom_w, w, wid_w in fenetres:
                    n, c = parcourir(Atspi, w, largeur, hauteur, deadline, elements, vus, wid_w)
                    noeuds += n
                    coupe = coupe or c
                # Chromium/Electron construisent leur arbre à la PREMIÈRE interrogation : le premier parcours revient
                # presque vide et `voir` concluait « pas d'arbre » (25/09 : Chrome, puis 93 éléments au 2e passage).
                if elements or not fenetres or relances >= 3 or time.monotonic() > deadline:
                    break
                relances += 1
                time.sleep(0.3)
            duree_atspi = time.monotonic() - t0
        # L'arbre peut prendre plusieurs secondes. Capturer AVANT le parcours
        # montrait par exemple « préparation » à côté d'une réponse déjà lue.
        # Vérifier aussi le focus : aucun repère de l'ancienne fenêtre sur la nouvelle.
        actif = fenetre_active()
        if actif[0] != wid:
            elements = []
            print("⚠ fenêtre changée pendant la lecture : repères AT-SPI écartés ; "
                  "utilise le texte de la capture ou refais `voir`", file=sys.stderr)
        wid, nom_actif, pid = actif
        # --zone FILTRE l'affichage sans renuméroter : les ids restent ceux de la liste complète (26/09 : après un
        # `voir --zone`, « cliquer 37 » visait un élément disparu de la liste renumérotée 1…16).
        affiches = elements
        if zone:
            zx, zy, zw, zh = zone
            affiches = [e for e in elements
                        if e["x"] + e["w"] > zx and e["x"] < zx + zw
                        and e["y"] + e["h"] > zy and e["y"] < zy + zh]
        # IMAGE livrée (lot 2) : fenêtre active à l'échelle 1 par défaut, ≤ 1 920 px ; --plein, --apercu, --tuile,
        # --autour. Son origine et son échelle vont dans ecran.json : `cliquer X Y --img` convertit seul.
        origine_pleine = (zone[0], zone[1]) if zone else (0, 0)
        point = None
        if mode_image == "autour":
            point = point_de_reference(ref_autour, elements)
        geo_capture = geometrie_x11(wid) if wid else None
        geo = geo_capture if (mode_image == "fenetre" and not zone and not tout and not motif) else None
        ts_capture = time.time()
        pleine = capture_texte(zone) if rapide else capture(zone, True)[2]
        boite, origine_img, echelle = cadrer(pleine.size, origine_pleine, mode_image if not zone or mode_image != "fenetre" else "zone",
                                             geo, point, taille_autour, tuile)
        img = pleine.crop(boite)
        if echelle < 1:
            from PIL import Image as _Image
            img = img.resize((round(img.width * echelle), round(img.height * echelle)), _Image.LANCZOS)
        visibles = [e for e in affiches
                    if e["x"] + e["w"] > origine_img[0] and e["x"] < origine_img[0] + (boite[2] - boite[0])
                    and e["y"] + e["h"] > origine_img[1] and e["y"] < origine_img[1] + (boite[3] - boite[1])]
        if not sans_marques and visibles:
            marquer(img, echelle, visibles, origine_img)
        png = chemin("ecran.png")
        img.save(png)
        ecrire("ecran.json", json.dumps({"ts": ts_capture, "origine": list(origine_img), "echelle": echelle,
                                         "taille": [img.width, img.height], "mode": mode_image}))
        if motif and not fenetres:
            # Plus d'ancienne liste gardée sous une nouvelle image (audit atspi-10 / bugs-03) : liste VIDE + avertissement.
            print(f"⚠ aucune fenêtre AT-SPI ne correspond à « {motif} » — liste d'éléments vidée"
                  " (le motif compare le nom AT-SPI = titre de fenêtre, pas un onglet)", file=sys.stderr)
        ecrire("elements.json", json.dumps(
            {"ts": ts_capture, "fenetre": nom_actif, "wid": wid,
             "echelle": echelle, "zone": zone, "elements": elements},
            ensure_ascii=False))
        # Couche texte : la capture PLEINE résolution, lue quelle que soit
        # l'appli (le « câble HDMI ») ; l'arbre AT-SPI n'est qu'un bonus.
        lignes_ocr, t_ocr = [], 0.0
        if avec_ocr:
            t1 = time.monotonic()
            _, mots, lignes_ocr = lire_ecran(zone, img=pleine)
            memoriser_texte(pleine, zone, mots, lignes_ocr, ts=ts_capture)
            t_ocr = time.monotonic() - t1

    print(ligne_etat())
    print(f"Fenêtre active : « {nom_actif} » ({wid}) — écran {largeur}×{hauteur}")
    quoi = {"fenetre": "fenêtre active", "zone": "zone", "plein": "écran entier", "apercu": "aperçu réduit",
            "tuile": f"tuile {tuile[1]} de {tuile[0][0]}x{tuile[0][1]}" if tuile else "tuile",
            "autour": f"autour de {ref_autour}"}.get(mode_image if not zone or mode_image != "fenetre" else "zone", mode_image)
    print(f"Image : {png} ({img.width}×{img.height}, {quoi}, origine {origine_img[0]},{origine_img[1]}, échelle {echelle:g})"
          f" → écran = origine + image ÷ {echelle:g} ; `ecran cliquer X Y --img` convertit tout seul")
    print(f"Capture prise il y a {max(0, time.time() - ts_capture):.1f} s (lecture OCR comprise)")
    if rapide:
        print("Vue rapide : sans OCR ni AT-SPI ; anciens ids invalidés. Inspecte l’image avant d’agir.")
        return
    source = ("toutes fenêtres" if tout else f"fenêtres « {motif} »" if motif
              else "fenêtre active")
    apps = ", ".join(sorted({a for a, _, _, _ in fenetres})) or "aucune appli AT-SPI trouvée"
    print(f"Éléments AT-SPI ({source} : {apps}) — {len(affiches)} retenus"
          + (f" dans la zone (sur {len(elements)}, numéros de la liste complète)" if zone else "")
          + (f" après {relances} relecture(s) de l'arbre" if relances else "")
          + f", {noeuds} nœuds en {duree_atspi:.1f} s"
          # `--zone` ne fait que FILTRER après coup : seul `--fenetre` réduit
          # réellement le parcours.
          + (" ⚠ arbre coupé (délai/plafond) : cible une fenêtre avec --fenetre"
             if coupe else ""))
    if not affiches:
        print("  (0 élément AT-SPI — pas une panne : Chromium n'expose parfois son arbre"
              " qu'au 2e `voir`, Tk/Mailspring/Codex jamais. Le TEXTE ci-dessous est lu"
              " sur la capture : `cliquer tN` / `cliquer @\"texte\"`)")
    geos = {}   # géométrie visible par fenêtre, pour signaler les éléments défilés hors du cadre (01/11/2026)
    for e in affiches:
        nom = f"« {e['nom']} »" if e["nom"] else ""
        texte = f"  [texte : {e['texte']}]" if e["texte"] else ""
        etats = f"  ({', '.join(e['etats'])})" if e["etats"] else ""
        cx, cy = e["x"] + e["w"] // 2, e["y"] + e["h"] // 2
        wid_e = e.get("wid") or wid
        if wid_e not in geos:
            geos[wid_e] = geo_capture if wid_e == wid else (geometrie_x11(wid_e) if wid_e else None)
        hv = "  ⚠ HORS VUE (défiler avant de cliquer)" if hors_vue(e, geos[wid_e], (largeur, hauteur)) else ""
        print(f"{e['id']:>4}  {e['role']:<12} {nom:<44} centre {cx},{cy}"
              f"  {e['w']}×{e['h']}{texte}{etats}{hv}")
    if avec_ocr:
        afficher_ocr_voir(lignes_ocr, elements, wid, t_ocr, bool(zone or tout or motif), geo_capture)


def afficher_ocr_voir(lignes, elements, wid, duree, tout_afficher, geo_capture=None):
    """Lignes OCR de `voir` : seulement celles de la fenêtre active (sauf
    --zone/--tout/--fenetre), sans celles qu'un élément AT-SPI nommé couvre
    déjà (≥ 80 %), plafonnées à MAX_LIGNES_VOIR. Toutes restent dans texte.json."""
    import perception as P
    # L'OCR décrit les pixels capturés, même si la fenêtre bouge ensuite.
    boite = None if tout_afficher or not wid else geo_capture

    def dans_fenetre(li):
        return boite is None or (boite[0] <= li["cx"] < boite[0] + boite[2]
                                 and boite[1] <= li["cy"] < boite[1] + boite[3])

    def couverte(li):
        for e in elements:
            if not e["nom"] or e["role_atspi"] in ROLES_TEXTE or e["w"] * e["h"] > 40000:
                continue
            m = {"x": e["x"], "y": e["y"], "l": e["w"], "h": e["h"]}
            if P._recouvre(li, m, 0.8) and P.normaliser(li["texte"]) in P.normaliser(e["nom"]):
                return True
        return False

    montrees = [li for li in lignes if dans_fenetre(li) and not couverte(li)]
    print(f"Texte lu sur la capture (OCR) — {len(lignes)} lignes à l'écran, {len(montrees)}"
          f" {'affichées' if tout_afficher or not boite else 'dans la fenêtre active, hors AT-SPI'}"
          f", {duree:.1f} s")
    for li in montrees[:MAX_LIGNES_VOIR]:
        print(afficher_ligne(li))
    if len(montrees) > MAX_LIGNES_VOIR:
        print(f"  … {len(montrees) - MAX_LIGNES_VOIR} lignes de plus : `ecran texte`"
              " ou `ecran chercher \"…\"`")


def charger_elements():
    try:
        d = json.load(open(chemin("elements.json")))
    except (OSError, ValueError):
        raise SystemExit("✗ pas de liste d'éléments : lance d'abord `ecran voir`")
    if not isinstance(d, dict) or not isinstance(d.get("elements"), list) \
            or not isinstance(d.get("ts"), (int, float)):
        raise SystemExit("✗ liste d'éléments illisible : refais `ecran voir`")
    if time.time() - d["ts"] > SEUIL_VIEUX:
        print(f"⚠ liste d'éléments vieille de {int(time.time() - d['ts'])} s"
              " — l'écran a pu changer", file=sys.stderr)
    return d


def cible(args, usage):
    """Résout <id> ou <x> <y> (--img : coordonnées de la capture) → (x, y, élément|None)."""
    largeur, hauteur = ecran_taille()
    img = "--img" in args
    n, args = extraire_option(args, "--n", lambda v: entier(v, "--n", 1, 99))
    zone, args = extraire_zone(args, largeur, hauteur)
    nums = [a for a in args if not a.startswith("--")]
    if len(nums) == 1 and (nums[0].startswith("@") or
                           (nums[0][:1] == "t" and nums[0][1:].isdigit())):
        if img:
            raise SystemExit("✗ --img ne s'utilise qu'avec des coordonnées X Y")
        if zone and not nums[0].startswith("@"):
            raise SystemExit('✗ --zone ne s\'utilise qu\'avec @"texte"')
        x, y, quoi = resoudre_texte(nums[0], n, zone)
        return x, y, {"desc": quoi}
    if n is not None or zone:
        raise SystemExit('✗ --n et --zone ne s\'utilisent qu\'avec @"texte"')
    if len(nums) == 1:
        if img:
            raise SystemExit("✗ --img ne s'utilise qu'avec des coordonnées X Y")
        d = charger_elements()
        # Les coordonnées d'un id ne valent que pour la fenêtre où `voir` les a
        # relevées : si une autre fenêtre est passée devant (ou au premier plan),
        # le clic tomberait dedans — un « = » de la calculatrice peut devenir
        # « Envoyer » ailleurs (constat de Codex, 08/09).
        wid_actif, nom_actif, _ = fenetre_active()
        e = next((x for x in d["elements"] if str(x["id"]) == nums[0]), None)
        if e is None:
            raise SystemExit(f"✗ élément {nums[0]} inconnu (1…{len(d['elements'])})")
        # La fenêtre qui compte est celle de L'ÉLÉMENT (avant le 26/09 : celle qui était active au `voir`, fausse
        # avec --fenetre/--tout — un élément d'une fenêtre d'arrière-plan était cliqué dans la fenêtre active).
        wid_e = e.get("wid") or d.get("wid")
        if wid_e and wid_actif and int(wid_e) != int(wid_actif):
            ok, nom_apres = activer_fenetre(int(wid_e))
            if not ok:
                raise SystemExit(
                    f"✗ l'élément {nums[0]} est dans une autre fenêtre ({hex(int(wid_e))}) que je n'arrive pas à"
                    f" activer (active : « {nom_apres} »).\n  → `ecran fenetre 0x…` puis `ecran voir`")
            print(f"ℹ fenêtre de l'élément activée d'abord ({hex(int(wid_e))})", file=sys.stderr)
        # Un élément défilé hors du cadre de sa fenêtre a des coordonnées AT-SPI exactes… dans le vide : le clic
        # partirait sur ce qu'il y a en dessous (dock, autre fenêtre). On refuse et on dit quoi faire.
        geo_e = geometrie_x11(wid_e) if wid_e else None
        if hors_vue(e, geo_e, (largeur, hauteur)):
            cx, cy = e["x"] + e["w"] // 2, e["y"] + e["h"] // 2
            ou = f" ; fenêtre visible {geo_e[0]},{geo_e[1]} {geo_e[2]}×{geo_e[3]}" if geo_e else ""
            raise SystemExit(f"✗ élément {nums[0]} « {e['nom']} » hors de la zone visible (centre {cx},{cy}{ou}) :"
                             " fais défiler (`ecran defiler bas|haut` ou `ecran touche End`) puis `ecran voir`")
        return e["x"] + e["w"] // 2, e["y"] + e["h"] // 2, e
    if len(nums) == 2:
        x = float(entier(nums[0], "X", -10000, 10000))
        y = float(entier(nums[1], "Y", -10000, 10000))
        if img:
            # Coordonnées lues sur l'IMAGE livrée par le dernier `voir` : origine et échelle dans ecran.json (lot 2).
            try:
                im = json.load(open(chemin("ecran.json")))
                (ox, oy), e_ = im["origine"], float(im["echelle"])
            except (OSError, ValueError, KeyError, TypeError):
                raise SystemExit("✗ --img : pas d'image décrite (ecran.json) — refais `ecran voir`")
            if time.time() - im.get("ts", 0) > SEUIL_VIEUX:
                print(f"⚠ image vieille de {int(time.time() - im.get('ts', 0))} s — l'écran a pu changer", file=sys.stderr)
            x, y = ox + x / e_, oy + y / e_
        x, y = int(round(x)), int(round(y))
        if not (0 <= x < largeur and 0 <= y < hauteur):
            raise SystemExit(f"✗ {x},{y} est hors écran ({largeur}×{hauteur})")
        return x, y, None
    raise SystemExit("✗ usage : " + usage)


# ───────────────────────── verdict après action ─────────────────────────
def zones_ignorees(img):
    """Barre GNOME du haut (horloge qui tourne) : jamais un « changement »."""
    return [(0, 0, img.width, 32)]


class frappe_en_cours:
    """`en-cours` = « taper <pid> » pendant une frappe d'agent : la vigie (lot 6) arrête alors aussi sur une touche du
    clavier de Félix (hors frappe, il doit pouvoir écrire à Claude)."""
    def __init__(self, cmd):
        self.cmd = cmd

    def __enter__(self):
        ecrire("en-cours", f"{self.cmd} {os.getpid()}")
        return self

    def __exit__(self, *_):
        effacer("en-cours")
        return False


def etat_vigie():
    try:
        pid, ts, n = open(chemin("vigie")).read().split()[:3]
        if time.time() - float(ts) < 5 and pid_vivant(int(pid)):
            return f"active ({n} périphérique(s) : clics, molette, et clavier pendant une frappe d'agent)"
    except (OSError, ValueError):
        pass
    return "ABSENTE — seuls les déplacements de la souris arrêtent (clics, molette, clavier non surveillés)"


@mesurer("verification")
def verdict(avant, attendre=1.5, verifier=None, voir=False):
    """Après une action : attendre que l'écran se stabilise (≤ `attendre` s),
    comparer à la capture d'AVANT, dire ce qui a changé et où. Le diff est un
    INDICE (un survol change aussi des pixels), jamais une preuve : `--verifier`
    relit le texte attendu. → (lignes à afficher, vérification réussie ou None)."""
    with agir(lecture=True) if voir else nullcontext():
        import perception as P
        contexte = {}
        def prendre_image():
            if voir:
                wid, nom, _ = fenetre_active()
                geo = geometrie_x11(wid) if wid else None
                contexte.update(wid=wid, nom=nom, geo=geo, ts=time.time())
            image = capture_texte()
            if voir and fenetre_active()[0] != contexte["wid"]:
                contexte.update(wid=None, nom="fenêtre changée pendant la capture", geo=None)
            return image
        if voir:
            invalider_reperes()
        apres, stable_ok, duree = P.stable(prendre_image, max_s=attendre)
        if voir:
            publier_vue_action(apres, contexte)
        d = P.diff(avant, apres, exclure=zones_ignorees(avant))
        try:
            avant.save(chemin("avant.png"), compress_level=1)
            apres.save(chemin("apres.png"), compress_level=1)
        except OSError:
            pass
        _, nom, _ = fenetre_active()
        if d["zones"]:
            z = d["zones"]
            desc = (f"changement {d['pct']:.1f} % — {len(z)} zone(s) : "
                    + " ; ".join(f"{x},{y} {l}×{h}" for x, y, l, h in z[:3])
                    + (" …" if len(z) > 3 else ""))
        else:
            desc = "aucun changement visible"
        if not stable_ok:
            desc += f" (l'écran bougeait encore après {duree:.1f} s : `ecran attendre --stable 1`)"
        lignes = [f"   → {desc} — fenêtre « {nom} »"]
        ok = None
        if verifier:
            _, mots, _ = lire_ecran(img=apres)
            res = P.chercher(mots, verifier)
            ok = bool(res)
            lignes.append(f"   {'✓' if ok else '✗'} « {verifier} » "
                          + (f"vu en {res[0]['cx']},{res[0]['cy']}" if ok
                             else "PAS vu à l'écran après l'action (l'action, elle, est partie)"))
        return lignes, ok


def conclure(message, lignes, ok):
    print(message)
    for l in lignes:
        print(l)
    if ok is False:
        sys.exit(INTROUVABLE)


def options_action(args):
    """--verifier "texte" et --attendre S, communes aux actions."""
    verifier, args = extraire_option(args, "--verifier")
    attendre, args = extraire_option(args, "--attendre",
                                     lambda v: float(entier(v, "--attendre (s)", 0, 30)))
    voir = "--voir" in args
    sans_capture = "--sans-capture" in args
    if sans_capture and (voir or verifier):
        raise SystemExit("✗ --sans-capture est incompatible avec --voir et --verifier")
    args = [a for a in args if a not in ("--voir", "--sans-capture")]
    return verifier, (1.5 if attendre is None else attendre), voir, sans_capture, args


def lire_carte():
    """État structuré local, sans capture ; nécessaire avant une action sans image."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT_REGARD}/carte", timeout=8) as r:
            return json.load(r)
    except Exception as e:
        raise SystemExit(f"✗ carte inaccessible (127.0.0.1:{PORT_REGARD}, service pilote-ecran-regard) : {e}")


def resoudre_cible_carte(identifiant):
    """Résolution fraîche par le service, à appeler SOUS le verrou d'action."""
    import urllib.error
    import urllib.parse
    import urllib.request
    try:
        url = f"http://127.0.0.1:{PORT_REGARD}/carte/cible?" + urllib.parse.urlencode({"id": identifiant})
        with urllib.request.urlopen(url, timeout=8) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            raison = json.load(e).get("erreur", str(e))
        except (ValueError, OSError):
            raison = str(e)
        raise SystemExit(f"✗ cible de la carte refusée : {raison}")
    except Exception as e:
        raise SystemExit(f"✗ cible de la carte inaccessible : {e}")
    try:
        element, fenetre = d["element"], d["fenetre"]
        wid = int(str(element.get("wid") or fenetre["wid"]), 0)
        actif, nom, _ = fenetre_active()
        if not actif or int(str(actif), 0) != wid:
            raise SystemExit(f"✗ cible dans une autre fenêtre ({hex(wid)}) ; active : « {nom} »"
                             " — active d'abord la fenêtre voulue, puis relis la carte")
        x, y, w, h = (element[k] for k in ("x", "y", "w", "h"))
        if not all(isinstance(v, (int, float)) for v in (x, y, w, h)) or w <= 0 or h <= 0:
            raise ValueError("géométrie invalide")
        x, y = int(x + w // 2), int(y + h // 2)
        largeur, hauteur = ecran_taille()
        if not (0 <= x < largeur and 0 <= y < hauteur):
            raise ValueError("centre hors écran")
        if element.get("id") != identifiant:
            raise ValueError("identité différente de la cible demandée")
        if "inactif" in element.get("etats", []):
            raise ValueError("élément inactif")
    except (KeyError, TypeError, ValueError, OverflowError) as e:
        raise SystemExit(f"✗ cible de la carte inutilisable : {e}")
    element = dict(element, wid=wid)
    return x, y, element, d


def verdict_carte(avant, attendre):
    """Rapporte uniquement les révisions lues, sans attribuer un changement à l'action."""
    if attendre:
        time.sleep(attendre)
    try:
        apres = lire_carte()
    except SystemExit as e:
        return [f"   → action envoyée ; relecture impossible : {e}"], None
    if avant.get("session") != apres.get("session"):
        texte = "service redémarré ; relis la nouvelle carte"
    elif avant.get("revision") == apres.get("revision"):
        texte = f"carte inchangée (révision {apres.get('revision', '?')})"
    else:
        texte = f"carte passée de la révision {avant.get('revision', '?')} à {apres.get('revision', '?')}"
    return [f"   → {texte} ; résultat de l'action non vérifié"], None


def verifier_apres_lecture_carte(ctx):
    """Le service peut prendre du temps : Félix reste prioritaire pendant sa lecture."""
    verifier_stop()
    courant = pointeur()
    if any(abs(a - b) > SEUIL for a, b in zip(courant, ctx.depart)):
        ecrire("stop", "[ecran] souris bougée par Félix pendant la lecture de la carte")
        raise SystemExit("⛔ ARRÊTÉ : souris bougée pendant la lecture ; rien n'est exécuté")


def verifier_fenetre_carte(carte):
    """Une frappe sans image reste dans la fenêtre réellement observée avant l'action."""
    attendue = carte.get("contexte", {}).get("fenetre_active")
    try:
        wid = int(str(attendue), 0)
        if wid <= 0:
            raise ValueError()
    except (TypeError, ValueError):
        raise SystemExit("✗ fenêtre active non décrite par la carte ; aucune frappe n'est envoyée")
    actif, nom, _ = fenetre_active()
    try:
        correspond = int(str(actif), 0) == wid
    except (TypeError, ValueError):
        correspond = False
    if not correspond:
        raise SystemExit(f"✗ fenêtre active changée depuis la carte ({hex(wid)}) : « {nom} »"
                         " — aucune frappe supplémentaire n'est envoyée")


def verifier_fenetre(motif):
    """`--dans MOTIF` : refuse d'agir si la fenêtre active n'est pas la bonne
    (un Return parti dans Mailspring au lieu du comptoir, c'est un mail envoyé)."""
    _, nom, _ = fenetre_active()
    if motif.lower() not in (nom or "").lower():
        raise SystemExit(f"✗ fenêtre active « {nom} » ≠ « {motif} » : rien n'est envoyé"
                         f"\n  → `ecran fenetre \"{motif}\"` d'abord")


# 09/11/2026 — « améliore pour éviter les frappes ratées » (Félix). Vécu le matin même : `cliquer @"FERMER"` a fermé la
# fenêtre Firefox pilotée ; la fenêtre active est devenue le VS Code de Félix et « France », Flèche bas, Entrée y sont
# partis. Deux règles désormais, pour `taper` et `touche` :
#   1. la FENÊTRE DE TRAVAIL = celle du dernier `voir`, du dernier clic ou de la dernière activation (`fenetre`) ; sans
#      `--dans`, on ne tape que là. Si la fenêtre active a changé entre-temps : refus, rien n'est envoyé.
#   2. les fenêtres de Félix (VS Code, Mailspring, Proton Mail Bridge, Codex, Agents) ne reçoivent JAMAIS de frappe,
#      sauf `--dans` qui les nomme — c'est la règle 4 d'AGENTS.md (lecture seule sauf demande explicite).
FENETRES_DE_FELIX = ("visual studio code", "mailspring", "proton mail bridge", "codex", "agents")


def noter_fenetre_travail(wid=None, nom=None, par="action"):
    """La fenêtre où le pilote vient de regarder ou d'agir devient la seule où une frappe est permise."""
    if wid is None:
        wid, nom, _ = fenetre_active()
    if not wid:
        return
    try:
        numero = int(str(wid), 0)
    except (TypeError, ValueError):
        return
    ecrire("fenetre_travail.json", json.dumps({"wid": numero, "nom": nom or "", "ts": time.time(), "par": par}))


def fenetre_travail():
    try:
        with open(chemin("fenetre_travail.json")) as f:
            d = json.load(f)
        return d if time.time() - float(d.get("ts", 0)) <= EXPIRATION_PILOTE else None
    except (OSError, ValueError):
        return None


def verifier_fenetre_felix(dans=None, nom=None):
    """Règle 4 : aucune frappe dans une fenêtre de Félix, sauf `--dans` qui la désigne (demande explicite)."""
    if nom is None:
        _, nom, _ = fenetre_active()
    nom_l = (nom or "").lower()
    if any(f in nom_l for f in FENETRES_DE_FELIX) and not (dans and dans.lower() in nom_l):
        raise SystemExit(f"✗ « {nom} » est une fenêtre de Félix : aucune frappe n'y est envoyée"
                         f" (règle 4) — `--dans \"{nom}\"` seulement sur sa demande explicite")


def verifier_fenetre_travail(action, dans=None):
    """Sans `--dans`, la frappe reste dans la fenêtre de travail (dernier voir / clic / activation, < 30 min)."""
    actif, nom, _ = fenetre_active()
    verifier_fenetre_felix(dans, nom)
    if dans:
        return                                   # `--dans MOTIF` est contrôlé par verifier_fenetre
    t = fenetre_travail()
    if not t:
        noter_fenetre_travail(actif, nom, action)
        return
    try:
        meme = bool(actif) and int(str(actif), 0) == int(t["wid"])
    except (TypeError, ValueError, KeyError):
        meme = False
    if not meme:
        raise SystemExit(f"✗ la fenêtre active « {nom} » n'est pas la fenêtre de travail « {t.get('nom')} »"
                         f" (dernier {t.get('par')}) : rien n'est tapé"
                         f"\n  → `ecran fenetre \"{t.get('nom')}\"` ou `ecran voir` d'abord")


MODIFICATEURS = {"ctrl", "shift", "alt", "super"}


def cmd_cliquer(args):
    verifier, attendre, voir, sans_capture, args = options_action(args)
    lent = "--lent" in args
    args = [a for a in args if a != "--lent"]
    avec, args = extraire_option(args, "--avec")
    options_valides(args, {"--double", "--droit", "--milieu", "--img", "--n", "--zone"}, "cliquer")
    if "--droit" in args and "--milieu" in args:
        raise SystemExit("✗ --droit et --milieu sont exclusifs")
    mods = avec.lower().split("+") if avec else []
    if any(m not in MODIFICATEURS for m in mods):
        raise SystemExit(f"✗ --avec : {avec} (attendu : ctrl, shift, alt, super, ou ctrl+shift…)")
    # `cliquer e12` : une référence du regard (service Rust) vaut `id:e12` ; jamais confondue avec un numéro de `voir`.
    args = [f"id:{a}" if re.fullmatch(r"e\d+", a) else a for a in args]
    stable = next((a for a in args if a.startswith("id:")), None)
    if stable:
        if not stable[3:] or any(a != stable and a not in ("--double", "--droit", "--milieu") for a in args):
            raise SystemExit("✗ cliquer id:<ID_CARTE> accepte --double, --droit, --milieu et --avec")
        sans_capture = sans_capture or not (voir or verifier)
        x, y, e = None, None, None
    else:
        if sans_capture and any(a.startswith("@") or re.fullmatch(r"t\d+", a) for a in args):
            raise SystemExit("✗ une référence OCR nécessite une capture ; utilise id:<ID_CARTE>")
        x, y, e = cible(args, 'ecran cliquer <id> | id:<ID_CARTE> | tN | @"texte" [--n K] | <x> <y>'
                          " [--double] [--droit] [--milieu] [--img] [--avec ctrl|shift]"
                          ' [--verifier "texte"] [--attendre S]')
    bouton = "3" if "--droit" in args else "2" if "--milieu" in args else "1"
    with agir(bouge=True, vers=None if stable else (x, y)) as ctx:
        fenetre_depart = fenetre_active()[0] if lent else None
        if lent and not fenetre_depart:
            raise SystemExit("✗ aucune fenêtre active pour le clic lent")
        if stable:
            x, y, e, avant_carte = resoudre_cible_carte(stable[3:])
        avant = (avant_carte if stable else lire_carte()) if sans_capture else capture_texte()
        if stable or sans_capture:
            verifier_apres_lecture_carte(ctx)
        if stable:
            actif, _, _ = fenetre_active()
            if not actif or int(str(actif), 0) != int(str(e["wid"]), 0):
                raise SystemExit("✗ fenêtre active changée avant le clic ; rien n'est exécuté")
            ctx.vers = (x, y)
        if lent:
            bouger_souris_lent(x, y, ctx)
        else:
            bouger_souris(x, y)
        time.sleep(0.05)
        if lent:
            verifier_position_souris((x, y))
            if stable:
                # Le survol peut ouvrir un menu ou déplacer le bouton : ne pas
                # cliquer sur une géométrie vérifiée deux secondes auparavant.
                nx, ny, nouvel_element, _ = resoudre_cible_carte(stable[3:])
                verifier_position_souris((x, y))
                if (nx, ny) != (x, y) or any(nouvel_element.get(k) != e.get(k)
                                              for k in ("wid", "role", "nom", "texte", "etats")):
                    raise SystemExit("✗ cible changée pendant l'approche lente ; relis la carte avant de cliquer")
            if fenetre_active()[0] != fenetre_depart:
                raise SystemExit("✗ fenêtre active changée pendant l'approche lente ; aucun clic envoyé")
        verifier_stop()
        # Modificateurs relâchés dans TOUS les cas : une touche restée
        # enfoncée après `ecran` resterait bloquée sur le clavier de Félix.
        try:
            for m in mods:
                xdotool("keydown", m)
            for numero in range(2 if "--double" in args else 1):
                if numero:
                    time.sleep(0.08)
                verifier_stop()
                if lent:
                    verifier_position_souris((x, y))
                    if fenetre_active()[0] != fenetre_depart:
                        raise SystemExit("✗ fenêtre active changée ; aucun clic supplémentaire envoyé")
                xdotool("click", bouton)
        finally:
            for m in reversed(mods):
                sh("xdotool", "keyup", m, ok=False)
    noter_fenetre_travail(par="clic")              # la fenêtre où l'on vient de cliquer est celle où l'on peut taper
    quoi = (e["desc"] if e and "desc" in e else f"{e['role']} « {e['nom']} »" if e
            else "coordonnées")
    lignes, ok = verdict_carte(avant, attendre) if sans_capture else verdict(avant, attendre, verifier, voir=voir)
    conclure(f"✓ clic{' double' if '--double' in args else ''}"
             f"{' droit' if bouton == '3' else ''}{' avec ' + avec if avec else ''}"
             f" sur {quoi} en {x},{y}", lignes, ok)


def cmd_captcha(args):
    """09/11/2026 — Félix : « pour les captcha, envoie-moi une notification, je vais le lire ». Le pilote ne devine plus un
    code de sécurité à l'œil : il découpe la zone, l'agrandit ×3 et l'envoie sur le téléphone de Félix (ntfy, pièce jointe).
    Félix répond dans la conversation ; l'agent tape alors le code avec `ecran taper`. Lecture seule : aucun clic."""
    vals = [a for a in args if not a.startswith("--")]
    message, _ = extraire_option(args, "--message")
    if len(vals) != 4:
        raise SystemExit('✗ usage : ecran captcha X Y L H [--message "…"]  (zone écran du code, en pixels)')
    try:
        zone = tuple(int(v) for v in vals)
    except ValueError:
        raise SystemExit("✗ X Y L H doivent être des entiers")
    if zone[2] <= 0 or zone[3] <= 0:
        raise SystemExit("✗ largeur et hauteur doivent être positives")
    from PIL import Image
    img = capture_texte(zone).convert("RGB")
    img = img.resize((img.width * 3, img.height * 3), Image.LANCZOS)
    png = chemin("captcha.png")
    img.save(png)
    texte = message or "Code de securite a lire : reponds-le dans la conversation Claude."
    if os.path.realpath(ETAT) != os.path.realpath(ETAT_REEL):
        print(f"✓ captcha découpé : {png} ({img.width}×{img.height}) — (test : pas d'envoi ntfy)")
        return
    try:
        import urllib.request
        topic = open(os.path.expanduser("~/.jarvis/ntfy_topic")).read().strip()
        with open(png, "rb") as f:
            corps = f.read()
        urllib.request.urlopen(urllib.request.Request(
            f"https://ntfy.sh/{topic}", data=corps, method="PUT",
            headers={"Title": "JARVIS - captcha a lire", "Filename": "captcha.png",
                     "Message": texte.encode("ascii", "replace").decode(), "Priority": "high", "Tags": "eyes"}),
            timeout=15)
    except Exception as e:
        raise SystemExit(f"✗ envoi ntfy impossible ({e}) — l'image est dans {png}")
    print(f"✓ captcha envoyé à Félix (ntfy, {img.width}×{img.height}) : {png}\n"
          "  → attends sa réponse dans la conversation, puis `ecran taper \"<code>\"` dans le champ.")


def presse_papiers(texte):
    """Charge le presse-papiers dans une unité systemd À PART.

    Deux pièges : (1) `xclip` sans `-quiet` forke et garde les tubes hérités
    ouverts → `subprocess.run(capture_output=True)` attend un EOF qui ne vient
    jamais ; (2) `ecran` tourne dans un service transitoire dont le cgroup est
    tué à la sortie — le propriétaire de la sélection mourrait avec lui et le
    Ctrl+V collerait du vide."""
    fichier = chemin("coller.txt")
    # 0600 et effacé dès la sélection chargée : il gardait le dernier texte
    # collé en clair, lisible par tous, indéfiniment (constat du 25/09).
    fd = os.open(fichier, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(texte)
    subprocess.run(["systemctl", "--user", "stop", PP_UNITE], capture_output=True)
    subprocess.run(["systemctl", "--user", "reset-failed", PP_UNITE], capture_output=True)
    subprocess.run(["systemd-run", "--user", "--quiet", "--collect",
                    f"--unit={PP_UNITE}", f"--setenv=DISPLAY={DISPLAY}",
                    f"--setenv=XAUTHORITY={XAUTH}",
                    "xclip", "-quiet", "-selection", "clipboard", "-i", fichier],
                   capture_output=True)
    try:
        for _ in range(20):                  # ≤ 2 s : sélection disponible ?
            if sh("xclip", "-selection", "clipboard", "-o", ok=False, delai=3) == texte.strip():
                return
            time.sleep(0.1)
        raise SystemExit("✗ presse-papiers non chargé (xclip) — rien n'a été collé")
    finally:
        effacer("coller.txt")


def cmd_taper(args):
    verifier, attendre, voir, sans_capture, args = options_action(args)
    dans, args = extraire_option(args, "--dans")
    options_valides(args, {"--coller", "--stdin", "--entree"}, "taper")
    mots = [a for a in args if not a.startswith("--")]
    if "--stdin" in args:
        if sys.stdin.isatty():
            raise SystemExit("✗ --stdin attend le texte sur l'entrée standard (tube ou"
                             " <<'EOF'), pas un terminal : la commande attendrait sans fin")
        texte = sys.stdin.read()
    else:
        texte = " ".join(mots)
    if not texte:
        raise SystemExit("✗ rien à taper (texte en argument, ou `--stdin`)")
    colle = "--coller" in args or len(texte) > 400 or "\n" in texte
    # --dans @"texte" : cliquer d'abord dans ce champ (trouvé à l'instant) ;
    # --dans MOTIF : exiger que la fenêtre active porte ce titre.
    if sans_capture and dans and dans.startswith("@"):
        raise SystemExit("✗ --dans @texte nécessite une capture ; clique d'abord l'identifiant de la carte")
    champ = resoudre_texte(dans) if dans and dans.startswith("@") else None
    with agir(bouge=bool(champ), vers=champ[:2] if champ else None) as ctx, frappe_en_cours("taper"):
        if dans and not champ:
            verifier_fenetre(dans)
        if not sans_capture:                      # sans capture, la carte épingle déjà la fenêtre (verifier_fenetre_carte)
            verifier_fenetre_travail("taper", dans if (dans and not champ) else None)
        avant = lire_carte() if sans_capture else capture_texte()
        if sans_capture:
            verifier_apres_lecture_carte(ctx)
            verifier_fenetre_carte(avant)
            if dans:
                verifier_fenetre(dans)
        if champ:
            bouger_souris(champ[0], champ[1])
            time.sleep(0.05)
            xdotool("click", "1")
            time.sleep(0.15)
        if colle:
            # Long / multiligne : presse-papiers + Ctrl+V (xdotool type
            # entrelace ou perd des caractères au-delà de quelques centaines).
            presse_papiers(texte)
            time.sleep(0.05)
            if sans_capture:
                verifier_apres_lecture_carte(ctx)
                verifier_fenetre_carte(avant)
            xdotool("key", "--clearmodifiers", "ctrl+v")
        else:
            # 29/10/2026 : sur le clavier fr (latin9), `xdotool type` rend « @ » par « à » (il trouve la touche à/0
            # mais oublie AltGr ; vécu sur Synology Drive, PayPal, iCloud) → « @ » envoyé à part, AltGr + à.
            morceaux = texte.split("@")
            for i, morceau in enumerate(morceaux):
                if morceau:
                    xdotool("type", "--clearmodifiers", "--delay", "2", "--", morceau)
                if i < len(morceaux) - 1:
                    xdotool("key", "--clearmodifiers", "ISO_Level3_Shift+agrave")
        if "--entree" in args:
            time.sleep(0.1)
            if os.path.exists(chemin("stop")):
                raise SystemExit("⛔ ARRÊTÉ avant Entrée : texte tapé, RIEN n'est envoyé")
            if sans_capture:
                verifier_fenetre_carte(avant)
            xdotool("key", "--clearmodifiers", "Return")
    lignes, ok = verdict_carte(avant, attendre) if sans_capture else verdict(avant, attendre, verifier, voir=voir)
    conclure(f"✓ tapé {len(texte)} caractère(s)"
             + (f" dans {champ[2]}" if champ else "")
             + (" puis Entrée" if "--entree" in args else "")
             + (" (collé — le presse-papiers de Félix a été remplacé ;"
                " dans un terminal c'est ctrl+shift+v)" if colle else ""), lignes, ok)


def cmd_touche(args):
    verifier, attendre, voir, sans_capture, args = options_action(args)
    dans, args = extraire_option(args, "--dans")
    if not args:
        raise SystemExit("✗ usage : ecran touche ctrl+l [Tab] [Return] … [--dans MOTIF]")
    for k in args:
        if k.startswith("-"):
            raise SystemExit(f"✗ « {k} » n'est pas une touche (xdotool le prendrait"
                             " pour une option)")
    envoyees = []
    with agir() as ctx, frappe_en_cours("touche"):
        if not sans_capture:
            verifier_fenetre_travail("touche", dans)
        avant = lire_carte() if sans_capture else capture_texte()
        if sans_capture:
            verifier_apres_lecture_carte(ctx)
        for k in args:
            # Un arrêt souris pendant la série doit couper la boucle : sinon un
            # `Return` part encore et valide un formulaire (constat de Codex).
            if envoyees and (os.path.exists(chemin("stop")) or ctx.pointeur_devie()):
                raise SystemExit(f"⛔ ARRÊTÉ pendant la frappe — envoyées :"
                                 f" {' '.join(envoyees)} ; abandon de"
                                 f" {' '.join(args[len(envoyees):])}")
            if sans_capture:
                try:
                    verifier_fenetre_carte(avant)
                except SystemExit as e:
                    raise SystemExit(f"{e}\n  envoyées : {' '.join(envoyees) or 'aucune'}")
            if dans:                          # la fenêtre peut changer en cours de série
                try:
                    verifier_fenetre(dans)
                except SystemExit as e:
                    raise SystemExit(f"{e}\n  envoyées : {' '.join(envoyees) or 'aucune'}")
            # xdotool sort avec 0 même sur un nom de touche inconnu : sans
            # lire stderr, un « ✓ » mensonger ferait croire à un écran changé.
            _, err = sh("xdotool", "key", "--clearmodifiers", "--delay", "60", k,
                        stderr_aussi=True)
            if "No such key name" in err:
                raise SystemExit(f"✗ touche inconnue : {k} ({err.splitlines()[0]})"
                                 f"\n  envoyées : {' '.join(envoyees) or 'aucune'}"
                                 f"\n  non envoyées : {' '.join(args[len(envoyees) + 1:]) or 'aucune'}")
            envoyees.append(k)
    lignes, ok = verdict_carte(avant, attendre) if sans_capture else verdict(avant, attendre, verifier, voir=voir)
    conclure(f"✓ touche(s) : {' '.join(envoyees)}", lignes, ok)


def cmd_defiler(args):
    verifier, attendre, voir, sans_capture, args = options_action(args)
    options_valides(args, {"--a"}, "defiler")
    sens = args[0] if args else "bas"
    if sens not in ("bas", "haut"):
        raise SystemExit("✗ usage : ecran defiler bas|haut [N] [--a X Y | tN | @\"texte\"]")
    n = 5
    pos = None
    reste = args[1:]
    if reste and not reste[0].startswith("--"):
        n = entier(reste[0], "N (crans)", 1, MAX_DEFILEMENT)
        reste = reste[1:]
    if "--a" in reste:
        i = reste.index("--a")
        suite = reste[i + 1:]
        if sans_capture and any(a.startswith("@") or re.fullmatch(r"t\d+", a) for a in suite):
            raise SystemExit("✗ une référence OCR nécessite une capture")
        if suite and (suite[0].startswith("@") or (suite[0][:1] == "t" and suite[0][1:].isdigit())):
            pos = cible(suite[:1], "ecran defiler bas|haut [N] --a tN|@texte")[:2]
        else:
            if len(suite) < 2:
                raise SystemExit("✗ --a attend X Y, tN ou @\"texte\"")
            pos = cible(suite[:2], "ecran defiler bas|haut [N] --a X Y")[:2]
    with agir(bouge=bool(pos), vers=pos) as ctx:
        avant = lire_carte() if sans_capture else capture_texte()
        if sans_capture:
            verifier_apres_lecture_carte(ctx)
        if pos:
            bouger_souris(*pos)
        xdotool("click", "--repeat", str(n), "--delay", "40", "5" if sens == "bas" else "4")
    lignes, ok = verdict_carte(avant, attendre) if sans_capture else verdict(avant, attendre, verifier, voir=voir)
    conclure(f"✓ défilé {sens} ×{n}" + (f" en {pos[0]},{pos[1]}" if pos else ""), lignes, ok)


def cmd_souris(args):
    lent = "--lent" in args
    args = [a for a in args if a != "--lent"]
    options_valides(args, {"--img", "--n", "--zone"}, "souris")
    x, y, _ = cible(args, 'ecran souris X Y [--img] | tN | @"texte" [--n K] [--lent]')
    with agir(bouge=True, vers=(x, y)) as ctx:
        if lent:
            bouger_souris_lent(x, y, ctx)
        else:
            bouger_souris(x, y)
    print(f"✓ souris en {x},{y}")


def lire_par_ocr(zone, refs, autour):
    """`lire tN | --zone | @"ancre" [--autour LxH]` : texte de la zone, OCR en
    mode bloc (psm 6, l'ordre de lecture est gardé)."""
    largeur, hauteur = ecran_taille()
    ref = refs[0] if refs else None
    if ref and ref.startswith("@"):
        l, h = 1000, 200
        if autour:
            try:
                l, h = (int(v) for v in autour.lower().split("x"))
            except ValueError:
                raise SystemExit("✗ --autour attend LARGEURxHAUTEUR, ex. --autour 1200x300")
        with agir(lecture=True):
            res, _ = trouver(ref[1:].strip())
        if not res:
            sortir(f"✗ ancre « {ref[1:]} » introuvable à l'écran", INTROUVABLE)
        if len(res) > 1:
            sortir(f"✗ ancre « {ref[1:]} » ambiguë ({len(res)} occurrences) — prends une"
                   " zone : `lire --zone X Y L H`\n"
                   + "\n".join(afficher_occurrence(k, o) for k, o in enumerate(res, 1)),
                   INTROUVABLE)
        cx, cy = res[0]["cx"], res[0]["cy"]
        x, y = max(0, cx - l // 2), max(0, cy - h // 2)
        zone = (x, y, min(l, largeur - x), min(h, hauteur - y))
    elif ref:
        try:
            d = json.load(open(chemin("texte.json")))
        except (OSError, ValueError):
            raise SystemExit("✗ pas de lignes lues : lance d'abord `ecran texte` ou `ecran voir`")
        li = next((x for x in d.get("lignes", []) if x.get("id") == ref), None)
        if li is None:
            raise SystemExit(f"✗ ligne {ref} inconnue")
        x, y = max(0, li["x"] - 6), max(0, li["y"] - 4)
        zone = (x, y, min(li["l"] + 12, largeur - x), min(li["h"] + 8, hauteur - y))
    with agir(lecture=True):
        _, mots, lignes = lire_ecran(zone, psm=6)
    print(ligne_etat())
    conf = round(sum(m["conf"] for m in mots) / len(mots)) if mots else 0
    print(f"[OCR psm 6, zone {zone[2]}×{zone[3]} à {zone[0]},{zone[1]}, {len(lignes)} lignes,"
          f" conf moy {conf}]")
    for li in lignes:
        print(li["texte"])
    if not lignes:
        sys.exit(INTROUVABLE)


def lire_regard(ref):
    """`ecran lire e42` : le texte COMPLET d'une référence du regard (terminal, page de document, mail, champ),
    par l'interface Texte de l'objet ou le DOM de la page — sans image. Lecture seule, sans réservation."""
    import urllib.error
    import urllib.parse
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT_REGARD}/texte?" + urllib.parse.urlencode({"id": ref}), timeout=20) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            raison = json.load(e).get("erreur", str(e))
        except (ValueError, OSError):
            raison = str(e)
        raise SystemExit(f"✗ lire {ref} : {raison}")
    except Exception as e:
        raise SystemExit(f"✗ regard inaccessible (127.0.0.1:{PORT_REGARD}) : {e}")
    tete = f"{d.get('role', '?')} « {d.get('nom', '')} » {ref} — {d.get('longueur', 0)} caractères (source {d.get('source', '?')})"
    if d.get("tronque"):
        tete += " — TRONQUÉ"
    if d.get("note"):
        tete += f" — {d['note']}"
    print(tete)
    print(d.get("texte", ""))


def cmd_lire(args):
    if not args:
        raise SystemExit('✗ usage : ecran lire <id> | eN | tN | --zone X Y L H | @"ancre" [--autour LxH]')
    if re.fullmatch(r"e\d+", args[0]):
        return lire_regard(args[0])
    largeur, hauteur = ecran_taille()
    zone, reste = extraire_zone(args, largeur, hauteur)
    autour, reste = extraire_option(reste, "--autour")
    options_valides(reste, set(), "lire")
    refs = [a for a in reste if not a.startswith("--")]
    if zone or (refs and (refs[0].startswith("@") or
                          (refs[0][:1] == "t" and refs[0][1:].isdigit()))):
        if zone and refs:
            raise SystemExit("✗ --zone OU une référence (tN, @texte), pas les deux")
        return lire_par_ocr(zone, refs, autour)
    if autour:
        raise SystemExit('✗ --autour ne s\'utilise qu\'avec @"ancre"')
    args = refs
    d = charger_elements()
    e = next((e for e in d["elements"] if str(e["id"]) == args[0]), None)
    if e is None:
        raise SystemExit(f"✗ élément {args[0]} inconnu")
    Atspi = atspi()
    with agir(lecture=True):
        fenetres, _ = choisir_fenetres_atspi(Atspi, True, None)
        texte = None
        # On retrouve l'élément par ses extents/rôle/nom (les handles AT-SPI ne
        # survivent pas d'un processus à l'autre). Plusieurs nœuds imbriqués
        # partagent souvent la même boîte (viewport, vue texte, paragraphe) :
        # on ne s'arrête pas au premier, on garde le texte le plus complet.
        for _, _, w, wid_w in fenetres:
            coord, dx, dy = repere(Atspi, w, wid_w)
            file = [(w, 0)]
            deadline = time.monotonic() + DELAI_ATSPI
            while file and time.monotonic() < deadline:
                acc, prof = file.pop(0)
                try:
                    comp = acc.get_component_iface()
                    r = comp.get_extents(coord) if comp else None
                    role = acc.get_role_name()
                    if (r and (r.x + dx, r.y + dy, r.width, r.height) == (e["x"], e["y"], e["w"], e["h"])
                            and (acc.get_name() or "") == e["nom"]
                            and role == e.get("role_atspi", role)):
                        t = acc.get_text_iface()
                        trouve = ""
                        if t:
                            n = Atspi.Text.get_character_count(t)
                            trouve = Atspi.Text.get_text(t, 0, min(n, 20000))
                        # Texte fait uniquement d'objets incorporés (U+FFFC, pages web) : les NOMS des enfants
                        # portent le contenu lisible (audit bugs-13).
                        if not trouve.replace("\ufffc", "").strip():
                            morceaux = []
                            pile = [acc]
                            while pile and len(morceaux) < 2000:
                                a = pile.pop(0)
                                nm = a.get_name() or ""
                                if nm:
                                    morceaux.append(nm)
                                for k in range(min(a.get_child_count(), 200)):
                                    c = a.get_child_at_index(k)
                                    if c is not None:
                                        pile.append(c)
                            trouve = "\n".join(morceaux)
                        if trouve and (texte is None or len(trouve) > len(texte)):
                            texte = trouve
                    if prof < 30 and (prof == 0 or acc.get_state_set().contains(Atspi.StateType.SHOWING)):
                        for k in range(min(acc.get_child_count(), 500)):
                            c = acc.get_child_at_index(k)
                            if c is not None:
                                file.append((c, prof + 1))
                except Exception:
                    continue
            if texte:
                break
    if texte is None:
        raise SystemExit("✗ élément introuvable (écran changé ?) — refais `ecran voir`")
    print(texte)


# ───────────────────────── texte (OCR pleine résolution) ─────────────────────────
@mesurer("capture")
def capture_texte(zone=None):
    """Capture PLEINE RÉSOLUTION (PIL/xcb, 18 ms) de `zone` ou de l'écran.

    PILOTE_CAPTURE_FIXE (tests seulement) remplace l'écran par une image : elle
    n'est honorée QUE si le dossier d'état n'est pas le vrai, sans quoi un
    agent pourrait cliquer sur l'écran réel d'après une image inventée."""
    fixe = os.environ.get("PILOTE_CAPTURE_FIXE")
    if fixe and os.path.realpath(ETAT) != os.path.realpath(f"/run/user/{os.getuid()}/pilote-ecran"):
        from PIL import Image
        img = Image.open(fixe).convert("RGB")
        if zone:
            img = img.crop((zone[0], zone[1], zone[0] + zone[2], zone[1] + zone[3]))
        return img
    import perception
    try:
        return perception.capturer(zone, DISPLAY)
    except Exception as e:
        raise SystemExit(f"✗ capture impossible : {e}")


def retirer_cadre(mots):
    """Enlève les lignes qui sont l'étiquette du cadre orange/rouge."""
    import perception as P
    garde = []
    for li in P.lignes(mots):
        n = P.normaliser(li["texte"])
        if not any(s in n for s in SIGNATURES_CADRE):
            garde += li["mots"]
    return garde


@mesurer("ocr")
def lire_ecran(zone=None, img=None, conf=None, psm=11):
    """(image, mots, lignes numérotées tN) de `zone` (écran entier par défaut)."""
    import perception as P
    if img is None:
        img = capture_texte(zone)
    mots = retirer_cadre(P.ocr(img, origine=zone[:2] if zone else (0, 0),
                               conf_min=conf or P.CONF_MIN, psm=psm))
    lignes = P.lignes(mots)
    for k, li in enumerate(lignes, 1):
        li["id"] = f"t{k}"
    return img, mots, lignes


def sans_mots(li):
    return {k: v for k, v in li.items() if k != "mots"}


def memoriser_texte(img, zone, mots, lignes, ts=None):
    """texte.json + l'image de référence : `cliquer tN` vérifie ensuite que la
    ligne est toujours là, au même endroit, avant de cliquer."""
    img.save(chemin("texte.png"), compress_level=1)
    ecrire("texte.json", json.dumps(
        {"ts": time.time() if ts is None else ts, "zone": list(zone) if zone else None, "image": "texte.png",
         "agent": agent_courant(), "lignes": [sans_mots(li) for li in lignes], "mots": mots},
        ensure_ascii=False))


def extraire_zone(args, largeur, hauteur):
    """Retire `--zone X Y L H` de args → (zone|None, reste)."""
    if "--zone" not in args:
        return None, list(args)
    i = args.index("--zone")
    vals = args[i + 1:i + 5]
    if len(vals) != 4 or any(v.startswith("--") for v in vals):
        raise SystemExit("✗ --zone attend 4 entiers : --zone X Y LARGEUR HAUTEUR")
    zx = entier(vals[0], "--zone X", 0, largeur - 1)
    zy = entier(vals[1], "--zone Y", 0, hauteur - 1)
    zw = entier(vals[2], "--zone LARGEUR", 1, largeur)
    zh = entier(vals[3], "--zone HAUTEUR", 1, hauteur)
    zone = (zx, zy, min(zw, largeur - zx), min(zh, hauteur - zy))
    if (zw, zh) != (zone[2], zone[3]):
        print(f"⚠ zone rognée à l'écran : {zone[2]}×{zone[3]}", file=sys.stderr)
    return zone, args[:i] + args[i + 5:]


def extraire_option(args, nom, conv=str):
    """Retire `nom VALEUR` de args → (valeur|None, reste)."""
    if nom not in args:
        return None, list(args)
    i = args.index(nom)
    if i + 1 >= len(args) or args[i + 1].startswith("--"):
        raise SystemExit(f"✗ {nom} attend une valeur")
    return conv(args[i + 1]), args[:i] + args[i + 2:]


def afficher_ligne(li):
    return (f"{li['id']:>5}  « {li['texte']} »  centre {li['cx']},{li['cy']}"
            f"  {li['l']}×{li['h']}  conf {li['conf']}")


def cmd_texte(args):
    largeur, hauteur = ecran_taille()
    zone, reste = extraire_zone(args, largeur, hauteur)
    conf, reste = extraire_option(reste, "--conf", lambda v: entier(v, "--conf", 0, 100))
    options_valides(reste, {"--fenetre-active", "--json"}, "texte")
    if [a for a in reste if not a.startswith("--")]:
        raise SystemExit(f"✗ argument inattendu : {reste[0]}")
    if "--fenetre-active" in reste:
        if zone:
            raise SystemExit("✗ --zone et --fenetre-active sont exclusifs")
        wid, _, _ = fenetre_active()
        g = geometrie_x11(wid) if wid else None
        if g:
            x, y = max(0, g[0]), max(0, g[1])
            zone = (x, y, min(g[2] - (x - g[0]), largeur - x), min(g[3] - (y - g[1]), hauteur - y))
    t0 = time.monotonic()
    with agir(lecture=True):
        img, mots, lignes = lire_ecran(zone, conf=conf)
        memoriser_texte(img, zone, mots, lignes)
    if "--json" in reste:
        print(json.dumps({"etat": ligne_etat(), "zone": zone,
                          "lignes": [sans_mots(li) for li in lignes]}, ensure_ascii=False))
    else:
        print(ligne_etat())
        ou = f"zone {zone[2]}×{zone[3]} à {zone[0]},{zone[1]}" if zone else f"écran {largeur}×{hauteur}"
        print(f"Texte lu ({ou}) — {len(lignes)} lignes, {len(mots)} mots en"
              f" {time.monotonic() - t0:.1f} s ; `cliquer tN`, `lire tN`")
        for li in lignes:
            print(afficher_ligne(li))
    if not lignes:
        sys.exit(INTROUVABLE)


def elements_frais():
    """Liste AT-SPI de `voir` si elle a moins de SEUIL_VIEUX s ET vient de la
    fenêtre active ; sinon None."""
    try:
        d = json.load(open(chemin("elements.json")))
        if time.time() - d["ts"] > SEUIL_VIEUX or not d.get("wid"):
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    # Une action APRÈS ce `voir` a pu changer l'écran : la liste ne vaut plus
    # pour chercher un texte (26/09, comptoir : « Enregistrer le mot de passe ? »
    # encore « trouvé » dans la liste alors que le clic l'avait fermé).
    try:
        if os.path.getmtime(chemin("avant.png")) > d["ts"]:
            return None
    except OSError:
        pass
    wid_actif, _, _ = fenetre_active()
    if not wid_actif or int(d["wid"]) != int(wid_actif):
        return None
    return d


def trouver(motif, zone=None, exact=False):
    """Où est `motif` ? AT-SPI d'abord (liste de `voir` fraîche, fenêtre active,
    nom identique), sinon capture pleine résolution + OCR à l'instant.
    → (occurrences, mots OCR ou None)."""
    import perception as P
    d = elements_frais()
    if d:
        cible_n = motif if exact else P.normaliser(motif)
        res = []
        for e in d["elements"]:
            nom = e["nom"] if exact else P.normaliser(e["nom"])
            if e["nom"] and nom == cible_n and (not zone or (
                    zone[0] <= e["x"] + e["w"] // 2 < zone[0] + zone[2]
                    and zone[1] <= e["y"] + e["h"] // 2 < zone[1] + zone[3])):
                res.append({"texte": e["nom"], "x": e["x"], "y": e["y"], "l": e["w"],
                            "h": e["h"], "cx": e["x"] + e["w"] // 2,
                            "cy": e["y"] + e["h"] // 2, "conf": 100,
                            "source": f"atspi #{e['id']} {e['role']}"})
        if res:
            return res, None
    _, mots, _ = lire_ecran(zone)
    return P.chercher(mots, motif, exact), mots


def afficher_occurrence(k, o):
    return (f"  @{k}  « {o['texte']} »  centre {o['cx']},{o['cy']}"
            f"  boîte {o['x']},{o['y']} {o['l']}×{o['h']}  conf {o['conf']}  [{o['source']}]")


def cmd_chercher(args):
    largeur, hauteur = ecran_taille()
    zone, reste = extraire_zone(args, largeur, hauteur)
    options_valides(reste, {"--exact", "--json"}, "chercher")
    motifs = [a for a in reste if not a.startswith("--")]
    if not motifs:
        raise SystemExit('✗ usage : ecran chercher "texte" ["texte2"…] [--zone X Y L H] [--exact]')
    import perception as P
    exact = "--exact" in reste
    resultats, mots = {}, None
    with agir(lecture=True):
        for m in motifs:
            if mots is not None:              # une seule capture + un seul OCR pour tous
                resultats[m] = (P.chercher(mots, m, exact), mots)
            else:
                res, mots_lus = trouver(m, zone, exact)
                mots = mots_lus
                resultats[m] = (res, mots_lus)
    manque = False
    if "--json" in reste:
        print(json.dumps({"etat": ligne_etat(), "resultats": {m: r for m, (r, _) in resultats.items()}},
                         ensure_ascii=False))
        manque = any(not r for r, _ in resultats.values())
    else:
        print(ligne_etat())
        for m, (res, mots_m) in resultats.items():
            if res:
                print(f"« {m} » : {len(res)} occurrence(s)"
                      + ("  → `cliquer @\"…\" --n K` pour choisir" if len(res) > 1 else ""))
                for k, o in enumerate(res, 1):
                    print(afficher_occurrence(k, o))
            else:
                manque = True
                print(f"« {m} » : INTROUVABLE à l'écran")
                if mots_m is None:
                    _, mots_m, _ = lire_ecran(zone)
                for li in P.proches(mots_m, m):
                    print(f"     proche : « {li['texte']} » centre {li['cx']},{li['cy']}")
    if manque:
        sys.exit(INTROUVABLE)


def resoudre_texte(ref, n=None, zone=None):
    """`tN` (ligne de `texte`/`voir`) ou `@texte` (cherché dans `zone` si
    donnée) → (x, y, description).
    Une ligne tN n'est cliquée que si elle est TOUJOURS là : la même zone est
    recapturée et comparée à l'image de référence."""
    import perception as P
    if ref.startswith("@"):
        motif = ref[1:].strip()
        if not motif:
            raise SystemExit('✗ @ attend un texte : cliquer @"Nouveau passage"')
        with agir(lecture=True):
            res, _ = trouver(motif, zone)
        if not res:
            sortir(f"✗ « {motif} » introuvable à l'écran — `ecran chercher \"{motif}\"`"
                   " montre les lignes proches", INTROUVABLE)
        if len(res) > 1 and n is None:
            sortir(f"✗ « {motif} » ambigu : {len(res)} occurrences — précise `--n K` :\n"
                   + "\n".join(afficher_occurrence(k, o) for k, o in enumerate(res, 1)),
                   INTROUVABLE)
        k = n or 1
        if not 1 <= k <= len(res):
            sortir(f"✗ --n {k} : seulement {len(res)} occurrence(s)", INTROUVABLE)
        o = res[k - 1]
        return o["cx"], o["cy"], f"« {o['texte']} » [{o['source']}]"
    try:
        d = json.load(open(chemin("texte.json")))
    except (OSError, ValueError):
        raise SystemExit("✗ pas de lignes lues : lance d'abord `ecran texte` ou `ecran voir`")
    if time.time() - d.get("ts", 0) > SEUIL_VIEUX:
        raise SystemExit(f"✗ lignes lues il y a {int(time.time() - d['ts'])} s : relis"
                         " (`ecran texte`) — ou `cliquer @\"texte\"`")
    li = next((l for l in d.get("lignes", []) if l.get("id") == ref), None)
    if li is None:
        raise SystemExit(f"✗ ligne {ref} inconnue (t1…t{len(d.get('lignes', []))})")
    from PIL import Image
    ox, oy = (d["zone"][0], d["zone"][1]) if d.get("zone") else (0, 0)
    ref_img = Image.open(chemin(d["image"])).convert("RGB")
    x0, y0 = max(li["x"] - 4, ox), max(li["y"] - 4, oy)
    x1 = min(li["x"] + li["l"] + 4, ox + ref_img.width)
    y1 = min(li["y"] + li["h"] + 4, oy + ref_img.height)
    avant = ref_img.crop((x0 - ox, y0 - oy, x1 - ox, y1 - oy))
    maintenant = capture_texte((x0, y0, x1 - x0, y1 - y0))
    if P.diff(avant, maintenant)["pct"] > 3:
        raise SystemExit(f"✗ la ligne {ref} « {li['texte']} » a changé ou bougé depuis la"
                         " lecture — relis (`ecran texte`) ou `cliquer @\"texte\"`")
    return li["cx"], li["cy"], f"ligne {ref} « {li['texte']} » [ocr]"


# ───────────────────────── decrire (Qwen local, lot 7) ─────────────────────────
LLM_URL = os.environ.get("PILOTE_LLM_URL", "http://127.0.0.1:8016/v1")
CONSIGNE_DECRIRE = (
    "Tu regardes une capture d'écran d'un poste de travail (pharmacie). Réponds en français, en 3 à 6 lignes courtes. "
    "Recopie les textes visibles EXACTEMENT, entre « », sans les corriger. Ne donne JAMAIS de coordonnées, de position "
    "en pixels ni de boîte : seulement ce que tu vois et où, en mots (en haut, à droite…). Si tu ne vois pas quelque "
    "chose, dis-le ; n'invente rien.")


def verdict_question(texte):
    """Premier mot OUI / NON / INCERTAIN de la réponse, ou « INCERTAIN » s'il manque."""
    m = re.match(r"\s*\**\s*(OUI|NON|INCERTAIN)\b", texte or "", re.I)
    return m.group(1).upper() if m else "INCERTAIN"


def citations(texte):
    """Textes cités « … » par le modèle (au moins 3 caractères)."""
    return [c.strip() for c in re.findall(r"«\s*([^»]{3,120}?)\s*»", texte or "")]


def confirmer_citations(texte, mots):
    """Chaque texte cité doit se RETROUVER dans l'OCR de la même image : le modèle décrit bien, mais peut inventer
    (mesuré le 25/09 : une cible absente « trouvée » avec assurance)."""
    import perception as P
    res = []
    for c in citations(texte):
        o = P.chercher(mots, c)
        res.append((c, (o[0]["cx"], o[0]["cy"]) if o else None))
    return res


def contradiction(question, verdict, texte, mots):
    """Le texte VISÉ par la question (cité par le modèle ET présent dans la question) est cherché par l'OCR : un NON
    alors qu'il est lu, ou un OUI alors qu'il ne l'est pas, est signalé — c'est l'OCR qui arbitre."""
    import perception as P
    q = P.normaliser(question or "")
    vises = [c for c in citations(texte) if P.normaliser(c) and P.normaliser(c) in q]
    alertes = []
    for c in vises:
        trouve = P.chercher(mots, c)
        if verdict == "NON" and trouve:
            alertes.append(f"⚠ Qwen dit NON, mais l'OCR lit « {trouve[0]['texte']} » en {trouve[0]['cx']},{trouve[0]['cy']}"
                           " — vérifie (autre sens du mot ?)")
        elif verdict == "OUI" and not trouve:
            alertes.append(f"⚠ Qwen dit OUI, mais l'OCR ne lit « {c} » nulle part dans la zone — ne le crois pas sur parole")
    return alertes


def cmd_decrire(args):
    largeur, hauteur = ecran_taille()
    zone, reste = extraire_zone(args, largeur, hauteur)
    question, reste = extraire_option(reste, "--question")
    maxt, reste = extraire_option(reste, "--max-tokens", lambda v: entier(v, "--max-tokens", 50, 4000))
    options_valides(reste, {"--fenetre-active", "--reflechir", "--json"}, "decrire")
    if "--fenetre-active" in reste and not zone:
        wid, _, _ = fenetre_active()
        g = geometrie_x11(wid) if wid else None
        if g:
            x, y = max(0, g[0]), max(0, g[1])
            zone = (x, y, min(g[2] - (x - g[0]), largeur - x), min(g[3] - (y - g[1]), hauteur - y))
    reflechir = "--reflechir" in reste
    with agir(lecture=True):
        img = capture_texte(zone)
    from PIL import Image
    import base64
    import io
    import urllib.request
    petite = img.copy()
    petite.thumbnail((1600, 1600), Image.LANCZOS)
    buf = io.BytesIO()
    petite.save(buf, "JPEG", quality=85)
    url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    consigne = CONSIGNE_DECRIRE
    demande = f"Image de {petite.width}×{petite.height} pixels. "
    if question:
        demande += (f"Question : {question} Réponds d'abord par OUI, NON ou INCERTAIN (premier mot), puis une phrase qui "
                    "cite entre « » le texte visible qui le justifie.")
    else:
        demande += "Décris ce qui est à l'écran : fenêtre, titre, zones, boutons, messages, montants."
    corps = {"model": "jarvis-llm", "temperature": 0, "max_tokens": maxt or (1200 if reflechir else 300),
             "messages": [{"role": "system", "content": consigne},
                          {"role": "user", "content": [{"type": "text", "text": demande},
                                                       {"type": "image_url", "image_url": {"url": url}}]}],
             "chat_template_kwargs": {"enable_thinking": reflechir}}
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(f"{LLM_URL}/chat/completions", data=json.dumps(corps).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=90 if reflechir else 30) as r:
            rep_ = json.loads(r.read())
        texte = ((rep_.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    except Exception as e:                                  # noqa: BLE001
        raise SystemExit(f"✗ modèle local injoignable ({LLM_URL}) : {type(e).__name__} — utilise `ecran texte` ou `ecran lire --zone`")
    texte = re.sub(r"<think>.*?</think>", "", texte, flags=re.S).strip()
    _, mots, _ = lire_ecran(zone, img=img)
    conf = confirmer_citations(texte, mots)
    verdict = verdict_question(texte) if question else None
    if "--json" in reste:
        print(json.dumps({"etat": ligne_etat(), "source": "Qwen local (jarvis-llm)", "verdict": verdict, "texte": texte,
                          "citations": [{"texte": c, "confirme": bool(p), "centre": p} for c, p in conf]},
                         ensure_ascii=False))
        return
    print(ligne_etat())
    print(f"source : Qwen local (jarvis-llm), {time.monotonic() - t0:.1f} s — une DESCRIPTION, à confirmer par lecture ;"
          " jamais une coordonnée à cliquer")
    if verdict:
        print(f"VERDICT : {verdict}")
    print(texte)
    for a in contradiction(question, verdict, texte, mots) if question else []:
        print(a)
    for c, p in conf:
        print(f"   « {c} » " + (f"[confirmé OCR {p[0]},{p[1]}]" if p else "[NON retrouvé à l'écran]"))


def cmd_fenetres(_args):
    wid, nom_actif, _ = fenetre_active()
    haut = empilement()
    visibles = []
    for w, bureau, nom in fenetres_x():
        reduite = "_NET_WM_STATE_HIDDEN" in sh("xprop", "-id", w, "_NET_WM_STATE", ok=False)
        visibles.append((w, nom, reduite))
    # « au-dessus » parmi les fenêtres NON réduites : une fenêtre réduite n'est au-dessus de rien (audit atspi-14).
    haut = [x for x in haut if not any(int(w, 16) == x and r for w, _, r in visibles)]
    for w, nom, reduite in visibles:
        actif = "▶" if wid and int(w, 16) == int(wid) else " "
        rang = haut.index(int(w, 16)) if int(w, 16) in haut else -1
        dessus = " (au-dessus)" if rang == len(haut) - 1 and not reduite else ""
        print(f"{actif} {w}  {nom}{dessus}{' (réduite)' if reduite else ''}")


def cmd_fenetre(args):
    """Active une fenêtre par titre ou par id X11 (0x…).

    Priorité : id exact > titre exact > correspondance unique > la plus haute
    dans la pile (avec avertissement) : `cands[0]` prenait la plus ANCIENNE,
    et deux fenêtres au même titre en rendaient une inatteignable."""
    if not args:
        raise SystemExit("✗ usage : ecran fenetre MOTIF | 0xID [--deplacer DX DY]")
    deplacer = None
    if "--deplacer" in args:                 # 09/10 : déplacement relatif, en pixels, après activation (expérience du 1 cm)
        i = args.index("--deplacer")
        if len(args) < i + 3:
            raise SystemExit("✗ --deplacer attend DX DY (pixels, relatifs)")
        deplacer = (entier(args[i + 1], "DX", -5000, 5000), entier(args[i + 2], "DY", -5000, 5000))
        args = args[:i] + args[i + 3:]
    motif = " ".join(args)
    toutes = fenetres_x()
    if motif.lower().startswith("0x"):
        cible_id = int(motif, 16)
        cands = [(w, nom) for w, _, nom in toutes if int(w, 16) == cible_id]
        if not cands:
            raise SystemExit(f"✗ aucune fenêtre d'id {motif} (voir `ecran fenetres`)")
    else:
        m = motif.lower()
        exact = [(w, nom) for w, _, nom in toutes if nom.lower() == m]
        contient = [(w, nom) for w, _, nom in toutes if m in nom.lower()]
        cands = exact or contient
        if not cands:
            raise SystemExit(f"✗ aucune fenêtre ne contient « {motif} »"
                             " (voir `ecran fenetres`)")
        if len(cands) > 1:
            haut = empilement()
            cands.sort(key=lambda c: haut.index(int(c[0], 16))
                       if int(c[0], 16) in haut else -1)
            liste = ", ".join(f"{w} « {n} »" for w, n in cands)
            print(f"⚠ {len(cands)} fenêtres correspondent ({liste}) — la plus haute"
                  " est activée ; précise un id 0x… si ce n'est pas la bonne",
                  file=sys.stderr)
            cands = [cands[-1]]
    with agir():
        ok, nom_apres = activer_fenetre(cands[0][0])
    if not ok:
        # 26/09 : « ✓ fenêtre activée » alors qu'AUCUNE fenêtre n'était active (_NET_ACTIVE_WINDOW vide) :
        # les touches suivantes partaient ailleurs. Désormais : échec explicite.
        raise SystemExit(f"✗ focus NON obtenu pour « {cands[0][1]} » ({cands[0][0]}) — fenêtre active :"
                         f" « {nom_apres or 'aucune'} ». Clique dans la fenêtre (zone neutre) puis vérifie `ecran etat`.")
    print(f"✓ fenêtre activée : {cands[0][1]} ({cands[0][0]})")
    noter_fenetre_travail(int(cands[0][0], 16), cands[0][1], "fenetre")
    if deplacer:
        wid_dec = str(int(cands[0][0], 16))
        avant = geometrie_x11(wid_dec)
        with agir():
            xdotool("windowmove", "--relative", wid_dec, str(deplacer[0]), str(deplacer[1]), ok=False)
        time.sleep(0.3)
        apres = geometrie_x11(wid_dec)
        if avant and apres:
            print(f"✓ fenêtre déplacée : x {avant[0]}→{apres[0]}, y {avant[1]}→{apres[1]} ({apres[2]}×{apres[3]})")
        else:
            print("✓ déplacement demandé (géométrie non relue)")


def activer_fenetre(wid_hex):
    """Donne le focus à la fenêtre et le VÉRIFIE : wmctrl, puis xdotool, puis lever/amener au bureau courant.
    Rend (réussi, nom de la fenêtre active à la fin)."""
    cible = int(str(wid_hex), 16) if str(wid_hex).lower().startswith("0x") else int(wid_hex)
    nom = ""
    for methode in (["wmctrl", "-i", "-a", hex(cible)], ["xdotool", "windowactivate", str(cible)],
                    ["wmctrl", "-i", "-R", hex(cible)]):
        sh(*methode, ok=False)
        for _ in range(10):
            time.sleep(0.1)
            a, nom, _ = fenetre_active()
            if a and int(a) == cible:
                return True, nom
    return False, nom


def cmd_lancer(args):
    """Lance une appli dans une unité systemd nommée (elle survit à `ecran`,
    qui tourne lui-même dans un service éphémère)."""
    if not args:
        raise SystemExit("✗ usage : ecran lancer CMD [ARGS…]")
    unite = f"pilote-app-{os.path.basename(args[0])[:32]}-{int(time.time())}"
    avant = {w for w, _, _ in fenetres_x()}
    with agir():
        r = subprocess.run(["systemd-run", "--user", "--quiet", "--collect",
                            f"--unit={unite}",
                            f"--setenv=DISPLAY={DISPLAY}", f"--setenv=XAUTHORITY={XAUTH}",
                            "--", *args], capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(f"✗ lancement : {r.stderr.strip()}")
        nouvelles = []
        for _ in range(40):                  # ≤ 4 s : une fenêtre GTK met du temps
            nouvelles = [n for w, _, n in fenetres_x() if w not in avant]
            if nouvelles:
                break
            time.sleep(0.1)
    # `systemd-run` rend 0 dès que le job est accepté : une appli qui meurt
    # aussitôt (bibliothèque manquante, option refusée) donnait « ✓ lancé ».
    etat = subprocess.run(["systemctl", "--user", "is-active", unite],
                          capture_output=True, text=True).stdout.strip()
    if etat not in ("active", "activating"):
        journal = subprocess.run(["journalctl", "--user", "-u", unite, "-n", "5",
                                  "--no-pager", "-o", "cat"],
                                 capture_output=True, text=True).stdout.strip()
        raise SystemExit(f"✗ « {' '.join(args)} » s'est arrêté aussitôt ({etat})"
                         + (f"\n{journal}" if journal else ""))
    print(f"✓ lancé : {' '.join(args)}  [unité {unite}]")
    if nouvelles:
        print("  nouvelle(s) fenêtre(s) : " + ", ".join(f"« {n} »" for n in nouvelles)
              + "\n  → `ecran fenetre <titre>` avant d'agir (le focus n'est pas garanti)")
    else:
        print("  ⚠ aucune fenêtre nouvelle après 1,5 s — `ecran fenetres` pour vérifier")


def rafraichir_actif():
    """Pendant une longue attente, le DÉTENTEUR de l'écran reste « actif » :
    sans ça le cadre s'effaçait et la réservation expirait pendant que Charli
    réfléchissait. Un observateur ne prolonge jamais la réservation d'autrui."""
    if os.path.exists(chemin("stop")):
        return
    p = pilote_en_cours()
    if p and p[0] == agent_courant():
        ecrire("actif", str(time.time()))


def cmd_attendre(args):
    """`attendre S` (pause) ou jusqu'à une condition : --texte (apparu),
    --disparu, --stable S (écran immobile S s). Plusieurs conditions = toutes.
    Lecture seule : permis sous STOP, ne prend pas le verrou entre deux tours."""
    largeur, hauteur = ecran_taille()
    zone, reste = extraire_zone(args, largeur, hauteur)
    texte, reste = extraire_option(reste, "--texte")
    disparu, reste = extraire_option(reste, "--disparu")
    immobile, reste = extraire_option(reste, "--stable",
                                      lambda v: float(entier(v, "--stable (s)", 1, 60)))
    maxi, reste = extraire_option(reste, "--max", lambda v: entier(v, "--max (s)", 1, 600))
    pas, reste = extraire_option(reste, "--pas", lambda v: entier(v, "--pas (ms)", 200, 10000) / 1000)
    options_valides(reste, set(), "attendre")
    libres = [a for a in reste if not a.startswith("--")]
    if not (texte or disparu or immobile):
        if zone or maxi or pas:
            raise SystemExit("✗ --zone/--max/--pas vont avec --texte, --disparu ou --stable")
        s_ = float(entier(libres[0], "durée (s)", 0, 60)) if libres else 1.0
        time.sleep(s_)
        print(f"✓ attendu {s_:g} s")
        return
    if libres:
        raise SystemExit("✗ durée ET condition : la durée maximale se donne par --max S")
    import perception as P
    maxi, pas = maxi or 60, pas or 0.5
    debut = time.monotonic()
    prec, immobile_depuis, mots, vu = None, None, [], None
    while True:
        rafraichir_actif()
        img = capture_texte(zone)
        conditions = []
        if texte or disparu:
            _, mots, _ = lire_ecran(zone, img=img)
        if texte:
            vu = P.chercher(mots, texte)
            conditions.append(bool(vu))
        if disparu:
            conditions.append(not P.chercher(mots, disparu))
        if immobile:
            maintenant = time.monotonic()
            if prec is not None and P.diff(prec, img, exclure=zones_ignorees(img),
                                           origine=zone[:2] if zone else (0, 0))["pct"] < 0.05:
                immobile_depuis = immobile_depuis or t_prec
            else:
                immobile_depuis = None
            prec, t_prec = img, maintenant
            conditions.append(bool(immobile_depuis) and maintenant - immobile_depuis >= immobile)
        duree = time.monotonic() - debut
        if all(conditions):
            quoi = []
            if texte:
                quoi.append(f"« {texte} » vu en {vu[0]['cx']},{vu[0]['cy']}")
            if disparu:
                quoi.append(f"« {disparu} » disparu")
            if immobile:
                quoi.append(f"écran immobile depuis {immobile:g} s")
            print(f"✓ {' ; '.join(quoi)} — après {duree:.1f} s")
            return
        if duree >= maxi:
            print(f"✗ condition non remplie en {maxi} s"
                  + (f" : « {texte} » pas vu" if texte and not vu else "")
                  + (f" : « {disparu} » toujours là" if disparu and P.chercher(mots, disparu) else "")
                  + (" : l'écran bouge encore" if immobile and not immobile_depuis else ""))
            if texte and mots:
                for li in P.proches(mots, texte):
                    print(f"     proche : « {li['texte']} » centre {li['cx']},{li['cy']}")
            sys.exit(INTROUVABLE)
        time.sleep(pas)


def cmd_diff(args):
    """Ce qui a changé depuis la capture d'AVANT la dernière action (avant.png),
    y compris ce qui est arrivé après coup (réponse de Charli…)."""
    largeur, hauteur = ecran_taille()
    zone, reste = extraire_zone(args, largeur, hauteur)
    options_valides(reste, {"--texte", "--json"}, "diff")
    import perception as P
    from PIL import Image
    try:
        avant = Image.open(chemin("avant.png")).convert("RGB")
    except OSError:
        raise SystemExit("✗ pas de capture « avant » : aucune action récente")
    age = time.time() - os.path.getmtime(chemin("avant.png"))
    with agir(lecture=True):
        maintenant = capture_texte()
    origine = (0, 0)
    exclure = zones_ignorees(avant)
    if zone:
        boite = (zone[0], zone[1], zone[0] + zone[2], zone[1] + zone[3])
        avant, maintenant, origine = avant.crop(boite), maintenant.crop(boite), zone[:2]
    d = P.diff(avant, maintenant, exclure=exclure, origine=origine)
    changes = []
    if "--texte" in reste:
        for x, y, l, h in d["zones"][:5]:
            # Zone élargie à la ligne : un mot changé se lit dans sa phrase.
            # Marge verticale ≥ 20 px : à 6 px (26/09 matin), tesseract perdait
            # « Compté » et le chiffre d'un texte de 24 px (mesuré).
            mv = max(20, h)
            bx0, by0 = max(origine[0], x - 300), max(origine[1], y - mv)
            bx1 = min(origine[0] + avant.width, x + l + 300)
            by1 = min(origine[1] + avant.height, y + h + mv)
            cadre_ = (bx0 - origine[0], by0 - origine[1], bx1 - origine[0], by1 - origine[1])
            lire_l = lambda im: {P.normaliser(li["texte"]): li["texte"] for li in
                                 P.lignes(P.ocr(im.crop(cadre_), origine=(bx0, by0)))}
            a, b = lire_l(avant), lire_l(maintenant)
            changes += [("+", b[k]) for k in b if k not in a] + [("−", a[k]) for k in a if k not in b]
    if "--json" in reste:
        print(json.dumps({"etat": ligne_etat(), "depuis_s": round(age, 1), **d,
                          "texte": [{"sens": s_, "texte": t} for s_, t in changes]},
                         ensure_ascii=False))
        return
    print(ligne_etat())
    if d["zones"]:
        print(f"changement {d['pct']:.1f} % depuis la capture d'il y a {age:.0f} s — "
              f"{len(d['zones'])} zone(s) : "
              + " ; ".join(f"{x},{y} {l}×{h}" for x, y, l, h in d["zones"][:5]))
    else:
        print(f"aucun changement visible depuis la capture d'il y a {age:.0f} s")
    for sens_, t in changes:
        print(f"  {sens_} « {t} »")


def cmd_etat(_args):
    actif_ = cadre_actif() and cadre_pret()
    print(f"cadre : {'actif' if actif_ else 'absent'}" + (f" · vigie : {etat_vigie()}" if actif_ else ""))
    p = pilote_en_cours()
    if p:
        print(f"écran réservé par : « {p[0]} » (inactif depuis {int(p[1])} s)"
              + ("  ← c'est toi" if p[0] == agent_courant() else "  ← PAS toi"))
    if os.path.exists(chemin("stop")):
        print("⛔ STOP : " + open(chemin("stop")).read().strip()
              + "\n   (seul `ecran reprendre`, après le GO de Félix, le lève)")
    if os.path.exists(chemin("occupe")):
        print("action en cours : " + ("oui" if occupe_valide() else "non (fichier orphelin)"))
    try:
        d = json.load(open(chemin("elements.json")))
        print(f"dernière vue : « {d.get('fenetre')} », {len(d.get('elements', []))}"
              f" éléments, il y a {int(time.time() - d.get('ts', 0))} s")
    except (OSError, ValueError, TypeError):
        print("dernière vue : aucune")
    try:
        wid, nom, _ = fenetre_active()
        x, y = pointeur()
        print(f"fenêtre active : « {nom} » — souris {x},{y}")
    except SystemExit as e:                  # X injoignable : `etat` reste utile (réservation, STOP)
        print(f"fenêtre active : ? (X11 injoignable : {str(e)[:80]})")


def cmd_prendre(args):
    """Réserve l'écran pour cet agent jusqu'à `ecran stop`."""
    nom = " ".join(args).strip() or agent_courant()
    with verrou_exclusif():
        p = pilote_en_cours()
        if p and p[0] != nom and p[0] != agent_courant():
            raise SystemExit(f"⛔ « {p[0]} » pilote déjà depuis {int(p[1])} s")
        os.makedirs(ETAT, exist_ok=True)
        ecrire("pilote", json.dumps({"agent": nom, "pid": os.getpid(),
                                     "ts": time.time()}))
        ecrire("actif", str(time.time()))
    print(f"✓ écran réservé par « {nom} » — libère-le avec `ecran stop`")
    if nom != agent_courant():
        print(f"  (exporte PILOTE_AGENT={nom} pour que tes commandes soient reconnues)")


def cmd_rendre(_args):
    with verrou_exclusif():
        p = pilote_en_cours()
        if p and p[0] != agent_courant():
            raise SystemExit(f"⛔ l'écran est à « {p[0]} », pas à toi")
        effacer("pilote")
    print("✓ écran rendu")


def cmd_stop(args):
    options_valides(args, {"--forcer"}, "stop")
    # Sous verrou : sans lui, `stop` retirait le cadre et effaçait `occupe`
    # pendant qu'une autre commande injectait encore des événements.
    with verrou_exclusif():
        p = pilote_en_cours()
        if p and p[0] != agent_courant():
            if "--forcer" not in args:
                sortir(f"⛔ l'écran est réservé par « {p[0]} » : `stop` ne libère pas la réservation d'un autre agent"
                       " (audit bugs-07). Si c'est voulu et justifié : `ecran stop --forcer` (journalisé, Félix prévenu).",
                       RESERVE)
            journaliser(commande="stop --forcer", pilote_evince=p[0])
            notifier_felix("Pilote écran : stop forcé", f"{agent_courant()} a libéré de force l'écran réservé par {p[0]}.")
        cadre_arreter()
        effacer("occupe", "actif", "pilote")
        subprocess.run(["systemctl", "--user", "stop", PP_UNITE], capture_output=True)
    print("✓ pilotage terminé, cadre retiré")
    if os.path.exists(chemin("stop")):
        print("⛔ un arrêt souris reste en vigueur : la prochaine action sera"
              " refusée jusqu'à `ecran reprendre`")


def cmd_reprendre(args):
    """Lève un STOP — SEULEMENT après le GO de Félix. Ordre sûr (audit securite-reprendre) : réservation d'autrui
    refusée ; pointeur immobile 0,5 s (Félix n'est pas en train d'utiliser la souris) ; `stop` effacé ; cadre
    relancé (s'il ne démarre pas, le STOP est reposé) ; trace dans le journal ; Félix prévenu (ntfy silencieux)."""
    raison_go, args = extraire_option(args, "--raison")
    options_valides(args, set(), "reprendre")
    with verrou_exclusif():
        p = pilote_en_cours()
        if p and p[0] != agent_courant():
            sortir(f"⛔ l'écran est réservé par « {p[0]} » : ce n'est pas à toi de lever son arrêt.", RESERVE)
        ancien = open(chemin("stop")).read().strip() if os.path.exists(chemin("stop")) else ""
        if stop_reel():
            sortir("⛔ un STOP est posé sur le vrai dossier d'état : `reprendre` doit s'y faire, pas dans PILOTE_ETAT.", ARRETE)
        try:
            a = pointeur()
            time.sleep(0.5)
            b = pointeur()
        except SystemExit:
            a = b = (0, 0)
        if abs(a[0] - b[0]) > 3 or abs(a[1] - b[1]) > 3:
            sortir("⛔ la souris bouge en ce moment (Félix l'utilise ?) : STOP maintenu, réessaie quand elle est immobile.",
                   ARRETE)
        effacer("stop", "souris", "occupe")
        cadre_arreter()
    try:
        with agir():
            pass
    except SystemExit:
        ecrire("stop", "[ecran] cadre non démarré après `reprendre` : STOP reposé")
        raise
    journaliser(commande="reprendre", stop_leve=ancien, raison=raison_go or "")
    if ancien:
        notifier_felix("Pilote écran : STOP levé", f"{agent_courant()} a repris la main à {time.strftime('%H:%M')}"
                       + (f" ({raison_go})" if raison_go else "") + f". Arrêt levé : {ancien[:160]}")
    print("✓ garde-fou réarmé, cadre affiché" + (f" — STOP levé : {ancien}" if ancien else ""))


def cmd_journal(args):
    """Les dernières actions des agents : `ecran journal [N] [--stop]`."""
    n = 20
    libres = [a for a in args if not a.startswith("--")]
    if libres:
        n = entier(libres[0], "N", 1, 1000)
    options_valides(args, {"--stop"}, "journal")
    try:
        lignes = open(journal_chemin()).read().splitlines()
    except OSError:
        lignes = []
    if "--stop" in args:
        lignes = [l for l in lignes if '"reprendre"' in l or "stop" in l]
    for l in lignes[-n:]:
        try:
            d = json.loads(l)
        except ValueError:
            continue
        reste = {k: v for k, v in d.items() if k not in ("ts", "agent", "commande")}
        print(f"{d.get('ts')}  {d.get('agent'):<8} {d.get('commande')}  " + json.dumps(reste, ensure_ascii=False))
    if os.path.exists(chemin("stop")):
        print("⛔ STOP en vigueur : " + open(chemin("stop")).read().strip())


def veille_etat(brut):
    """`ecran veille --etat` : l'état instantané écrit par le service (etat.json), en quelques millisecondes."""
    try:
        with open(chemin("etat.json"), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        raise SystemExit(f"✗ pas d'état instantané ({e}) — service : `systemctl --user status pilote-ecran-veille`")
    if brut:
        print(json.dumps(d, ensure_ascii=False))
        return
    fa = d.get("fenetre_active") or {}
    print(f"état à {d.get('ts', '')[-8:]}")
    print("fenêtre active : " + (f"{fa.get('app') or '?'} « {fa.get('titre', '')} »" + (f" (pid {fa['pid']})" if fa.get("pid") else "")
                                 if fa else "aucune"))
    fen = d.get("fenetres") or {}
    if fen:
        print(f"fenêtres ouvertes : {len(fen)}")
        for v in list(fen.values())[:14]:
            print(f"  - {v.get('app') or '?'} « {(v.get('titre') or '')[:50]} »" + (f" : {v['deduction']}" if v.get("deduction") else ""))
    f = d.get("focus")
    if f:
        print(f"focus : {f.get('role')} « {f.get('nom')} » ({f.get('app')}) à {(f.get('ts') or '')[-8:]}")
    e = d.get("ecran")
    if e:
        print(f"écran : {e.get('etat')} ({e.get('pct')} %)"
              + ((f" — FIGÉ depuis {e['fige_depuis_s']} s" if e.get("suspect") else f" — immobile depuis {e['fige_depuis_s']} s, rien ne se passe")
                 if e.get("fige_depuis_s") else ""))
    des = d.get("dessin") or {}
    if des:
        print("dessin : " + " ; ".join(f"{v.get('app') or '?'} « {(v.get('titre') or '')[:30]} » "
                                      + (f"en continu {v.get('taux')}/s" if v.get("etat") == "continu" else "actif") for v in des.values()))
    g = d.get("gpu")
    if g:
        procs = list((g.get("procs") or {}).values())
        print(f"GPU : {g.get('bande')} {g.get('util')} %, {g.get('mem')} Mo" + (f" ; {len(procs)} processus : " + ", ".join(procs[:6]) if procs else ""))
    a = d.get("audio") or {}
    print("audio : " + (", ".join(f"{k} {v}" for k, v in a.items()) if a else "silence")
          + (" ; micro : " + ", ".join(d.get("micro") or []) if d.get("micro") else ""))
    p = d.get("pilote") or {}
    print("pilote : " + (f"STOP ({p['stop']})" if p.get("stop") else "pas de STOP") + (f" ; réservé par {p['agent']}" if p.get("agent") else " ; écran libre"))
    pages = d.get("pages") or {}
    if pages:
        print("pages : " + " ; ".join(f"{k} « {(v or '')[:50]} »" for k, v in pages.items()))
    for t in (d.get("texte_recent") or [])[-5:]:
        print(f"  {(t.get('ts') or '')[-8:]} {t.get('app')} : « {(t.get('extrait') or t.get('alerte') or '')[:100]} »")
    if d.get("scene"):
        afficher_scene(d["scene"], "œil à " + (d["scene"].get("ts") or "")[-8:])
    for ev in (d.get("derniers") or [])[-8:]:
        print(f"  {(ev.get('ts') or '')[-8:]}  {ev.get('src', ''):<8} {ev.get('msg', '')}")


def afficher_scene(s, titre="œil"):
    print(f"{titre} ({s.get('type')}, {s.get('duree_ms')} ms, raison : {s.get('raison')}) : {s.get('scene')}")
    if s.get("evenement"):
        print(f"  évènement : {s['evenement']}")
    if s.get("alerte"):
        print(f"  ALERTE : {s['alerte']}")
    for e in s.get("elements") or []:
        print(f"  - {e.get('type')} « {e.get('libelle')} » ({e.get('zone')})" + (f" [{e['etat']}]" if e.get("etat") else ""))
    if s.get("texte_cle"):
        print("  textes clés : " + " | ".join(s["texte_cle"]))


def ligne_element_carte(e):
    rep = f"id:{e['id']} " if e.get("id") else ""
    ligne = f"{rep}[{e.get('x')},{e.get('y')} {e.get('w')}×{e.get('h')}] {e.get('role')}"
    if e.get("nom"):
        ligne += f" « {e['nom']} »"
    if e.get("texte"):
        ligne += f" = {e['texte']}"
    if e.get("etats"):
        ligne += " [" + ", ".join(e["etats"]) + "]"
    if e.get("identite") == "estimee":
        ligne += " [identité estimée]"
    return ligne


def heure_carte(ts):
    """Le snapshot porte une heure texte, les deltas un timestamp Unix."""
    if isinstance(ts, (int, float)):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        try:
            return datetime.fromtimestamp(ts, ZoneInfo("Europe/Brussels")).strftime("%H:%M:%S")
        except (ValueError, OverflowError, OSError):
            return "heure inconnue"
    return str(ts or "")[-8:]


def qualite_carte(f):
    fraicheur, couverture = f.get("fraicheur", {}), f.get("couverture", {})
    etat = "perime" if f.get("perime") else fraicheur.get("etat")
    libelles = {"observe": "observée", "perime": "périmée", "a_relire": "à relire",
                "indisponible": "indisponible", "partielle": "partielle", "declaration": "déclaration"}
    parties = []
    if etat:
        texte = "fraîcheur : " + libelles.get(etat, str(etat))
        if f.get("age_ms") is not None:
            texte += f" (âge {f['age_ms']} ms)"
        parties.append(texte)
    if couverture:
        texte = "couverture : " + libelles.get(couverture.get("etat"), str(couverture.get("etat", "inconnue")))
        if couverture.get("raison"):
            texte += " — " + str(couverture["raison"])
        if couverture.get("tronquee"):
            texte += " (tronquée)"
        parties.append(texte)
    return " ; ".join(parties)


def afficher_carte(d, lecture_ms=None):
    fen = d.get("fenetres", [])
    print(f"carte à {heure_carte(d.get('ts'))} — {len(fen)} fenêtre{'s' if len(fen) > 1 else ''}"
          + (f", lue en {lecture_ms} ms" if lecture_ms is not None else "")
          + (f" — révision {d['revision']}" if "revision" in d else "")
          + (" — resynchronisée" if d.get("resynchronise") else "")
          + (f" ({d['a_rafraichir']} en attente de relecture)" if d.get("a_rafraichir") else ""))
    for f in fen:
        pos = f"[{f.get('x')},{f.get('y')} {f.get('l')}×{f.get('h')}]" if f.get("l") else "[position inconnue]"
        print(f"\n== {f.get('app') or '?'} « {f.get('titre')} » {pos}{' (active)' if f.get('active') else ''}"
              f" — {f.get('deduction', {}).get('resume', '')}"
              f" — relue {f.get('ts')} en {f.get('lecture_ms')} ms" + (f" par {f['source']}" if f.get("source") else ""))
        if qualite_carte(f):
            print("  " + qualite_carte(f))
        if f.get("annonce"):
            a = f["annonce"]
            print(f"  déclare ({a.get('ts')}) : " + " | ".join(l for l in a.get("lignes", []) if l)
                  + (f"  — état : {json.dumps(a['etat'], ensure_ascii=False)}" if a.get("etat") else ""))
        for e in f.get("elements", []):
            print("  " + ligne_element_carte(e))


def afficher_changement_carte(d):
    prefixe = f"{heure_carte(d.get('ts'))} révision {d.get('revision')}"
    if d.get("type") == "fermeture":
        print(f"{prefixe} — fenêtre {d.get('wid')} fermée")
    elif d.get("type") == "contexte":
        print(f"{prefixe} — contexte : " + json.dumps(d.get("contexte", {}), ensure_ascii=False))
    else:
        f = d.get("fenetre", {})
        print(f"{prefixe} — fenêtre {d.get('wid')} « {f.get('titre', '')} »")
        if qualite_carte(f):
            print("  " + qualite_carte(f))
        for cle, marque in (("ajoutes", "+"), ("modifies", "~")):
            for e in d.get(cle, []):
                print(f"  {marque} " + ligne_element_carte(e))
        for identifiant in d.get("retires", []):
            print(f"  − id:{identifiant}")


def suivre_carte(brut, motif, secondes):
    """SSE local : instantané initial, puis deltas ; JSON = un événement par ligne."""
    import urllib.parse
    import urllib.request
    url = f"http://127.0.0.1:{PORT_REGARD}/carte/flux?" + urllib.parse.urlencode({"fenetre": motif, "duree": secondes})
    requete = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
    try:
        with urllib.request.urlopen(requete, timeout=8) as r:
            evenement, donnees = "message", []
            for brut_ligne in r:
                ligne = brut_ligne.decode("utf-8").rstrip("\r\n")
                if not ligne:
                    if donnees and evenement in ("instantane", "changement"):
                        d = json.loads("\n".join(donnees))
                        if brut:
                            print(json.dumps({"evenement": evenement, **d}, ensure_ascii=False), flush=True)
                        else:
                            if evenement == "instantane":
                                afficher_carte(d)
                            else:
                                afficher_changement_carte(d)
                            sys.stdout.flush()
                    evenement, donnees = "message", []
                elif ligne.startswith("event:"):
                    evenement = ligne[6:].lstrip(" ")
                elif ligne.startswith("data:"):
                    donnees.append(ligne[5:].lstrip(" "))
    except (OSError, ValueError, UnicodeError) as e:
        raise SystemExit(f"✗ flux de la carte interrompu : {e} — relance --carte --suivre pour te resynchroniser")


def veille_carte(brut, motif="", suivre=None):
    """`ecran veille --carte [--fenetre MOTIF]` : ce qu'il y a sur l'écran et à quel endroit, avec une déduction par
    fenêtre, lu en mémoire dans le service (tenu à jour sur événement) : quelques millisecondes."""
    if suivre is not None:
        return suivre_carte(brut, motif, suivre)
    import urllib.parse
    import urllib.request
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT_REGARD}/carte?" + urllib.parse.urlencode({"fenetre": motif}), timeout=8) as r:
            d = json.load(r)
    except Exception as e:
        raise SystemExit(f"✗ pont veille injoignable (127.0.0.1:{PORT_VEILLE}) : {e}")
    if brut:
        print(json.dumps(d, ensure_ascii=False))
        return
    afficher_carte(d, int((time.monotonic() - t0) * 1000))


def veille_oeil(brut):
    """`ecran veille --oeil` : l'anti-graphique, à la demande — Qwen vision (vLLM local) rend l'écran en scène structurée."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT_VEILLE}/oeil?raison=demande+ecran+veille", timeout=90) as r:
            d = json.load(r)
    except Exception as e:
        raise SystemExit(f"✗ pont veille injoignable (127.0.0.1:{PORT_VEILLE}) : {e}")
    if brut:
        print(json.dumps(d, ensure_ascii=False))
        return
    if d.get("erreur"):
        raise SystemExit("✗ " + d["erreur"] + (" — moteur : `systemctl --user start jarvis-vllm.service`" if "injoignable" in d["erreur"] else ""))
    afficher_scene(d)


def veille_texte(brut, tout=False):
    """`ecran veille --texte [--tout]` : la fenêtre active (ou tout l'écran) en lignes AT-SPI, lue par le service, sans image."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT_VEILLE}/texte" + ("?tout=1" if tout else ""), timeout=20) as r:
            d = json.load(r)
    except Exception as e:
        raise SystemExit(f"✗ pont veille injoignable (127.0.0.1:{PORT_VEILLE}) : {e}")
    if brut:
        print(json.dumps(d, ensure_ascii=False))
        return
    if d.get("erreur"):
        raise SystemExit("✗ " + d["erreur"])
    if tout:
        print(f"écran en mode texte à {d.get('ts', '')[-8:]} — fenêtre active « {d.get('fenetre_active')} »")
        for f in d.get("fenetres", []):
            print(f"\n== {f.get('app')} « {f.get('titre')} »{' (active)' if f.get('active') else ''} : {f.get('elements')} éléments"
                  + ("" if f.get("elements") else " — pas d'arbre"))
            for i, l in enumerate(f.get("lignes", []), 1):
                print(f"{i:>3} {l}")
        return
    print(f"fenêtre « {d.get('fenetre')} » (pid {d.get('pid')}) : {d.get('elements')} éléments AT-SPI"
          + ("" if d.get("elements") else " — pas d'arbre : `ecran texte --fenetre-active` (OCR) en secours"))
    for i, l in enumerate(d.get("lignes", []), 1):
        print(f"{i:>3} {l}")


def cmd_veille(args):
    """L'écran en ÉVÉNEMENTS (service `pilote-ecran-veille`, veille.py) : `ecran veille [N] [--src a,b] [--suivre [S]] [--json]`,
    `--etat` (état instantané), `--texte` (fenêtre active en lignes AT-SPI via le pont HTTP).
    Lecture seule : pas de réservation, pas de cadre."""
    n, srcs, suivre, brut, libres, motif = 30, None, None, "--json" in args, [], ""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--src" and i + 1 < len(args):
            srcs = {s.strip() for s in args[i + 1].split(",") if s.strip()}
            i += 2
            continue
        if a == "--fenetre" and i + 1 < len(args):
            motif = args[i + 1]
            i += 2
            continue
        if a == "--suivre":
            suivre = 60
            if i + 1 < len(args) and args[i + 1].isdigit():
                suivre = entier(args[i + 1], "S", 1, 600)
                i += 1
        elif a not in ("--json", "--etat", "--texte", "--tout", "--oeil", "--carte"):
            if a.startswith("--"):
                raise SystemExit(f"✗ option inconnue pour `veille` : {a}\n  connues : --carte --etat --fenetre --json --oeil --src --suivre --texte --tout")
            libres.append(a)
        i += 1
    if libres:
        n = entier(libres[0], "N", 1, 5000)
    if "--carte" in args:
        return veille_carte(brut, motif, suivre)
    if "--etat" in args:
        return veille_etat(brut)
    if "--texte" in args:
        return veille_texte(brut, "--tout" in args)
    if "--oeil" in args:
        return veille_oeil(brut)
    fichier = chemin("veille.jsonl")
    etat = subprocess.run(["systemctl", "--user", "is-active", "pilote-ecran-veille"],
                          capture_output=True, text=True).stdout.strip() or "?"
    if not os.path.exists(fichier):
        raise SystemExit(f"✗ pas de veille ({fichier} absent) ; service : {etat}"
                         " — `systemctl --user start pilote-ecran-veille`")

    def montrer(ligne):
        try:
            d = json.loads(ligne)
        except ValueError:
            return False
        if srcs and d.get("src") not in srcs:
            return False
        print(ligne if brut else f"{d.get('ts', '')[-8:]}  {d.get('src', ''):<8} {d.get('msg', '')}")
        return True

    with open(fichier, encoding="utf-8") as f:
        lignes = f.read().splitlines()
    retenues = []
    for l in lignes:
        try:
            d = json.loads(l)
        except ValueError:
            continue
        if not srcs or d.get("src") in srcs:
            retenues.append(l)
    print(f"service : {etat} · {len(lignes)} événements" + (f" ({len(retenues)} retenus)" if srcs else "") + f" · {fichier}")
    for l in retenues[-n:]:
        montrer(l)
    if not suivre:
        return
    pos = os.path.getsize(fichier)
    fin = time.time() + suivre
    print(f"… suivi pendant {suivre} s")
    while time.time() < fin:
        time.sleep(0.5)
        try:
            taille = os.path.getsize(fichier)
        except OSError:
            continue
        if taille < pos:                  # rotation : on repart du début
            pos = 0
        if taille > pos:
            with open(fichier, encoding="utf-8") as f:
                f.seek(pos)
                nouveau = f.read()
                pos = f.tell()
            for l in nouveau.splitlines():
                montrer(l)


def cmd_regard(args):
    """La page qu'un agent lit À LA PLACE d'une image (service Rust `regard`, 127.0.0.1:8793, 07/11/2026) :
    `ecran regard [--fenetre MOTIF] [--element eN] [--tout] [--lignes N] [--lecteur NOM] [--sans-memoire] [--json]`.
    Lecture seule : ni réservation, ni cadre, ni capture. Chaque lecteur garde la mémoire de son dernier regard : la
    page dit ce qui a changé POUR LUI (✚ nouveau, ✎ modifié, ✖ disparu). Les références `eN` se cliquent :
    `ecran cliquer e12` (vérification fraîche par le service, refus si la cible a bougé ou est recouverte)."""
    import urllib.error
    import urllib.parse
    import urllib.request
    q = {"lecteur": agent_courant()}
    brut = False
    attendre, disparu, maxi, pas = None, None, 60.0, 0.3
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--fenetre", "--element", "--lignes", "--lecteur") and i + 1 < len(args):
            q[a[2:]] = args[i + 1]
            i += 2
            continue
        if a in ("--attendre", "--disparu", "--max", "--pas") and i + 1 < len(args):
            if a == "--attendre":
                attendre = args[i + 1]
            elif a == "--disparu":
                disparu = args[i + 1]
            elif a == "--max":
                maxi = float(entier(args[i + 1], "S", 1, 3600))
            else:
                pas = entier(args[i + 1], "MS", 50, 5000) / 1000
            i += 2
            continue
        if a == "--tout":
            q["tout"] = "1"
        elif a == "--sans-memoire":
            q["memoriser"] = "0"
        elif a == "--json":
            brut = True
        elif a == "--oeil":
            # L'œil à la demande : capture + OCR + Qwen (vLLM local) sur la fenêtre, puis la page comme d'habitude.
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{PORT_REGARD}/oeil?" + urllib.parse.urlencode({"fenetre": q.get("fenetre", "")}), method="POST")
                with urllib.request.urlopen(req, timeout=75) as r:
                    d = json.load(r)
                print(f"œil sur « {d.get('titre')} » en {d.get('duree_ms', 0) / 1000:.1f} s : {d.get('lignes_ocr')} lignes lues, "
                      f"{d.get('elements_oeil')} éléments vus — {d.get('scene', '')}")
            except urllib.error.HTTPError as e:
                try:
                    raison = json.load(e).get("erreur", str(e))
                except (ValueError, OSError):
                    raison = str(e)
                raise SystemExit(f"✗ œil : {raison}")
            except Exception as e:
                raise SystemExit(f"✗ regard inaccessible (127.0.0.1:{PORT_REGARD}) : {e}")
        else:
            raise SystemExit(f"✗ regard : option inconnue {a}\n  usage : ecran regard [--fenetre MOTIF] [--element eN] [--tout] [--lignes N] [--lecteur NOM] [--sans-memoire] [--json]"
                             '\n          ecran regard --attendre "texte" | --disparu "texte" [--fenetre MOTIF] [--max S] [--pas MS]')
        i += 1
    if attendre or disparu:
        return regard_attendre(attendre, disparu, q.get("fenetre", ""), maxi, pas)
    if "lignes" in q:
        q["lignes"] = str(entier(q["lignes"], "N", 10, 2000))
    chemin_http = "/regard.json" if brut else "/regard"
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT_REGARD}{chemin_http}?" + urllib.parse.urlencode(q), timeout=15) as r:
            sys.stdout.write(r.read().decode("utf-8", "replace"))
            if not brut:
                sys.stdout.write("\n")
    except Exception as e:
        raise SystemExit(f"✗ regard inaccessible (127.0.0.1:{PORT_REGARD}, service pilote-ecran-regard) : {e}")


def regard_attendre(motif, disparu, fenetre, maxi, pas):
    """`ecran regard --attendre "Facture"` : attend qu'un élément (nom ou contenu) apparaisse dans la carte — ou
    disparaisse avec `--disparu` — sans OCR ni capture ; exit 2 si le délai passe. Remplace `attendre --texte`
    pour toute fenêtre lisible."""
    import urllib.parse
    import urllib.request
    cible = (motif or disparu).lower()
    debut = time.monotonic()
    derniere = None
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT_REGARD}/carte?" + urllib.parse.urlencode({"fenetre": fenetre}), timeout=8) as r:
                d = json.load(r)
        except Exception as e:
            raise SystemExit(f"✗ regard inaccessible (127.0.0.1:{PORT_REGARD}) : {e}")
        trouves = [(f, e) for f in d.get("fenetres", []) for e in f.get("elements", [])
                   if cible in (e.get("nom") or "").lower() or cible in (e.get("texte") or "").lower()]
        derniere = d.get("revision")
        if motif and trouves:
            f, e = trouves[0]
            print(f"✓ « {motif} » présent après {time.monotonic() - debut:.1f} s : {e.get('role')} « {e.get('nom')} » {e.get('id')}"
                  f" dans « {f.get('titre')} » (révision {derniere})")
            return
        if disparu and not trouves:
            print(f"✓ « {disparu} » absent après {time.monotonic() - debut:.1f} s (révision {derniere})")
            return
        if time.monotonic() - debut >= maxi:
            raise SystemExit(f"✗ « {motif or disparu} » {'toujours absent' if motif else 'toujours présent'} après {maxi:.0f} s (révision {derniere}) ; exit 2") if False else _sortie2(f"« {motif or disparu} » {'toujours absent' if motif else 'toujours présent'} après {maxi:.0f} s (révision {derniere})")
        time.sleep(pas)


def _sortie2(message):
    print(f"✗ {message}", file=sys.stderr)
    sys.exit(2)


COMMANDES = {
    "regard": cmd_regard,
    "voir": cmd_voir, "texte": cmd_texte, "chercher": cmd_chercher, "diff": cmd_diff, "decrire": cmd_decrire,
    "cliquer": cmd_cliquer, "taper": cmd_taper, "captcha": cmd_captcha,
    "touche": cmd_touche, "defiler": cmd_defiler, "souris": cmd_souris,
    "lire": cmd_lire, "fenetres": cmd_fenetres, "fenetre": cmd_fenetre,
    "lancer": cmd_lancer, "attendre": cmd_attendre, "etat": cmd_etat,
    "prendre": cmd_prendre, "rendre": cmd_rendre,
    "stop": cmd_stop, "reprendre": cmd_reprendre, "journal": cmd_journal, "veille": cmd_veille,
}
SANS_X = {"etat", "stop", "prendre", "rendre", "journal", "veille", "regard"}
# Commandes qui AGISSENT : journalisées (le texte tapé n'est jamais écrit, seulement sa longueur).
JOURNALISEES = {"cliquer", "taper", "touche", "defiler", "souris", "lancer", "fenetre", "prendre", "rendre", "stop"}


def main():
    # `ecran voir | head` : le tube fermé ne doit pas ressembler à un échec.
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    # RuntimeMaxSec / `systemctl stop` envoient SIGTERM : passer par SystemExit
    # pour que `agir.__exit__` libère `occupe` et le verrou.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit("✗ interrompu (SIGTERM)"))
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "aide"):
        print(__doc__.strip())
        return
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd not in COMMANDES:
        raise SystemExit(f"✗ commande inconnue : {cmd}\n\n{__doc__.strip()}")
    deconfiner()
    # PIL/xcb (capture pleine résolution) lit DISPLAY/XAUTHORITY du processus.
    os.environ["DISPLAY"] = DISPLAY
    os.environ.setdefault("XAUTHORITY", XAUTH)
    if cmd not in SANS_X:
        verifier_x()
    try:
        os.makedirs(ETAT, exist_ok=True)
    except OSError as e:
        raise SystemExit(f"✗ dossier d'état inutilisable ({ETAT}) : {e}")
    if cmd not in JOURNALISEES:
        executer_commande(cmd, args)
        return
    code = 0
    try:
        executer_commande(cmd, args)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
        raise
    finally:
        if cmd == "taper":
            libres = [a for a in args if not a.startswith("--")]
            trace = [a if a.startswith("--") else "" for a in args if a.startswith("--")] + [f"<{len(' '.join(libres))} caractères>"]
        else:
            trace = args
        try:
            _, fen, _ = fenetre_active()
        except SystemExit:
            fen = "?"
        journaliser(commande=cmd, args=trace, sortie=code, fenetre=fen)


if __name__ == "__main__":
    main()
