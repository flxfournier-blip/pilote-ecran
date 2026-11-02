#!/bin/bash
# recette-ecran.sh — recette de `ecran` SUR LE VRAI ÉCRAN (ex-tests.sh, 08/09 → 26/09/2026).
#
# Elle déplace réellement la souris, ouvre une calculatrice et une petite
# fenêtre Tk, et lève des STOP qu'elle a elle-même provoqués. Elle ne part
# donc que sur le GO de Félix, donné de SA main :
#
#     touch /run/user/$(id -u)/pilote-ecran/go-recette     # un GO = une recette
#     ~/jarvis/pilote-ecran/recette-ecran.sh [-v]
#
# Refus (exit 2, rien n'est touché) : STOP en vigueur (posé par Félix : seul
# `ecran reprendre` après son GO le lève, pas une recette), écran réservé par
# un autre agent, GO absent. Les tests SANS écran sont ailleurs :
#     /usr/bin/python3 -m unittest discover -s tests -t .
set -u
ICI=$(cd -- "$(dirname -- "$(readlink -f "$0")")" && pwd)
export PATH="$ICI:$PATH"          # `ecran` = le lanceur de CE dossier
export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-/run/user/$(id -u)/gdm/Xauthority}"
ETAT="${PILOTE_ETAT:-/run/user/$(id -u)/pilote-ecran}"
VERBEUX=${1:-}
OK=0; KO=0; NOMS_KO=()
mkdir -p "$ICI/recettes"
JOURNAL="$ICI/recettes/$(date +%Y%m%d-%H%M%S).log"

# ── Garde-fous AVANT toute action ──
if [ -f "$ETAT/stop" ]; then
    echo "⛔ STOP en vigueur : $(cat "$ETAT/stop")"
    echo "   La recette ne lève pas un arrêt de Félix. Rien n'est exécuté."
    exit 2
fi
if [ -f "$ETAT/pilote" ] && [ -f "$ETAT/actif" ] \
   && [ $(( $(date +%s) - $(stat -c %Y "$ETAT/actif") )) -lt 300 ]; then
    echo "⛔ écran réservé : $(cat "$ETAT/pilote") — rien n'est exécuté."
    exit 2
fi
if [ ! -f "$ETAT/go-recette" ]; then
    echo "⛔ pas de GO de Félix : la recette bouge la souris et ouvre des fenêtres."
    echo "   Félix : touch $ETAT/go-recette   puis relance."
    exit 2
fi
rm -f "$ETAT/go-recette"          # un GO = une recette
export PILOTE_AGENT=recette

UNITES=()                         # unités lancées PAR cette recette, et elles seules
x0=$(xdotool getmouselocation --shell | sed -n 's/^X=//p')
y0=$(xdotool getmouselocation --shell | sed -n 's/^Y=//p')
nettoyer() {
    for u in "${UNITES[@]}"; do systemctl --user stop "$u" >/dev/null 2>&1; done
    PILOTE_AGENT=recette ecran stop >/dev/null 2>&1
    [ -n "${x0:-}" ] && xdotool mousemove "$x0" "$y0" >/dev/null 2>&1
    echo "journal : $JOURNAL"
}
trap nettoyer EXIT

t() {  # t "nom" "condition bash" — sortie gardée dans le journal
    local nom="$1"; shift
    local sortie
    sortie=$(eval "$@" 2>&1); local r=$?
    { echo "── $nom  [exit $r]"; echo "\$ $*"; echo "$sortie"; } >> "$JOURNAL"
    [ "$VERBEUX" = "-v" ] && echo "$sortie" | sed 's/^/      /'
    if [ $r -eq 0 ]; then
        printf "  \033[32m✓\033[0m %s\n" "$nom"; OK=$((OK+1))
    else
        printf "  \033[31m✗\033[0m %s\n" "$nom"; KO=$((KO+1)); NOMS_KO+=("$nom")
    fi
}
souris_a() { xdotool mousemove "$1" "$2"; sleep 0.3; }
lancer() {  # lance via ecran et retient l'unité pour le ménage
    local sortie; sortie=$(ecran lancer "$@" 2>&1); echo "$sortie" >> "$JOURNAL"
    local u; u=$(echo "$sortie" | sed -n 's/.*\[unité \(pilote-app-[^]]*\)\].*/\1/p')
    [ -n "$u" ] && UNITES+=("$u")
}

