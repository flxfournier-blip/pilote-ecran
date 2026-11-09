//! Le secours à l'ancienne, intégré : quand aucune source ne décrit une fenêtre (ni arbre, ni
//! DevTools, ni annonce), on lit ses pixels. Capture X11 de la zone visible (en mémoire, jamais
//! écrite sur disque), OCR par tesseract (fra + eng), lignes regroupées. Les éléments obtenus ont
//! une identité ESTIMÉE : avant un clic, la zone est recapturée et relue, et le texte doit y être
//! encore. Jamais pour une fenêtre qui se redessine en continu (vidéo, jeu : Félix s'en moque).
//! L'image capturée sert aussi à l'œil (`qwen.rs`) et aux empreintes de pixels qui vérifient ses positions.

use std::collections::BTreeMap;
use std::process::Stdio;
use std::time::Duration;

use tokio::io::AsyncWriteExt;
use x11rb::connection::Connection;
use x11rb::protocol::xproto::{ConnectionExt as _, ImageFormat};

use crate::modele::{Element, Lu};

pub const CONFIANCE_MIN: f64 = 40.0;

/// Une image RVB en mémoire.
#[derive(Clone, Debug)]
pub struct Image {
    pub w: u32,
    pub h: u32,
    pub rgb: Vec<u8>,
}

pub fn disponible() -> bool {
    std::env::var_os("PATH")
        .map(|p| std::env::split_paths(&p).any(|d| d.join("tesseract").is_file()))
        .unwrap_or(false)
}

/// Les pixels d'une zone de l'écran, bornée à l'écran ; rend l'image et l'origine réellement capturée.
fn capturer(
    display: Option<&str>,
    x: i32,
    y: i32,
    w: u32,
    h: u32,
) -> Result<(Image, i32, i32), String> {
    let (conn, ecran) = x11rb::connect(display).map_err(|e| format!("X11 : {e}"))?;
    let racine = conn.setup().roots[ecran].root;
    let (lx, ly) = (
        conn.setup().roots[ecran].width_in_pixels as i32,
        conn.setup().roots[ecran].height_in_pixels as i32,
    );
    let x0 = x.max(0);
    let y0 = y.max(0);
    let w = ((x + w as i32).min(lx) - x0).max(0) as u32;
    let h = ((y + h as i32).min(ly) - y0).max(0) as u32;
    if w < 8 || h < 8 {
        return Err("zone vide".into());
    }
    let img = conn
        .get_image(
            ImageFormat::Z_PIXMAP,
            racine,
            x0 as i16,
            y0 as i16,
            w as u16,
            h as u16,
            !0,
        )
        .map_err(|e| format!("GetImage : {e}"))?
        .reply()
        .map_err(|e| format!("GetImage : {e}"))?;
    let n = (w * h) as usize;
    let par_pixel = if n > 0 { img.data.len() / n } else { 0 };
    if par_pixel < 3 {
        return Err(format!(
            "format d'image inattendu ({par_pixel} octets par pixel)"
        ));
    }
    let mut rgb = Vec::with_capacity(n * 3);
    // ZPixmap 24/32 bits petit-boutiste : B, G, R, (X).
    for p in img.data.chunks_exact(par_pixel) {
        rgb.push(p[2]);
        rgb.push(p[1]);
        rgb.push(p[0]);
    }
    Ok((Image { w, h, rgb }, x0, y0))
}

pub async fn capturer_async(
    display: Option<String>,
    x: i32,
    y: i32,
    w: i32,
    h: i32,
) -> Result<(Image, i32, i32), String> {
    let (w, h) = (w.max(0) as u32, h.max(0) as u32);
    tokio::task::spawn_blocking(move || capturer(display.as_deref(), x, y, w, h))
        .await
        .map_err(|e| e.to_string())?
}

fn ppm(img: &Image) -> Vec<u8> {
    let mut v = format!("P6\n{} {}\n255\n", img.w, img.h).into_bytes();
    v.extend_from_slice(&img.rgb);
    v
}

