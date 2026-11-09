//! La prise DevTools (Chrome DevTools Protocol) : Chrome, Chromium et Electron lancés avec
//! `--remote-debugging-port=PORT` exposent chaque page entière, devant ou derrière, là où leur
//! arbre AT-SPI se vide en arrière-plan. Lecture seule : arbre d'accessibilité de la page +
//! boîtes de mise en page, projetées en pixels écran ; hit-test DOM avant un clic.
//!
//! Le pilote ne branche que les ports qu'on lui donne (`REGARD_CDP_PORTS`, 9222-9224 par
//! défaut) : un port DevTools ouvert expose la session du navigateur à tout programme local.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::sync::{mpsc, oneshot, Mutex, Notify};
use tokio_tungstenite::tungstenite::Message;

use crate::modele::{maintenant, Element, Lu, Wid};
use crate::Partage;

pub const MAX_ELEMENTS: usize = 900;

fn libelle_ax(role: &str) -> Option<&'static str> {
    Some(match role {
        "link" => "lien",
        "button" => "bouton",
        "textbox" | "searchbox" => "champ",
        "combobox" | "listbox" => "liste",
        "checkbox" => "case",
        "radio" => "radio",
        "switch" => "interrupteur",
        "tab" => "onglet",
        "menuitem" | "menuitemcheckbox" | "menuitemradio" => "menu",
        "menu" => "menu",
        "menubar" => "barre-menu",
        "toolbar" => "barre-outils",
        "option" | "listitem" | "treeitem" => "élément",
        "heading" => "titre",
        "StaticText" | "paragraph" | "LabelText" | "code" | "time" => "texte",
        "image" | "img" => "image",
        "dialog" | "alertdialog" => "dialogue",
        "alert" => "alerte",
        "slider" => "curseur",
        "spinbutton" => "compteur",
        "cell" | "gridcell" | "columnheader" | "rowheader" => "cellule",
        "progressbar" => "progression",
        "status" => "barre-état",
        "tooltip" => "infobulle",
        "RootWebArea" => "document",
        "navigation" | "main" | "banner" | "contentinfo" | "complementary" | "region" | "form"
        | "search" | "article" => "zone",
        _ => return None,
    })
}

struct Commande {
    methode: String,
    params: Value,
    repondre: oneshot::Sender<Result<Value, String>>,
}

pub struct Page {
    pub id: String,
    pub port: u16,
    pub connexion: String,
    pub titre: Mutex<String>,
    pub url: Mutex<String>,
    /// Rang dans `/json/list` au dernier balayage : Chrome y met l'onglet actif le plus récent en premier.
    pub ordre: Mutex<usize>,
    cmd: mpsc::Sender<Commande>,
}

impl Page {
    pub async fn appeler(
        &self,
        methode: &str,
        params: Value,
        delai: Duration,
    ) -> Result<Value, String> {
        let (tx, rx) = oneshot::channel();
        self.cmd
            .send(Commande {
                methode: methode.to_string(),
                params,
                repondre: tx,
            })
            .await
            .map_err(|_| "session DevTools fermée".to_string())?;
        match tokio::time::timeout(delai, rx).await {
            Ok(Ok(r)) => r,
            Ok(Err(_)) => Err("session DevTools fermée".into()),
            Err(_) => Err(format!(
                "{methode} : pas de réponse en {:.1} s",
                delai.as_secs_f64()
            )),
        }
    }
}

pub struct Devtools {
    pub ports: Vec<u16>,
    pages: Mutex<HashMap<String, Arc<Page>>>,
    partage: Partage,
    reveil: Arc<Notify>,
    compteur: AtomicU64,
}

impl Devtools {
    pub fn nouveau(partage: Partage, reveil: Arc<Notify>) -> Arc<Self> {
        let ports: Vec<u16> = std::env::var("REGARD_CDP_PORTS")
            .unwrap_or_else(|_| "9222,9223,9224".into())
            .split(',')
            .filter_map(|p| p.trim().parse().ok())
            .collect();
        Arc::new(Devtools {
            ports,
            pages: Mutex::new(HashMap::new()),
            partage,
            reveil,
            compteur: AtomicU64::new(0),
        })
    }

