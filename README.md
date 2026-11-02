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

# pilote-ecran — Claude pilote l'écran de JARVIS

Créé le 08/09/2026 sur demande de Félix (« ça regarde l'écran comme tu fais
avec le connecteur Chrome mais sur tout l'écran de l'ordi »). C'est du
« computer use » local : Claude Code (dans VS Code) regarde une capture et
l'arbre d'accessibilité, puis agit avec xdotool. Le pilote est Claude — pas
Qwen — parce que JARVIS ne contiendra jamais de vraie donnée patient (un
autre PC y sera dédié).

## Carte vivante sans capture — 10/10/2026

Le service `pilote-ecran-veille` fournit une représentation des interfaces exposées
par AT-SPI, DevTools et les annonces des applications. Un agent peut suivre cette
carte, agir avec le pilote, puis lire les changements pour vérifier son résultat.
Les captures automatiques de la veille et l'œil Qwen sont **désactivés par défaut**.
L'activité graphique est suivie par X Damage ; sans lui elle est signalée inconnue,
sans repli sur une image. `PILOTE_VEILLE_IMAGES=1` réactive explicitement l'ancien
mode visuel dans le service ; les commandes `voir`/OCR restent distinctes.

```bash
ecran veille --carte --json
ecran veille --carte --suivre 60 --json
ecran veille --carte --fenetre Firefox --suivre 30
```

Le flux envoie un `instantane`, puis des `changement` : fenêtres, contexte
(focus, fenêtre active, pointeur, empilement et STOP), éléments ajoutés, modifiés
ou retirés de la projection. Les éléments possèdent un `id` stable pendant leur
vie et la session du service. AT-SPI fournit l'identité bus/objet ; DevTools inclut
la connexion et le document, renouvelés lors d'une navigation. Un repli sans identité
native porte `identite: estimee` et n'autorise pas de clic par cet identifiant.

Chaque fenêtre indique `fraicheur`, `age_ms`, `source` et `couverture` : un contenu
inaccessible n'est pas inventé, une ancienne lecture conservée devient `perime`.
Une couverture partielle reste partielle même si la lecture est récente. La carte
ne décrit pas le contenu des photos/vidéos, ni tous les contrôles des applications.
La visibilité exposée par une application ne garantit pas à elle seule qu'un
élément n'est pas recouvert à l'écran.

Les lectures sont déclenchées par les événements, regroupées puis complétées par
une relecture de l'active (cible : 1 s) et de l'arrière-plan (15 s). Le temps réel
n'est pas garanti : une application lente ou un arbre volumineux ajoute du délai.

### Boucle d'action

Après réservation habituelle, utiliser un ID
reçu de la carte, jamais un ID numérique de `voir` :

```bash
export PILOTE_AGENT=codex
ecran prendre
ecran cliquer 'id:<ID_RECU_DE_LA_CARTE>' --attendre 0
ecran taper 'texte' --sans-capture --dans 'Titre de la fenêtre' --attendre 0
ecran touche Tab --sans-capture --dans 'Titre de la fenêtre' --attendre 0
ecran defiler bas 3 --sans-capture --attendre 0
ecran veille --carte --suivre 3 --json
ecran stop
```

Le clic par ID relit la cible sous le verrou habituel, exige la bonne fenêtre
active, une identité native et une confirmation du point par AT-SPI ou DevTools.
Une cible ancienne, modifiée, hors fenêtre, désactivée ou recouverte est refusée ;
une confirmation indisponible est également un refus. Les frappes sans capture
revérifient la fenêtre active après lecture. STOP, réservation et garde-fou souris
restent appliqués. Un changement de révision ne prouve **pas** la réussite de
l'action : l'agent doit lire le résultat attendu dans les éléments.

### Approche lente du pointeur

`--lent` sur `souris` ou `cliquer` déplace progressivement le pointeur vers sa
cible en environ deux secondes, avec un départ et une arrivée ralentis :

```bash
ecran souris 800 500 --lent
ecran cliquer 'id:<ID_RECU_DE_LA_CARTE>' --lent --attendre 0
```

Les règles de réservation et de relecture restent les mêmes. Le trajet vérifie
STOP et la position entre chaque pas. La vigie détecte aussi les mouvements
physiques pendant le déplacement programmé ; aucun mouvement supplémentaire
ni clic n'est envoyé après détection. Un trajet retardé de plus de cinq secondes
est abandonné. Avant un clic par ID, la cible est de nouveau vérifiée à l'arrivée :
si elle a changé ou est recouverte, le clic est refusé. Cette option ne détecte
ni ne résout les CAPTCHA automatiquement.

Le pont HTTP ne fournit aucune action. Routes locales :

- `GET /carte?fenetre=MOTIF` : instantané, `session` et `revision`.
- `GET /carte/flux?fenetre=MOTIF&duree=60` : SSE, abonnement et instantané initiaux
  atomiques. `duree` est facultative, limitée à 600 s si fournie. Heartbeat chaque
  seconde d'inactivité. Reconnexion = nouvel instantané complet.
- `GET /carte/cible?id=ID` : résolution et vérification en lecture seule, HTTP 409
  si la cible ne peut pas être confirmée.

Appliquer un delta `type: fenetre` en remplaçant les métadonnées par `fenetre`,
puis les éléments par `ajoutes`/`modifies`, en supprimant `retires` et en respectant
`ordre`. `fermeture` retire la fenêtre ; `contexte` remplace le contexte global.
Vérifier `session` et `precedent` (dernière révision livrée à cet abonné, même sous
filtre). Un débordement produit un nouvel `instantane` avec `resynchronise: true` :
remplacer alors tout l'état local. L'âge dérive de `fraicheur.observe_a` (horodatage
Unix), il continue à croître entre les événements. Aucun historique complet des
éléments n'est écrit dans `veille.jsonl`.

Vérification : tests sans bureau des identités, du rejeu des deltas, de la
concurrence à l'abonnement, des filtres, de la resynchronisation, des refus de
cibles et des garde-fous. Le hit-test CDP est également vérifié dans un navigateur
headless isolé, sans capture. Ne pas lever un STOP réel pour lancer les anciens
tests subprocess qui consultent volontairement le dossier d'état réel.

Validation du 10/10 : 110 tests ciblés réussis (veille, CDP, CLI, actions de carte
et vérification image existante). Cinq tests supplémentaires de `test_securite`
se heurtent au STOP réel déjà présent, laissé intact. Service redémarré et flux
réel vérifié en lecture seule : 19 fenêtres, environ 1 200 éléments natifs,
lecture d'instantané mesurée à 12,5 ms, chaîne des révisions reconstruite sur
5 secondes. Cela mesure l'accès au cache, pas la latence complète d'une action.
Le VS Code actif de cette session annonce un enfant AT-SPI mais retourne un
objet null : sa lecture est signalée indisponible, sans relancer l'application.

## Fichiers

- `ecran` — CLI (python3 système : PIL, gi/Atspi, xdotool, maim, wmctrl,
  xclip). Lien dans `~/.local/bin/ecran` → utilisable sans chemin.
- `cadre.py` — cadre orange + garde-fou souris (PySide6 du venv
  `~/jarvis/charli-app/venv` : le python système n'a pas `python3-gi-cairo`,
  impossible de dessiner en GTK sans lui). Unité systemd utilisateur
  transitoire `pilote-ecran-cadre`.