/// PNG (pour l'œil : l'API ne lit pas le PPM).
pub fn png(img: &Image) -> Result<Vec<u8>, String> {
    let mut out = Vec::new();
    {
        let mut enc = png::Encoder::new(&mut out, img.w, img.h);
        enc.set_color(png::ColorType::Rgb);
        enc.set_depth(png::BitDepth::Eight);
        enc.set_compression(png::Compression::Fast);
        let mut w = enc.write_header().map_err(|e| e.to_string())?;
        w.write_image_data(&img.rgb).map_err(|e| e.to_string())?;
    }
    Ok(out)
}

/// Réduction par moyenne de blocs à une largeur maximale (multiple de 28, ce que le modèle préfère).
pub fn redimensionner(img: &Image, max_l: u32) -> Image {
    if img.w <= max_l {
        return img.clone();
    }
    let nw = (max_l / 28) * 28;
    let nh = ((img.h as u64 * nw as u64 / img.w as u64) as u32 / 28).max(1) * 28;
    let mut rgb = Vec::with_capacity((nw * nh * 3) as usize);
    for j in 0..nh {
        let y1 = (j as u64 * img.h as u64 / nh as u64) as u32;
        let y2 = (((j + 1) as u64 * img.h as u64 / nh as u64) as u32)
            .max(y1 + 1)
            .min(img.h);
        for i in 0..nw {
            let x1 = (i as u64 * img.w as u64 / nw as u64) as u32;
            let x2 = (((i + 1) as u64 * img.w as u64 / nw as u64) as u32)
                .max(x1 + 1)
                .min(img.w);
            let (mut r, mut g, mut b, mut n) = (0u64, 0u64, 0u64, 0u64);
            for y in y1..y2 {
                for x in x1..x2 {
                    let k = ((y * img.w + x) * 3) as usize;
                    r += img.rgb[k] as u64;
                    g += img.rgb[k + 1] as u64;
                    b += img.rgb[k + 2] as u64;
                    n += 1;
                }
            }
            rgb.push((r / n) as u8);
            rgb.push((g / n) as u8);
            rgb.push((b / n) as u8);
        }
    }
    Image { w: nw, h: nh, rgb }
}

/// Empreinte d'une boîte (coordonnées image) : 12×6 niveaux de gris, pour vérifier qu'un élément est toujours là.
pub fn vignette(img: &Image, x: i32, y: i32, w: i32, h: i32) -> Vec<u8> {
    let (cx, cy) = (12u32, 6u32);
    let x0 = x.clamp(0, img.w as i32 - 1) as u32;
    let y0 = y.clamp(0, img.h as i32 - 1) as u32;
    let x1 = (x + w).clamp(x0 as i32 + 1, img.w as i32) as u32;
    let y1 = (y + h).clamp(y0 as i32 + 1, img.h as i32) as u32;
    let mut v = Vec::with_capacity((cx * cy) as usize);
    for j in 0..cy {
        let ya = y0 + (y1 - y0) * j / cy;
        let yb = (y0 + (y1 - y0) * (j + 1) / cy).max(ya + 1);
        for i in 0..cx {
            let xa = x0 + (x1 - x0) * i / cx;
            let xb = (x0 + (x1 - x0) * (i + 1) / cx).max(xa + 1);
            let (mut s, mut n) = (0u64, 0u64);
            for yy in ya..yb {
                for xx in xa..xb {
                    let k = ((yy * img.w + xx) * 3) as usize;
                    s += (img.rgb[k] as u64 * 299
                        + img.rgb[k + 1] as u64 * 587
                        + img.rgb[k + 2] as u64 * 114)
                        / 1000;
                    n += 1;
                }
            }
            v.push((s / n.max(1)) as u8);
        }
    }
    v
}

/// Écart moyen entre deux empreintes (0 = identiques, 1 = opposées).
pub fn ecart(a: &[u8], b: &[u8]) -> f64 {
    if a.is_empty() || a.len() != b.len() {
        return 1.0;
    }
    a.iter()
        .zip(b)
        .map(|(x, y)| (*x as i32 - *y as i32).unsigned_abs() as f64)
        .sum::<f64>()
        / (a.len() as f64 * 255.0)
}

#[derive(Clone, Debug)]
pub struct Ligne {
    pub texte: String,
    pub x: i32,
    pub y: i32,
    pub w: i32,
    pub h: i32,
    pub confiance: f64,
}

