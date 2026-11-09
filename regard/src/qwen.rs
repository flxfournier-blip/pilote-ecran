//! L'œil : troisième niveau de secours, derrière l'arbre et l'OCR. Quand une fenêtre muette ne
//! livre presque aucun texte (icônes, boutons graphiques), le modèle de vision local (vLLM,
//! `jarvis-llm` sur 127.0.0.1:8016, jamais le cloud) décrit l'image en éléments positionnés.
//! Ce qu'il dit est ESTIMÉ : un libellé textuel est recalé sur la ligne OCR correspondante quand
//! elle existe, et avant un clic la boîte est recapturée et comparée à son empreinte de pixels.
//! Jamais pour un CAPTCHA : il est signalé à Félix, pas déchiffré.

use std::time::Duration;

use base64::Engine;
use serde_json::{json, Value};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;

use crate::modele::Lu;
use crate::pixels::{self, Image, Ligne};

pub const LARGEUR: u32 = 1288; // multiple de 28
pub const MAX_ELEMENTS: usize = 40;

pub fn actif() -> bool {
    std::env::var("REGARD_QWEN")
        .map(|v| v != "0")
        .unwrap_or(true)
}

fn url() -> (String, u16, String) {
    let u = std::env::var("REGARD_QWEN_URL").unwrap_or_else(|_| "http://127.0.0.1:8016".into());
    let sans = u.trim_start_matches("http://");
    let (hote, port) = sans.split_once(':').unwrap_or((sans, "80"));
    (
        hote.to_string(),
        port.parse().unwrap_or(8016),
        std::env::var("REGARD_QWEN_MODELE").unwrap_or_else(|_| "jarvis-llm".into()),
    )
}

fn libelle(t: &str) -> &'static str {
    let t = t.to_lowercase();
    if t.contains("bouton") || t.contains("button") {
        "bouton"
    } else if t.contains("champ")
        || t.contains("input")
        || t.contains("saisie")
        || t.contains("text field")
    {
        "champ"
    } else if t.contains("lien") || t.contains("link") {
        "lien"
    } else if t.contains("icône") || t.contains("icone") || t.contains("icon") {
        "icône"
    } else if t.contains("image") || t.contains("photo") {
        "image"
    } else if t.contains("titre") || t.contains("heading") || t.contains("title") {
        "titre"
    } else if t.contains("menu") {
        "menu"
    } else if t.contains("onglet") || t.contains("tab") {
        "onglet"
    } else if t.contains("case") || t.contains("checkbox") {
        "case"
    } else if t.contains("radio") {
        "radio"
    } else if t.contains("liste") || t.contains("list") || t.contains("combo") {
        "liste"
    } else if t.contains("curseur") || t.contains("slider") {
        "curseur"
    } else if t.contains("dialog") {
        "dialogue"
    } else {
        "texte"
    }
}