- État dans `/run/user/1000/pilote-ecran/` : `ecran.png` (capture marquée),
  `brut.png`, `elements.json` (dernière liste, horodatée), `souris`
  (position laissée par `ecran`), `occupe` (déplacement de souris en cours,
  pid), `pret` (cadre affiché, pid), `actif` (dernière commande), `verrou`
  (un seul `ecran` à la fois), `stop` (garde-fou déclenché, raison).
- Unité `pilote-ecran-presse-papiers` : le xclip qui détient le
  presse-papiers pour `taper --coller` (il doit survivre à `ecran`).
- Skill Claude Code : `~/.claude/skills/pilote-ecran/SKILL.md`.

## Comment ça marche

1. `ecran` détecte qu'il tourne sous le snap VS Code (`/proc/self/attr/current`
   = `snap.code.code`) et se relance via `systemd-run --user --pipe --wait`
   (`RuntimeMaxSec=180`) : sans ça, AppArmor bloque les messages AT-SPI vers
   les applis snap (Firefox, Chromium, Mailspring).
2. `voir` : `maim` → PNG réduit à 1280 px (échelle 0,5) ; fenêtre active
   par `xdotool getactivewindow` ; appli AT-SPI retrouvée par PID ; BFS de
   l'arbre (rôles utiles, états SHOWING+VISIBLE, plafond 4 s / 400
   éléments) ; marques orange numérotées (PIL) ; `elements.json`.
3. Coordonnées : GTK3/Qt/Firefox donnent de vraies coordonnées SCREEN ;
   **GTK4 renvoie 0,0** en SCREEN pour tous les enfants → on prend WINDOW +
   origine visible X11 (`getwindowgeometry` − `_GTK_FRAME_EXTENTS`, les
   ombres CSD). Vérifié sur la Calculatrice : 7 + 8 = 15 lu par `lire`.
4. `get_text_iface()` renvoie le MÊME objet Accessible : `t.get_text(0, n)`
   tombe sur `Atspi.Accessible.get_text` (déprécié, 0 argument) → appeler
   `Atspi.Text.get_text(t, 0, n)` explicitement. `lire` garde le nœud au
   texte le plus complet (plusieurs nœuds imbriqués partagent la même boîte).
5. Garde-fou : `cadre.py` compare la souris toutes les 100 ms à `souris`.
   Écart > 40 px = Félix → `stop` écrit, cadre rouge 1,5 s, sortie ; `ecran`
   refuse toute action jusqu'à `reprendre` (mais `voir`/`lire`/`etat`
   restent permis en lecture seule). Le cadre s'efface après 300 s sans
   commande (180 s avant le 26/09). Chaque `stop` est signé de sa source :
   `[cadre] …` (geste vu entre deux commandes) ou `[ecran] …` (geste vu
   pendant une action) ; le cadre lit un `stop` d'`ecran` et passe au rouge
   sans écraser la raison.
6. `lancer` passe par `systemd-run --user --collect --unit=pilote-app-…` :
   `ecran` lui-même vit dans un service éphémère dont le cgroup est tué.

## Ce que la revue adversariale du 08/09 a corrigé

Revue en 110 agents (4 angles, sceptiques par constat) sur le code du
matin ; 42 constats, corrigés puis retestés en réel :

