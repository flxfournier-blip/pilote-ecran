//! Le regard : la page qu'un agent lit à la place d'une image.
//!
//! Une scène en une ligne, ce qui a changé depuis SON dernier regard, puis la fenêtre active en
//! plan (rôle, nom, contenu, états, référence `eN`), les autres fenêtres en une ligne chacune.
//! Le budget de lignes est tenu par priorité : dialogues, focus, contrôles, titres, textes.

use std::collections::{HashMap, HashSet};

use crate::modele::{heure, maintenant, Bureau, Diff, Element, Fenetre, SHELL};

#[derive(Clone, Debug, Default)]
pub struct Options {
    pub lecteur: String,
    pub fenetre: Option<String>,
    pub element: Option<String>,
    pub tout: bool,
    pub lignes: usize,
}

fn duree(s: f64) -> String {
    let s = s.max(0.0);
    if s < 1.0 {
        format!("{} ms", (s * 1000.0) as u64)
    } else if s < 60.0 {
        format!("{s:.0} s")
    } else if s < 3600.0 {
        format!("{} min {:02} s", (s / 60.0) as u64, (s % 60.0) as u64)
    } else {
        format!(
            "{} h {:02}",
            (s / 3600.0) as u64,
            ((s % 3600.0) / 60.0) as u64
        )
    }
}

fn couper(s: &str, n: usize) -> String {
    if s.chars().count() > n {
        format!("{}…", s.chars().take(n).collect::<String>())
    } else {
        s.to_string()
    }
}

fn ligne_element(e: &Element, marque: &str) -> String {
    let mut l = String::new();
    l += marque;
    l += &e.role;
    if !e.nom.is_empty() {
        l += &format!(" « {} »", couper(&e.nom, 90));
    }
    if let Some(v) = &e.valeur {
        l += &format!(" = {v}");
    }
    if let Some(t) = &e.texte {
        if t != &e.nom {
            l += &format!(" : {}", couper(t, 110));
        }
    }
    let mut etats: Vec<&str> = e.etats.iter().map(|s| s.as_str()).collect();
    if e.hors_vue {
        etats.push("HORS VUE");
    }
    if !etats.is_empty() {
        l += &format!(" ({})", etats.join(", "));
    }
    l += &format!("  {}", e.id);
    l
}

fn score(e: &Element, d: &Diff, doc_y: Option<i32>) -> i32 {
    let mut s = match e.role.as_str() {
        "dialogue" | "alerte" | "notif" => 100,
        "bouton" | "lien" | "champ" | "case" | "radio" | "interrupteur" | "onglet" | "menu"
        | "liste" | "curseur" | "compteur" => 60,
        "élément" | "cellule" => 45,
        "titre" | "document" => 50,
        "terminal" => 70,
        "texte" => 25,
        "image" | "icône" => 12,
        "barre-menu" | "barre-outils" | "ascenseur" | "barre-état" | "progression" => 8,
        _ => 20,
    };
    if e.etats.iter().any(|x| x == "focus") {
        s += 40;
    }
    if e.hors_vue {
        s -= 35;
    }
    if d.ajoutes.contains(&e.id) || d.modifies.contains(&e.id) {
        s += 30;
    }
    // Le chrome d'un navigateur (barres, marque-pages, onglets non sélectionnés) compte moins que la page.
    if let Some(dy) = doc_y {
        let selectionne = e.etats.iter().any(|x| x == "sélectionné" || x == "focus");
        if e.y + e.h <= dy
            && e.role != "document"
            && !selectionne
            && !matches!(e.role.as_str(), "dialogue" | "alerte" | "notif")
        {
            s -= 30;
        }
    }
    s
}

