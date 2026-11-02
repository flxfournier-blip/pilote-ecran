"""Outils communs aux tests : dossier d'état jetable et faux binaires X11.

Aucun test unitaire ne touche l'écran de Félix : `ecran` est lancé avec
PILOTE_ETAT dans un dossier temporaire et un PATH dont xdotool, wmctrl,
xprop, maim, systemctl et systemd-run sont de faux scripts qui JOURNALISENT
leurs appels. On vérifie ensuite dans ce journal qu'aucun clic, frappe ou
déplacement n'est parti.
"""
import os
import shutil
import subprocess
import tempfile

ICI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LANCEUR = os.path.join(ICI, "ecran")
FIXTURES = os.path.join(ICI, "tests", "fixtures")
PYTHON_CADRE = os.path.expanduser("~/jarvis/charli-app/venv/bin/python")

# Sous-commandes xdotool qui AGISSENT (le reste ne fait que lire).
INJECTIONS = ("mousemove", "click", "mousedown", "mouseup", "key", "keydown",
              "keyup", "type", "windowactivate", "windowraise", "windowfocus")

FAUX = {
    "xdotool": r'''#!/bin/bash
echo "xdotool $*" >> "$FAUX_JOURNAL"
POS="$FAUX_JOURNAL.souris"
case "$1" in
  getdisplaygeometry) echo "2560 1440" ;;
  getmouselocation) read -r X Y < "$POS" 2>/dev/null || { X=100; Y=100; }
                    printf 'X=%s\nY=%s\nSCREEN=0\nWINDOW=4242\n' "$X" "$Y" ;;
  mousemove) echo "$2 $3" > "$POS" ;;
  click|type|key) [ -n "$FAUX_COPIE_EN_COURS" ] && cat "$PILOTE_ETAT/en-cours" >> "$(dirname "$FAUX_JOURNAL")/en-cours-vu" 2>/dev/null
                  [ -n "$FAUX_APRES" ] && cp "$FAUX_APRES" "$FAUX_ECRAN" ;;
  getactivewindow) echo 4242 ;;
  getwindowname) echo "Fenetre de test" ;;
  getwindowpid) echo 1 ;;
  *) : ;;
esac
exit 0
''',
    "systemctl": r'''#!/bin/bash
echo "systemctl $*" >> "$FAUX_JOURNAL"
if [[ " $* " == *" is-active "* ]]; then
  [[ -n "$FAUX_CADRE" && " $* " == *pilote-ecran-cadre* ]] && { echo active; exit 0; }
  echo inactive; exit 3
fi
exit 0
''',
}
for _nom in ("wmctrl", "xprop", "xwininfo", "maim", "xclip", "journalctl", "gsettings"):
    FAUX[_nom] = f'#!/bin/bash\necho "{_nom} $*" >> "$FAUX_JOURNAL"\nexit 0\n'
# Faux systemd-run : « démarre » le faux cadre (écrit `pret` avec un pid vivant, 1) quand FAUX_CADRE est posé.
FAUX["systemd-run"] = r'''#!/bin/bash
echo "systemd-run $*" >> "$FAUX_JOURNAL"
if [[ -n "$FAUX_CADRE" && " $* " == *pilote-ecran-cadre* ]]; then echo "1 0" > "$PILOTE_ETAT/pret"; fi
exit 0
'''


class Bac:
    """Dossier d'état + faux binaires ; `lancer(...)` exécute `ecran` dedans."""

    def __init__(self):
        self.racine = tempfile.mkdtemp(prefix="pilote-test-")
        self.etat = os.path.join(self.racine, "etat")
        self.bin = os.path.join(self.racine, "bin")
        self.journal = os.path.join(self.racine, "appels.log")
        os.makedirs(self.etat)
        os.makedirs(self.bin)
        open(self.journal, "w").close()
        for nom, corps in FAUX.items():
            p = os.path.join(self.bin, nom)
            with open(p, "w") as f:
                f.write(corps)
            os.chmod(p, 0o755)

    def env(self, **extra):
        e = {k: v for k, v in os.environ.items()
             if not k.startswith("PILOTE_") and k != "DISPLAY"}
        e.update(PATH=f"{self.bin}:/usr/bin:/bin", PILOTE_ETAT=self.etat,
                 PILOTE_DECONFINE="1", FAUX_JOURNAL=self.journal,
                 DISPLAY=":0", PILOTE_AGENT="test", PILOTE_VIGIE_DEVICES="aucun")
        e.update(getattr(self, "extra", {}))
        e.update(extra)
        return e

    def lancer(self, *args, entree=None, **extra):
        return subprocess.run(["/usr/bin/python3", LANCEUR, *args], env=self.env(**extra),
                              input=entree, capture_output=True, text=True, timeout=30)

    def ecran_fixe(self, image):
        """Faux écran = copie de `image` (capture figée) et faux cadre
        « affiché » : les actions passent, les clics sont journalisés."""
        with open(self.fichier("pret"), "w") as f:
            f.write(f"{os.getpid()} 0")
        self.ecran = os.path.join(self.racine, "ecran-faux.png")
        shutil.copy(image, self.ecran)
        self.extra = {"PILOTE_CAPTURE_FIXE": self.ecran, "FAUX_ECRAN": self.ecran,
                      "FAUX_CADRE": "1"}

    def apres_action(self, image):
        """Au premier clic/frappe, le faux écran devient `image`."""
        self.extra["FAUX_APRES"] = image

    def clics(self):
        """[(x, y)] des mousemove suivis d'un click."""
        res, pos = [], None
        for a in self.appels():
            p = a.split()
            if p[:2] == ["xdotool", "mousemove"]:
                pos = (int(p[2]), int(p[3]))
            elif p[:2] == ["xdotool", "click"] and pos:
                res.append(pos)
        return res

    def appels(self):
        return lire(self.journal).splitlines()

    def injections(self):
        return [a for a in self.appels()
                if a.startswith("xdotool ") and a.split()[1] in INJECTIONS]

    def fichier(self, nom):
        return os.path.join(self.etat, nom)

    def fermer(self):
        shutil.rmtree(self.racine, ignore_errors=True)


def lire(chemin):
    with open(chemin) as f:
        return f.read()