# Fenêtre Tk de la recette : Tk n'a AUCUN arbre AT-SPI, seule la couche texte
# (OCR de la capture) permet de la piloter. Position et taille fixes.
TK_ZONE="400 300 600 400"
lancer_tk() {
    [ -n "${TK_LANCEE:-}" ] && return
    lancer /usr/bin/python3 -c "
import tkinter as t
r = t.Tk(); r.title('Recette ecran'); r.geometry('600x400+400+300'); r.configure(bg='white')
v = t.StringVar(value='Compté 0 fois'); n = [0]
def compter():
    n[0] += 1; v.set(f'Compté {n[0]} fois')
t.Label(r, text='Recette ecran', font=('DejaVu Sans', 18), bg='white').pack(pady=20)
t.Label(r, textvariable=v, font=('DejaVu Sans', 18), bg='white').pack(pady=10)
t.Button(r, text='Compter', font=('DejaVu Sans', 18), command=compter).pack(pady=10)
e = t.Entry(r, font=('DejaVu Sans', 18), width=20); e.insert(0, 'nom'); e.pack(pady=10)
salut = t.StringVar(value=''); t.Label(r, textvariable=salut, font=('DejaVu Sans', 18), bg='white').pack()
e.bind('<Return>', lambda _ev: salut.set('Bonjour ' + e.get()))
r.mainloop()"
    TK_LANCEE=1; sleep 1.5
}

echo "── 1. Environnement"
t "lanceur en python système (gi/PIL)" "head -1 $ICI/ecran | grep -q '^#!/usr/bin/python3$'"
t "utilisable depuis un venv (piège 'No module named gi')" \
  "bash -c '. ~/hermes-venv/bin/activate 2>/dev/null; ecran etat' | grep -q 'cadre'"
t "X11 injoignable → refus propre" "! DISPLAY=:77 ecran voir"

echo "── 2. Cadre et garde-fou"
t "reprendre affiche le cadre" "ecran reprendre | grep -q 'cadre affiché'"
t "fichier 'pret' écrit par le cadre" "[ -f $ETAT/pret ]"
t "etat voit le cadre actif" "ecran etat | grep -q 'cadre : actif'"
ecran souris 1400 900 >/dev/null 2>&1; sleep 0.3   # position posée PAR ecran, puis geste brut
souris_a 700 300 >/dev/null; sleep 1.2
t "souris bougée entre 2 commandes → stop signé [cadre]" "grep -q '^\[cadre\].*1400,900 → 700,300' $ETAT/stop"
t "action refusée après stop" "! ecran souris 1000 500"
t "voir reste permis (lecture seule)" "ecran voir --sans-marques | grep -q 'Fenêtre active'"
t "etat affiche la raison de l'arrêt" "ecran etat | grep -q 'STOP'"
ecran reprendre >/dev/null 2>&1
t "reprendre (stop provoqué par la recette) l'efface" "[ ! -f $ETAT/stop ]"

echo "── 3. occupe orphelin (le kill-switch ne doit pas être désarmé)"
ecran souris 1200 700 >/dev/null 2>&1
echo 4194304 > "$ETAT/occupe"          # pid mort, AUCUNE commande ecran ensuite
souris_a 600 400 >/dev/null; sleep 1.4
t "occupe orphelin ignoré par le cadre → stop" "[ -f $ETAT/stop ]"
t "occupe orphelin effacé" "[ ! -f $ETAT/occupe ]"
ecran reprendre >/dev/null 2>&1

echo "── 4. Cadre après une pause (bug du 25/09 : relancé, il mourait en 200 ms)"
touch -d '-400 seconds' "$ETAT/actif" 2>/dev/null
systemctl --user stop pilote-ecran-cadre >/dev/null 2>&1; sleep 0.3
ecran souris 1250 750 >/dev/null 2>&1; sleep 1.5
t "cadre relancé et toujours vivant 1,5 s après" "ecran etat | grep -q 'cadre : actif'"

echo "── 5. Verrou (un seul pilote)"
( ecran voir --tout --sans-marques >/dev/null 2>&1 & ) ; sleep 0.25
d0=$(date +%s%N); ecran souris 1300 800 >/dev/null 2>&1; d1=$(date +%s%N)
attente=$(( (d1-d0)/1000000 ))
t "2e commande sérialisée (attente ${attente} ms > 200)" "[ $attente -gt 200 ]"