/// Choisit, dans l'ordre du document, les éléments qui tiennent dans le budget, avec leurs ancêtres.
fn selection<'a>(
    els: &'a [Element],
    d: &Diff,
    budget: usize,
) -> (Vec<&'a Element>, HashMap<String, usize>) {
    let mut omis: HashMap<String, usize> = HashMap::new();
    if els.len() <= budget {
        return (els.iter().collect(), omis);
    }
    let doc_y = els
        .iter()
        .filter(|e| e.role == "document")
        .map(|e| e.y)
        .min();
    let par_id: HashMap<&str, usize> = els
        .iter()
        .enumerate()
        .map(|(i, e)| (e.id.as_str(), i))
        .collect();
    let mut notes: Vec<(i32, usize)> = els
        .iter()
        .enumerate()
        .map(|(i, e)| (score(e, d, doc_y), i))
        .collect();
    notes.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)));
    let mut gardes: HashSet<usize> = HashSet::new();
    for (_, i) in notes {
        if gardes.len() >= budget {
            break;
        }
        gardes.insert(i);
        // Les ancêtres donnent le contexte (le dialogue qui contient le bouton).
        let mut p = els[i].parent.as_deref();
        let mut n = 0;
        while let Some(pid) = p {
            if n > 6 {
                break;
            }
            if let Some(&j) = par_id.get(pid) {
                gardes.insert(j);
                p = els[j].parent.as_deref();
            } else {
                break;
            }
            n += 1;
        }
    }
    for (i, e) in els.iter().enumerate() {
        if !gardes.contains(&i) {
            *omis.entry(e.role.clone()).or_insert(0) += 1;
        }
    }
    let choisis: Vec<&Element> = els
        .iter()
        .enumerate()
        .filter(|(i, _)| gardes.contains(i))
        .map(|(_, e)| e)
        .collect();
    (choisis, omis)
}

fn entete_fenetre(f: &Fenetre, now: f64, active: bool) -> String {
    let puce = if active { "▶" } else { "○" };
    let mut l = format!(
        "{puce} {} — « {} »",
        if f.app.is_empty() { "?" } else { &f.app },
        f.titre
    );
    if let Some((x, y, w, h)) = f.boite_visible() {
        l += &format!(" · {x},{y} {w}×{h}");
    }
    if f.visible == Some(false) {
        l += " · réduite";
    }
    match (f.etat, f.observe_a) {
        ("indisponible", _) => l += " · muette (aucune source ne décrit son contenu)",
        (_, Some(t)) => {
            let age = now - t;
            let voie = if f.voie.is_empty() {
                String::new()
            } else {
                format!("/{}", f.voie)
            };
            l += &format!(
                " · {}{} · lu il y a {}",
                f.source.clone().unwrap_or_default(),
                voie,
                duree(age)
            );
            if f.perime(now) {
                l += " · PÉRIMÉ";
            } else if f.etat == "a_relire" && age > 1.5 {
                l += " · relecture en attente";
            }
            l += &format!(" · {} éléments", f.elements.len());
            if f.tronquee {
                l += " (tronqué)";
            }
        }
        _ => {}
    }
    if f.dessin.continu {
        l += " · se redessine en continu (vidéo, animation)";
    }
    l
}

fn resume_diff(d: &Diff, b: &Bureau) -> Vec<String> {
    let mut v = vec![];
    if d.premier {
        v.push("premier regard de ce lecteur : tout est nouveau".into());
        return v;
    }
    let depuis = d.depuis.unwrap_or(0.0);
    let mut l = format!(
        "depuis ton dernier regard ({}, il y a {}) : ",
        heure(depuis),
        duree(maintenant() - depuis)
    );
    let mut parts = vec![];
    if !d.ajoutes.is_empty() {
        parts.push(format!("✚ {} nouveaux", d.ajoutes.len()));
    }
    if !d.modifies.is_empty() {
        parts.push(format!("✎ {} modifiés", d.modifies.len()));
    }
    if !d.retires.is_empty() {
        parts.push(format!("✖ {} disparus", d.retires.len()));
    }
    if parts.is_empty() {
        l += "rien n'a changé dans ce qui est lisible";
    } else {
        l += &parts.join(" · ");
    }
    v.push(l);
    if let Some(av) = &d.active_avant {
        let ap = b
            .fenetre_active()
            .map(|f| f.titre.clone())
            .unwrap_or_else(|| "aucune".into());
        v.push(format!(
            "fenêtre active changée : « {} » → « {} »",
            couper(av, 60),
            couper(&ap, 60)
        ));
    }
    if !d.fenetres_ouvertes.is_empty() {
        v.push(format!(
            "fenêtres ouvertes : {}",
            d.fenetres_ouvertes
                .iter()
                .map(|s| couper(s, 60))
                .collect::<Vec<_>>()
                .join(" ; ")
        ));
    }
    if !d.fenetres_fermees.is_empty() {
        v.push(format!(
            "fenêtres fermées : {}",
            d.fenetres_fermees
                .iter()
                .map(|s| couper(s, 60))
                .collect::<Vec<_>>()
                .join(" ; ")
        ));
    }
    for (a, b2) in d.titres.iter().take(5) {
        v.push(format!(
            "titre changé : « {} » → « {} »",
            couper(a, 50),
            couper(b2, 50)
        ));
    }
    v
}