- **`mousemove --sync` ne rend jamais la main** quand la cible est déjà la
  position du pointeur (xdotool 3.2016 attend qu'il QUITTE sa place). Le
  blocage se levait quand Félix bougeait la souris… et son geste était
  absorbé (aucun `stop`). → `bouger_souris()` sans `--sync`, avec attente
  active ≤ 300 ms et échec explicite si la souris n'arrive pas.
- **`occupe` rendait le cadre aveugle** pour TOUTES les commandes (dont
  `voir`, 4 s) et `__exit__` réécrivait `souris` avec la position de Félix :
  sa prise de main était effacée. → `occupe` seulement pour les commandes
  qui déplacent la souris ; à la sortie on compare la position réelle à la
  position ATTENDUE et on écrit `stop` en cas d'écart.
- **`occupe` orphelin** (processus tué, action bloquée) désarmait le
  kill-switch pour de bon et rendait le cadre immortel. → le cadre exige un
  pid vivant de moins de 30 s, teste l'inactivité en premier, et efface le
  fichier ; `agir` libère `occupe` dans un `finally` et gère SIGTERM.
- **Cadre absent ou crashé : `ecran` agissait quand même**, en silence
  (l'attente « ≤ 2 s » testait `souris`, jamais effacé). → `cadre.py` écrit
  `pret` après affichage ; sans lui, `ecran` refuse d'agir et affiche le
  journal de l'unité.
- **`taper --coller` gelait** (`xclip` forke en gardant les tubes → EOF
  jamais reçu) et le presse-papiers mourait avec l'unité `ecran` (collage
  vide). → xclip `-quiet` dans son unité `pilote-ecran-presse-papiers`, plus
  un `timeout` sur tous les sous-processus.
- **Deux `ecran` en parallèle** se volaient `occupe` (faux arrêts). →
  verrou `flock`.
- **Course de 100 ms à l'entrée** : un geste de Félix juste avant `occupe`
  n'était jamais vu. → `agir` compare lui-même le pointeur à `souris`.
- Validation des arguments (`--doulbe` silencieux, `defiler bas 999999` =
  11 h de molette, `--zone` à 3 valeurs = traceback, coordonnées hors
  écran), `touche` inconnue avalée par xdotool (code 0), `lancer` qui disait
  « ✓ » sur une appli morte, `fenetre` qui prenait la plus ancienne
  homonyme, `elements.json` non atomique, `voir | head` en code 120,
  `ecran` absent du PATH, `--action` annoncé mais non implémenté.

## Second avis Codex (08/09, binaire de l'extension VS Code)

Trois défauts que la revue n'avait pas fermés, corrigés et retestés :

- **Une série de `touche` continuait après un arrêt souris** : Félix bougeait
  la souris, `stop` était écrit… et le `Return` final partait quand même. La
  boucle interroge maintenant `stop` et la position du pointeur entre chaque
  touche (« envoyées : 1 2 3 4 5 ; abandon de 6 7 8 »).
- **`stop` et `reprendre` échappaient au verrou** : ils pouvaient retirer le
  cadre et effacer `occupe` pendant qu'une autre commande injectait encore des
  événements. → `verrou_exclusif`, partagé avec `agir`.
- **`cliquer <id>` ne revalidait pas la fenêtre** : l'âge de la liste était
  vérifié, pas son origine. Si une autre fenêtre était passée devant, le clic
  y tombait (un « = » de la calculatrice devenant un « Envoyer » ailleurs).
  → refus explicite si `wid` ne correspond plus à la fenêtre active.

Appel : `~/.vscode/extensions/openai.chatgpt-*/bin/linux-x86_64/codex exec
--sandbox read-only --skip-git-repo-check --ephemeral "…"` (auth ChatGPT déjà
dans `~/.codex/auth.json`, rien à installer).

## ecran v2 — lot 0 (26/09/2026)

Plan complet : [PLAN-ECRAN-V2.md](PLAN-ECRAN-V2.md). Sauvegarde de la v1 :
`.sauvegardes/v1-20260926-0400.tar.gz` (ecran, cadre.py, tests.sh, README, SKILL).

Fichiers :
- `ecran` — lanceur de 3 lignes (python système) ; tout le code est dans
  `ecran.py`, importable par les tests. `deconfiner()` relance le lanceur.
- `commun.py` — dossier d'état, écriture atomique, `pid_vivant`,
  `occupe_valide` : UNE version partagée par `ecran` et `cadre.py` (il y en
  avait deux, qui traitaient différemment un `occupe` orphelin).
- `perception.py` — la vue « câble HDMI », pas encore branchée sur les
  commandes (lot 1) : `capturer()` pleine résolution (PIL/xcb, 18 ms),
  `ocr()` (tesseract fra sur gris inversé ×2 + seconde passe sur les boutons
  colorés, regroupée en une mosaïque : 545 mots en 1,3 s sur le comptoir),
  `lignes()`, `chercher()`, `proches()`, `diff()` (23 ms, curseur ignoré),
  `stable()`.
- `recette-ecran.sh` — remplace `tests.sh`. Bouge la vraie souris, donc ne
  part que sur le GO de Félix (`touch /run/user/1000/pilote-ecran/go-recette`,
  un GO = une recette), jamais sous STOP ni sur un écran réservé par un autre
  agent (exit 2 sans rien toucher). Journal par exécution dans `recettes/`,
  ménage des seules fenêtres qu'elle a lancées (`trap`), plus jamais d'action
  dans VS Code (la 2e fenêtre est une fenêtre Tk qu'elle ouvre elle-même).
- `tests/` — tests SANS écran : `/usr/bin/python3 -m unittest discover -s tests -t .`
  (32 tests, ~8 s). Faux xdotool/systemctl qui journalisent leurs appels (on
  vérifie qu'aucun clic ni frappe n'est parti), cadre.py lancé en
  `QT_QPA_PLATFORM=offscreen` (rien ne s'affiche), OCR sur
  `tests/fixtures/comptoir-2560.png` (capture d'un logiciel métier, fixture non publiée) avec
  les centres attendus dans `attendus.json`.

Corrigé :
- **Cadre fantôme** : relancé après une pause, le cadre lisait l'ancien
  `actif`, se croyait inactif et mourait en 200 ms — l'action suivante
  partait sans cadre ni kill-switch. `actif` est désormais écrit AVANT de
  lancer le cadre, et le cadre ne remonte jamais avant son propre démarrage.
- **Geste de Félix pardonné** : au redémarrage, le cadre réécrivait la
  position de référence ; un mouvement fait pendant la pause ne comptait plus.
  Une position de moins de 300 s est désormais gardée.
- **Cadre effacé pendant la réservation** : 180 s → 300 s, comme la réservation.
- Les tests du cadre ont été rejoués sur l'ancien `cadre.py` : 4 sur 5
  échouent, ils attrapent bien ces bugs.

## ecran v2 — lot 1 : couche texte (26/09/2026)

Toute appli se lit désormais sur la capture PLEINE RÉSOLUTION, arbre AT-SPI ou pas :

    ecran texte [--zone X Y L H | --fenetre-active] [--json]   # lignes tN + centres écran
    ecran chercher "Nouveau passage" "Envoyer" [--zone …]       # exit 2 + 3 lignes proches si absent
    ecran cliquer @"Nouveau passage" [--n K] [--zone …]         # cherche à l'instant, puis clique
    ecran cliquer t12                                           # ligne lue ; refus si elle a bougé
    ecran lire --zone X Y L H | tN | @"ancre" --autour 1200x300 # OCR en mode bloc

- `voir` ajoute la couche OCR (lignes `tN` de la fenêtre active, sans celles
  qu'un élément AT-SPI nommé couvre déjà ; toutes dans `texte.json`) ;
  `--sans-ocr` pour s'en passer. À 0 élément, le message ne dit plus
  « l'appli n'expose pas son arbre » mais renvoie à la couche texte.
- `chercher`/`cliquer @` : AT-SPI d'abord si la liste de `voir` a < 60 s et
  vient de la fenêtre active (nom identique), sinon OCR frais. Ambigu →
  refus (exit 2) avec les occurrences numérotées, `--n K` ou `--zone`.
- `cliquer tN` recapture la zone de la ligne et la compare à `texte.png` :
  si elle a changé (> 3 % de pixels), refus.
- Première ligne `ETAT ok | ETAT STOP — … | ETAT réservé par …` sur
  `voir`/`texte`/`chercher`/`lire`. Codes de sortie : 0 ok, 1 erreur,
  2 introuvable/ambigu, 3 STOP, 4 réservé par un autre agent.
- L'étiquette du cadre (« CLAUDE PILOTE L'ÉCRAN… ») est retirée de l'OCR.
- **Géométrie des fenêtres** : `xdotool getwindowgeometry` compte deux fois
  la barre de titre mutter (Y=106 pour une fenêtre en 69). `geometrie_x11`
  lit maintenant `xwininfo`. Sans barre mutter (VS Code, GTK4) rien ne change.
- Recette écran (section 10) : fenêtre Tk sans arbre, pilotée uniquement
  par la couche texte (chercher, cliquer @, cliquer tN, relire « Compté 2
  fois »). 39/39 le 26/09 à 07 h 49. Tests sans écran : 53.
- Mesuré sur le comptoir réel (Chrome dédié `--app`, agrandi) : 137
  éléments AT-SPI + 110 lignes OCR (591 mots) en 1,4 s ; « Nouveau
  passage », « Dépose une ordonnance », « Envoyer », « Biper » trouvés.

## ecran v2 — lot 3 : des actions qui se vérifient (26/09/2026)

- **Verdict** après `cliquer`, `taper`, `touche`, `defiler` : capture avant,
  attente que l'écran se stabilise (≤ 1,5 s, `--attendre S`), puis
  `→ changement 0,1 % — 2 zone(s) : 596,589 209×24 ; … — fenêtre « … »` ou
  `→ aucun changement visible`. Indice, pas preuve (un survol change aussi
  des pixels) : `--verifier "Compté 3 fois"` relit le texte attendu (exit 2
  s'il n'y est pas — l'action, elle, est partie). `avant.png`/`apres.png`
  gardent la dernière paire.
- Le diff ignore la barre GNOME et le bruit < 60 px² ; un CHIFFRE qui change
  (~12×18 px) est vu. Le premier seuil (800 px², pour un curseur qui n'est de
  toute façon pas dans les captures xcb) le rendait invisible — trouvé par la
  recette, corrigé le même matin.
- `taper "…" --dans @"Dépose une ordonnance" --entree` : clic dans le champ
  trouvé à l'instant, frappe, Entrée (pas d'Entrée si un STOP tombe entre
  deux). `--dans FENÊTRE` : refus si la fenêtre active ne porte pas ce titre ;
  `touche … --dans FENÊTRE` revérifie avant CHAQUE touche. Attention : cliquer
  au milieu d'un champ déjà rempli y place le curseur (vu : « noFelixm ») —
  `touche End` d'abord si besoin.
- `cliquer … --avec ctrl|shift|ctrl+shift` : modificateurs toujours relâchés
  (bloc `finally`). Pas de `--enfoncer/--relacher` : une touche laissée
  enfoncée après `ecran` resterait bloquée sur le clavier de Félix.
- `attendre --texte "…" | --disparu "…" | --stable S [--zone …] [--max S]
  [--pas MS]` : conditions combinées (toutes), OCR à chaque tour, pas de
  verrou entre deux tours, permis sous STOP ; exit 2 + lignes proches si le
  délai passe. Le DÉTENTEUR de l'écran reste « actif » pendant l'attente (le
  cadre ne s'efface plus pendant que Charli réfléchit) ; un observateur ne
  prolonge rien. `attendre S` (pause simple) est gardé.
- `ecran diff [--texte] [--zone …]` : ce qui a changé depuis la capture
  d'avant la dernière action, avec les lignes apparues (+) et disparues (−).
- `taper --coller` : `coller.txt` créé en 0600 et effacé dès la sélection
  chargée (il gardait le dernier texte collé en clair, lisible par tous).
- Recette : 46/46 (section Tk : `--verifier`, verdict, `diff --texte`,
  `taper --dans … --entree`, `attendre --texte`, `touche --dans` refusée,
  `attendre --stable`). Tests sans écran : 75.

Chrome dédié pour le comptoir (décision de Félix §5.5, pas de ses fenêtres) :

    ecran lancer google-chrome --user-data-dir=$HOME/.local/share/pilote-chrome \
        --force-renderer-accessibility --no-first-run --no-default-browser-check \
        --app=http://127.0.0.1:8034/

## Limites connues

- ~~L'étiquette du cadre recouvre le bas d'une fenêtre agrandie~~ : corrigé
  le 26/09 (lot 5 partiel). Elle est dans la barre GNOME du haut (x 120,
  32 px, à gauche de l'horloge) et porte le nom de l'agent qui pilote
  (« CODEX PILOTE L'ÉCRAN… »), lu dans `pilote` puis PILOTE_AGENT.
- Texte gris très peu contrasté : lu sur le comptoir, mais une égalisation
  locale (CLAHE) reste en réserve si un cas réel échoue (sans gain mesuré
  sur la capture de référence, 509 mots contre 520).

- Applis lancées avant l'activation de `toolkit-accessibility` (08/09) :
  pas d'arbre avant relance → pilotage à la capture.
- Pages web : arbre trop grand ; `--fenetre` réduit le parcours, `--zone`
  ne fait que filtrer après coup.
- ~~Pas de détection du clavier humain~~ : vigie evdev depuis le 26/09
  (lot 6, section ci-dessous). Elle ne voit PAS une injection XTEST/uinput
  d'un autre agent : seule la comparaison de position du cadre l'attrape.
- Mono-écran : `ecran_taille()` prend l'union X11 mais `cadre.py` n'entoure
  que l'écran principal.
- Test du 08/09 (après corrections) : gnome-calculator (GTK4) piloté de bout
  en bout, collage multiligne accentué de 476 caractères relu par `lire`,
  kill-switch et `occupe` orphelin vérifiés en réel.

## 26/09 fin de matinée — OCR renforcé sur le vrai comptoir

- **Deux seuillages en parallèle, fusionnés** (`perception.ocr`) : global (Otsu, le meilleur sur le texte vif) +
  adaptatif (gaussien 31 px, le seul qui lit un texte gris peu contrasté comme l'invite « Dépose une ordonnance… »
  dès que l'écran contient aussi du blanc vif). De la passe adaptative on ne prend que des mots à lettre ou chiffre
  (elle lit les bords de boutons « | »), et un mot déjà vu (centre dans la boîte de l'autre ou 30 % de recouvrement)
  n'est pas dupliqué (« Compté 4 4 fois »).
- **Boutons** (`zones_colorees`) : aplats saturés ET, sur page sombre, capsules claires à texte sombre (thème façon
  ChatGPT du comptoir) ; jusqu'à 1 400 px de large ; 8 px de bord ignorés (le cadre orange y reliait tout en une zone).
  Dans une zone, fusion mot à mot, le plus sûr gardé (confiance × longueur).
- `chercher`/`cliquer @` : la liste AT-SPI d'un `voir` n'est plus utilisée si une action a eu lieu depuis (vu : fenêtre
  « Enregistrer le mot de passe ? » encore « trouvée » après sa fermeture).
- Captures réelles en tests : comptoir-nouveau-passage.png, comptoir-theme-chatgpt-1280.png. 80 tests, recette 46/46.

## ecran v2 — lot 4 : AT-SPI honnête (26/09/2026, fin de matinée)

- `fenetre` VÉRIFIE le focus (wmctrl, puis xdotool, puis « lever ») et sort en erreur s'il n'est pas obtenu : le 26/09
  elle disait « ✓ » alors qu'aucune fenêtre n'était active (`_NET_ACTIVE_WINDOW` vide) — `activer_fenetre()`.
- Chaque élément de `voir` porte SA fenêtre (`wid`) : `cliquer <id>` active d'abord la fenêtre de l'élément (ou refuse
  s'il n'y arrive pas) ; avant, la garde comparait à la fenêtre active au moment du `voir` (fausse avec --fenetre/--tout).
- `voir --zone` filtre l'affichage SANS renuméroter : les numéros restent ceux de la liste complète (vécu : `cliquer 37`
  après un `voir --zone` visait une liste renumérotée 1…16).
- Arbre Chromium/Electron vide au premier parcours : relu jusqu'à 3 × 300 ms (il se construit à la première demande).
- `fenetres` marque « (réduite) » et ne dit plus « au-dessus » d'une fenêtre réduite ; `etat` ne plante plus sans X ;
  `voir` n'écrit plus dans gsettings sous STOP ; `lire` : texte fait d'objets incorporés (U+FFFC) → noms des enfants.
- Recette 48/48 (nouveaux : focus vérifié, clic par id qui active la fenêtre de l'élément, `--zone` sans renumérotation).

## ecran v2 — lot 5 : garde-fous et multi-agents (26/09/2026, midi)

Décisions de Félix de la nuit (PLAN-ECRAN-V2.md) appliquées :
- **Cadre armé toute la réservation** : réservation = 30 min d'inactivité (`commun.EXPIRATION_PILOTE`, 5 min avant) ;
  le cadre reste affiché tant qu'un `pilote` existe, en pointillé « <AGENT> RÉFLÉCHIT » après 60 s, toujours armé.
  Sous STOP la réservation est gelée (plus d'expiration).
- **Identité** : `PILOTE_AGENT`, sinon déduite du client (CLAUDECODE → claude, CODEX_* → codex, HERMES_* → charli),
  calculée AVANT le déconfinement ; plus jamais `anonyme-<ppid>`. **Réservation implicite** à la 1re action.
- `stop` refuse de libérer la réservation d'un autre (exit 4) ; `stop --forcer` : journalisé + ntfy à Félix.
- `reprendre [--raison "…"]` : refus si l'écran est à un autre agent ; pointeur immobile 0,5 s exigé ; `stop` effacé,
  cadre relancé (STOP reposé s'il ne démarre pas) ; journal + ntfy silencieux à Félix (directement ntfy.sh).
- `PILOTE_SANS_CADRE` SUPPRIMÉ ; un `PILOTE_ETAT` détourné ne contourne pas un STOP du vrai dossier (`stop_reel`).
- Un agent qui ne fait que regarder ne prolonge plus la réservation d'un autre.
- **Journal** append-only `~/.local/state/pilote-ecran/journal.ndjson` (cliquer, taper, touche, defiler, souris,
  lancer, fenetre, prendre, rendre, stop, reprendre : agent, arguments, code de sortie, fenêtre) ; le texte tapé n'y
  est JAMAIS, seulement sa longueur. `ecran journal [N] [--stop]`.
- Permissions Claude Code (`~/jarvis/.claude/settings.local.json`) : deny xdotool click/key/type/mousemove/…,
  ydotool, `wmctrl -a` ; allow windowactivate/windowraise retirés ; `env.PILOTE_AGENT=claude`.
- AGENTS.md (règles 2, 7, méthode 2, réservation) et skill Hermès de Charli mis à jour le même jour.
- Tests : 94 sans écran (dont `tests/test_securite.py`), recette 50/50.

## ecran v2 — lot 7 : `decrire` (Qwen local, 26/09/2026)

`ecran decrire [--zone X Y L H | --fenetre-active] [--question "…"] [--reflechir] [--json]` : capture propre (JPEG
≤ 1 600 px, taille annoncée), Qwen :8016 décrit en 3-6 lignes (1,4-2,6 s) ou répond OUI / NON / INCERTAIN. Il ne
donne JAMAIS de coordonnée (consigne + règle : il invente une position quand la cible est absente). Chaque texte qu'il
cite « … » est recontrôlé par l'OCR de la même image : `[confirmé OCR x,y]` ou `[NON retrouvé à l'écran]` (vu : il
écrit « PharmOS » pour « PharmIOS ») ; un OUI sur un texte que l'OCR ne lit pas, ou un NON sur un texte qu'il lit, est
signalé ⚠. Usage : économiser une lecture d'image de Claude (les images comptent), jamais pour cliquer.

## ecran v2 — lot 2 : image lisible (26/09/2026, après-midi)

- `voir` livre par défaut la **fenêtre active à l'échelle 1** (plafonnée à 1 920 px de large, strictement sous les
  2 000 px que l'API refuse dans les requêtes à nombreuses images) au lieu de l'écran réduit ×0,5 (texte à 5 px).
  Comptoir en demi-écran : 1 280×1 304 à l'échelle 1 ; agrandi : 1 920×978 à 0,75.
- `--plein` (écran entier, 1 920 px), `--apercu` (l'ancienne vue 1 280), `--tuile 2x2 3` (pleine résolution),
  `--autour tN|@"texte"|id|X,Y [LxH]` (zoom autour d'un repère, 1 200×600 par défaut), `--zone` inchangé.
- **ecran.json** accompagne chaque image : origine écran, échelle, taille. `cliquer X Y --img` (et `souris`,
  `defiler --a`) convertissent avec lui — plus de calcul de tête ; refus si l'image n'est pas décrite.
- `voir --fenetre MOTIF` sans correspondance vide la liste d'éléments (elle gardait l'ancienne sous une nouvelle image).
- Le cadrage est une fonction pure (`cadrer`) testée sans écran ; 110 tests ; recette 50/50.

## 26/09 après-midi — Vigie evdev (lot 6)

Décision de Félix (26/09, 3 h 45) : « oui, bornée, fail-open ».

- `vigie.py` (python système, module `evdev` de `~/.local`) est lancée par
  `cadre.py` et meurt avec lui (terminate à la sortie, et elle s'arrête seule
  si son parent disparaît). Elle lit `/dev/input/by-id/*-event-*` (clavier
  CASUE, souris YICHIP : 6 périphériques), SANS grab : Félix garde la main.
  Relecture toutes les 5 s (souris sans fil reconnectée) ; un périphérique
  retiré est oublié sans planter.
- Pose `stop` « [vigie] … » quand Félix CLIQUE (tout bouton BTN_*) ou tourne
  la MOLETTE, toujours ; quand il FRAPPE une touche, seulement pendant une
  frappe d'agent. Hors frappe, il doit pouvoir écrire à Claude dans VS Code.
- Depuis le 10/10, un mouvement physique `REL_X`/`REL_Y` déclenche aussi STOP
  pendant un déplacement d'agent (`occupe` valide), notamment une approche lente.
  Hors déplacement, le cadre conserve son contrôle habituel du pointeur.
- `taper` et `touche` posent `en-cours` (« taper <pid> » / « touche <pid> »)
  pendant toute la commande, effacé dans un `finally` ; un pid mort ne compte
  pas.
- Les codes de touche ne sont jamais écrits (ni journal, ni stop, ni sortie).
- Les actions des agents passent par xdotool (XTEST) : aucun évènement evdev,
  donc pas de faux arrêt.
- Fail-open : si la vigie ne démarre pas ou meurt, le cadre continue avec le
  seul contrôle du pointeur. `ecran etat` le dit : « vigie : active (6
  périphérique(s)…) » ou « vigie : ABSENTE… » (battement `vigie` = pid, heure,
  nombre, réécrit chaque seconde, jugé mort après 5 s).
- Tests : `PILOTE_VIGIE_DEVICES` (« aucun » ou chemins séparés par « : »),
  forcé à « aucun » dans le banc. `tests/test_vigie.py` : motifs (clic,
  relâchement, répétition, molette, mouvement, clavier avec/sans frappe),
  marqueur (pid vivant/mort), `en-cours` posé pendant la frappe puis effacé,
  affichage dans `etat`. Suite : 120 tests OK.
- Vérifié en réel le 26/09 : 6 périphériques surveillés, vigie active dans
  `etat`, arrêtée avec le cadre. RESTE À FAIRE avec Félix : un vrai clic et
  une vraie touche pendant un `taper` (xdotool ne génère pas d'évènement
  evdev, seul un geste physique teste la chaîne complète).
- Non fait, sciemment : le marqueur `dictee-en-cours` prévu au plan
  (`jarvis-dictee` est coupée depuis le 25/09 et tape par xdotool, invisible
  pour la vigie ; seul le bouton 8 de la souris, geste de Félix, arrêterait).

### Fidélité de l'instantané — 26/09/2026

`voir` capture maintenant les pixels après le parcours AT-SPI : une réponse
arrivée pendant ce parcours ne laisse plus une image antérieure à côté du
texte plus récent de l'arbre. Si la fenêtre active a changé pendant le
parcours, ses anciens repères sont écartés et un avertissement apparaît.
Le filtrage OCR conserve la géométrie relevée pour la capture, même si la
fenêtre se déplace ensuite. L'image et son OCR portent le même horodatage ;
la sortie indique l'âge de la capture, délai de lecture compris.

C'est une suite d'instantanés des pixels X11, pas un flux vidéo continu.
L'OCR peut se tromper et AT-SPI reste une lecture séparée : pour une interface
en animation, refaire `voir` avant d'agir. Quatre régressions automatisées
simulent une réponse qui arrive, un changement de fenêtre, un déplacement
après capture et le délai OCR, sans toucher au bureau réel.

### Pilotage rapide — 26/09/2026

- `ecran voir --rapide` : image fraîche sans OCR ni AT-SPI. Le cadrage habituel
  fonctionne (`--zone`, `--plein`, `--apercu`, `--tuile`, `--autour X,Y`). Les
  options d'arbre `--fenetre`/`--tout` et les références textuelles `--autour`
  sont refusées dans ce mode. Les anciens ids numériques et `tN` sont invalidés.
- `ecran cliquer 500 500 --voir` : fournit directement `ecran.png` et
  `ecran.json` à partir de la dernière capture de vérification, sans nouvelle
  capture ni OCR supplémentaire. Disponible aussi sur `taper`, `touche` et
  `defiler`. Les délais de stabilisation et `--verifier` restent inchangés.
- `--profil` : ligne JSON `PROFIL` sur stderr, avec durée totale monotone,
  durées et compteurs par phase. Les phases sont inclusives (la vérification
  contient des captures) : ne pas les additionner. Aucun contenu d'écran ni
  argument saisi dans cette sortie ; le journal d'actions existant reste inchangé.

Boucle conseillée : réserver avec `ecran prendre`, observer avec
`ecran voir --rapide`, **ouvrir et inspecter l'image**, agir avec `--voir`,
puis **ouvrir et inspecter la nouvelle image avant l'action suivante**.
Les coordonnées de l'image se convertissent avec `--img`. Pour lire précisément
un texte ou retrouver un identifiant, employer `voir`, `texte` ou `chercher`.
Terminer par `ecran stop`. Aucun flux vidéo ni service supplémentaire.

Si le focus change pendant la capture après action, l'image entière est
fournie avec un avertissement plutôt qu'un cadrage d'une autre fenêtre.
Une erreur de capture interrompt la commande ; ne pas réutiliser l'ancien PNG.

Mesure locale du 26/09, dix répétitions alternées sur la fixture fictive
`comptoir-2560.png` (2560×1440), OCR réel et arbre AT-SPI vide simulé :
`cliquer` puis `voir` prennent **2,244 s** en médiane ; `cliquer --voir`
prend **0,607 s**, soit environ **73 % de moins** pour cette boucle d'outils.
Captures : 4 → 3 ; OCR : 1 → 0 ; sélection AT-SPI : 1 → 0.
Cette mesure exclut le temps de raisonnement et de consultation de l'image
par l'agent, et ne constitue pas une mesure sur une application réelle.

## 09/10 soir — `veille` : l'écran en événements, sans image (PLAN-VOIR niveau 1)

Demande de Félix : « voir ce qui se passe en live » sans passer par une image. `veille.py` (service utilisateur
`pilote-ecran-veille`, python système, lecture seule, `~/.config/systemd/user/pilote-ecran-veille.service`) écrit une ligne
JSON par événement dans `/run/user/1000/pilote-ecran/veille.jsonl` (`ts`, `src`, `ev`, `msg` lisible + champs) ;
`ecran veille [N] [--src fenetre,atspi,proc,gpu,audio,pilote,ecran] [--suivre [S]] [--json]` la lit, sans X, sans
réservation, sans cadre. Sources :

- `fenetre` : X11 (`_NET_ACTIVE_WINDOW`, `_NET_CLIENT_LIST`, titres) → activée, ouverte, fermée, titre ;
- `atspi` : focus, texte modifié (un événement par champ toutes les 2 s ; extrait seulement hors champ éditable, jamais
  un mot de passe ; 50 événements/s détaillés au plus ; libellé réécrit à l'identique tu, résumé ×30), alertes, pages ;
- `proc` : processus nés (vivants à deux relevés) et morts, utilitaires de shell et fils du noyau ignorés ;
- `gpu` : NVML — bande repos/actif/fort/saturé (deux relevés d'affilée), mémoire par pas de 1 Go, processus ;
- `audio` : `pactl subscribe` — flux qui jouent / pause / fin, micro pris / rendu ;
- `pilote` : STOP posé/levé, écran réservé/libéré ;
- `ecran` : capture réduite à 64×36 toutes les 2 s → calme / activité / vidéo (hystérésis), **FIGÉ** après 30 s sans
  aucun changement, rappel toutes les 5 min, « bouge à nouveau ».

Première recette, réelle : Hogwarts Legacy figé en jeu → « écran : calme 0 % » à 18:14:37, « FIGÉ depuis 30 s (fenêtre
active « Hogwarts Legacy ») » à 18:15:03, avant que Félix ne l'écrive ; puis « fenêtre fermée », « GPU : −7 042 Mo »,
« audio : fin Hogwarts Legacy (3 flux) » à la fermeture. Pièges : python-xlib n'a pas l'extension Damage (d'où la capture
réduite) ; libatspi lit d'abord la propriété X `AT_SPI_BUS`, périmée sur ce poste (socket d'un lanceur snap disparu) ;
`pactl` est localisé (`LC_ALL=C` pour l'analyser). Tests sans écran : `tests/test_veille.py` (16) ; base avant la veille :
131 tests dont 28 échecs préexistants (test_cli/test_actions).

### Le pont (18 h 33) — « un HDMI pour l'agent »

Félix : « un pont entre toi et l'écran, ou Charli et l'écran, qui permet de lire en instantané ». Le service garde l'état
de l'écran en données (`Etat`, reconstruit depuis les événements) et le sert :

- `ecran veille --etat` (lit `etat.json`, réécrit au plus une fois par seconde) : fenêtre active, fenêtres ouvertes,
  focus, écran (calme/vidéo/FIGÉ), GPU et ses processus, audio, micro, pilote, pages, derniers extraits et événements.
  Mesuré : 67 ms, sans capture ni OCR.
- `ecran veille --texte` : la fenêtre active en lignes AT-SPI compactes (`bouton « Envoyer » [focus]`), lue par le
  service DANS la boucle GLib (libatspi n'est pas sûre entre fils), 2 s max, deux interrogations pour Chromium ;
  0 élément (Steam, jeux) → repli annoncé sur `ecran texte --fenetre-active` (OCR). Mesuré : 270 ms.
- Pont HTTP `127.0.0.1:8792`, lecture seule, sans action possible : `/etat`, `/texte`, `/evenements?n=50&src=…`,
  `/flux?src=…` (SSE : chaque événement poussé à la seconde où il arrive, `: ping` toutes les 15 s). C'est le canal
  « instantané » pour Charli (documenté dans `~/jarvis/AGENTS.md` et la skill Hermès) et pour tout programme.
Le HDMI physique, lui, ne transporte que des pixels : c'est le seul endroit où la donnée n'existe plus. Le pont la prend
avant (niveau 0 et prises 2, 3, 5, 6 de PLAN-VOIR).

### L'œil, ou l'anti-graphique (18 h 41)

Félix : « tu as une carte graphique qui transforme du 3D en image ; utilise-la à l'envers, il faut un anti-graphique ».
`Oeil` (veille.py) : à chaque changement notable (fenêtre activée ou ouverte, état d'écran, FIGÉ, alerte AT-SPI,
processus GPU, audio, STOP) et au plus toutes les 5 s, plus un rafraîchissement par minute quand l'écran bouge, la
capture réduite à 1 280 px part à Qwen vision (vLLM local `jarvis-llm` :8016, le moteur d'`ecran decrire`, réflexion
coupée par `chat_template_kwargs.enable_thinking=false`, 1 500 jetons) avec une consigne qui exige une SCÈNE JSON :
`scene`, `type` (bureau / application / page web / jeu / vidéo / dialogue / terminal), `elements` (≤ 12 : type, libellé,
zone 3×3, état), `evenement`, `alerte`, `texte_cle` (≤ 5). Pas d'OCR, pas de prose pour un humain. Publié sur le bus
(`src: oeil`), dans l'état (`scene`), et à la demande : `ecran veille --oeil` ou `GET /oeil`.
Première scène réelle, 18:41:46 : « la fenêtre des paramètres Steam, onglet Compte, par-dessus un fond d'écran de volcan
au coucher du soleil — consultation des paramètres ». Le modèle dit « bureau Windows » pour GNOME : l'œil se trompe sur
ce qu'aucune donnée ne lui dit ; la fenêtre X11 et AT-SPI, eux, savent. Règle : la donnée prime, l'œil complète.
Premiers essais : 700 jetons et la réflexion de Qwen3 tronquaient le JSON (« réponse non structurée ») ; `extraire_json`
ignore aussi un bloc `<think>`. Moteur absent (vLLM arrêté pour un jeu : il prend 30 Go de la 5090) → une seule erreur,
nouvel essai toutes les 5 min. Tests : `tests/test_veille.py` (20, faux moteur HTTP).
Correctifs de 18 h 50 : l'œil reçoit les FAITS CERTAINS du bus (GNOME, fenêtre active X11, état, carte, audio, focus)
et n'a plus le droit de lire lettre à lettre (noms, adresses, numéros → `[adresse mail]`, `[numéro]`, vérifié par le
code) ; « FIGÉ » n'est dit que si l'écran bougeait avant ou qu'un jeu ou une vidéo tourne, sinon « immobile, rien ne se
passe ». Puis Félix l'a mis de côté (« ce n'est pas exactement ce que je voulais ») : l'œil reste, dormant sans vLLM.

### 19 h — le capteur direct (X Damage), le mode texte, l'expérience « anti-graphique »

Félix : « pas forcément un jeu : tout l'écran, comme si, quand les choses allaient s'afficher, c'était direct » ;
« lire comme s'il n'y avait pas de graphisme ».
- **`dessin` (classe `Dessin`)** : plus de capture toutes les 2 s. Chaque fenêtre cliente reçoit un objet Damage
  (ctypes sur libX11 + libXdamage, connexion X à part, gestionnaire d'erreurs muet : une fenêtre disparue ne tue pas
  le service) ; le serveur X signale la zone redessinée à la milliseconde, comme au compositeur. Agrégé par fenêtre
  toutes les 250 ms (`AgregeDessin`) : « s'affiche », « redessine zone L×H en (x,y) » (lent, < 5/s), « en continu
  N/s » (≥ 10/s, rappel 30 s), « s'arrête après N s ». Rythme lent → lecture AT-SPI de la zone (`LecteurTexte.zone`,
  dans la boucle GLib, ≤ 1/s) : ce qui vient de s'afficher, en données. Part d'écran qui change = la pire période des
  2 dernières secondes (somme des boîtes, une par fenêtre) → calme/activité/vidéo/FIGÉ sans aucune image. Le premier
  rapport d'une fenêtre (toute sa surface) est ignoré 0,5 s. Trouvaille du premier démarrage : Proton Mail Bridge
  redessine sa fenêtre 32 fois par seconde, en permanence.
- **`ecran veille --texte --tout`** (`/texte?tout=1`) : tout l'écran « en mode texte », fenêtre par fenêtre (AT-SPI) :
  « lire comme s'il n'y avait pas de graphisme », ce qu'un lecteur d'écran fait pour un aveugle.
- **Expérience « anti-graphique »** (`flux/antigraphique.py`, à la demande de Félix : « prends une image et regarde si
  tu peux la retranscrire en code, avec la carte graphique ») : capture 2560×1440 → Qwen vision sur la 5090 → code SVG
  (105 éléments, 8 408 caractères, 4 042 jetons, 22,7 s) → rendu rsvg → comparaison. La structure y est (fenêtre
  Steam, menu de 16 entrées, boutons, libellés, icônes du bureau, barre du bas) ; la photo de fond devient un triangle
  sur un dégradé ; corrélation des gris 0,34, des contours 0,45 ; « 19:04 » lu « 1904 », adresse mail inventée.
  Verdict : la carte sait faire l'anti-graphique pour la structure, pas pour les pixels ni les petits caractères ;
  pour une fenêtre qui a un arbre, AT-SPI donne le « code » exact en 0,3 s, sans inférence.
- **Expérience du 1 cm (19 h 20, sur le go de Félix)** : « déplace la fenêtre d'un centimètre et regarde ce qui change ».
  `ecran fenetre "Steam Settings" --deplacer 43 0` (nouvelle option, xdotool windowmove relatif sous `agir()`), puis la
  carte retranscrit l'écran une seconde fois. Pixels : 166 333 pixels changés (4,5 % de l'écran), 499 k nombres.
  Code de la carte : deux transcriptions indépendantes, 152 lignes retirées, 138 ajoutées, 30 identiques sur ~180 ;
  le déplacement y est (x de « Friend Code » +21 à l'échelle ½, soit les 43 px) mais noyé dans le bruit : la carte
  redécrit tout, différemment, à chaque regard. Données : X11 a dit « x 686→729 », un nombre (l'événement
  `fenetre/geometrie` du bus n'est sorti qu'au retour : les géométries n'étaient pas initialisées au démarrage, corrigé).
  Leçon : le principe de Félix (un changement = une petite différence de code) exige une transcription INCRÉMENTALE :
  ne retranscrire que la zone que Damage signale et rapiécer le code en place ; une retranscription complète par la carte
  ne donne jamais un diff petit. Fenêtre remise en place après l'essai.
  **Même essai sur une fenêtre qui déclare son contenu (Fichiers, 19 h 25, Steam fermée par Échap, Nautilus ouvert par
  `ecran lancer`)** : pixels 295 769 changés (8 %), 887 k nombres ; code de la carte 266 lignes différentes sur ~180
  (réponse coupée à 6 000 jetons, `extraire_svg` referme désormais un document tronqué) ; contenu AT-SPI 334 lignes
  avant, 334 après, **0 différente** ; X11 : un événement `fenetre/geometrie`, « x −61→−18 », 324 caractères, un nombre.
  Le principe de Félix est vérifié sur la voie des données, et seulement sur elle. Nautilus garde un décalage de −61,−23
  (ombre CSD) dans ses coordonnées client ; `geometrie_x11` d'`ecran` donne la boîte visible (0→43).
- **La carte (19 h 35)** — Félix : « en Python, ce qu'il y a sur l'écran et à quel endroit, et l'analyse de ce qu'il y a
  là, lu en déduction, très très rapidement ». Classe `Carte` (veille.py) : pour chaque fenêtre X11 (position et taille
  écran, tenues à jour par ConfigureNotify) ses éléments AT-SPI avec x, y, l, h écran, et une **déduction**
  (`deduire` : genre navigateur / éditeur / terminal / gestionnaire de fichiers / courrier / messagerie / muette…, ce
  que la fenêtre attend : une réponse à un dialogue, une saisie dans le champ qui a le focus, boutons visibles). Mise à
  jour sur événement (ouverture, titre, géométrie, redessin ; 2 fenêtres par seconde, l'active d'abord, dans la boucle
  GLib), lue en mémoire : `ecran veille --carte [--fenetre MOTIF]` et `GET /carte?fenetre=…`. La déduction de chaque
  fenêtre apparaît aussi dans `ecran veille --etat`. Tests : 27. Mesuré : lecture de la carte en 16 ms ; relecture
  d'une fenêtre Nautilus de 334 éléments en 757 ms, des petites fenêtres en 370-580 ms.
  Correspondance fenêtre X11 ↔ application AT-SPI par **pid** (`_NET_WM_PID` = `get_process_id()`) puis titre, sans
  xdotool : Chrome ne nomme pas ses cadres et `wid_de` tombait sur une mauvaise fenêtre.
  **Limites vérifiées (19 h 45, sondes hors confinement snap)** : Chrome, VS Code, Codex, Mailspring, WhatsApp
  (Chromium/Electron) n'exposent leur arbre que pour la fenêtre AU PREMIER PLAN ; en arrière-plan, le cadre est vide
  (0 enfant, `showing=False`). La carte les relit à l'activation (`fenetre/activee`, puis deux essais à 2,5 s). Firefox
  snap : cadre de 33 enfants mais `parcourir` n'en retient aucun (états non « affichés ») : à creuser. Steam (CEF), Tk :
  rien. Pour ces fenêtres, `ecran texte` (OCR) et l'œil restent les secours.
  **Première navigation par la carte (19 h 48-52, « essaye de l'utiliser pour aller sur GitHub »)** : Chrome du profil
  pilote (`--user-data-dir=~/.local/share/pilote-chrome`, accessibilité forcée) ouvert par `ecran lancer` sur
  github.com ; la carte lit 81 éléments positionnés, dont la boîte de choix du moteur de Chrome (radios en x=1358) et le
  bouton « Search or jump to » [1898,158 201×32] ; clics aux centres calculés depuis la carte, frappe « claude code »,
  Entrée ; page « Repository search results » lue par la carte : 345 éléments, « 264k results », anthropics/claude-code en
  tête ; clic sur le lien [390,287]. Aucune image, aucun OCR. Lecture de la carte : 12-16 ms ; relecture d'une page
  Chrome après changement : 0,5-1 s. Piège corrigé : `Fenetres.noms` n'était pas mis à jour au changement de titre, la
  carte gardait « Sans titre - Google Chrome » (filtre `--fenetre GitHub` vide).
- **20 h 25 — « déjà le bureau, vas-y » : trois trous bouchés.**
  (a) *Arrière-plan* : quand Chrome/Electron vident leur arbre, la carte garde la dernière lecture faite au premier
  plan, datée (`perime: true`, « lu à HH:MM:SS, en arrière-plan ») au lieu de « sans arbre ».
  (b) *Nos applis parlent* : `POST http://127.0.0.1:8792/annonce` `{app, titre, lignes[], etat{}}` ; le pont range
  l'annonce par titre et par app, publie `annonce/affiche` si le contenu change, et la carte l'attache à la fenêtre du
  même titre (`déclare : …`). Premier client : `pharmiosv3/v3/tpe_simule.py` (`_annoncer`, fil détaché, jamais bloquant,
  silencieux sans pont) — le TPE Tk, muet en AT-SPI, est lu exactement.
  (c) *DevTools* (`cdp.py`, la prise 1 de PLAN-VOIR) : WebSocket client minimal (RFC 6455, stdlib), `Session`
  (appels/réponses/événements), `Devtools` (ports `PILOTE_CDP_PORTS` = 9222-9224 : pages suivies, `Page.frameNavigated`,
  `loadEventFired`, erreurs de console et exceptions → bus `devtools/*`), `lire(titre, geometrie)` = `Accessibility.
  getFullAXTree` + `DOMSnapshot.captureSnapshot` → éléments en pixels écran (origine = fenêtre X11 + bordure Chrome
  `outerHeight − innerHeight`). La carte s'en sert pour toute fenêtre Chrome/Electron (devant OU derrière) et garde en
  plus l'interface de Chrome par AT-SPI quand elle est devant (`source: devtools+atspi`). Chrome du pilote relancé avec
  `--remote-debugging-port=9222` (unité `pilote-app-google-chrome-*`, `pharmios.desktop` mis à jour) : le comptoir est lu
  en arrière-plan, 146 éléments. Tests : `tests/test_cdp.py` (faux serveur WebSocket, conversion AX+DOM), 31 au total.
  Non fait : le Chrome personnel de Félix (relancer avec `--remote-debugging-port=9223`, à sa décision : un port DevTools
  expose la session à tout programme local) ; Steam (`-cef-enable-debugging` écoute sur 8080, déjà pris sur ce poste) ;
  Firefox.
- **Couche Vulkan** (`flux/pilote_flux.c`, `construire.sh`, manifeste utilisateur activé par `PILOTE_FLUX=1`) :
  compilée, insérée à l'instance par le chargeur (`VK_LOADER_DEBUG=layer`), test d'appareil non concluant (rien
  d'écrit) — mise de côté à la demande de Félix (« le jeu, on s'en fout »). Inerte sans la variable.

## 28/10 midi — Indra OCR en complément de tesseract

- Félix : « installe Indra pour le pilote écran ». **Indra OCR** (datalab, 1 M de paramètres, ONNX **sur CPU**, 18 Mo,
  code Apache 2.0 / poids OpenRAIL-M) vit dans un venv séparé `~/jarvis/ocr-indra/venv` (`pip install indra-ocr`,
  modèle dans le cache Hugging Face). Mesures sur une capture 2560×1440 : 93 lignes / ~400 mots en 1,2 s (0,19 s d'OCR,
  le reste est le lancement du processus) contre 353 mots en 1,5 s pour tesseract. Limites : **une boîte par ligne**
  (pas par mot), deux onglets voisins parfois collés en une ligne, accents perdus. Donc **complément, pas remplacement**.
- `perception._indra()` : `venv/bin/python -m indra_ocr capture.png --format json --cores 8` (`HF_HUB_OFFLINE=1`,
  PNG temporaire dans /dev/shm), boîtes de mots au prorata des caractères de la ligne, `source: indra`, et **jamais
  d'exception** (venv absent ou échec → `[]`, le pilote continue avec tesseract). `perception.ocr()` le lance **en
  parallèle** des deux passes tesseract (3 threads) et n'ajoute que ses mots contenant une lettre ou un chiffre qui ne
  recouvrent aucun mot déjà lu. Fusion mesurée : 409 mots en 1,6 s (+56, ex. « DOCUMENTATION », « Plex »,
  « Kaamelott », « carol »).
- Réglage par variable d'environnement : `PILOTE_OCR=fusion` (défaut) | `tesseract` (comme avant) | `indra` (seul).
  Le test `test_capture_reduite_illisible` épingle tesseract : Indra lit une capture réduite (470 mots), ce que le
  test interdit volontairement à tesseract. Retour arrière : `perception.py.bak-20261028-indra`.

## 01/11/2026 — « hors vue » : un élément défilé hors du cadre ne se clique plus

Vécu pendant un virement au CIC : AT-SPI annonçait le bouton « Valider » en y = 1391 alors que la fenêtre Firefox,
maximisée, finit en y = 1373. `ecran cliquer 68` envoyait donc le clic **sous** la fenêtre, sur le dock GNOME, qui
relançait Zoom à chaque essai ; Zoom prenait le focus et on tournait en rond. Les coordonnées AT-SPI d'une page web
restent exactes pour un élément défilé hors du cadre : seul le cadre visible de la fenêtre dit s'il est atteignable.

- `hors_vue(e, geo, taille_ecran)` (ecran.py) : vrai si le centre de l'élément sort de la géométrie visible de sa
  fenêtre (`geometrie_x11`) ou de l'écran.
- `voir` ajoute `⚠ HORS VUE (défiler avant de cliquer)` à la ligne de l'élément.
- `cliquer <id>` refuse un tel élément et dit quoi faire : `ecran defiler bas|haut` ou `ecran touche End`, puis `voir`.
- Les clics par coordonnées (`cliquer X Y`) ne sont pas bloqués : l'agent y assume la cible.
- Tests : `tests/test_hors_vue.py`. Sauvegarde : `ecran.py.bak-20261101-horsvue`.