/// tesseract sur une image en mémoire → mots (TSV) → lignes (coordonnées écran grâce à l'origine).
async fn ocr(img: &Image, ox: i32, oy: i32) -> Result<Vec<Ligne>, String> {
    let ppm = ppm(img);
    let mut enfant = tokio::process::Command::new("tesseract")
        .args(["stdin", "stdout", "-l", "fra+eng", "--psm", "11", "tsv"])
        .env("OMP_THREAD_LIMIT", "2")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("tesseract : {e}"))?;
    let mut entree = enfant.stdin.take().ok_or("tesseract : pas d'entrée")?;
    let ecriture = tokio::spawn(async move {
        let _ = entree.write_all(&ppm).await;
        let _ = entree.shutdown().await;
    });
    let sortie = tokio::time::timeout(Duration::from_secs(12), enfant.wait_with_output()).await;
    let _ = ecriture.await;
    let sortie = match sortie {
        Ok(Ok(s)) => s,
        Ok(Err(e)) => return Err(format!("tesseract : {e}")),
        Err(_) => return Err("tesseract : plus de 12 s, abandon".into()),
    };
    let tsv = String::from_utf8_lossy(&sortie.stdout);
    // Regroupement par (bloc, paragraphe, ligne) : la boîte englobante et la confiance moyenne.
    let mut lignes: BTreeMap<(u32, u32, u32), (Vec<String>, i32, i32, i32, i32, f64, u32)> =
        BTreeMap::new();
    for l in tsv.lines().skip(1) {
        let c: Vec<&str> = l.split('\t').collect();
        if c.len() < 12 || c[0] != "5" {
            continue;
        }
        let conf: f64 = c[10].parse().unwrap_or(-1.0);
        let mot = c[11].trim();
        if conf < CONFIANCE_MIN || mot.is_empty() || !mot.chars().any(|ch| ch.is_alphanumeric()) {
            continue;
        }
        let (b, p, li) = (
            c[2].parse().unwrap_or(0),
            c[3].parse().unwrap_or(0),
            c[4].parse().unwrap_or(0),
        );
        let (x, y, w, h): (i32, i32, i32, i32) = (
            c[6].parse().unwrap_or(0),
            c[7].parse().unwrap_or(0),
            c[8].parse().unwrap_or(0),
            c[9].parse().unwrap_or(0),
        );
        let e = lignes
            .entry((b, p, li))
            .or_insert((vec![], i32::MAX, i32::MAX, 0, 0, 0.0, 0));
        e.0.push(mot.to_string());
        let (x2, y2) = (x + w, y + h);
        e.1 = e.1.min(x);
        e.2 = e.2.min(y);
        e.3 = e.3.max(x2);
        e.4 = e.4.max(y2);
        e.5 += conf;
        e.6 += 1;
    }
    let mut out: Vec<Ligne> = lignes
        .into_values()
        .filter(|(mots, ..)| !mots.is_empty())
        .map(|(mots, x1, y1, x2, y2, conf, n)| Ligne {
            texte: mots.join(" "),
            x: ox + x1,
            y: oy + y1,
            w: x2 - x1,
            h: y2 - y1,
            confiance: conf / n as f64,
        })
        .collect();
    out.sort_by_key(|l| (l.y, l.x));
    Ok(out)
}

/// Lit une zone de l'écran en lignes de texte positionnées (coordonnées écran).
pub async fn lire_zone(
    display: Option<String>,
    x: i32,
    y: i32,
    w: i32,
    h: i32,
) -> Result<Vec<Ligne>, String> {
    let (img, ox, oy) = capturer_async(display, x, y, w, h).await?;
    ocr(&img, ox, oy).await
}

/// Même chose, en gardant l'image et son origine (pour l'œil et les empreintes).
pub async fn lire_zone_image(
    display: Option<String>,
    x: i32,
    y: i32,
    w: i32,
    h: i32,
) -> Result<(Vec<Ligne>, Image, (i32, i32)), String> {
    let (img, ox, oy) = capturer_async(display, x, y, w, h).await?;
    let lignes = ocr(&img, ox, oy).await?;
    Ok((lignes, img, (ox, oy)))
}