/// Un POST JSON local, sans dépendance HTTP : en-tête, puis corps par Content-Length ou par morceaux.
async fn poster(
    hote: &str,
    port: u16,
    chemin: &str,
    corps: &str,
    delai: Duration,
) -> Result<Value, String> {
    let travail = async {
        let mut s = TcpStream::connect((hote, port))
            .await
            .map_err(|e| format!("connexion : {e}"))?;
        let req = format!("POST {chemin} HTTP/1.1\r\nHost: {hote}:{port}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", corps.len());
        s.write_all(req.as_bytes())
            .await
            .map_err(|e| e.to_string())?;
        s.write_all(corps.as_bytes())
            .await
            .map_err(|e| e.to_string())?;
        let mut buf = Vec::new();
        s.read_to_end(&mut buf).await.map_err(|e| e.to_string())?;
        let pos = buf
            .windows(4)
            .position(|w| w == b"\r\n\r\n")
            .ok_or("réponse sans en-tête")?;
        let entete = String::from_utf8_lossy(&buf[..pos]).to_lowercase();
        let mut corps = buf[pos + 4..].to_vec();
        if entete.contains("transfer-encoding: chunked") {
            let mut out = Vec::new();
            let mut i = 0;
            while i < corps.len() {
                let fin = corps[i..]
                    .windows(2)
                    .position(|w| w == b"\r\n")
                    .map(|p| i + p)
                    .ok_or("morceau mal formé")?;
                let taille = usize::from_str_radix(
                    String::from_utf8_lossy(&corps[i..fin])
                        .trim()
                        .split(';')
                        .next()
                        .unwrap_or("0"),
                    16,
                )
                .map_err(|_| "taille de morceau invalide")?;
                if taille == 0 {
                    break;
                }
                let debut = fin + 2;
                out.extend_from_slice(corps.get(debut..debut + taille).ok_or("morceau tronqué")?);
                i = debut + taille + 2;
            }
            corps = out;
        }
        let statut = entete.lines().next().unwrap_or("").to_string();
        if !statut.contains(" 200") {
            return Err(format!(
                "{} : {}",
                statut.trim(),
                String::from_utf8_lossy(&corps)
                    .chars()
                    .take(160)
                    .collect::<String>()
            ));
        }
        serde_json::from_slice::<Value>(&corps).map_err(|e| format!("JSON : {e}"))
    };
    tokio::time::timeout(delai, travail)
        .await
        .map_err(|_| format!("pas de réponse en {:.0} s", delai.as_secs_f64()))?
}

/// Le modèle écrit un JSON presque juste (vu : `"scene": "…" : "elements"`). On lit la scène et chaque
/// objet d'élément séparément, sans dépendre de la validité de l'ensemble.
fn extraire(s: &str) -> (String, Vec<Value>) {
    let s = s.trim();
    let s = s
        .strip_prefix("```json")
        .or_else(|| s.strip_prefix("```"))
        .unwrap_or(s);
    let s = s.strip_suffix("```").unwrap_or(s).trim();
    // Un tableau nu (le modèle oublie parfois l'enveloppe) : la liste, sans scène.
    if s.starts_with('[') {
        if let Some(f) = s.rfind(']') {
            if let Ok(Value::Array(a)) = serde_json::from_str::<Value>(&s[..=f]) {
                return (String::new(), a);
            }
        }
    }
    if let (Some(d), Some(f)) = (s.find('{'), s.rfind('}')) {
        if let Ok(v) = serde_json::from_str::<Value>(&s[d..=f]) {
            let scene = v["scene"].as_str().unwrap_or("").to_string();
            let els = v["elements"].as_array().cloned().unwrap_or_default();
            return (scene, els);
        }
    }
    let scene = s
        .find("\"scene\"")
        .and_then(|i| {
            let reste = &s[i + 7..];
            let d = reste.find('"')?;
            let reste = &reste[d + 1..];
            let f = reste.find('"')?;
            Some(reste[..f].to_string())
        })
        .unwrap_or_default();
    let mut els = vec![];
    let depart = s.find("\"elements\"").map(|i| i + 10).unwrap_or(0);
    let corps = &s[depart..];
    let octets = corps.as_bytes();
    let mut i = 0;
    while i < octets.len() {
        if octets[i] == b'{' {
            let mut prof = 0;
            let mut j = i;
            while j < octets.len() {
                match octets[j] {
                    b'{' => prof += 1,
                    b'}' => {
                        prof -= 1;
                        if prof == 0 {
                            break;
                        }
                    }
                    _ => {}
                }
                j += 1;
            }
            if j < octets.len() {
                if let Ok(v) = serde_json::from_str::<Value>(&corps[i..=j]) {
                    els.push(v);
                }
                i = j + 1;
                continue;
            }
            break;
        }
        i += 1;
    }
    (scene, els)
}