    /// La page dont le titre correspond au titre de fenêtre X11 (« Titre - Google Chrome »).
    /// Plusieurs onglets peuvent porter le même titre : on garde ceux dont la fenêtre a la géométrie
    /// X11 attendue, puis l'onglet visible, puis le plus récemment actif (ordre de `/json/list`).
    pub async fn page_pour(&self, titre: &str, geo: Option<crate::x11::Geo>) -> Option<Arc<Page>> {
        let t = titre.trim();
        let pages: Vec<Arc<Page>> = self.pages.lock().await.values().cloned().collect();
        let mut candidats = vec![];
        for p in &pages {
            let pt = p.titre.lock().await.clone();
            if !pt.is_empty()
                && (t == pt
                    || t.starts_with(&format!("{pt} - "))
                    || t.starts_with(&format!("{pt} · ")))
            {
                candidats.push(p.clone());
            }
        }
        if candidats.is_empty() {
            for p in &pages {
                let pt = p.titre.lock().await.clone();
                if !pt.is_empty() && t.contains(&pt) {
                    candidats.push(p.clone());
                }
            }
        }
        if candidats.len() <= 1 {
            return candidats.pop();
        }
        let mut notes: Vec<(bool, bool, usize, Arc<Page>)> = vec![];
        for p in candidats {
            let r = p
                .appeler(
                    "Runtime.evaluate",
                    json!({"expression": "JSON.stringify([window.screenX, window.screenY, window.outerWidth, window.outerHeight, document.visibilityState])", "returnByValue": true}),
                    Duration::from_millis(500),
                )
                .await;
            let (mut meme_fenetre, mut visible) = (false, false);
            if let Ok(v) = r {
                if let Some(a) = v["result"]["value"]
                    .as_str()
                    .and_then(|s| serde_json::from_str::<Vec<Value>>(s).ok())
                {
                    let f = |i: usize| a.get(i).and_then(|x| x.as_f64()).unwrap_or(-1e9);
                    visible = a.get(4).and_then(|x| x.as_str()) == Some("visible");
                    if let Some(g) = geo {
                        meme_fenetre = (f(0) - g.x as f64).abs() <= 4.0
                            && (f(1) - g.y as f64).abs() <= 4.0
                            && (f(2) - g.l as f64).abs() <= 8.0
                            && (f(3) - g.h as f64).abs() <= 8.0;
                    }
                }
            }
            let ordre = *p.ordre.lock().await;
            notes.push((meme_fenetre, visible, ordre, p));
        }
        if geo.is_some() && notes.iter().any(|n| n.0) {
            notes.retain(|n| n.0);
        }
        notes.sort_by(|a, b| b.1.cmp(&a.1).then(a.2.cmp(&b.2)));
        notes.into_iter().next().map(|n| n.3)
    }

    pub async fn pages_resume(&self) -> Vec<Value> {
        let pages: Vec<Arc<Page>> = self.pages.lock().await.values().cloned().collect();
        let mut v = vec![];
        for p in pages {
            let vis = p
                .appeler(
                    "Runtime.evaluate",
                    json!({"expression": "document.visibilityState", "returnByValue": true}),
                    Duration::from_millis(400),
                )
                .await
                .ok()
                .and_then(|r| r["result"]["value"].as_str().map(|s| s.to_string()));
            v.push(json!({"port": p.port, "titre": p.titre.lock().await.clone(), "url": p.url.lock().await.chars().take(80).collect::<String>(), "visibilite": vis}));
        }
        v
    }

