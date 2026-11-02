#!/usr/bin/env python3
"""cadre.py — cadre orange « Claude pilote l'écran » + garde-fou souris.

Fenêtre Qt transparente (PySide6 du venv charli-app : le python système
n'a pas python3-gi-cairo pour dessiner en GTK), sans bordure, au-dessus
de tout, transparente aux clics (WindowTransparentForInput = X Shape
d'entrée vide). Elle entoure la zone de travail (sous la barre GNOME,
au-dessus du dock).

Garde-fou : toutes les 100 ms, on compare la souris à la dernière position
laissée par `ecran` (fichier `souris`). Si elle a bougé de plus de SEUIL px
alors qu'aucune commande ne déplace la souris (fichier `occupe` absent),
c'est Félix qui a pris la main → on écrit `stop`, le cadre passe au rouge
une seconde et disparaît. `ecran` refuse alors toute action jusqu'à
`reprendre`.

`occupe` rend le cadre aveugle : on ne l'honore donc que s'il désigne un
processus VIVANT et RÉCENT, sinon un `ecran` tué (ou bloqué) désarmerait le
kill-switch pour de bon — et le cadre ne s'effacerait plus jamais.

Le cadre s'efface tout seul après INACTIVITE secondes sans commande (300 s,
comme la réservation ; c'était 180 s avant le 26/09 : le cadre disparaissait
pendant que l'agent réfléchissait, Félix reprenait la souris de bonne foi et
se voyait ensuite reprocher un STOP). Un `stop` écrit par `ecran` est lu à
chaque tick : le cadre passe au rouge sans réécrire la raison.
Il écrit `pret` une fois affiché : `ecran` refuse d'agir tant qu'il n'existe
pas (pas de cadre = pas de signal visible pour Félix, pas de kill-switch).
Lancé par `ecran` dans l'unité systemd utilisateur `pilote-ecran-cadre`.
"""
import atexit
import os
import signal
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication, QPainter, QPen  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from commun import (EXPIRATION_PILOTE, PAUSE_CADRE, SEUIL, agent_pilote, chemin, ecrire,  # noqa: E402
                    occupe_valide, position_etiquette)

INACTIVITE = 300    # s sans commande et SANS réservation → le cadre s'efface ; réservé : EXPIRATION_PILOTE (30 min)
AGE_SOURIS = 300    # s : une position de référence plus récente est gardée au redémarrage
ORANGE = QColor(240, 138, 36, 242)
ROUGE = QColor(217, 38, 38, 242)
BORD = 6


def souris_recente():
    """Vrai si `souris` date de moins de AGE_SOURIS s : c'est la position
    laissée par la dernière commande de la session en cours. La réécrire au
    redémarrage du cadre « pardonnait » un geste de Félix fait pendant la
    pause (constat bugs-09 du 25/09)."""
    try:
        return time.time() - os.path.getmtime(chemin("souris")) < AGE_SOURIS
    except OSError:
        return False