fn plan(f: &Fenetre, d: &Diff, budget: usize, racine: Option<&str>, out: &mut Vec<String>) {
    let els: Vec<Element> = match racine {
        None => f.elements.clone(),
        Some(r) => {
            // Le sous-arbre d'un élément : lui et ceux dont la chaîne de parents le contient.
            let par_id: HashMap<&str, &Element> =
                f.elements.iter().map(|e| (e.id.as_str(), e)).collect();
            f.elements
                .iter()
                .filter(|e| {
                    if e.id == r {
                        return true;
                    }
                    let mut p = e.parent.as_deref();
                    let mut n = 0;
                    while let Some(pid) = p {
                        if pid == r {
                            return true;
                        }
                        p = par_id.get(pid).and_then(|x| x.parent.as_deref());
                        n += 1;
                        if n > 60 {
                            break;
                        }
                    }
                    false
                })
                .cloned()
                .collect()
        }
    };
    let (choisis, omis) = selection(&els, d, budget);
    let base = choisis.first().map(|e| e.profondeur).unwrap_or(0);
    for e in &choisis {
        let marque = if d.ajoutes.contains(&e.id) {
            "✚ "
        } else if d.modifies.contains(&e.id) {
            "✎ "
        } else {
            ""
        };
        let prof = e.profondeur.saturating_sub(base).min(12) as usize;
        out.push(format!(
            "  {}{}",
            "  ".repeat(prof),
            ligne_element(e, marque)
        ));
    }
    if !omis.is_empty() {
        let mut v: Vec<(String, usize)> = omis.into_iter().collect();
        v.sort_by(|a, b| b.1.cmp(&a.1));
        let txt: Vec<String> = v.iter().take(6).map(|(r, n)| format!("{n} {r}")).collect();
        out.push(format!("  … non montrés : {} (`ecran regard --fenetre \"{}\"` ou `--element eN` pour descendre)", txt.join(", "), couper(&f.titre, 30)));
    }
}