    /// Scrute les ports : une session par page, retirée quand la page disparaît.
    pub async fn scruter(self: Arc<Self>) {
        let mut vus: HashMap<u16, bool> = HashMap::new();
        loop {
            for &port in &self.ports {
                let cibles = liste_pages(port).await;
                match cibles {
                    None => {
                        if vus.remove(&port).is_some() {
                            let n = {
                                let mut pages = self.pages.lock().await;
                                let n = pages.values().filter(|p| p.port == port).count();
                                pages.retain(|_, p| p.port != port);
                                n
                            };
                            crate::alerte::alerter(&mut *self.partage.write().await, "DevTools", false, format!("port {port} fermé ({n} page(s) perdues) : ce navigateur ne se lit plus qu'au premier plan"));
                        }
                    }
                    Some(cibles) => {
                        vus.entry(port).or_insert(true);
                        let mut presents = std::collections::HashSet::new();
                        for (rang, c) in cibles.iter().enumerate() {
                            let id = c
                                .get("id")
                                .and_then(|v| v.as_str())
                                .unwrap_or("")
                                .to_string();
                            let ws = c
                                .get("webSocketDebuggerUrl")
                                .and_then(|v| v.as_str())
                                .unwrap_or("")
                                .to_string();
                            let url = c
                                .get("url")
                                .and_then(|v| v.as_str())
                                .unwrap_or("")
                                .to_string();
                            let titre = c
                                .get("title")
                                .and_then(|v| v.as_str())
                                .unwrap_or("")
                                .to_string();
                            if id.is_empty()
                                || ws.is_empty()
                                || c.get("type").and_then(|v| v.as_str()) != Some("page")
                                || url.starts_with("devtools://")
                                || url.starts_with("chrome://")
                                || url.starts_with("chrome-extension://")
                            {
                                continue;
                            }
                            presents.insert(id.clone());
                            let existante = self.pages.lock().await.get(&id).cloned();
                            match existante {
                                Some(p) => {
                                    let mut t = p.titre.lock().await;
                                    if *t != titre {
                                        *t = titre.clone();
                                    }
                                    *p.url.lock().await = url;
                                    *p.ordre.lock().await = rang;
                                }
                                None => self.clone().attacher(port, id, ws, titre, url, rang).await,
                            }
                        }
                        let mut pages = self.pages.lock().await;
                        pages.retain(|id, p| p.port != port || presents.contains(id));
                    }
                }
            }
            tokio::time::sleep(Duration::from_secs(2)).await;
        }
    }

    async fn attacher(
        self: Arc<Self>,
        port: u16,
        id: String,
        ws: String,
        titre: String,
        url: String,
        rang: usize,
    ) {
        let (tx, rx) = mpsc::channel::<Commande>(32);
        let n = self.compteur.fetch_add(1, Ordering::Relaxed);
        let page = Arc::new(Page {
            id: id.clone(),
            port,
            connexion: format!("{:x}{:x}", std::process::id(), n + (maintenant() as u64)),
            titre: Mutex::new(titre),
            url: Mutex::new(url),
            ordre: Mutex::new(rang),
            cmd: tx,
        });
        self.pages.lock().await.insert(id.clone(), page.clone());
        let moi = self.clone();
        tokio::spawn(async move {
            session(
                ws,
                rx,
                page.clone(),
                moi.partage.clone(),
                moi.reveil.clone(),
            )
            .await;
            let mut pages = moi.pages.lock().await;
            if pages
                .get(&id)
                .map(|p| Arc::ptr_eq(p, &page))
                .unwrap_or(false)
            {
                pages.remove(&id);
            }
        });
    }

    /// Les éléments d'une page en pixels écran, ou None si aucune page ne correspond au titre.
    pub async fn lire(
        &self,
        titre: &str,
        geo: Option<crate::x11::Geo>,
        delai: Duration,
    ) -> Option<Result<LectureCdp, String>> {
        let page = self.page_pour(titre, geo).await?;
        Some(lire_page(&page, geo.map(|g| (g.x, g.y, g.marges.0, g.marges.1, g.marges.2, g.marges.3)), delai).await)
    }

