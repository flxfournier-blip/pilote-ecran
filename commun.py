"""commun.py — ce que `ecran` et `cadre.py` partagent : dossier d'état, écriture
atomique, pid vivant, validité de `occupe`.

Python pur (stdlib seule) : importé par /usr/bin/python3 (`ecran`, tests) ET par
le python PySide6 du venv charli-app (`cadre.py`). Avant le 26/09/2026 chaque
fichier avait sa copie de `pid_vivant`/`occupe_valide`, avec deux comportements
différents pour un fichier `occupe` orphelin ; ici il n'y en a qu'un.
"""
import os
import time

ETAT = os.environ.get("PILOTE_ETAT") or f"/run/user/{os.getuid()}/pilote-ecran"
AGE_OCCUPE = 30            # s : au-delà, le verrou d'action est réputé orphelin
# Réservation ET cadre (décision de Félix du 26/09 : cadre visible et souris armée pendant TOUTE la réservation) :
# 30 min d'inactivité avant qu'un pilote soit réputé parti (5 min avant, le cadre s'effaçait pendant qu'un agent
# réfléchissait et Félix reprenait la souris « de bonne foi »).
EXPIRATION_PILOTE = 1800
PAUSE_CADRE = 60           # s sans commande : le cadre passe en pointillé « réfléchit », toujours armé
SEUIL = 40                 # px de mouvement « humain » avant arrêt (ecran ET cadre)


def chemin(nom):
    return os.path.join(ETAT, nom)


def ecrire(nom, contenu):
    """Écriture atomique (fichier temporaire propre au processus, puis
    `os.replace`) : un lecteur ne voit jamais un fichier à moitié écrit,
    même quand trois agents partagent le même dossier d'état."""
    tmp = chemin(f"{nom}.tmp.{os.getpid()}")
    with open(tmp, "w") as f:
        f.write(contenu)
    os.replace(tmp, chemin(nom))


def effacer(*noms):
    for n in noms:
        try:
            os.remove(chemin(n))
        except OSError:
            pass


X11_PRISES = "/tmp/.X11-unix"


def affichage(display=None, prises=X11_PRISES):
    """L'affichage X11 VIVANT de l'utilisateur.

    Le numéro change quand la session graphique redémarre (08/11/2026 : `:0` →
    `:1` après la coupure de 10:16), et un DISPLAY hérité par un service lancé
    avant reste figé sur l'ancien : `regard` et `veille` ont tourné 3 h 30 sur
    « Connection refused », Charli pilotait un écran disparu. On garde DISPLAY
    s'il a encore sa prise `X<n>` ; sinon la prise la plus récente de
    l'utilisateur. Un affichage distant (`hôte:n`) n'est pas jugé."""
    d = os.environ.get("DISPLAY") if display is None else display
    if d and not d.startswith(":"):
        return d
    if d:
        n = d[1:].split(".")[0]
        if n.isdigit() and os.path.exists(os.path.join(prises, f"X{n}")):
            return d
    vivantes = []
    try:
        for nom in os.listdir(prises):
            if nom.startswith("X") and nom[1:].isdigit():
                st = os.stat(os.path.join(prises, nom))
                if st.st_uid == os.getuid():
                    vivantes.append((st.st_mtime, int(nom[1:])))
    except OSError:
        pass
    if vivantes:
        return f":{max(vivantes)[1]}"
    return d or ":0"


def pid_vivant(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, TypeError):
        return False


def position_etiquette(haut_barre, largeur_texte, bas_travail=None):
    """(x, y, hauteur) de l'étiquette du cadre.

    Dans la barre GNOME du haut, à gauche de l'horloge (x ≈ 120 → 1150 est
    vide) : en bas de la zone de travail, elle recouvrait le contenu d'une
    fenêtre agrandie (vu le 26/09 : le bas du bouton « Valider → Charli
    prépare la facture » du comptoir). Sans barre (≤ 20 px), repli en bas."""
    if haut_barre >= 20:
        return 120, 0, haut_barre
    return 120, (bas_travail or 1400) - 36, 32


def agent_pilote():
    """Nom affiché par le cadre : détenteur de la réservation, sinon
    PILOTE_AGENT, sinon « Claude »."""
    import json
    try:
        with open(chemin("pilote")) as f:
            nom = json.load(f).get("agent")
        if nom:
            return nom
    except (OSError, ValueError, AttributeError):
        pass
    return os.environ.get("PILOTE_AGENT") or "Claude"


def occupe_valide():
    """`occupe` (pid de l'`ecran` qui déplace la souris) rend le cadre aveugle.
    Il n'est honoré que s'il désigne un processus VIVANT et RÉCENT ; sinon il
    est retiré et on répond False — un `ecran` tué ou bloqué désarmerait le
    kill-switch pour de bon, et le cadre ne s'effacerait plus jamais."""
    p = chemin("occupe")
    try:
        with open(p) as f:
            pid = int(f.read().split()[0])
        age = time.time() - os.path.getmtime(p)
    except (OSError, ValueError, IndexError):
        effacer("occupe")
        return False
    if age <= AGE_OCCUPE and pid_vivant(pid):
        return True
    effacer("occupe")
    return False