/// La page complète.
pub fn page(b: &Bureau, o: &Options) -> String {
    let now = maintenant();
    let d = b.diff(&o.lecteur);
    let mut out: Vec<String> = vec![];
    out.push(format!(
        "regard {} · révision {} · lecteur {}",
        heure(now),
        b.revision,
        o.lecteur
    ));
    out.extend(resume_diff(&d, b));
    let pilote = match &b.pilote {
        v if v.is_null() => "pilote : écran libre, pas de STOP".to_string(),
        v => {
            let stop = v.get("stop").and_then(|s| s.as_str());
            let agent = v.get("agent").and_then(|s| s.as_str());
            match (stop, agent) {
                (Some(s), _) => format!("pilote : ⛔ STOP — {}", couper(s, 100)),
                (None, Some(a)) => format!("pilote : écran réservé par {a}"),
                _ => "pilote : écran libre, pas de STOP".to_string(),
            }
        }
    };
    let souris = b
        .pointeur
        .map(|(x, y)| format!("souris {x},{y}"))
        .unwrap_or_else(|| "souris ?".into());
    out.push(format!(
        "écran {}×{} · {} · {} · {} fenêtres",
        b.largeur,
        b.hauteur,
        souris,
        pilote,
        b.fenetres.values().filter(|f| f.wid != SHELL).count()
    ));
    if let Some(f) = &b.focus {
        let role = f.get("role").and_then(|x| x.as_str()).unwrap_or("");
        let nom = f.get("nom").and_then(|x| x.as_str()).unwrap_or("");
        if !role.is_empty() || !nom.is_empty() {
            out.push(format!("focus clavier : {} « {} »", role, couper(nom, 60)));
        }
    }
    for e in b.erreurs.iter().rev().take(2) {
        out.push(format!("⚠ {e}"));
    }
    out.push(String::new());

    let budget = if o.lignes == 0 { 110 } else { o.lignes };
    let motif = o.fenetre.as_ref().map(|m| m.to_lowercase());
    // Ordre : l'active, puis l'empilement de haut en bas, puis le shell.
    let mut ordre: Vec<&Fenetre> = vec![];
    if let Some(f) = b.fenetre_active() {
        ordre.push(f);
    }
    for w in b.empilement.iter().rev() {
        if let Some(f) = b.fenetres.get(w) {
            if !ordre.iter().any(|x| x.wid == f.wid) {
                ordre.push(f);
            }
        }
    }
    for f in b.fenetres.values() {
        if !ordre.iter().any(|x| x.wid == f.wid) {
            ordre.push(f);
        }
    }
    let mut detaillees = 0;
    for f in ordre {
        let correspond = motif
            .as_ref()
            .map(|m| f.titre.to_lowercase().contains(m) || f.app.to_lowercase().contains(m))
            .unwrap_or(false);
        let detail =
            o.tout || correspond || (motif.is_none() && f.wid == b.active && f.wid != SHELL);
        if motif.is_some() && !correspond && !o.tout {
            continue;
        }
        if f.wid == SHELL && !detail {
            continue;
        }
        out.push(entete_fenetre(f, now, f.wid == b.active));
        if detail {
            detaillees += 1;
            // Quand aucune source ne décrit la fenêtre, l'ancienne boucle (capture + OCR) reste le secours.
            if f.etat == "indisponible" {
                out.push("  → fenêtre muette (ni arbre, ni DevTools, ni annonce) : secours à l'ancienne, `ecran voir` ou `ecran texte --fenetre-active` (capture + OCR)".into());
            } else if f.elements.is_empty() && f.annonce.is_none() {
                out.push(format!(
                    "  → rien d'exposé en arrière-plan (Chromium/Electron vident leur arbre) : `ecran fenetre \"{}\"` puis `ecran regard`, ou `ecran voir`",
                    couper(&f.titre, 30)
                ));
            }
            let ded = f.deduction();
            let genre = ded.get("genre").and_then(|x| x.as_str()).unwrap_or("");
            let attend = ded.get("attend").and_then(|x| x.as_str());
            let mut l = format!("  {genre}");
            if let Some(a) = attend {
                l += &format!(" ; attend {a}");
            }
            if let Some(a) = f
                .annonce
                .as_ref()
                .and_then(|a| a.get("lignes"))
                .and_then(|x| x.as_array())
            {
                let t: Vec<&str> = a
                    .iter()
                    .filter_map(|x| x.as_str())
                    .filter(|x| !x.is_empty())
                    .take(12)
                    .collect();
                l += &format!(" ; déclare : {}", couper(&t.join(" / "), 300));
            }
            out.push(l);
            if let Some(s) = &f.scene {
                out.push(format!("  œil : {}", couper(s, 200)));
            }
            let b_lignes = if o.tout {
                budget * 3
            } else if correspond {
                budget * 2
            } else {
                budget
            };
            plan(f, &d, b_lignes, o.element.as_deref(), &mut out);
            let disparus: Vec<&Element> =
                d.retires.iter().filter(|e| e.wid == f.wid_hex()).collect();
            if !disparus.is_empty() {
                let t: Vec<String> = disparus
                    .iter()
                    .take(8)
                    .map(|e| ligne_element(e, ""))
                    .collect();
                out.push(format!(
                    "  ✖ disparus depuis ton dernier regard : {}{}",
                    t.join(" ; "),
                    if disparus.len() > 8 {
                        format!(" … (+{})", disparus.len() - 8)
                    } else {
                        String::new()
                    }
                ));
            }
            out.push(String::new());
        }
    }
    if detaillees == 0 {
        out.push(
            "(aucune fenêtre active détaillée : `--fenetre MOTIF` pour en choisir une)".into(),
        );
    }
    out.join("\n")
}
