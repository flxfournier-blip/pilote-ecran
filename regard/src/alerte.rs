//! Les alertes : quand une source tombe (bus AT-SPI, DevTools, X11, OCR) ou que le lecteur se fige,
//! la page le dit (⚠) et, si c'est grave, Félix reçoit une notification ntfy sur son téléphone
//! (même canal qu'`ecran reprendre`), au plus une par sujet toutes les 30 minutes.

use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};

use crate::modele::{maintenant, Bureau};

const SILENCE_S: f64 = 1800.0;

fn derniers() -> &'static Mutex<HashMap<String, f64>> {
    static D: OnceLock<Mutex<HashMap<String, f64>>> = OnceLock::new();
    D.get_or_init(|| Mutex::new(HashMap::new()))
}

/// Une alerte : journal + page ; grave → ntfy (si le sujet existe et REGARD_NTFY ≠ 0).
pub fn alerter(b: &mut Bureau, sujet: &str, grave: bool, texte: String) {
    b.erreur(format!("{sujet} : {texte}"));
    b.tracer(format!("alerte {sujet} : {texte}"));
    if !grave {
        return;
    }
    let now = maintenant();
    {
        let mut d = derniers().lock().unwrap_or_else(|e| e.into_inner());
        if let Some(t) = d.get(sujet) {
            if now - t < SILENCE_S {
                return;
            }
        }
        d.insert(sujet.to_string(), now);
    }
    if std::env::var("REGARD_NTFY")
        .map(|v| v == "0")
        .unwrap_or(false)
    {
        return;
    }
    let Some(topic) = sujet_ntfy() else { return };
    let message = format!("regard (pilote-ecran) — {sujet} : {texte}");
    tokio::spawn(async move {
        let _ = tokio::process::Command::new("curl")
            .args([
                "-s",
                "-m",
                "8",
                "-o",
                "/dev/null",
                "-H",
                "Title: JARVIS — pilote-ecran",
                "-H",
                "Priority: high",
                "-H",
                "Tags: warning",
                "-d",
                &message,
                &format!("https://ntfy.sh/{topic}"),
            ])
            .status()
            .await;
    });
}

fn sujet_ntfy() -> Option<String> {
    let maison = std::env::var("HOME").ok()?;
    let t = std::fs::read_to_string(format!("{maison}/.jarvis/ntfy_topic"))
        .ok()?
        .trim()
        .to_string();
    if t.is_empty() {
        None
    } else {
        Some(t)
    }
}

/// Le chien de garde, appelé toutes les 10 s par la boucle principale.
pub fn veiller(b: &mut Bureau, ocr_disponible: bool) {
    let now = maintenant();
    if b.derniere_x11 > 0.0 && now - b.derniere_x11 > 30.0 {
        let grave = now - b.derniere_x11 > 120.0;
        alerter(
            b,
            "X11",
            grave,
            format!(
                "plus aucun événement depuis {:.0} s (fenêtres et pointeur non suivis)",
                now - b.derniere_x11
            ),
        );
    }
    if b.dernier_lecteur > 0.0 && now - b.dernier_lecteur > 30.0 {
        let grave = now - b.dernier_lecteur > 90.0;
        alerter(
            b,
            "lecteur",
            grave,
            format!(
                "figé depuis {:.0} s (aucune lecture AT-SPI/DevTools)",
                now - b.dernier_lecteur
            ),
        );
    }
    if !ocr_disponible && !b.ocr_signale {
        b.ocr_signale = true;
        alerter(
            b,
            "OCR",
            false,
            "tesseract absent : pas de secours à l'ancienne pour les fenêtres muettes".into(),
        );
    }
}