    /// Le texte complet d'un nœud de page (valeur d'un champ, sinon `innerText`), lecture seule.
    pub async fn texte(
        &self,
        titre: &str,
        geo: Option<crate::x11::Geo>,
        e: &Element,
        max: usize,
    ) -> Option<Result<(String, usize), String>> {
        let morceaux: Vec<&str> = e.cle_source.split('|').collect();
        if morceaux.len() != 8 || morceaux[0] != "devtools" {
            return None;
        }
        let page = self.page_pour(titre, geo).await?;
        if page.port.to_string() != morceaux[1] || page.id != morceaux[2] {
            return Some(Err("la page a changé depuis le regard".into()));
        }
        let bid: i64 = morceaux[7].parse().ok()?;
        let d = Duration::from_millis(2500);
        let obj = match page
            .appeler("DOM.resolveNode", json!({"backendNodeId": bid}), d)
            .await
        {
            Ok(o) => o,
            Err(r) => return Some(Err(r)),
        };
        let Some(object_id) = obj["object"]["objectId"].as_str().map(|s| s.to_string()) else {
            return Some(Err("nœud introuvable".into()));
        };
        let fonction = format!(
            "function() {{ const n = this.nodeType === 1 ? this : this.parentElement; \
             const v = (this.nodeType === 1 && typeof this.value === 'string') ? this.value : (n ? n.innerText : this.textContent); \
             const t = String(v == null ? '' : v); return [t.length, t.slice(0, {max})]; }}"
        );
        let rep = page
            .appeler("Runtime.callFunctionOn", json!({"objectId": object_id, "functionDeclaration": fonction, "returnByValue": true, "silent": true}), d)
            .await;
        let _ = page
            .appeler(
                "Runtime.releaseObject",
                json!({"objectId": object_id}),
                Duration::from_millis(500),
            )
            .await;
        Some(match rep {
            Ok(v) => {
                let n = v["result"]["value"][0].as_u64().unwrap_or(0) as usize;
                let t = v["result"]["value"][1].as_str().unwrap_or("").to_string();
                Ok((t, n))
            }
            Err(r) => Err(r),
        })
    }

    /// Hit-test DOM en lecture seule : Some(true) confirmé, Some(false) réfuté, None non vérifiable.
    pub async fn verifier_cible(
        &self,
        titre: &str,
        geo: Option<crate::x11::Geo>,
        e: &Element,
    ) -> Option<bool> {
        let origine = geo.map(|g| (g.x, g.y, g.marges.0, g.marges.1, g.marges.2, g.marges.3));
        let morceaux: Vec<&str> = e.cle_source.split('|').collect();
        if morceaux.len() != 8 || morceaux[0] != "devtools" {
            return None;
        }
        let (port, id, connexion, frame, loader, racine, bid) = (
            morceaux[1],
            morceaux[2],
            morceaux[3],
            morceaux[4],
            morceaux[5],
            morceaux[6],
            morceaux[7],
        );
        let page = self.page_pour(titre, geo).await?;
        if page.port.to_string() != port || page.id != id || page.connexion != connexion {
            return Some(false);
        }
        if e.etats.iter().any(|s| s == "inactif") {
            return Some(false);
        }
        let fin = Instant::now() + Duration::from_millis(1200);
        let restant = |fin: Instant| fin.saturating_duration_since(Instant::now());
        let meme_document = |page: Arc<Page>| async move {
            let f = page
                .appeler("Page.getFrameTree", json!({}), restant(fin))
                .await
                .ok()?;
            let fr = &f["frameTree"]["frame"];
            if fr["id"].as_str() != Some(frame) || fr["loaderId"].as_str() != Some(loader) {
                return Some(false);
            }
            let d = page
                .appeler("DOM.getDocument", json!({"depth": 0}), restant(fin))
                .await
                .ok()?;
            Some(
                d["root"]["backendNodeId"]
                    .as_i64()
                    .map(|v| v.to_string())
                    .as_deref()
                    == Some(racine),
            )
        };
        if meme_document(page.clone()).await != Some(true) {
            return Some(false);
        }
        let bid: i64 = bid.parse().ok()?;
        let obj = page
            .appeler(
                "DOM.resolveNode",
                json!({"backendNodeId": bid}),
                restant(fin),
            )
            .await
            .ok()?;
        let Some(object_id) = obj["object"]["objectId"].as_str().map(|s| s.to_string()) else {
            return None;
        };
        let fonction = r#"function(point, geometrie) {
            const n = this.nodeType === 1 ? this : this.parentElement;
            if (!n || !this.isConnected || n.ownerDocument !== document) return false;
            if (n.matches(':disabled') || n.closest('[inert], [aria-disabled="true"]')) return false;
            const dpr = window.devicePixelRatio || 1;
            const dl = window.outerWidth - window.innerWidth, dh = window.outerHeight - window.innerHeight;
            // geometrie = [x, y, marge gauche, marge droite, marge haut, marge bas] de la fenêtre X11 (ombres CSD comprises dans outer*).
            const ox = geometrie ? geometrie[0] + geometrie[2] + Math.floor((dl - geometrie[2] - geometrie[3]) / 2) : window.screenX + Math.floor(dl / 2);
            const oy = geometrie ? geometrie[1] + geometrie[4] + (dh - geometrie[4] - geometrie[5]) : window.screenY + dh;
            const x = (point[0] - ox) / dpr, y = (point[1] - oy) / dpr;
            if (![x, y].every(Number.isFinite) || x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) return false;
            for (let a = n; a; a = a.parentElement) {
                const s = getComputedStyle(a);
                if (s.display === 'none' || s.visibility === 'hidden' || s.visibility === 'collapse' || Number(s.opacity) === 0) return false;
            }
            let rects;
            if (this.nodeType === 1) rects = this.getClientRects();
            else { const r = document.createRange(); r.selectNodeContents(this); rects = r.getClientRects(); }
            if (!Array.from(rects).some(r => r.left <= x && x < r.right && r.top <= y && y < r.bottom)) return false;
            let hit = document.elementFromPoint(x, y);
            while (hit && hit.shadowRoot) { const deeper = hit.shadowRoot.elementFromPoint(x, y); if (!deeper || deeper === hit) break; hit = deeper; }
            return !!hit && (hit === n || n.contains(hit));
        }"#;
        let point = json!([e.x as f64 + e.w as f64 / 2.0, e.y as f64 + e.h as f64 / 2.0]);
        let geo = origine.map(|(x, y, mg, md, mh, mb)| json!([x, y, mg, md, mh, mb])).unwrap_or(Value::Null);
        let rep = page
            .appeler(
                "Runtime.callFunctionOn",
                json!({"objectId": object_id, "functionDeclaration": fonction, "arguments": [{"value": point}, {"value": geo}],
                       "returnByValue": true, "silent": true, "userGesture": false}),
                restant(fin),
            )
            .await;
        let _ = page
            .appeler(
                "Runtime.releaseObject",
                json!({"objectId": object_id}),
                Duration::from_millis(500),
            )
            .await;
        let rep = rep.ok()?;
        if rep.get("exceptionDetails").is_some() {
            return None;
        }
        match rep["result"]["value"].as_bool() {
            Some(false) => Some(false),
            Some(true) => meme_document(page).await,
            None => None,
        }
    }
}