/// La boîte d'un élément, quel que soit le format écrit : x/y/w/h, ou bbox/bbox_2d/boite [x1, y1, x2, y2].
fn boite(e: &Value) -> Option<(f64, f64, f64, f64)> {
    let f = |k: &str| e[k].as_f64();
    if let (Some(x), Some(y), Some(w), Some(h)) = (f("x"), f("y"), f("w"), f("h")) {
        return Some((x, y, w, h));
    }
    for k in ["bbox_2d", "bbox", "boite", "box"] {
        if let Some(a) = e[k].as_array() {
            if a.len() == 4 {
                let v: Vec<f64> = a.iter().filter_map(|x| x.as_f64()).collect();
                if v.len() == 4 {
                    return Some((v[0], v[1], v[2] - v[0], v[3] - v[1]));
                }
            }
        }
    }
    None
}

/// Ce que l'œil a vu : éléments en coordonnées écran, leurs empreintes, et la scène en une phrase.
pub struct Vue {
    pub elements: Vec<Lu>,
    pub empreintes: Vec<(String, Vec<u8>)>,
    pub scene: String,
}

/// Décrit l'image d'une fenêtre (origine écran `origine`) en éléments positionnés.
pub async fn regarder(
    img: &Image,
    origine: (i32, i32),
    wid: u32,
    lignes_ocr: &[Ligne],
) -> Result<Vue, String> {
    let petite = pixels::redimensionner(img, LARGEUR);
    let echelle = img.w as f64 / petite.w as f64;
    let png = pixels::png(&petite)?;
    let b64 = base64::engine::general_purpose::STANDARD.encode(&png);
    let (hote, port, modele) = url();
    // Un tableau, la scène en premier : avec une enveloppe {"scene", "elements"} le modèle s'arrêtait parfois
    // après la phrase de scène (34 jetons, vu le 08/11) ; en tableau il énumère tout.
    let consigne = format!(
        "Voici la capture d'une fenêtre d'application ({} × {} pixels). Liste les éléments d'interface visibles et utiles \
         pour agir : boutons, champs, liens, icônes, onglets, menus, cases, titres, textes courts. Pour chacun donne \
         son type, son libellé exact tel qu'il est écrit (ou une courte description s'il n'y a pas de texte), et sa \
         boîte en pixels de CETTE image : x, y du coin haut gauche, largeur w, hauteur h. Au plus {} éléments, les plus \
         utiles d'abord. Ne cite jamais un texte que tu ne lis pas nettement. Réponds UNIQUEMENT par un tableau JSON, \
         sans commentaire, dont le premier objet décrit la scène en une phrase : \
         [{{\"type\": \"scene\", \"libelle\": \"une phrase\"}}, {{\"type\": \"bouton\", \"libelle\": \"…\", \"x\": 0, \"y\": 0, \"w\": 0, \"h\": 0}}]",
        petite.w, petite.h, MAX_ELEMENTS
    );
    let corps = json!({
        "model": modele,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": format!("data:image/png;base64,{b64}")}},
            {"type": "text", "text": consigne}
        ]}],
        "max_tokens": 2000, "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": false}
    })
    .to_string();
    let rep = poster(
        &hote,
        port,
        "/v1/chat/completions",
        &corps,
        Duration::from_secs(45),
    )
    .await?;
    let contenu = rep["choices"][0]["message"]["content"]
        .as_str()
        .unwrap_or("")
        .to_string();
    let (mut scene, mut bruts) = extraire(&contenu);
    // La scène peut être le premier objet du tableau.
    if let Some(i) = bruts
        .iter()
        .position(|e| e["type"].as_str() == Some("scene"))
    {
        if scene.is_empty() {
            scene = bruts[i]["libelle"].as_str().unwrap_or("").to_string();
        }
        bruts.remove(i);
    }
    eprintln!(
        "œil : {} jetons ({}), {} objets, début : {}",
        rep["usage"]["completion_tokens"].as_u64().unwrap_or(0),
        rep["choices"][0]["finish_reason"].as_str().unwrap_or("?"),
        bruts.len(),
        contenu
            .chars()
            .take(140)
            .collect::<String>()
            .replace('\n', " ")
    );
    if scene.is_empty() && bruts.is_empty() {
        return Err(format!(
            "réponse non structurée : {}",
            contenu.chars().take(120).collect::<String>()
        ));
    }
    // Le modèle raisonne parfois dans sa propre grille (image agrandie en multiples de 28) : si les boîtes
    // débordent nettement de l'image annoncée, on les ramène à l'échelle.
    let boites: Vec<(f64, f64, f64, f64)> = bruts.iter().filter_map(boite).collect();
    let (mut mx, mut my) = (0f64, 0f64);
    for (x, y, w, h) in &boites {
        mx = mx.max(x + w);
        my = my.max(y + h);
    }
    let facteur = if mx > petite.w as f64 * 1.05 || my > petite.h as f64 * 1.05 {
        (petite.w as f64 / mx).min(petite.h as f64 / my).min(1.0)
    } else {
        1.0
    };
    let mut elements = vec![];
    let mut empreintes = vec![];
    for (i, e) in bruts.iter().enumerate().take(MAX_ELEMENTS) {
        let Some((x, y, w, h)) = boite(e) else {
            continue;
        };
        let (x, y, w, h) = (x * facteur, y * facteur, w * facteur, h * facteur);
        // Boîte ramenée dans l'image ; rejetée seulement si elle en sort entièrement ou n'a pas de surface.
        let (x1, y1) = (x.max(0.0), y.max(0.0));
        let (x2, y2) = ((x + w).min(petite.w as f64), (y + h).min(petite.h as f64));
        let (x, y, w, h) = (x1, y1, x2 - x1, y2 - y1);
        if w <= 2.0 || h <= 2.0 {
            continue;
        }
        let lib = e["libelle"].as_str().unwrap_or("").trim().to_string();
        let role = libelle(e["type"].as_str().unwrap_or(""));
        // Boîte dans l'image d'origine, puis à l'écran.
        let (mut ix, mut iy, mut iw, mut ih) = (
            (x * echelle) as i32,
            (y * echelle) as i32,
            (w * echelle) as i32,
            (h * echelle) as i32,
        );
        let mut source = "qwen";
        // Un libellé textuel se recale sur la ligne OCR qui le porte, bien plus précise.
        let nom_n = pixels::normaliser(&lib);
        if nom_n.len() >= 3 {
            let (cx, cy) = (origine.0 + ix + iw / 2, origine.1 + iy + ih / 2);
            if let Some(l) = lignes_ocr
                .iter()
                .filter(|l| {
                    let t = pixels::normaliser(&l.texte);
                    (t.contains(&nom_n) || nom_n.contains(&t) && t.len() * 2 >= nom_n.len())
                        && (l.x + l.w / 2 - cx).abs() < 200
                        && (l.y + l.h / 2 - cy).abs() < 120
                })
                .min_by_key(|l| (l.x + l.w / 2 - cx).abs() + (l.y + l.h / 2 - cy).abs())
            {
                ix = l.x - origine.0;
                iy = l.y - origine.1;
                iw = l.w;
                ih = l.h;
                source = "qwen+ocr";
            }
        }
        let cle = format!(
            "qwen|{wid}|{i}|{}|{}|{}",
            ix + origine.0,
            iy + origine.1,
            lib
        );
        empreintes.push((cle.clone(), pixels::vignette(img, ix, iy, iw, ih)));
        elements.push(Lu {
            cle_source: cle,
            role: role.into(),
            role_atspi: "oeil".into(),
            nom: lib.chars().take(120).collect(),
            texte: None,
            valeur: None,
            etats: vec![],
            x: ix + origine.0,
            y: iy + origine.1,
            w: iw,
            h: ih,
            parent_cle: None,
            profondeur: 1,
            enfants: 0,
            source: Some(source.into()),
        });
    }
    Ok(Vue {
        elements,
        empreintes,
        scene,
    })
}