echo "── 6. Souris déjà à la cible (ex-blocage mousemove --sync)"
ecran souris 1400 900 >/dev/null 2>&1
d0=$(date +%s%N); timeout 8 ecran souris 1400 900 >/dev/null 2>&1; r=$?; d1=$(date +%s%N)
t "2e déplacement identique ne bloque pas" "[ $r -eq 0 ] && [ $(( (d1-d0)/1000000 )) -lt 2000 ]"

if command -v gnome-calculator >/dev/null; then
    echo "── 7. Cycle fonctionnel complet (calculatrice GTK4)"
    lancer gnome-calculator; sleep 1
    ecran fenetre Calculatrice >/dev/null 2>&1
    LISTE=$(ecran voir --sans-marques 2>/dev/null)
    NB_BOUTONS=$(printf '%s' "$LISTE" | grep -c 'bouton')
    t "arbre AT-SPI lu ($NB_BOUTONS boutons, > 20 attendus)" "[ $NB_BOUTONS -gt 20 ]"
    id_de() { echo "$LISTE" | awk -v n="$1" '$0 ~ "« "n" »" {print $1; exit}'; }
    for k in C 8 × 7 =; do ecran cliquer "$(id_de "$k")" >/dev/null 2>&1; done
    NOUV=$(ecran voir --sans-marques 2>/dev/null)
    IDCHAMP=$(echo "$NOUV" | awk '$2=="champ"{print $1; exit}')
    RES=$(ecran lire "$IDCHAMP" 2>/dev/null | tr -d ' \n')
    t "8 × 7 = 56 lu par AT-SPI (obtenu : ${RES:-vide})" "[ \"$RES\" = 56 ]"

    echo "── 8. Clic par id après changement de fenêtre (2e fenêtre lancée par la recette) — lot 4"
    lancer_tk
    t "fenetre : focus vérifié (Tk active après ✓)" "ecran fenetre 'Recette ecran' && xdotool getactivewindow getwindowname | grep -q 'Recette ecran'"
    ID_C=$(printf '%s' "$NOUV" | awk '/« C »/{print $1; exit}')
    t "clic par id : la fenêtre de l'élément est activée d'abord" \
      "ecran cliquer $ID_C 2>&1 | grep -q \"fenêtre de l'élément activée\" && xdotool getactivewindow getwindowname | grep -q Calculatrice"
    PLEINE=$(ecran voir --sans-marques --sans-ocr 2>/dev/null)
    GX=$(xdotool search --name Calculatrice getwindowgeometry --shell 2>/dev/null | sed -n 's/^X=//p' | head -1)
    ID_7=$(echo "$PLEINE" | awk '/« 7 »/{print $1; exit}')
    ZONE=$(ecran voir --zone 0 0 2560 1440 --sans-marques --sans-ocr 2>/dev/null)
    t "voir --zone garde les numéros de la liste complète" "echo \"\$ZONE\" | grep -qE \"^ *$ID_7 +bouton +« 7 »\""

    echo "── 9. Série de touches interrompue par la souris"
    ecran fenetre Calculatrice >/dev/null 2>&1 && ecran reprendre >/dev/null 2>&1
    ( sleep 0.4; xdotool mousemove 900 350 ) &
    SORTIE=$(ecran touche 1 2 3 4 5 6 7 8 2>&1)
    wait
    t "frappe interrompue et abandon signalé" "echo \"\$SORTIE\" | grep -q 'ARRÊTÉ pendant la frappe'"
    ecran reprendre >/dev/null 2>&1
else
    echo "── 7-9. sautés : gnome-calculator absent"
fi

echo "── 10. Appli SANS arbre AT-SPI (Tk) : couche texte, lot 1"
lancer_tk
ecran fenetre "Recette ecran" >/dev/null 2>&1; sleep 0.5
VUE=$(ecran voir --sans-marques 2>&1)
echo "$VUE" >> "$JOURNAL"
t "voir : ligne ETAT en tête" "echo \"\$VUE\" | head -1 | grep -q '^ETAT ok'"
t "voir : 0 élément AT-SPI mais « Compter » lu par OCR" \
  "echo \"\$VUE\" | grep -q '0 élément AT-SPI' && echo \"\$VUE\" | grep -q '« Compter »'"