pub struct LectureCdp {
    pub elements: Vec<Lu>,
    pub titre: String,
    pub url: String,
    pub tronquee: bool,
    pub lecture_ms: u32,
}

async fn lire_page(
    page: &Arc<Page>,
    origine: Option<(i32, i32, i32, i32, i32, i32)>,
    delai: Duration,
) -> Result<LectureCdp, String> {
    let t0 = Instant::now();
    let fin = t0 + delai;
    let restant = |fin: Instant| fin.saturating_duration_since(Instant::now());
    let avant = page
        .appeler("Page.getFrameTree", json!({}), restant(fin))
        .await?;
    let (frame, loader) = (
        avant["frameTree"]["frame"]["id"]
            .as_str()
            .unwrap_or("")
            .to_string(),
        avant["frameTree"]["frame"]["loaderId"]
            .as_str()
            .unwrap_or("")
            .to_string(),
    );
    if frame.is_empty() || loader.is_empty() {
        return Err("document DevTools sans identifiant de chargement".into());
    }
    let rep = page
        .appeler(
            "Runtime.evaluate",
            json!({"expression": "JSON.stringify([window.screenX, window.screenY, window.outerWidth - window.innerWidth, window.outerHeight - window.innerHeight, window.devicePixelRatio, document.title])", "returnByValue": true}),
            restant(fin),
        )
        .await?;
    let metriques: Vec<Value> = rep["result"]["value"]
        .as_str()
        .and_then(|s| serde_json::from_str(s).ok())
        .unwrap_or_default();
    let m = |i: usize| metriques.get(i).and_then(|v| v.as_f64()).unwrap_or(0.0);
    let (sx, sy, dl, dh, dpr) = (m(0), m(1), m(2), m(3), if m(4) > 0.0 { m(4) } else { 1.0 });
    let titre_doc = metriques
        .get(5)
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let ax = page
        .appeler("Accessibility.getFullAXTree", json!({}), restant(fin))
        .await?;
    let inst = page
        .appeler(
            "DOMSnapshot.captureSnapshot",
            json!({"computedStyles": [], "includeDOMRects": true}),
            restant(fin),
        )
        .await?;
    let apres = page
        .appeler("Page.getFrameTree", json!({}), restant(fin))
        .await?;
    if apres["frameTree"]["frame"]["id"].as_str() != Some(&frame)
        || apres["frameTree"]["frame"]["loaderId"].as_str() != Some(&loader)
    {
        return Err("document changé pendant la lecture DevTools ; réessayer".into());
    }
    let doc = inst["documents"].get(0).cloned().unwrap_or(Value::Null);
    let strings = inst["strings"].as_array().cloned().unwrap_or_default();
    let frame_id = doc["frameId"]
        .as_i64()
        .and_then(|i| strings.get(i as usize))
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if frame_id != frame {
        return Err("instantané DevTools d'un autre document ; réessayer".into());
    }
    let backend_ids: Vec<i64> = doc["nodes"]["backendNodeId"]
        .as_array()
        .map(|a| a.iter().filter_map(|v| v.as_i64()).collect())
        .unwrap_or_default();
    let racine_dom = backend_ids.first().copied().unwrap_or(0);
    // Chrome en CSD compte ses ombres dans outerWidth/outerHeight : on les retire (marges X11) pour trouver
    // l'origine réelle de la page. Vu le 08/11 : 26 px d'écart en y sans ça (ombre du bas).
    let (ox, oy) = match origine {
        Some((x, y, mg, md, mh, mb)) => (x as f64 + mg as f64 + ((dl - (mg + md) as f64) / 2.0).floor(), y as f64 + mh as f64 + (dh - (mh + mb) as f64)),
        None => (sx + (dl / 2.0).floor(), sy + dh),
    };
    let (scroll_x, scroll_y) = (
        doc["scrollOffsetX"].as_f64().unwrap_or(0.0),
        doc["scrollOffsetY"].as_f64().unwrap_or(0.0),
    );
    // Boîtes de mise en page par nœud DOM (backendNodeId).
    let mut boites: HashMap<i64, (f64, f64, f64, f64)> = HashMap::new();
    if let (Some(idx), Some(bounds)) = (
        doc["layout"]["nodeIndex"].as_array(),
        doc["layout"]["bounds"].as_array(),
    ) {
        for (i, ni) in idx.iter().enumerate() {
            let Some(ni) = ni.as_i64() else { continue };
            let Some(b) = bounds.get(i).and_then(|b| b.as_array()) else {
                continue;
            };
            if b.len() == 4 {
                let (x, y, w, h) = (
                    b[0].as_f64().unwrap_or(0.0),
                    b[1].as_f64().unwrap_or(0.0),
                    b[2].as_f64().unwrap_or(0.0),
                    b[3].as_f64().unwrap_or(0.0),
                );
                if w > 0.0 && h > 0.0 {
                    if let Some(bid) = backend_ids.get(ni as usize) {
                        boites.insert(*bid, (x, y, w, h));
                    }
                }
            }
        }
    }
    let prefixe = format!(
        "devtools|{}|{}|{}|{}|{}|{}",
        page.port, page.id, page.connexion, frame, loader, racine_dom
    );
    let noeuds = ax["nodes"].as_array().cloned().unwrap_or_default();
    // Profondeur et parent : le plus proche ancêtre RETENU dans l'arbre d'accessibilité de la page.
    let mut retenu: HashMap<String, (String, u8)> = HashMap::new(); // nodeId AX -> (cle, profondeur)
    let mut parent_ax: HashMap<String, String> = HashMap::new();
    for n in &noeuds {
        if let (Some(id), Some(p)) = (n["nodeId"].as_str(), n["parentId"].as_str()) {
            parent_ax.insert(id.to_string(), p.to_string());
        }
    }
    let mut out = vec![];
    let mut tronquee = false;
    for n in &noeuds {
        if n["ignored"].as_bool().unwrap_or(false) {
            continue;
        }
        let role_ax = n["role"]["value"].as_str().unwrap_or("");
        let Some(libelle) = libelle_ax(role_ax) else {
            continue;
        };
        let nom = n["name"]["value"].as_str().unwrap_or("").trim().to_string();
        let valeur = match &n["value"]["value"] {
            Value::String(s) => s.trim().to_string(),
            Value::Number(x) => x.to_string(),
            _ => String::new(),
        };
        if matches!(libelle, "texte" | "zone" | "image" | "élément" | "cellule")
            && nom.is_empty()
            && valeur.is_empty()
        {
            continue;
        }
        let Some(bid) = n["backendDOMNodeId"].as_i64() else {
            continue;
        };
        let Some((bx, by, bw, bh)) = boites.get(&bid).copied() else {
            continue;
        };
        let x = (bx - scroll_x) * dpr + ox;
        let y = (by - scroll_y) * dpr + oy;
        let mut etats = vec![];
        if let Some(props) = n["properties"].as_array() {
            for p in props {
                let nomp = p["name"].as_str().unwrap_or("");
                let v = &p["value"]["value"];
                let vrai = v.as_bool() == Some(true) || v.as_str() == Some("true");
                match nomp {
                    "focused" if vrai => etats.push("focus".to_string()),
                    "checked" if vrai => etats.push("coché".to_string()),
                    "selected" if vrai => etats.push("sélectionné".to_string()),
                    "disabled" if vrai => etats.push("inactif".to_string()),
                    "expanded" if vrai => etats.push("déplié".to_string()),
                    "expanded" if v.as_bool() == Some(false) => etats.push("replié".to_string()),
                    "required" if vrai => etats.push("requis".to_string()),
                    "invalid" if vrai => etats.push("invalide".to_string()),
                    "readonly" if vrai => etats.push("lecture seule".to_string()),
                    "modal" if vrai => etats.push("modal".to_string()),
                    "hasPopup"
                        if v.as_str()
                            .map(|s| !s.is_empty() && s != "false")
                            .unwrap_or(false) =>
                    {
                        etats.push("menu".to_string())
                    }
                    _ => {}
                }
            }
        }
        let node_id = n["nodeId"].as_str().unwrap_or("").to_string();
        let mut parent_cle = None;
        let mut profondeur = 0u8;
        let mut p = parent_ax.get(&node_id).cloned();
        let mut garde = 0;
        while let Some(pid) = p {
            if let Some((c, d)) = retenu.get(&pid) {
                parent_cle = Some(c.clone());
                profondeur = d.saturating_add(1);
                break;
            }
            p = parent_ax.get(&pid).cloned();
            garde += 1;
            if garde > 200 {
                break;
            }
        }
        let cle = format!("{prefixe}|{bid}");
        retenu.insert(node_id, (cle.clone(), profondeur));
        if out.len() >= MAX_ELEMENTS {
            tronquee = true;
            break;
        }
        let enfants = n["childIds"]
            .as_array()
            .map(|a| a.len() as u32)
            .unwrap_or(0);
        out.push(Lu {
            cle_source: cle,
            role: libelle.to_string(),
            role_atspi: role_ax.to_string(),
            nom: nom.chars().take(120).collect(),
            texte: if valeur.is_empty() {
                None
            } else {
                Some(valeur.chars().take(240).collect())
            },
            valeur: None,
            etats,
            x: x.round() as i32,
            y: y.round() as i32,
            w: (bw * dpr).round() as i32,
            h: (bh * dpr).round() as i32,
            parent_cle,
            profondeur,
            enfants,
            source: None,
        });
    }
    Ok(LectureCdp {
        elements: out,
        titre: titre_doc,
        url: page.url.lock().await.clone(),
        tronquee,
        lecture_ms: t0.elapsed().as_millis() as u32,
    })
}

