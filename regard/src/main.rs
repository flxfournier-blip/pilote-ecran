//! regard — l'écran en données vivantes pour un agent, sans capture.
//!
//! Trois sources, une vérité : X11 (fenêtres, géométrie, redessins), AT-SPI (contenu), annonces
//! des applications. Un modèle versionné, des identités stables, un diff par lecteur, un pont
//! HTTP local. Les actions restent dans `ecran` (cadre, vigie, réservation, STOP).

mod alerte;
mod arbre;
mod devtools;
mod modele;
mod pixels;
mod pont;
mod qwen;
mod regard;
#[cfg(test)]
mod tests;
mod x11;

use std::sync::Arc;
use std::time::Duration;

use serde_json::{json, Value};
use tokio::sync::{broadcast, mpsc, Notify, RwLock};

use modele::{maintenant, Bureau, Fenetre, Wid, SHELL};
use x11::Evenement;

pub type Partage = Arc<RwLock<Bureau>>;

fn log(s: &str) {
    eprintln!("{} {s}", modele::heure(maintenant()));
}

/// Les fichiers du pilote (`stop`, `pilote`) : réservation et arrêt, lus sans les modifier.
fn lire_pilote(dossier: &str) -> Value {
    let stop = std::fs::read_to_string(format!("{dossier}/stop"))
        .ok()
        .map(|s| s.trim().to_string());
    let pilote = std::fs::read_to_string(format!("{dossier}/pilote"))
        .ok()
        .map(|s| s.trim().to_string());
    match (stop, pilote) {
        (None, None) => Value::Null,
        (s, p) => {
            // `pilote` est un JSON écrit par `ecran` ({"agent": …, …}) ; à défaut, le premier mot.
            let agent = p.as_ref().and_then(|p| {
                serde_json::from_str::<Value>(p)
                    .ok()
                    .and_then(|v| {
                        v.get("agent")
                            .and_then(|a| a.as_str())
                            .map(|a| a.to_string())
                    })
                    .or_else(|| p.split_whitespace().next().map(|a| a.to_string()))
            });
            json!({"stop": s, "agent": agent, "reservation": p})
        }
    }
}

fn est_cadre(titre: &str) -> bool {
    titre.to_lowercase().contains("pilote l'écran")
}