/// Les lignes lues deviennent des éléments à identité estimée (clé = position + texte).
pub fn en_elements(wid: u32, lignes: &[Ligne]) -> Vec<Lu> {
    lignes
        .iter()
        .map(|l| Lu {
            cle_source: format!("ocr|{wid}|{}|{}|{}|{}|{}", l.x, l.y, l.w, l.h, l.texte),
            role: "texte".into(),
            role_atspi: "ocr".into(),
            nom: l.texte.clone(),
            texte: None,
            valeur: Some(format!("{:.0} %", l.confiance)),
            etats: vec![],
            x: l.x,
            y: l.y,
            w: l.w,
            h: l.h,
            parent_cle: None,
            profondeur: 1,
            enfants: 0,
            source: Some("ocr".into()),
        })
        .collect()
}

pub fn normaliser(s: &str) -> String {
    s.chars()
        .filter(|c| c.is_alphanumeric())
        .flat_map(|c| c.to_lowercase())
        .collect()
}

/// Un élément connu par son nom mais sans position fiable (une icône du bureau, par exemple) : la
/// ligne lue dont le texte correspond lui donne sa position. Rend le nombre d'éléments repositionnés.
pub fn positionner(elements: &mut [Lu], lignes: &[Ligne], mal: &[usize]) -> usize {
    let mut n = 0;
    for &i in mal {
        let nom = normaliser(&elements[i].nom);
        if nom.len() < 3 {
            continue;
        }
        let mut meilleur: Option<(usize, &Ligne)> = None;
        for l in lignes {
            let t = normaliser(&l.texte);
            let ok =
                t.contains(&nom) || (t.len() >= 6 && nom.contains(&t) && t.len() * 3 >= nom.len());
            if ok {
                let ecart = t.len().abs_diff(nom.len());
                if meilleur.map(|m| ecart < m.0).unwrap_or(true) {
                    meilleur = Some((ecart, l));
                }
            }
        }
        if let Some((_, l)) = meilleur {
            let e = &mut elements[i];
            e.x = l.x;
            e.y = l.y;
            e.w = l.w;
            e.h = l.h;
            e.source = Some("atspi+ocr".into());
            n += 1;
        }
    }
    n
}

/// Position jugée non fiable : nulle, ou couvrant presque toute la fenêtre pour un simple texte ou contrôle.
pub fn mal_positionne(l: &Lu, zone: (i32, i32, i32, i32)) -> bool {
    let (_, _, zl, zh) = zone;
    if l.w <= 0 || l.h <= 0 {
        return true;
    }
    let aire = l.w as i64 * l.h as i64;
    let aire_zone = zl as i64 * zh as i64;
    aire_zone > 0
        && aire * 10 >= aire_zone * 6
        && matches!(
            l.role.as_str(),
            "texte" | "titre" | "lien" | "bouton" | "élément" | "image" | "icône"
        )
}

/// Avant un clic sur une ligne lue : on recapture une bande autour d'elle et le texte doit y être encore.
pub async fn verifier(display: Option<String>, e: &Element) -> Option<bool> {
    let marge = 24;
    let lignes = lire_zone(
        display,
        e.x - marge,
        e.y - marge,
        e.w + 2 * marge,
        e.h + 2 * marge,
    )
    .await
    .ok()?;
    let attendu = normaliser(&e.nom);
    if attendu.len() < 2 {
        return Some(false);
    }
    let (cx, cy) = (e.x + e.w / 2, e.y + e.h / 2);
    Some(lignes.iter().any(|l| {
        let t = normaliser(&l.texte);
        (t.contains(&attendu) || attendu.contains(&t) && t.len() * 2 >= attendu.len())
            && cx >= l.x - 8
            && cx <= l.x + l.w + 8
            && cy >= l.y - 8
            && cy <= l.y + l.h + 8
    }))
}

/// Avant un clic sur un élément vu par l'œil : la boîte est recapturée et doit ressembler à son empreinte.
pub async fn verifier_empreinte(
    display: Option<String>,
    e: &Element,
    empreinte: &[u8],
) -> Option<bool> {
    let (img, ox, oy) = capturer_async(display, e.x, e.y, e.w, e.h).await.ok()?;
    let v = vignette(&img, e.x - ox, e.y - oy, e.w, e.h);
    Some(ecart(&v, empreinte) <= 0.08)
}