class Cadre(QWidget):
    def __init__(self):
        super().__init__()
        self.arret = None            # instant de l'arrêt (cadre rouge)
        self.pause = False           # > PAUSE_CADRE s sans commande : pointillé « réfléchit », toujours armé
        self.depart = time.time()
        self.setWindowTitle("Claude pilote l'écran")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.Tool | Qt.WindowTransparentForInput
                            | Qt.WindowDoesNotAcceptFocus
                            | Qt.X11BypassWindowManagerHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        ecran = QGuiApplication.primaryScreen()
        self.zone = ecran.availableGeometry()   # zone de travail
        self.setGeometry(ecran.geometry())
        self.minuteur = QTimer(self)
        self.minuteur.timeout.connect(self.surveiller)
        self.minuteur.start(100)

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        couleur = ROUGE if self.arret else ORANGE
        z = self.zone
        stylo = QPen(couleur, BORD)
        if self.pause and not self.arret:
            stylo.setStyle(Qt.DashLine)
        p.setPen(stylo)
        p.drawRect(z.x() + BORD // 2, z.y() + BORD // 2,
                   z.width() - BORD, z.height() - BORD)
        # Étiquette dans la barre GNOME du haut : Félix sait QUI tient la
        # souris, et elle ne cache plus rien des fenêtres (cf. position_etiquette).
        agent = agent_pilote()
        texte = (f"ARRÊT — tu as bougé la souris, {agent} lâche l'écran" if self.arret
                 else f"{agent.upper()} RÉFLÉCHIT — l'écran reste à lui, bouge la souris pour l'arrêter" if self.pause
                 else f"{agent.upper()} PILOTE L'ÉCRAN — bouge la souris pour l'arrêter")
        police = QFont("Sans", 11, QFont.Bold)
        p.setFont(police)
        larg = p.fontMetrics().horizontalAdvance(texte) + 28
        lx, ly, haut = position_etiquette(z.y(), larg, z.y() + z.height())
        p.setPen(Qt.NoPen)
        p.setBrush(couleur)
        p.drawRect(lx, ly, larg, haut)
        p.setPen(QColor(12, 12, 12))
        p.drawText(lx, ly, larg, haut, Qt.AlignCenter, texte)
        p.end()

    # ── garde-fou ──
    @staticmethod
    def position():
        pos = QCursor.pos()
        return pos.x(), pos.y()

    def ecrire_souris(self):
        x, y = self.position()
        ecrire("souris", f"{x} {y}")

    def surveiller(self):
        if self.arret:
            return
        # `stop` posé par `ecran` (souris déviée pendant une action) : rouge
        # garanti, et la raison d'`ecran` n'est pas écrasée.
        if os.path.exists(chemin("stop")):
            self.arreter(None)
            return
        # L'inactivité passe AVANT `occupe` : un fichier orphelin ne doit pas
        # rendre le cadre immortel. Jamais avant `depart` : un `actif` périmé
        # d'avant le démarrage tuait le cadre relancé en 200 ms (25/09).
        try:
            actif = max(os.path.getmtime(chemin("actif")), self.depart)
        except OSError:
            actif = self.depart
        inactif = time.time() - actif
        # Tant qu'un agent a RÉSERVÉ l'écran, le cadre reste (armé) jusqu'à 30 min d'inactivité : décision de Félix
        # du 26/09 (« armé pendant toute la réservation »). Sans réservation, il s'efface après INACTIVITE.
        limite = EXPIRATION_PILOTE if os.path.exists(chemin("pilote")) else INACTIVITE
        if inactif > limite:
            print(f"[cadre] {int(limite)} s sans commande — le cadre s'efface", flush=True)
            QApplication.quit()
            return
        pause = inactif > PAUSE_CADRE
        if pause != self.pause:
            self.pause = pause
            self.update()
        if occupe_valide():
            return                  # `ecran` bouge la souris : c'est nous
        try:
            sx, sy = (int(v) for v in open(chemin("souris")).read().split())
        except (OSError, ValueError):
            self.ecrire_souris()
            return
        x, y = self.position()
        if abs(x - sx) > SEUIL or abs(y - sy) > SEUIL:
            self.arreter(f"souris bougée par Félix ({sx},{sy} → {x},{y}) "
                         f"à {time.strftime('%H:%M:%S')}")

    def arreter(self, raison):
        """`raison` None : `stop` existe déjà (écrit par `ecran`), on ne fait
        que passer au rouge."""
        self.arret = time.time()
        if raison is not None:
            ecrire("stop", f"[cadre] {raison}")
        print(f"[cadre] ARRÊT : {raison or open(chemin('stop')).read().strip()}",
              flush=True)
        self.update()
        QTimer.singleShot(1500, QApplication.quit)


def main():
    os.makedirs(os.path.dirname(chemin("x")), exist_ok=True)
    app = QApplication(sys.argv)
    # `stop` n'est jamais effacé ici : c'est `ecran reprendre` qui décide.
    cadre = Cadre()
    cadre.show()
    # Point de départ = position actuelle (un `souris` d'une session ANCIENNE
    # déclencherait un faux arrêt), sauf si `ecran` agit déjà (il écrira la
    # position en fin d'action) ou si la position date de la session en cours
    # (`ecran stop`/`reprendre` l'effacent ; seul un redémarrage après pause la
    # retrouve, et un geste de Félix pendant la pause doit alors compter).
    if not occupe_valide() and not souris_recente():
        cadre.ecrire_souris()

    # `pret` = contrat avec `ecran` : tant qu'il n'existe pas, aucune action
    # n'est exécutée (cadre absent = pas de kill-switch).
    def nettoyer():
        for f in ("pret",):
            try:
                os.remove(chemin(f))
            except OSError:
                pass

    # Vigie (lot 6) : clics, molette et frappe de Félix → STOP. « Fail-open » : si elle ne démarre pas ou meurt, le
    # cadre continue avec le contrôle du pointeur, et `ecran etat` le dit.
    import subprocess
    vigie = None
    try:
        vigie = subprocess.Popen(["/usr/bin/python3", os.path.join(os.path.dirname(os.path.realpath(__file__)), "vigie.py")],
                                 env=os.environ.copy())
    except OSError as e:
        print(f"[cadre] vigie non lancée : {e}", flush=True)

    def arreter_vigie():
        if vigie and vigie.poll() is None:
            vigie.terminate()
    atexit.register(arreter_vigie)
    with open(chemin("pret"), "w") as f:
        f.write(f"{os.getpid()} {time.time()}")
    atexit.register(nettoyer)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: QApplication.quit())
    # Laisse Python traiter les signaux pendant la boucle Qt.
    battement = QTimer()
    battement.timeout.connect(lambda: None)
    battement.start(200)
    code = app.exec()
    nettoyer()
    sys.exit(code)


if __name__ == "__main__":
    main()