t "chercher Compter (zone Tk) → exit 0" "ecran chercher Compter --zone $TK_ZONE"
t "chercher zzz → exit 2" "ecran chercher 'zzz introuvable' --zone $TK_ZONE; [ \$? -eq 2 ]"
t "cliquer @Compter" "ecran cliquer @Compter --zone $TK_ZONE"
sleep 0.4
t "l'appli a compté 1 (relu par OCR)" "ecran chercher 'Compté 1 fois' --zone $TK_ZONE"
t "cliquer tN (ligne relue par texte)" \
  "ID=\$(ecran texte --zone $TK_ZONE | awk '/« Compter »/{print \$1; exit}'); [ -n \"\$ID\" ] && ecran cliquer \$ID"
sleep 0.4
t "lire --zone : « Compté 2 fois »" "ecran lire --zone $TK_ZONE | grep -q 'Compté 2 fois'"
t "cliquer --verifier : « Compté 3 fois » relu" "ecran cliquer @Compter --zone $TK_ZONE --verifier 'Compté 3 fois'"
t "verdict : changement localisé dans la fenêtre Tk" "ecran cliquer @Compter --zone $TK_ZONE | grep -q '→ changement'"
t "diff --texte : « Compté 4 fois » apparu" "ecran diff --texte --zone $TK_ZONE | grep -q '+ « Compté 4 fois »'"
ecran cliquer @nom --zone $TK_ZONE >/dev/null 2>&1 && ecran touche End >/dev/null 2>&1
t "taper --dans FENÊTRE --entree" "ecran taper Felix --dans 'Recette ecran' --entree"
t "attendre --texte Bonjour (≤ 5 s)" "ecran attendre --texte 'Bonjour nomFelix' --zone $TK_ZONE --max 5"
t "touche --dans Mailspring refusée (Tk active), rien envoyé" "! ecran touche Return --dans Mailspring"
t "attendre --stable 1" "ecran attendre --stable 1 --zone $TK_ZONE --max 10"
t "texte plein écran < 3 s" "d0=\$(date +%s%N); ecran texte >/dev/null; [ \$(( (\$(date +%s%N)-d0)/1000000 )) -lt 3000 ]"

echo "── 11. Réservation entre agents (verrou de TÂCHE)"
ecran stop >/dev/null 2>&1
t "prendre réserve l'écran" "PILOTE_AGENT=agentA ecran prendre | grep -q 'agentA'"
t "un autre agent ne peut pas agir" "! PILOTE_AGENT=agentB ecran souris 1200 700"
t "un autre agent peut regarder" "PILOTE_AGENT=agentB ecran voir --sans-marques | grep -q 'Fenêtre active'"
t "un autre agent ne peut pas prendre" "! PILOTE_AGENT=agentB ecran prendre"
t "le détenteur agit" "PILOTE_AGENT=agentA ecran souris 1250 750"
t "etat nomme le pilote" "PILOTE_AGENT=agentB ecran etat | grep -q 'PAS toi'"
t "stop d'un AUTRE agent refusé (lot 5)" "PILOTE_AGENT=agentB ecran stop; [ \$? -eq 4 ]"
PILOTE_AGENT=agentA ecran stop >/dev/null 2>&1
t "stop du détenteur libère la réservation" "PILOTE_AGENT=agentB ecran prendre | grep -q agentB"
t "journal : les actions sont tracées (lot 5)" "ecran journal 50 | grep -q souris"
PILOTE_AGENT=agentB ecran stop >/dev/null 2>&1

echo "── 12. Ménage"
for u in "${UNITES[@]}"; do systemctl --user stop "$u" >/dev/null 2>&1; done
sleep 1
t "fenêtres de la recette refermées" "! ecran fenetres | grep -qiE 'calculatrice|Recette ecran'"
ecran stop >/dev/null 2>&1
t "cadre retiré" "! ecran etat | grep -q 'cadre : actif'"
t "aucune unité de la recette résiduelle" \
  "[ -z \"\$(for u in ${UNITES[*]:-}; do systemctl --user is-active \$u 2>/dev/null | grep -x active; done)\" ]"

echo
if [ $KO -eq 0 ]; then
    printf "\033[32m%d/%d tests passés\033[0m\n" "$OK" "$((OK+KO))"
else
    printf "\033[31m%d/%d passés — %d échec(s)\033[0m\n" "$OK" "$((OK+KO))" "$KO"
    printf "  ✗ %s\n" "${NOMS_KO[@]}"
fi
{ echo; echo "RÉSULTAT : $OK/$((OK+KO))"; } >> "$JOURNAL"
exit $((KO > 0))