/// GET http://127.0.0.1:port/json/list — la liste des pages d'un port DevTools (None : port fermé).
/// Chrome garde la connexion ouverte : on lit l'en-tête, puis exactement `Content-Length` octets.
async fn liste_pages(port: u16) -> Option<Vec<Value>> {
    let travail = async {
        let mut s = TcpStream::connect(("127.0.0.1", port)).await.ok()?;
        s.write_all(
            format!(
                "GET /json/list HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
            )
            .as_bytes(),
        )
        .await
        .ok()?;
        let mut buf = Vec::new();
        let mut morceau = [0u8; 65536];
        loop {
            let fin_entete = buf.windows(4).position(|w| w == b"\r\n\r\n");
            if let Some(i) = fin_entete {
                let entete = String::from_utf8_lossy(&buf[..i]).to_lowercase();
                let longueur = entete
                    .lines()
                    .find_map(|l| {
                        l.strip_prefix("content-length:")
                            .map(|v| v.trim().parse::<usize>().ok())
                    })
                    .flatten();
                match longueur {
                    Some(n) if buf.len() >= i + 4 + n => break,
                    Some(_) => {}
                    None => {
                        // Sans longueur annoncée : la fermeture de la connexion termine le corps.
                    }
                }
            }
            let n = s.read(&mut morceau).await.ok()?;
            if n == 0 {
                break;
            }
            buf.extend_from_slice(&morceau[..n]);
            if buf.len() > 8 << 20 {
                return None;
            }
        }
        let texte = String::from_utf8_lossy(&buf);
        let corps = texte.split("\r\n\r\n").nth(1)?;
        serde_json::from_str::<Value>(corps)
            .ok()?
            .as_array()
            .cloned()
    };
    tokio::time::timeout(Duration::from_millis(1500), travail)
        .await
        .ok()
        .flatten()
}

