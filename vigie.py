#!/usr/bin/python3
"""vigie.py — le garde-fou voit aussi les CLICS, la MOLETTE et le CLAVIER de Félix (lot 6, 26/09/2026).

Avant : seul le déplacement du pointeur (> 40 px) arrêtait un agent ; un clic sur place, un coup de molette ou une
frappe de Félix pendant une action passaient inaperçus. La vigie lit les périphériques PHYSIQUES (evdev,
/dev/input/by-id : clavier CASUE, souris YICHIP), sans les capturer (pas de « grab » : Félix garde la main), et pose
`stop` quand :
  - Félix CLIQUE (tout bouton) ou tourne la MOLETTE → toujours ;
  - Félix BOUGE la souris physique pendant une action qui la déplace (`occupe`) → toujours ; hors action,
    le cadre conserve son seuil de déplacement habituel ;
  - Félix FRAPPE une touche → seulement pendant une frappe d'agent (`en-cours` = taper/touche), sinon il ne pourrait
    plus écrire à Claude dans VS Code (décision de Félix).
Les actions des agents passent par xdotool (XTEST), jamais par ces périphériques : pas de faux arrêt. Les codes de
touche ne sont JAMAIS écrits nulle part. Décision « fail-open » : si la vigie meurt, le cadre continue avec le
contrôle du pointeur, et `ecran etat` affiche « vigie absente ». Battement : fichier `vigie` (pid, heure, nombre de
périphériques) chaque seconde ; périphériques relus toutes les 5 s (souris sans fil reconnectée).

Lancée par cadre.py ; `PILOTE_VIGIE_DEVICES` (chemins séparés par « : », ou « aucun ») sert aux tests.
"""
import glob
import os
import selectors
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from commun import chemin, ecrire, pid_vivant, occupe_valide  # noqa: E402

EV_KEY, EV_REL = 0x01, 0x02
REL_X, REL_Y = 0x00, 0x01
REL_WHEEL, REL_HWHEEL, REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES = 0x08, 0x06, 0x0B, 0x0C
BTN_MIN, BTN_MAX = 0x100, 0x2FF          # BTN_MISC … (souris 0x110-0x117, joystick, etc.)


def frappe_agent_en_cours():
    """`en-cours` = « taper <pid> » ou « touche <pid> » posé par `ecran` pendant une frappe, pid vivant."""
    try:
        with open(chemin("en-cours")) as f:
            cmd, pid = f.read().split()[:2]
        return cmd in ("taper", "touche") and pid_vivant(int(pid))
    except (OSError, ValueError):
        return False


def motif_arret(typ, code, valeur, frappe_en_cours, deplacement_en_cours=False):
    """Raison d'arrêt pour UN événement evdev, ou None. Fonction pure (testée)."""
    if typ == EV_KEY and valeur == 1:                      # appui (pas relâchement ni répétition)
        if BTN_MIN <= code <= BTN_MAX:
            return "clic de Félix"
        if frappe_en_cours:
            return "frappe de Félix pendant une frappe d'agent"
        return None
    if typ == EV_REL and code in (REL_WHEEL, REL_HWHEEL, REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES) and valeur != 0:
        return "molette de Félix"
    if typ == EV_REL and code in (REL_X, REL_Y) and valeur != 0 and deplacement_en_cours:
        return "mouvement de la souris de Félix pendant un déplacement d'agent"
    return None


def peripheriques():
    env = os.environ.get("PILOTE_VIGIE_DEVICES")
    if env is not None:
        return [] if env in ("", "aucun") else env.split(":")
    vus = {}
    for lien in glob.glob("/dev/input/by-id/*-event-*"):
        vus.setdefault(os.path.realpath(lien), lien)
    return sorted(vus)


def main():
    import evdev
    sel = selectors.DefaultSelector()
    ouverts = {}                                           # chemin → InputDevice

    def rescanner():
        """Toutes les 5 s : une souris sans fil qui se reconnecte change de /dev/input/eventN."""
        for p in peripheriques():
            if p in ouverts:
                continue
            try:
                d = evdev.InputDevice(p)
                sel.register(d, selectors.EVENT_READ, p)
                ouverts[p] = d
                print(f"[vigie] surveille {d.name}", flush=True)
            except OSError as e:
                print(f"[vigie] {p} illisible : {e}", flush=True)

    def oublier(p):
        d = ouverts.pop(p, None)
        if d is not None:
            try:
                sel.unregister(d)
                d.close()
            except (KeyError, ValueError, OSError):
                pass
            print(f"[vigie] {p} débranché", flush=True)

    rescanner()
    dernier = scan = 0.0
    parent = os.getppid()
    while True:
        if os.getppid() != parent:                         # le cadre est mort : la vigie ne lui survit pas
            print("[vigie] cadre disparu — arrêt", flush=True)
            return
        maintenant = time.time()
        if maintenant - scan >= 5:
            rescanner()
            scan = maintenant
        if maintenant - dernier >= 1:
            ecrire("vigie", f"{os.getpid()} {maintenant:.0f} {len(ouverts)}")
            dernier = maintenant
        for cle, _ in sel.select(timeout=0.5):
            try:
                evenements = list(cle.fileobj.read())
            except BlockingIOError:
                continue
            except OSError:                                # ENODEV : périphérique retiré
                oublier(cle.data)
                continue
            frappe, deplacement = None, None
            for ev in evenements:
                if frappe is None and ev.type == EV_KEY:
                    frappe = frappe_agent_en_cours()
                if deplacement is None and ev.type == EV_REL and ev.code in (REL_X, REL_Y):
                    deplacement = occupe_valide()
                motif = motif_arret(ev.type, ev.code, ev.value, bool(frappe), bool(deplacement))
                if motif and not os.path.exists(chemin("stop")):
                    ecrire("stop", f"[vigie] {motif} à {time.strftime('%H:%M:%S')}")
                    print(f"[vigie] ARRÊT : {motif}", flush=True)    # jamais le code de la touche


if __name__ == "__main__":
    main()
