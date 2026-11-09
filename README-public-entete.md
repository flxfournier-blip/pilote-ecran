# pilote-ecran

**Computer use, en local, pour un agent IA sur un bureau Linux (X11).** `ecran` donne à un agent (Claude Code, Codex, un agent local Hermès/Qwen…) le contrôle du bureau : capture d'écran + arbre d'accessibilité AT-SPI numéroté + lecture du texte (OCR), puis clic, frappe et touches via xdotool, avec des garde-fous pensés pour un poste réellement utilisé par un humain.

*Local computer-use for AI agents on a Linux X11 desktop: numbered screenshot + AT-SPI accessibility tree + OCR, then click/type/keys through xdotool, with a mouse kill-switch, per-agent screen reservation, an event-based screen watcher (no screenshots), a DevTools bridge for Chromium apps and a Vulkan layer that streams frames. Documentation is in French.*

Ce dépôt est la copie publique du système qui pilote l'écran de la maison dans la série vidéo « Des IA dans la maison » (YouTube : https://youtu.be/FKNa3MhNZEs). Il est publié tel qu'il tourne, journal de bord compris : le README ci-dessous est le document de travail, écrit au fil des semaines.

## Ce que ça fait

- `ecran voir` : capture + liste numérotée des éléments de la fenêtre active (AT-SPI, DevTools pour Chrome/VS Code, OCR pleine résolution), centres directement cliquables.
- `ecran cliquer 12` / `ecran cliquer @"Publier"` / `ecran taper "…"` / `ecran touche ctrl+s` / `ecran defiler bas` / `ecran fenetre MOTIF` / `ecran lancer CMD` : les actions, chacune suivie d'un verdict (ce qui a changé à l'écran).
- **Kill-switch** : si l'humain bouge la souris, tout s'arrête (cadre rouge, fichier `stop`), et le cadre orange autour de l'écran dit en permanence quel agent pilote.
- **Réservation** : un seul agent pilote à la fois (`ecran prendre` / `ecran stop`), les autres peuvent seulement regarder ; journal de toutes les actions, texte tapé jamais écrit.
- `ecran veille` : l'écran transcrit en **événements** (fenêtres, focus, texte modifié par champ, alertes, processus, GPU, audio), sans image ; carte vivante des interfaces avec identifiants stables ; pont HTTP local en lecture seule.
- Garde-fous : refus de cliquer sur un élément défilé hors du cadre, fenêtres de l'humain en lecture seule par convention, frappe de mots de passe par `--stdin` depuis un fichier, notifications ntfy des reprises.

## Installation (Ubuntu 24.04, GNOME sur X11)

```bash
sudo apt install python3-gi gir1.2-atspi-2.0 xdotool python3-pil tesseract-ocr tesseract-ocr-fra
git clone https://github.com/flxfournier-blip/pilote-ecran.git ~/pilote-ecran
ln -s ~/pilote-ecran/ecran ~/.local/bin/ecran          # python3 SYSTÈME (gi, PIL)
ecran aide
```

La couche Vulkan optionnelle (`flux/`) se construit avec `flux/construire.sh` après `git clone --depth 1 https://github.com/KhronosGroup/Vulkan-Headers.git flux/Vulkan-Headers`. Les tests : `/usr/bin/python3 -m unittest discover tests` (ceux qui dépendent de captures de logiciels métier, non publiées, sont à ignorer).

Licence MIT. Auteur : Félix Fournier, avec Claude (Anthropic) comme développeur principal, au fil de la série.

---

# Journal de bord (document de travail, tel quel)