#[tokio::main]
async fn main() {
    let port: u16 = std::env::var("REGARD_PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(8793);
    let dossier = std::env::var("PILOTE_ETAT")
        .unwrap_or_else(|_| format!("/run/user/{}/pilote-ecran", unsafe { libc_uid() }));
    let display = std::env::var("DISPLAY").ok();
    let partage: Partage = Arc::new(RwLock::new(Bureau::nouveau()));
    let (diffusion, _) = broadcast::channel::<Value>(512);
    let reveil = Arc::new(Notify::new());
    let (cibles_tx, cibles_rx) = mpsc::channel::<arbre::Demande>(8);
    log(&format!(
        "regard démarre : port {port}, état du pilote dans {dossier}"
    ));

    // X11 : un fil bloquant, des événements typés.
    let (x_tx, mut x_rx) = mpsc::unbounded_channel::<Evenement>();
    {
        let display = display.clone();
        std::thread::Builder::new()
            .name("x11".into())
            .spawn(move || x11::fil(display, x_tx))
            .expect("fil X11");
    }
    // DevTools : les pages des navigateurs lancés avec un port de débogage, devant ou derrière.
    let dev = devtools::Devtools::nouveau(partage.clone(), reveil.clone());
    tokio::spawn(dev.clone().scruter());
    // AT-SPI : le lecteur (lectures, cibles) et l'écoute des événements.
    {
        let (p, d, r, dv) = (
            partage.clone(),
            diffusion.clone(),
            reveil.clone(),
            dev.clone(),
        );
        tokio::spawn(async move { arbre::boucle(p, d, r, cibles_rx, dv).await });
    }
    // Le pont HTTP.
    {
        let etat = pont::Etat {
            partage: partage.clone(),
            diffusion: diffusion.clone(),
            cibles: cibles_tx,
            devtools: dev.clone(),
            demarre: maintenant(),
        };
        tokio::spawn(async move {
            if let Err(e) = pont::servir(etat, port).await {
                eprintln!("pont HTTP impossible sur 127.0.0.1:{port} : {e}");
                std::process::exit(2);
            }
        });
    }
    // Le shell GNOME (barre, dock) : une entrée sans fenêtre X11.
    {
        let mut b = partage.write().await;
        let mut f = Fenetre::nouvelle(
            SHELL,
            "barre du haut, dock, notifications".into(),
            "gnome-shell".into(),
            None,
            None,
        );
        f.etat = "indisponible";
        b.fenetres.insert(SHELL, f);
        b.programmer(SHELL);
    }

    let mut tic = tokio::time::interval(Duration::from_millis(250));
    let mut dernier_pilote = Value::Null;
    let mut dernier_visibles: Vec<(Wid, bool)> = vec![];
    let mut derniere_garde = maintenant();
    let ocr_disponible = pixels::disponible();
    log(&format!(
        "secours OCR (tesseract) : {}",
        if ocr_disponible {
            "disponible"
        } else {
            "ABSENT"
        }
    ));
    loop {
        tokio::select! {
            Some(ev) = x_rx.recv() => {
                let mut b = partage.write().await;
                b.derniere_x11 = maintenant();
                let mut deltas: Vec<Value> = vec![];
                let mut relire: Vec<Wid> = vec![];
                match ev {
                    Evenement::Ecran { largeur, hauteur } => { b.largeur = largeur; b.hauteur = hauteur; }
                    Evenement::Ouverte { wid, titre, classe, pid, geo } => {
                        if est_cadre(&titre) { continue; }
                        let f = Fenetre::nouvelle(wid, titre.clone(), classe.clone(), pid, geo);
                        b.fenetres.insert(wid, f);
                        b.tracer(format!("fenêtre ouverte : {classe} « {titre} »"));
                        relire.push(wid);
                    }
                    Evenement::Fermee { wid } => {
                        b.a_relire.remove(&wid);
                        b.derniers_essais.remove(&wid);
                        if let Some(d) = b.fermer(wid) { deltas.push(d); }
                    }
                    Evenement::Titre { wid, titre } => {
                        if est_cadre(&titre) { continue; }
                        if let Some(f) = b.fenetres.get_mut(&wid) {
                            if f.titre != titre {
                                let av = f.titre.clone();
                                f.titre = titre.clone();
                                b.tracer(format!("titre : « {av} » → « {titre} »"));
                                relire.push(wid);
                            }
                        }
                    }
                    Evenement::Geometrie { wid, geo } => {
                        if let Some(f) = b.fenetres.get_mut(&wid) {
                            if f.geo != Some(geo) { f.geo = Some(geo); relire.push(wid); }
                        }
                    }
                    Evenement::Active { wid } => {
                        if b.active != wid {
                            b.active = wid;
                            for f in b.fenetres.values_mut() { f.active = f.wid == wid; }
                            let t = b.fenetres.get(&wid).map(|f| format!("{} « {} »", f.app, f.titre)).unwrap_or_else(|| "aucune".into());
                            b.tracer(format!("fenêtre active : {t}"));
                            if wid != 0 { relire.push(wid); }
                            deltas.push(b.publier_contexte());
                        }
                    }
                    Evenement::Empilement(e) => {
                        if b.empilement != e { b.empilement = e; deltas.push(b.publier_contexte()); }
                    }
                    Evenement::Pointeur { x, y } => { b.pointeur = Some((x, y)); }
                    Evenement::Visibles(v) => {
                        if v != dernier_visibles {
                            dernier_visibles = v.clone();
                            let mut change = false;
                            for (w, vis) in v {
                                if let Some(f) = b.fenetres.get_mut(&w) {
                                    if f.visible != Some(vis) { f.visible = Some(vis); change = true; if vis { relire.push(w); } }
                                }
                            }
                            if change { deltas.push(b.publier_contexte()); }
                        }
                    }
                    Evenement::Dessin { wid, .. } => {
                        let now = maintenant();
                        let mut programmer = false;
                        if let Some(f) = b.fenetres.get_mut(&wid) {
                            let d = &mut f.dessin;
                            d.compte += 1;
                            if now - d.fenetre_debut >= 1.0 {
                                let continu = d.compte >= 10;
                                if continu != d.continu { d.continu = continu; }
                                d.compte = 0;
                                d.fenetre_debut = now;
                            }
                            let derniere_lecture = f.observe_a.unwrap_or(0.0);
                            // Une fenêtre qui se redessine en continu (vidéo) n'est relue que toutes les 2 s.
                            programmer = !d.continu || now - derniere_lecture > 2.0;
                            d.dernier = now;
                        }
                        if programmer { relire.push(wid); }
                    }
                    Evenement::Erreur(e) => { log(&e); alerte::alerter(&mut b, "X11", true, e); }
                }
                for w in relire { b.programmer(w); }
                drop(b);
                for d in deltas { let _ = diffusion.send(d); }
                reveil.notify_one();
            }
            _ = tic.tick() => {
                let mut b = partage.write().await;
                let now = maintenant();
                // Filet de sécurité : l'active toutes les 2 s, les autres toutes les 20 s.
                let mut relire = vec![];
                for f in b.fenetres.values() {
                    if f.wid == SHELL { continue; }
                    let periode = if f.wid == b.active { 2.0 } else { 20.0 };
                    if now - b.derniers_essais.get(&f.wid).copied().unwrap_or(0.0) >= periode && !b.a_relire.contains_key(&f.wid) {
                        relire.push(f.wid);
                    }
                }
                for w in relire { b.programmer(w); }
                if now - derniere_garde >= 10.0 {
                    derniere_garde = now;
                    alerte::veiller(&mut b, ocr_disponible);
                }
                let p = lire_pilote(&dossier);
                if p != dernier_pilote {
                    dernier_pilote = p.clone();
                    b.pilote = p;
                    let d = b.publier_contexte();
                    drop(b);
                    let _ = diffusion.send(d);
                } else {
                    drop(b);
                }
                reveil.notify_one();
            }
        }
    }
}

unsafe fn libc_uid() -> u32 {
    extern "C" {
        fn getuid() -> u32;
    }
    getuid()
}