/// Une session WebSocket par page : commandes → réponses par id ; événements → fenêtres à relire.
async fn session(
    ws_url: String,
    mut cmd: mpsc::Receiver<Commande>,
    page: Arc<Page>,
    partage: Partage,
    reveil: Arc<Notify>,
) {
    let ws = match tokio::time::timeout(
        Duration::from_secs(5),
        tokio_tungstenite::connect_async(ws_url.as_str()),
    )
    .await
    {
        Ok(Ok((ws, _))) => ws,
        _ => {
            partage.write().await.erreur(format!(
                "DevTools : page « {} » injoignable",
                page.titre.lock().await.chars().take(60).collect::<String>()
            ));
            return;
        }
    };
    let (mut tx, mut rx) = ws.split();
    let mut attentes: HashMap<u64, oneshot::Sender<Result<Value, String>>> = HashMap::new();
    let mut suivant: u64 = 0;
    for m in ["Page.enable", "Runtime.enable"] {
        suivant += 1;
        let _ = tx
            .send(Message::Text(
                json!({"id": suivant, "method": m, "params": {}})
                    .to_string()
                    .into(),
            ))
            .await;
    }
    loop {
        tokio::select! {
            c = cmd.recv() => {
                let Some(c) = c else { break };
                suivant += 1;
                let msg = json!({"id": suivant, "method": c.methode, "params": c.params}).to_string();
                if tx.send(Message::Text(msg.into())).await.is_err() {
                    let _ = c.repondre.send(Err("envoi DevTools impossible".into()));
                    break;
                }
                attentes.insert(suivant, c.repondre);
            }
            m = rx.next() => {
                match m {
                    Some(Ok(Message::Text(t))) => {
                        let Ok(v) = serde_json::from_str::<Value>(&t) else { continue };
                        if let Some(id) = v["id"].as_u64() {
                            if let Some(r) = attentes.remove(&id) {
                                let res = match v.get("error") {
                                    Some(e) => Err(e["message"].as_str().unwrap_or("erreur DevTools").to_string()),
                                    None => Ok(v.get("result").cloned().unwrap_or(json!({}))),
                                };
                                let _ = r.send(res);
                            }
                        } else {
                            let meth = v["method"].as_str().unwrap_or("");
                            if meth == "Page.frameNavigated" && v["params"]["frame"]["parentId"].is_null() {
                                if let Some(u) = v["params"]["frame"]["url"].as_str() {
                                    *page.url.lock().await = u.to_string();
                                }
                            }
                            if meth == "Page.frameNavigated" || meth == "Page.loadEventFired" {
                                // La fenêtre X11 qui porte ce titre est à relire.
                                let titre = page.titre.lock().await.clone();
                                let mut b = partage.write().await;
                                let wids: Vec<Wid> = b.fenetres.values().filter(|f| !titre.is_empty() && f.titre.contains(&titre)).map(|f| f.wid).collect();
                                for w in wids { b.programmer(w); }
                                drop(b);
                                reveil.notify_one();
                            }
                        }
                    }
                    Some(Ok(Message::Ping(p))) => { let _ = tx.send(Message::Pong(p)).await; }
                    Some(Ok(Message::Close(_))) | None => break,
                    Some(Err(_)) => break,
                    _ => {}
                }
            }
        }
    }
    for (_, r) in attentes.drain() {
        let _ = r.send(Err("session DevTools fermée".into()));
    }
}
