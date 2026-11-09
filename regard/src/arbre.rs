//! L'arbre d'accessibilité (AT-SPI) : la source de vérité quand l'application en expose un.
//!
//! Deux voies de lecture : le cache de l'application (`org.a11y.atspi.Cache.GetItems`, tout
//! l'arbre en un aller-retour, comme le font les lecteurs d'écran) puis les géométries en
//! parallèle ; à défaut, un parcours en largeur, concurrent, borné par un délai et un plafond.
//! Les événements du bus (focus, enfants, texte, nom, fenêtres, pages) ne servent qu'à dire
//! QUOI relire ; ils ne modifient jamais le modèle directement.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::time::{Duration, Instant};

use atspi::connection::{AccessibilityConnection, P2P};
use atspi::events::{DocumentEvents, Event, ObjectEvents, WindowEvents};
use atspi::proxy::accessible::{AccessibleProxy, ObjectRefExt};
use atspi::proxy::cache::CacheProxy;
use atspi::proxy::component::ComponentProxy;
use atspi::proxy::text::TextProxy;
use atspi::proxy::value::ValueProxy;
use atspi::{
    CacheItem, CoordType, EventProperties, Interface, ObjectRefOwned, Role, State, StateSet,
};
use futures_util::stream::{self, StreamExt};
use serde_json::{json, Value};
use tokio::sync::{broadcast, mpsc, oneshot, Notify};
use zbus::names::BusName;

use crate::devtools::Devtools;
use crate::modele::{maintenant, Element, Lecture, Lu, Wid, SHELL};
use crate::Partage;

pub const MAX_ELEMENTS: usize = 600;
const DELAI_ACTIVE: Duration = Duration::from_millis(1500);
const DELAI_FOND: Duration = Duration::from_millis(2500);
const TEXTE_MAX: i32 = 240;

/// Ce que le pont demande au lecteur : vérifier une cible (avant un clic du pilote), ou décrire
/// les applications vues sur le bus (diagnostic).
pub enum Demande {
    Cible {
        id: String,
        repondre: oneshot::Sender<(u16, Value)>,
    },
    Texte {
        id: String,
        repondre: oneshot::Sender<(u16, Value)>,
    },
    /// L'œil à la demande : capture + OCR + Qwen sur une fenêtre (l'active par défaut), quoi qu'elle expose.
    Oeil {
        motif: Option<String>,
        repondre: oneshot::Sender<(u16, Value)>,
    },
    Applis {
        repondre: oneshot::Sender<Value>,
    },
}

/// Longueur maximale d'un texte complet rendu par `/texte` (un terminal, un document, un mail).
pub const TEXTE_COMPLET_MAX: usize = 60_000;

/// Rôles listés (libellé court en français) ; les autres sont des conteneurs, parcourus mais muets.
pub fn libelle(role: &str) -> Option<&'static str> {
    Some(match role {
        "push button" | "toggle button" | "button" => "bouton",
        "check box" => "case",
        "radio button" => "radio",
        "switch" => "interrupteur",
        "menu" | "menu item" | "check menu item" | "radio menu item" => "menu",
        "menu bar" => "barre-menu",
        "combo box" | "list box" | "list" => "liste",
        "list item" | "tree item" => "élément",
        "table cell" => "cellule",
        "text" | "entry" | "password text" | "editbar" | "search box" => "champ",
        "link" => "lien",
        "page tab" | "tab" => "onglet",
        "slider" => "curseur",
        "spin button" => "compteur",
        "scroll bar" => "ascenseur",
        "label" | "static" | "paragraph" | "section" => "texte",
        "heading" => "titre",
        "image" => "image",
        "icon" => "icône",
        "tool bar" => "barre-outils",
        "dialog" => "dialogue",
        "alert" => "alerte",
        "notification" => "notif",
        "terminal" => "terminal",
        "progress bar" => "progression",
        "status bar" => "barre-état",
        "tooltip" => "infobulle",
        "document web" | "document frame" | "document text" => "document",
        _ => return None,
    })
}

fn est_texte(role: &str) -> bool {
    matches!(
        role,
        "text"
            | "entry"
            | "editbar"
            | "search box"
            | "terminal"
            | "label"
            | "static"
            | "paragraph"
            | "heading"
            | "section"
    )
}

fn etats_de(s: StateSet) -> Vec<String> {
    let mut v = Vec::new();
    if s.contains(State::Focused) {
        v.push("focus".into());
    }
    if !s.contains(State::Enabled) && !s.contains(State::Sensitive) {
        v.push("inactif".into());
    }
    if s.contains(State::Checked) {
        v.push("coché".into());
    }
    if s.contains(State::Pressed) {
        v.push("enfoncé".into());
    }
    if s.contains(State::Selected) {
        v.push("sélectionné".into());
    }
    if s.contains(State::Expanded) {
        v.push("déplié".into());
    } else if s.contains(State::Expandable) {
        v.push("replié".into());
    }
    if s.contains(State::Editable) {
        v.push("éditable".into());
    }
    if s.contains(State::ReadOnly) {
        v.push("lecture seule".into());
    }
    if s.contains(State::Modal) {
        v.push("modal".into());
    }
    if s.contains(State::Busy) {
        v.push("occupé".into());
    }
    if s.contains(State::Required) {
        v.push("requis".into());
    }
    if s.contains(State::InvalidEntry) {
        v.push("invalide".into());
    }
    if s.contains(State::HasPopup) {
        v.push("menu".into());
    }
    if s.contains(State::IsDefault) {
        v.push("par défaut".into());
    }
    v
}

fn cle(o: &ObjectRefOwned) -> String {
    format!("{}{}", o.name_as_str().unwrap_or(""), o.path_as_str())
}

fn nettoyer(s: &str, n: usize) -> String {
    // U+FFFC = « objet incorporé » (une image, un contrôle dans un texte) : rien à lire.
    let s: String = s
        .replace('\u{FFFC}', " ")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    if s.chars().count() > n {
        format!("{}…", s.chars().take(n).collect::<String>())
    } else {
        s
    }
}

async fn avec_delai<T, F: std::future::Future<Output = T>>(d: Duration, f: F) -> Option<T> {
    tokio::time::timeout(d, f).await.ok()
}

pub struct Lecteur {
    conn: AccessibilityConnection,
    partage: Partage,
    diffusion: broadcast::Sender<Value>,
    pids: HashMap<String, Option<u32>>,
    pids_refus: HashMap<String, f64>,
    /// Applications dont le cache a échoué (instant) : on ne le redemande pas avant une minute.
    cache_refus: std::sync::Mutex<HashMap<String, f64>>,
    /// Par fenêtre : le cadre AT-SPI lu en dernier (pour le hit-test) et l'origine de ses coordonnées.
    racines: HashMap<Wid, (ObjectRefOwned, i32, i32)>,
    devtools: Arc<Devtools>,
    /// Empreintes de pixels des éléments vus par l'œil (clé → vignette), vérifiées avant un clic.
    pub empreintes: HashMap<String, Vec<u8>>,
    /// Dernier appel de l'œil par fenêtre : au plus un toutes les 20 s.
    pub dernier_oeil: HashMap<Wid, f64>,
    pub oeil_signale: bool,
    p2p: bool,
}

impl Lecteur {
    pub async fn nouveau(
        partage: Partage,
        diffusion: broadcast::Sender<Value>,
        devtools: Arc<Devtools>,
    ) -> Result<Self, Box<dyn std::error::Error + Send + Sync>> {
        let conn = AccessibilityConnection::new().await?;
        // Connexions directes aux applications (p2p) : coupées par défaut. Vu le 07/11 : pour GTK4
        // (Nautilus), Qt (Charli) ou Steam, la bibliothèque n'obtient pas le canal direct et les appels
        // partent dans le vide ; par le bus, tout répond.
        let p2p = std::env::var("REGARD_P2P")
            .map(|v| v == "1")
            .unwrap_or(false);
        Ok(Lecteur {
            conn,
            partage,
            diffusion,
            pids: HashMap::new(),
            pids_refus: HashMap::new(),
            cache_refus: std::sync::Mutex::new(HashMap::new()),
            racines: HashMap::new(),
            devtools,
            empreintes: HashMap::new(),
            dernier_oeil: HashMap::new(),
            oeil_signale: false,
            p2p,
        })
    }

    fn bus(&self) -> &zbus::Connection {
        self.conn.connection()
    }

    async fn accessible<'a>(&'a self, o: &'a ObjectRefOwned) -> Option<AccessibleProxy<'a>> {
        if self.p2p {
            if let Ok(p) = self.conn.object_as_accessible(o).await {
                return Some(p);
            }
        }
        o.as_accessible_proxy(self.bus()).await.ok()
    }

    async fn composant(&self, o: &ObjectRefOwned) -> Option<ComponentProxy<'_>> {
        let name: BusName = o.name()?.clone().into();
        ComponentProxy::builder(self.bus())
            .destination(name)
            .ok()?
            .path(o.path().clone())
            .ok()?
            .cache_properties(zbus::proxy::CacheProperties::No)
            .build()
            .await
            .ok()
    }

    async fn texte(&self, o: &ObjectRefOwned) -> Option<TextProxy<'_>> {
        let name: BusName = o.name()?.clone().into();
        TextProxy::builder(self.bus())
            .destination(name)
            .ok()?
            .path(o.path().clone())
            .ok()?
            .cache_properties(zbus::proxy::CacheProperties::No)
            .build()
            .await
            .ok()
    }

    async fn valeur(&self, o: &ObjectRefOwned) -> Option<ValueProxy<'_>> {
        let name: BusName = o.name()?.clone().into();
        ValueProxy::builder(self.bus())
            .destination(name)
            .ok()?
            .path(o.path().clone())
            .ok()?
            .cache_properties(zbus::proxy::CacheProperties::No)
            .build()
            .await
            .ok()
    }

    /// Le pid d'un nom de bus (mémorisé) : c'est le lien entre fenêtre X11 et application AT-SPI.
    /// Un échec (délai dépassé au démarrage, par exemple) n'est jamais mémorisé pour de bon.
    async fn pid_de(&mut self, nom: &str) -> Option<u32> {
        if let Some(Some(p)) = self.pids.get(nom) {
            return Some(*p);
        }
        if let Some(t) = self.pids_refus.get(nom) {
            if maintenant() - t < 5.0 {
                return None;
            }
        }
        let dbus = zbus::fdo::DBusProxy::new(self.bus()).await.ok()?;
        let bn = BusName::try_from(nom.to_string()).ok()?;
        let pid = avec_delai(
            Duration::from_millis(1500),
            dbus.get_connection_unix_process_id(bn),
        )
        .await
        .and_then(|r| r.ok());
        match pid {
            Some(p) => {
                self.pids.insert(nom.to_string(), Some(p));
                self.pids_refus.remove(nom);
            }
            None => {
                self.pids_refus.insert(nom.to_string(), maintenant());
            }
        }
        pid
    }

    async fn applications(&self) -> Vec<ObjectRefOwned> {
        let Ok(racine) = self.conn.root_accessible_on_registry().await else {
            return vec![];
        };
        avec_delai(Duration::from_millis(1500), racine.get_children())
            .await
            .and_then(|r| r.ok())
            .unwrap_or_default()
    }

    /// Les applications dont le pid correspond (une application peut avoir plusieurs connexions).
    async fn applications_du_pid(&mut self, pid: u32) -> Vec<ObjectRefOwned> {
        let apps = self.applications().await;
        let mut res = vec![];
        for a in apps {
            let Some(n) = a.name_as_str().map(|s| s.to_string()) else {
                continue;
            };
            if self.pid_de(&n).await == Some(pid) {
                res.push(a);
            }
        }
        res
    }

    /// Le texte complet d'un élément, sans image : un terminal, une page de document, un mail, un champ.
    /// Par l'interface Texte de l'objet (AT-SPI) ou le DOM (DevTools) ; à défaut, les textes de ses descendants
    /// tels que le modèle les connaît.
    pub async fn texte_complet(&mut self, id: &str) -> (u16, Value) {
        let (e, titre, geo, descendants) = {
            let b = self.partage.read().await;
            match b.trouver(id) {
                Some((f, e)) => {
                    let par_id: HashMap<&str, &Element> =
                        f.elements.iter().map(|x| (x.id.as_str(), x)).collect();
                    let desc: Vec<String> = f
                        .elements
                        .iter()
                        .filter(|x| {
                            let mut p = x.parent.as_deref();
                            let mut n = 0;
                            while let Some(pid) = p {
                                if pid == id {
                                    return true;
                                }
                                p = par_id.get(pid).and_then(|y| y.parent.as_deref());
                                n += 1;
                                if n > 80 {
                                    break;
                                }
                            }
                            false
                        })
                        .filter_map(|x| {
                            x.texte.clone().or_else(|| {
                                if x.nom.is_empty() {
                                    None
                                } else {
                                    Some(x.nom.clone())
                                }
                            })
                        })
                        .collect();
                    (e.clone(), f.titre.clone(), f.geo, desc)
                }
                None => {
                    return (
                        404,
                        json!({"erreur": "référence inconnue (relis `ecran regard`)"}),
                    )
                }
            }
        };
        let reponse = |texte: String, longueur: usize, source: &str| {
            (
                200,
                json!({"id": id, "role": e.role, "nom": e.nom, "texte": texte, "longueur": longueur,
                         "tronque": longueur > TEXTE_COMPLET_MAX, "source": source}),
            )
        };
        if e.source.starts_with("devtools") {
            return match self
                .devtools
                .texte(&titre, geo, &e, TEXTE_COMPLET_MAX)
                .await
            {
                Some(Ok((t, n))) => reponse(t, n, "devtools"),
                Some(Err(r)) => (409, json!({"erreur": r})),
                None => (
                    409,
                    json!({"erreur": "page DevTools introuvable pour cette fenêtre"}),
                ),
            };
        }
        if let Some((nom_bus, chemin)) = e.cle_source.split_once('/') {
            let chemin = format!("/{chemin}");
            let proxy = async {
                let name = BusName::try_from(nom_bus.to_string()).ok()?;
                let path = zbus::zvariant::ObjectPath::try_from(chemin.clone()).ok()?;
                TextProxy::builder(self.bus())
                    .destination(name)
                    .ok()?
                    .path(path)
                    .ok()?
                    .cache_properties(zbus::proxy::CacheProperties::No)
                    .build()
                    .await
                    .ok()
            }
            .await;
            if let Some(t) = proxy {
                if let Some(Ok(n)) =
                    avec_delai(Duration::from_millis(800), t.character_count()).await
                {
                    if n > 0 {
                        let fin = (n as usize).min(TEXTE_COMPLET_MAX) as i32;
                        if let Some(Ok(s)) =
                            avec_delai(Duration::from_millis(2500), t.get_text(0, fin)).await
                        {
                            let s = s.replace('\u{FFFC}', " ");
                            if !s.trim().is_empty() {
                                return reponse(s, n as usize, "atspi");
                            }
                        }
                    }
                }
            }
        }
        if descendants.is_empty() {
            return (
                204,
                json!({"id": id, "texte": e.texte.clone().unwrap_or_default(), "longueur": 0, "source": "modele",
                                "note": "aucun texte exposé au-delà de ce que montre le regard"}),
            );
        }
        let t = descendants.join("\n");
        let n = t.chars().count();
        reponse(t.chars().take(TEXTE_COMPLET_MAX).collect(), n, "modele")
    }

    /// L'œil à la demande (`ecran regard --oeil`) : la fenêtre est capturée, lue par OCR puis décrite par Qwen,
    /// et le résultat remplace sa lecture dans le modèle (source `ocr+qwen`), avec empreintes pour les clics.
    pub async fn oeil(&mut self, motif: Option<&str>) -> (u16, Value) {
        let (wid, zone, titre, continu) = {
            let b = self.partage.read().await;
            let f = match motif {
                Some(m) => {
                    let m = m.to_lowercase();
                    b.fenetres.values().find(|f| {
                        f.wid != SHELL
                            && (f.titre.to_lowercase().contains(&m)
                                || f.app.to_lowercase().contains(&m))
                    })
                }
                None => b.fenetre_active(),
            };
            match f {
                Some(f) => (f.wid, f.boite_visible(), f.titre.clone(), f.dessin.continu),
                None => return (404, json!({"erreur": "aucune fenêtre ne correspond"})),
            }
        };
        if !crate::qwen::actif() {
            return (409, json!({"erreur": "œil coupé (REGARD_QWEN=0)"}));
        }
        let Some((x, y, w, h)) = zone else {
            return (409, json!({"erreur": "fenêtre sans géométrie"}));
        };
        let t0 = Instant::now();
        let (lignes, img, origine) =
            match crate::pixels::lire_zone_image(std::env::var("DISPLAY").ok(), x, y, w, h).await {
                Ok(r) => r,
                Err(e) => return (409, json!({"erreur": format!("capture/OCR : {e}")})),
            };
        let vue = match crate::qwen::regarder(&img, origine, wid, &lignes).await {
            Ok(v) => v,
            Err(e) => return (409, json!({"erreur": format!("œil (Qwen) : {e}")})),
        };
        for (k, v) in vue.empreintes {
            self.empreintes.insert(k, v);
        }
        self.dernier_oeil.insert(wid, maintenant());
        let mut els = crate::pixels::en_elements(wid, &lignes);
        let n_ocr = els.len();
        let n_oeil = vue.elements.len();
        els.extend(vue.elements);
        let lecture = Lecture {
            elements: Some(els),
            source: "ocr+qwen".into(),
            voie: "tesseract+oeil",
            tronquee: false,
            lecture_ms: t0.elapsed().as_millis() as u32,
            observe_a: maintenant(),
            lisible: true,
            scene: Some(vue.scene.clone()),
        };
        let mut b = self.partage.write().await;
        if let Some(d) = b.enregistrer(wid, lecture) {
            let _ = self.diffusion.send(d);
        }
        b.repos.insert(wid, 5.0);
        b.tracer(format!(
            "œil sur « {} » : {}",
            titre,
            vue.scene.chars().take(160).collect::<String>()
        ));
        (
            200,
            json!({"wid": format!("{wid:#x}"), "titre": titre, "scene": vue.scene, "lignes_ocr": n_ocr, "elements_oeil": n_oeil,
                     "duree_ms": t0.elapsed().as_millis() as u64, "en_continu": continu}),
        )
    }

    /// Diagnostic : les applications du bus, leur pid tel que le lecteur le voit, leurs cadres.
    pub async fn applis(&mut self) -> Value {
        let mut out = vec![];
        for a in self.applications().await {
            let nom_bus = a.name_as_str().unwrap_or("").to_string();
            let pid = self.pid_de(&nom_bus).await;
            let (mut nom, mut cadres) = (String::new(), vec![]);
            if let Some(p) = self.accessible(&a).await {
                nom = avec_delai(Duration::from_millis(400), p.name())
                    .await
                    .and_then(|r| r.ok())
                    .unwrap_or_default();
                if let Some(Ok(enfants)) =
                    avec_delai(Duration::from_millis(600), p.get_children()).await
                {
                    for c in enfants.iter().take(6) {
                        if let Some(pc) = self.accessible(c).await {
                            let n = avec_delai(Duration::from_millis(300), pc.name())
                                .await
                                .and_then(|r| r.ok())
                                .unwrap_or_default();
                            let s = avec_delai(Duration::from_millis(300), pc.get_state())
                                .await
                                .and_then(|r| r.ok())
                                .unwrap_or_else(StateSet::empty);
                            let k = avec_delai(Duration::from_millis(300), pc.child_count())
                                .await
                                .and_then(|r| r.ok())
                                .unwrap_or(-1);
                            cadres.push(json!({"nom": n, "active": s.contains(State::Active), "showing": s.contains(State::Showing), "enfants": k}));
                        }
                    }
                }
            }
            out.push(json!({"bus": nom_bus, "pid": pid, "nom": nom, "cadres": cadres}));
        }
        let b = self.partage.read().await;
        let fen: Vec<Value> = b.fenetres.values().map(|f| json!({"wid": f.wid_hex(), "pid": f.pid, "titre": f.titre, "app": f.app, "etat": f.etat})).collect();
        json!({"applications": out, "fenetres": fen})
    }

    async fn application_nommee(&self, nom: &str) -> Vec<ObjectRefOwned> {
        let mut res = vec![];
        for a in self.applications().await {
            if let Some(p) = self.accessible(&a).await {
                if avec_delai(Duration::from_millis(500), p.name())
                    .await
                    .and_then(|r| r.ok())
                    .as_deref()
                    == Some(nom)
                {
                    res.push(a);
                }
            }
        }
        res
    }

    /// Lit une fenêtre X11 : trouve son cadre AT-SPI (pid, puis titre), puis son contenu.
    pub async fn lire_fenetre(&mut self, wid: Wid) -> Lecture {
        let t0 = Instant::now();
        let observe_a = maintenant();
        let (pid, titre, origine, active, geo) = {
            let b = self.partage.read().await;
            match b.fenetres.get(&wid) {
                Some(f) => (
                    f.pid,
                    f.titre.clone(),
                    f.geo.map(|g| (g.x, g.y)).unwrap_or((0, 0)),
                    f.active,
                    f.geo,
                ),
                None => {
                    return Lecture {
                        elements: None,
                        source: "atspi".into(),
                        voie: "",
                        tronquee: false,
                        lecture_ms: 0,
                        observe_a,
                        lisible: false,
                        scene: None,
                    }
                }
            }
        };
        let vide = |t0: Instant| Lecture {
            elements: None,
            source: "atspi".into(),
            voie: "",
            tronquee: false,
            lecture_ms: t0.elapsed().as_millis() as u32,
            observe_a,
            lisible: false,
            scene: None,
        };
        // Une page suivie par DevTools se lit entière, devant ou derrière ; AT-SPI n'ajoute que le chrome du navigateur.
        let page = self
            .devtools
            .lire(
                &titre,
                geo,
                if active {
                    Duration::from_millis(3000)
                } else {
                    Duration::from_millis(4000)
                },
            )
            .await;
        if let Some(Ok(p)) = page {
            let mut natifs = vec![];
            if let Some(pid) = pid {
                let apps = self.applications_du_pid(pid).await;
                if !apps.is_empty() {
                    if let Some((cadre, _)) = self.meilleur_cadre(&apps, &titre).await {
                        let delai_arbre = if active { Duration::from_millis(1500) } else { Duration::from_millis(600) };
                        let (els, _, _) = self.lire_arbre(&cadre, origine, delai_arbre, Some(wid)).await;
                        let limite = origine.1 + 140;
                        if active {
                            // Au premier plan, l'arbre AT-SPI de Chrome est complet, cadres (iframes) compris, là où
                            // DevTools ne lit que le document principal : on garde tout ce que DevTools n'a pas déjà.
                            natifs = els
                                .into_iter()
                                .filter(|e| {
                                    e.y < limite
                                        || !p.elements.iter().any(|c| {
                                            c.nom == e.nom
                                                && (c.x + c.w / 2 - (e.x + e.w / 2)).abs() < 10
                                                && (c.y + c.h / 2 - (e.y + e.h / 2)).abs() < 10
                                        })
                                })
                                .collect();
                        } else {
                            natifs = els.into_iter().filter(|e| e.y < limite).collect();
                        }
                        self.racines.insert(wid, (cadre, origine.0, origine.1));
                    }
                }
            }
            let source = if natifs.is_empty() {
                "devtools"
            } else {
                "devtools+atspi"
            };
            let mut els = natifs;
            els.extend(p.elements);
            return Lecture {
                elements: Some(els),
                source: source.into(),
                voie: "cdp",
                tronquee: p.tronquee,
                lecture_ms: t0.elapsed().as_millis() as u32,
                observe_a,
                lisible: true,
                scene: None,
            };
        }
        if let Some(Err(e)) = page {
            self.partage.write().await.erreur(format!(
                "DevTools « {} » : {}",
                titre.chars().take(40).collect::<String>(),
                e
            ));
        }
        let Some(pid) = pid else { return vide(t0) };
        let apps = self.applications_du_pid(pid).await;
        if apps.is_empty() {
            return vide(t0);
        }
        let cadres = self.cadres(&apps, &titre).await;
        if cadres.is_empty() {
            return vide(t0);
        }
        let delai = if active { DELAI_ACTIVE } else { DELAI_FOND };
        let mut lisible = false;
        for (score, cadre) in cadres.into_iter().take(2) {
            lisible |= score.3;
            let (els, tronquee, voie) = self.lire_arbre(&cadre, origine, delai, Some(wid)).await;
            if !els.is_empty() {
                self.racines.insert(wid, (cadre, origine.0, origine.1));
                return Lecture {
                    elements: Some(els),
                    source: "atspi".into(),
                    voie,
                    tronquee,
                    lecture_ms: t0.elapsed().as_millis() as u32,
                    observe_a,
                    lisible: true,
                    scene: None,
                };
            }
        }
        // Un arbre visible peut légitimement n'avoir aucun contrôle : c'est une lecture, pas une absence.
        Lecture {
            elements: if lisible { Some(vec![]) } else { None },
            source: "atspi".into(),
            voie: "",
            tronquee: false,
            lecture_ms: t0.elapsed().as_millis() as u32,
            observe_a,
            lisible,
            scene: None,
        }
    }

    /// Les cadres AT-SPI d'une application, du plus plausible au moins : titre exact, ACTIVE, titre
    /// inclus, SHOWING, enfants.
    async fn cadres(
        &self,
        apps: &[ObjectRefOwned],
        titre: &str,
    ) -> Vec<((bool, bool, bool, bool, bool), ObjectRefOwned)> {
        let mut cadres: Vec<((bool, bool, bool, bool, bool), ObjectRefOwned)> = vec![];
        for app in apps {
            let Some(p) = self.accessible(app).await else {
                continue;
            };
            let Some(Ok(enfants)) = avec_delai(Duration::from_millis(800), p.get_children()).await
            else {
                continue;
            };
            for c in enfants {
                let Some(pc) = self.accessible(&c).await else {
                    continue;
                };
                let nom = avec_delai(Duration::from_millis(400), pc.name())
                    .await
                    .and_then(|r| r.ok())
                    .unwrap_or_default();
                let etats = avec_delai(Duration::from_millis(400), pc.get_state())
                    .await
                    .and_then(|r| r.ok())
                    .unwrap_or_else(StateSet::empty);
                let n = avec_delai(Duration::from_millis(400), pc.child_count())
                    .await
                    .and_then(|r| r.ok())
                    .unwrap_or(0);
                let score = (
                    nom == titre,
                    etats.contains(State::Active),
                    !nom.is_empty() && titre.contains(&nom),
                    etats.contains(State::Showing),
                    n > 0,
                );
                cadres.push((score, c));
            }
        }
        cadres.sort_by(|a, b| b.0.cmp(&a.0));
        cadres
    }

    async fn meilleur_cadre(
        &self,
        apps: &[ObjectRefOwned],
        titre: &str,
    ) -> Option<(ObjectRefOwned, (bool, bool, bool, bool, bool))> {
        self.cadres(apps, titre)
            .await
            .into_iter()
            .next()
            .map(|(s, c)| (c, s))
    }

    /// gnome-shell (barre du haut, dock, notifications) : une application sans fenêtre X11 cliente.
    pub async fn lire_shell(&mut self) -> Lecture {
        let t0 = Instant::now();
        let observe_a = maintenant();
        let apps = self.application_nommee("gnome-shell").await;
        if apps.is_empty() {
            return Lecture {
                elements: None,
                source: "atspi".into(),
                voie: "",
                tronquee: false,
                lecture_ms: 0,
                observe_a,
                lisible: false,
                scene: None,
            };
        }
        let mut tous = vec![];
        let mut tronquee = false;
        let mut voie = "";
        for app in &apps {
            let Some(p) = self.accessible(app).await else {
                continue;
            };
            let Some(Ok(enfants)) = avec_delai(Duration::from_millis(800), p.get_children()).await
            else {
                continue;
            };
            for c in enfants {
                let (els, t, v) = self
                    .lire_arbre(&c, (0, 0), Duration::from_millis(600), None)
                    .await;
                tronquee |= t;
                voie = v;
                tous.extend(els);
            }
        }
        Lecture {
            elements: Some(tous),
            source: "atspi".into(),
            voie,
            tronquee,
            lecture_ms: t0.elapsed().as_millis() as u32,
            observe_a,
            lisible: true,
            scene: None,
        }
    }

    /// Le contenu d'un cadre : cache de l'application si possible, sinon parcours borné.
    async fn lire_arbre(
        &self,
        cadre: &ObjectRefOwned,
        origine: (i32, i32),
        delai: Duration,
        _wid: Option<Wid>,
    ) -> (Vec<Lu>, bool, &'static str) {
        let (largeur, hauteur) = {
            let b = self.partage.read().await;
            (b.largeur, b.hauteur)
        };
        let debut = Instant::now();
        let (noeuds, voie) = match self.par_cache(cadre, delai).await {
            Some((n, v)) if !n.is_empty() => (n, v),
            _ => (self.par_parcours(cadre, delai).await, "parcours"),
        };
        let tronquee_parcours = noeuds.len() >= MAX_ELEMENTS;
        let nds = &noeuds;
        // Géométries en parallèle (coordonnées fenêtre + origine X11 : vrai pour GTK3/4, Qt, Firefox, Chromium).
        let geos: Vec<Option<(i32, i32, i32, i32)>> = stream::iter(0..nds.len())
            .map(|i| async move {
                let n = &nds[i];
                let c = self.composant(&n.objet).await?;
                avec_delai(Duration::from_millis(700), c.get_extents(CoordType::Window))
                    .await
                    .and_then(|r| r.ok())
            })
            .buffered(24)
            .collect()
            .await;
        // Textes et valeurs : seulement pour les nœuds qui en ont besoin, en parallèle.
        let textes: Vec<Option<String>> = stream::iter(0..nds.len())
            .map(|i| async move {
                let n = &nds[i];
                if !n.ifaces.contains(Interface::Text)
                    || n.role_nom == "password text"
                    || !(est_texte(&n.role_nom) || n.nom.is_empty())
                {
                    return None;
                }
                let t = self.texte(&n.objet).await?;
                let nb = avec_delai(Duration::from_millis(400), t.character_count())
                    .await
                    .and_then(|r| r.ok())?;
                if nb <= 0 {
                    return None;
                }
                // Un terminal se lit par la fin (l'invite, les dernières lignes) ; le reste par le début.
                let (debut, fin) = if n.role_nom == "terminal" {
                    ((nb - TEXTE_MAX).max(0), nb)
                } else {
                    (0, nb.min(TEXTE_MAX))
                };
                let s = avec_delai(Duration::from_millis(500), t.get_text(debut, fin))
                    .await
                    .and_then(|r| r.ok())?;
                let s = nettoyer(&s, TEXTE_MAX as usize);
                if s.is_empty() {
                    None
                } else {
                    Some(s)
                }
            })
            .buffered(24)
            .collect()
            .await;
        let valeurs: Vec<Option<String>> = stream::iter(0..nds.len())
            .map(|i| async move {
                let n = &nds[i];
                if !n.ifaces.contains(Interface::Value) {
                    return None;
                }
                let v = self.valeur(&n.objet).await?;
                let x = avec_delai(Duration::from_millis(400), v.current_value())
                    .await
                    .and_then(|r| r.ok())?;
                Some(if x.fract() == 0.0 {
                    format!("{}", x as i64)
                } else {
                    format!("{x:.2}")
                })
            })
            .buffered(16)
            .collect()
            .await;
        let mut out = Vec::with_capacity(noeuds.len());
        let mut vus: HashSet<(i32, i32, i32, i32, String)> = HashSet::new();
        for (i, n) in noeuds.iter().enumerate() {
            let Some((x, y, w, h)) = geos[i] else {
                continue;
            };
            let (x, y) = (x + origine.0, y + origine.1);
            if w <= 0
                || h <= 0
                || x + w <= 0
                || y + h <= 0
                || (largeur > 0 && x >= largeur)
                || (hauteur > 0 && y >= hauteur)
            {
                continue;
            }
            let plein_ecran =
                largeur > 0 && (w as i64 * h as i64) >= (6 * largeur as i64 * hauteur as i64) / 10;
            if plein_ecran
                && !est_texte(&n.role_nom)
                && n.libelle != "document"
                && n.libelle != "dialogue"
            {
                continue;
            }
            let texte = textes[i].clone();
            let valeur = valeurs[i].clone();
            let nom = n.nom.clone();
            // Un texte ou une image sans nom ni contenu n'apprend rien.
            if nom.is_empty()
                && texte.is_none()
                && valeur.is_none()
                && matches!(
                    n.libelle,
                    "texte" | "image" | "icône" | "cellule" | "élément" | "document"
                )
            {
                continue;
            }
            let k = (x, y, w, h, nom.clone());
            if n.libelle == "texte" && !vus.insert(k) {
                continue;
            }
            out.push(Lu {
                cle_source: cle(&n.objet),
                role: n.libelle.to_string(),
                role_atspi: n.role_nom.clone(),
                nom,
                texte,
                valeur,
                etats: etats_de(n.etats),
                x,
                y,
                w,
                h,
                parent_cle: n.parent.as_ref().map(cle),
                profondeur: n.profondeur,
                enfants: n.enfants,
                source: None,
            });
        }
        (out, tronquee_parcours || debut.elapsed() > delai, voie)
    }

    /// La voie rapide : tout le cache de l'application en un appel, puis sélection du sous-arbre du cadre.
    /// Deux formats existent : le courant (index + nombre d'enfants) et l'ancien (liste des enfants).
    async fn par_cache(
        &self,
        cadre: &ObjectRefOwned,
        delai: Duration,
    ) -> Option<(Vec<Noeud>, &'static str)> {
        let nom_bus = cadre.name_as_str()?.to_string();
        if let Some(t) = self.cache_refus.lock().ok()?.get(&nom_bus) {
            if maintenant() - t < 60.0 {
                return None;
            }
        }
        let refuser = |raison: String| {
            if let Ok(mut m) = self.cache_refus.lock() {
                m.insert(nom_bus.clone(), maintenant());
            }
            raison
        };
        let name: BusName = cadre.name()?.clone().into();
        let cache = CacheProxy::builder(self.bus())
            .destination(name)
            .ok()?
            .cache_properties(zbus::proxy::CacheProperties::No)
            .build()
            .await
            .ok()?;
        let d = delai.max(Duration::from_millis(2000));
        let (items, voie): (Vec<Article>, &'static str) = match avec_delai(d, cache.get_items())
            .await
        {
            None => {
                let r = refuser("cache : délai dépassé".into());
                self.partage
                    .write()
                    .await
                    .erreur(format!("{nom_bus} : {r}"));
                return None;
            }
            Some(Ok(v)) if !v.is_empty() => (v.into_iter().map(Article::from).collect(), "cache"),
            Some(autre) => {
                // Format courant refusé (Qt, par exemple, parle l'ancien) : on essaie l'ancien avant de se plaindre.
                let anciens = match avec_delai(d, cache.get_legacy_items()).await {
                    Some(Ok(a)) => a,
                    _ => {
                        let detail = match &autre {
                            Err(e) => e.to_string().chars().take(80).collect::<String>(),
                            Ok(_) => "vide".into(),
                        };
                        let r = refuser(format!("cache : {detail} ; ancien format : indisponible"));
                        self.partage
                            .write()
                            .await
                            .erreur(format!("{nom_bus} : {r}"));
                        return None;
                    }
                };
                if anciens.is_empty() {
                    refuser("cache : vide".into());
                    return None;
                }
                // L'index d'un ancien article = sa place dans la liste d'enfants de son parent.
                let mut rang: HashMap<String, i32> = HashMap::new();
                for a in &anciens {
                    for (i, c) in a.children.iter().enumerate() {
                        rang.insert(cle(c), i as i32);
                    }
                }
                (
                    anciens
                        .into_iter()
                        .map(|a| {
                            let index = rang.get(&cle(&a.object)).copied().unwrap_or(0);
                            Article {
                                object: a.object,
                                parent: a.parent,
                                index,
                                children: a.children.len() as i32,
                                ifaces: a.ifaces,
                                short_name: a.short_name,
                                role: a.role,
                                states: a.states,
                            }
                        })
                        .collect(),
                    "cache-ancien",
                )
            }
        };
        let mut par_parent: HashMap<String, Vec<&Article>> = HashMap::new();
        let mut par_cle: HashMap<String, &Article> = HashMap::new();
        for it in &items {
            par_cle.insert(cle(&it.object), it);
            par_parent.entry(cle(&it.parent)).or_default().push(it);
        }
        for v in par_parent.values_mut() {
            v.sort_by_key(|i| i.index);
        }
        let racine_cle = cle(cadre);
        if !par_cle.contains_key(&racine_cle) && !par_parent.contains_key(&racine_cle) {
            return None;
        }
        let mut out = vec![];
        // Parcours en profondeur (ordre de lecture), conteneurs muets traversés.
        let mut pile: Vec<(String, u8, Option<ObjectRefOwned>)> = vec![(racine_cle, 0, None)];
        let mut vus: HashSet<String> = HashSet::new();
        while let Some((k, prof, parent_liste)) = pile.pop() {
            if out.len() >= MAX_ELEMENTS {
                break;
            }
            let mut parent_suivant = parent_liste.clone();
            if let Some(it) = par_cle.get(&k) {
                if prof > 0 {
                    let visible =
                        it.states.contains(State::Showing) && it.states.contains(State::Visible);
                    let role_nom = it.role.name().to_string();
                    let affichable = role_nom == "menu" || role_nom == "menu bar" || visible;
                    if !affichable && !matches!(role_nom.as_str(), "menu" | "menu bar") {
                        continue;
                    }
                    if visible {
                        if let Some(l) = libelle(&role_nom) {
                            if vus.insert(k.clone()) {
                                out.push(Noeud {
                                    objet: it.object.clone(),
                                    parent: parent_liste.clone(),
                                    nom: nettoyer(&it.short_name, 120),
                                    role_nom: role_nom.clone(),
                                    libelle: l,
                                    etats: it.states,
                                    ifaces: it.ifaces,
                                    profondeur: prof.min(40),
                                    enfants: it.children.max(0) as u32,
                                });
                                parent_suivant = Some(it.object.clone());
                            }
                        }
                    }
                }
            }
            if prof < 40 {
                if let Some(enfants) = par_parent.get(&k) {
                    for it in enfants.iter().rev() {
                        pile.push((cle(&it.object), prof + 1, parent_suivant.clone()));
                    }
                }
            }
        }
        Some((out, voie))
    }

    /// Sans cache : parcours en largeur, concurrent par niveau, borné.
    async fn par_parcours(&self, cadre: &ObjectRefOwned, delai: Duration) -> Vec<Noeud> {
        let debut = Instant::now();
        let mut out = vec![];
        let mut niveau: Vec<(ObjectRefOwned, u8, Option<ObjectRefOwned>)> =
            vec![(cadre.clone(), 0, None)];
        while !niveau.is_empty() && out.len() < MAX_ELEMENTS && debut.elapsed() < delai {
            let entrees: Vec<(ObjectRefOwned, u8, Option<ObjectRefOwned>)> =
                niveau.drain(..).collect();
            let ent = &entrees;
            let lus: Vec<
                Option<(
                    usize,
                    String,
                    StateSet,
                    Role,
                    i32,
                    Vec<ObjectRefOwned>,
                    atspi::InterfaceSet,
                )>,
            > = stream::iter(0..ent.len())
                .map(|i| async move {
                    let (o, prof, _) = &ent[i];
                    let p = self.accessible(o).await?;
                    let d = Duration::from_millis(500);
                    let nom = avec_delai(d, p.name())
                        .await
                        .and_then(|r| r.ok())
                        .unwrap_or_default();
                    let etats = avec_delai(d, p.get_state())
                        .await
                        .and_then(|r| r.ok())
                        .unwrap_or_else(StateSet::empty);
                    let role = avec_delai(d, p.get_role())
                        .await
                        .and_then(|r| r.ok())
                        .unwrap_or(Role::Invalid);
                    let ifaces = avec_delai(d, p.get_interfaces())
                        .await
                        .and_then(|r| r.ok())
                        .unwrap_or_else(|| atspi::InterfaceSet::empty());
                    let visible = etats.contains(State::Showing) && etats.contains(State::Visible);
                    let descend =
                        *prof == 0 || visible || matches!(role, Role::Menu | Role::MenuBar);
                    let enfants = if descend && *prof < 30 {
                        avec_delai(d, p.get_children())
                            .await
                            .and_then(|r| r.ok())
                            .unwrap_or_default()
                    } else {
                        vec![]
                    };
                    let n = enfants.len() as i32;
                    Some((i, nom, etats, role, n, enfants, ifaces))
                })
                .buffered(16)
                .collect()
                .await;
            for l in lus.into_iter().flatten() {
                let (i, nom, etats, role, n, enfants, ifaces) = l;
                let (o, prof, parent) = entrees[i].clone();
                let role_nom = role.name().to_string();
                let visible = etats.contains(State::Showing) && etats.contains(State::Visible);
                let mut parent_suivant = parent.clone();
                if prof > 0 && visible {
                    if let Some(l) = libelle(&role_nom) {
                        out.push(Noeud {
                            objet: o.clone(),
                            parent: parent.clone(),
                            nom: nettoyer(&nom, 120),
                            role_nom: role_nom.clone(),
                            libelle: l,
                            etats,
                            ifaces,
                            profondeur: prof,
                            enfants: n.max(0) as u32,
                        });
                        parent_suivant = Some(o.clone());
                    }
                }
                for c in enfants.into_iter().take(500) {
                    niveau.push((c, prof + 1, parent_suivant.clone()));
                }
            }
        }
        out
    }

    /// Hit-test AT-SPI au centre d'un élément : l'objet effectivement sous ce point est-il la cible (ou un
    /// de ses descendants), montré, visible et actif ? Aucun clic ici.
    async fn confirmer(&self, wid: Wid, e: &Element) -> Option<bool> {
        let (racine, dx, dy) = self.racines.get(&wid)?;
        let (cx, cy) = (e.x + e.w / 2, e.y + e.h / 2);
        // Les coordonnées fenêtre d'abord (règle AT-SPI), puis écran : certains toolkits (Chrome) répondent
        // au hit-test dans l'autre repère que celui de leurs extents.
        let mut dernier = Some(false);
        for (x, y, repere) in [(cx - dx, cy - dy, CoordType::Window), (cx, cy, CoordType::Screen)] {
            match self.confirmer_point(racine, e, x, y, repere).await {
                Some(true) => return Some(true),
                r => dernier = r,
            }
        }
        dernier
    }

    async fn confirmer_point(&self, racine: &ObjectRefOwned, e: &Element, x: i32, y: i32, repere: CoordType) -> Option<bool> {
        let limite = Instant::now() + Duration::from_millis(900);
        let mut courant = racine.clone();
        let mut vus: HashSet<String> = HashSet::new();
        for _ in 0..30 {
            if Instant::now() > limite {
                return None;
            }
            let Some(c) = self.composant(&courant).await else {
                break;
            };
            let Some(Ok(suivant)) = avec_delai(Duration::from_millis(400), c.get_accessible_at_point(x, y, repere)).await else {
                break;
            };
            if suivant.is_null() {
                break;
            }
            let k = cle(&suivant);
            if !vus.insert(k) {
                break;
            }
            courant = suivant;
        }
        for _ in 0..30 {
            if Instant::now() > limite {
                return None;
            }
            if cle(&courant) == e.cle_source {
                let p = self.accessible(&courant).await?;
                let s = avec_delai(Duration::from_millis(400), p.get_state()).await?.ok()?;
                return Some(s.contains(State::Showing) && s.contains(State::Visible) && s.contains(State::Enabled));
            }
            let p = self.accessible(&courant).await?;
            let Some(Ok(parent)) = avec_delai(Duration::from_millis(400), p.parent()).await else {
                return None;
            };
            if parent.is_null() {
                return Some(false);
            }
            courant = parent;
        }
        Some(false)
    }

    /// Vérifie une cible pour le pilote : relecture fraîche, fenêtre active, géométrie, recouvrement, hit-test.
    pub async fn verifier_cible(&mut self, id: &str) -> (u16, Value) {
        let (wid, attendu) = {
            let b = self.partage.read().await;
            match b.trouver(id) {
                Some((f, e)) => (f.wid, e.clone()),
                None => {
                    return (
                        409,
                        json!({"erreur": "cible absente ou sans fenêtre contrôlable"}),
                    )
                }
            }
        };
        if wid == SHELL {
            return (
                409,
                json!({"erreur": "cible absente ou sans fenêtre contrôlable"}),
            );
        }
        {
            let b = self.partage.read().await;
            if b.active != wid {
                return (409, json!({"erreur": "cible périmée ou fenêtre inactive"}));
            }
        }
        let par_pixels = attendu.source == "ocr"
            || attendu.source == "atspi+ocr"
            || attendu.source == "qwen"
            || attendu.source == "qwen+ocr";
        let lecture = if par_pixels {
            // Pas de relecture complète par les pixels (lente) : la vérification ciblée suit.
            Lecture {
                elements: Some(vec![]),
                source: "ocr".into(),
                voie: "",
                tronquee: false,
                lecture_ms: 0,
                observe_a: maintenant(),
                lisible: true,
                scene: None,
            }
        } else {
            self.lire_fenetre(wid).await
        };
        if lecture.elements.is_none() {
            return (409, json!({"erreur": "relecture de la cible indisponible"}));
        }
        let lecture = if par_pixels {
            // On garde les lignes connues telles quelles ; seule la cible est revérifiée sur les pixels.
            let b = self.partage.read().await;
            let f = b.fenetres.get(&wid);
            let lus: Vec<Lu> = f
                .map(|f| f.elements.iter().map(Lu::from).collect())
                .unwrap_or_default();
            Lecture {
                elements: Some(lus),
                source: attendu.source.clone(),
                voie: "tesseract",
                tronquee: false,
                lecture_ms: 0,
                observe_a: maintenant(),
                lisible: true,
                scene: None,
            }
        } else {
            lecture
        };
        let (generation, element, titre) = {
            let mut b = self.partage.write().await;
            if let Some(d) = b.enregistrer(wid, lecture) {
                let _ = self.diffusion.send(d);
            }
            let g = b.generations.get(&wid).copied().unwrap_or(0);
            let Some((f, e)) = b.trouver(id) else {
                return (
                    409,
                    json!({"erreur": "cible non confirmée par une lecture récente"}),
                );
            };
            if f.wid != wid
                || b.active != wid
                || f.visible == Some(false)
                || f.etat != "observe" && f.etat != "a_relire"
            {
                return (
                    409,
                    json!({"erreur": "cible non confirmée par une lecture récente"}),
                );
            }
            if e.cle_source != attendu.cle_source
                || e.role != attendu.role
                || e.nom != attendu.nom
                || e.texte != attendu.texte
                || e.etats != attendu.etats
            {
                return (
                    409,
                    json!({"erreur": "contenu de la cible changé depuis la carte ; relis-la avant d'agir"}),
                );
            }
            let pixels = e.source == "ocr"
                || e.source == "qwen"
                || e.source == "qwen+ocr"
                || e.source == "atspi+ocr";
            if e.etats.iter().any(|s| s == "inactif") || (e.identite != "native" && !pixels) {
                return (
                    409,
                    json!({"erreur": "identité estimée ou élément désactivé"}),
                );
            }
            if e.w <= 0 || e.h <= 0 {
                return (409, json!({"erreur": "coordonnées indisponibles"}));
            }
            if e.hors_vue {
                return (
                    409,
                    json!({"erreur": "centre de la cible hors fenêtre ou écran (défiler avant de cliquer)"}),
                );
            }
            let (cx, cy) = (e.x + e.w / 2, e.y + e.h / 2);
            if let Some(autre) = b.recouvert(wid, cx, cy) {
                return (
                    409,
                    json!({"erreur": format!("centre de la cible potentiellement recouvert par « {} »", autre.titre)}),
                );
            }
            (g, e.clone(), f.titre.clone())
        };
        let confirme = if element.source.starts_with("devtools") {
            let geo = self
                .partage
                .read()
                .await
                .fenetres
                .get(&wid)
                .and_then(|f| f.geo);
            self.devtools.verifier_cible(&titre, geo, &element).await
        } else if element.source == "ocr"
            || element.source == "atspi+ocr"
            || element.source == "qwen+ocr"
        {
            // Secours à l'ancienne : la zone est recapturée et relue, le texte doit y être encore.
            crate::pixels::verifier(std::env::var("DISPLAY").ok(), &element).await
        } else if element.source == "qwen" {
            // Vu par l'œil : la boîte recapturée doit ressembler à son empreinte au moment du regard.
            match self.empreintes.get(&element.cle_source) {
                Some(emp) => {
                    crate::pixels::verifier_empreinte(std::env::var("DISPLAY").ok(), &element, emp)
                        .await
                }
                None => Some(false),
            }
        } else {
            self.confirmer(wid, &element).await
        };
        if confirme != Some(true) {
            return (
                409,
                json!({"erreur": "visibilité de la cible non confirmée au point de clic"}),
            );
        }
        let b = self.partage.read().await;
        let Some((f, actuel)) = b.trouver(id) else {
            return (
                409,
                json!({"erreur": "cible modifiée pendant la vérification"}),
            );
        };
        if *actuel != element
            || b.generations.get(&wid).copied().unwrap_or(0) != generation
            || b.active != wid
            || f.titre != titre
        {
            return (
                409,
                json!({"erreur": "cible modifiée pendant la vérification"}),
            );
        }
        let mut meta = f.to_json(maintenant());
        meta.as_object_mut().map(|o| o.remove("elements"));
        (
            200,
            json!({"element": actuel, "fenetre": meta, "session": b.session, "revision": b.revision}),
        )
    }
}

/// Un article de cache, dans un seul format quel que soit celui de l'application.
struct Article {
    object: ObjectRefOwned,
    parent: ObjectRefOwned,
    index: i32,
    children: i32,
    ifaces: atspi::InterfaceSet,
    short_name: String,
    role: Role,
    states: StateSet,
}

impl From<CacheItem> for Article {
    fn from(c: CacheItem) -> Self {
        Article {
            object: c.object,
            parent: c.parent,
            index: c.index,
            children: c.children,
            ifaces: c.ifaces,
            short_name: if c.name.is_empty() {
                c.short_name
            } else {
                c.name
            },
            role: c.role,
            states: c.states,
        }
    }
}

struct Noeud {
    objet: ObjectRefOwned,
    parent: Option<ObjectRefOwned>,
    nom: String,
    role_nom: String,
    libelle: &'static str,
    etats: StateSet,
    ifaces: atspi::InterfaceSet,
    profondeur: u8,
    enfants: u32,
}

/// La boucle du lecteur : événements du bus (quoi relire) + lectures (une à la fois) + cibles.
async fn repondre(lecteur: &mut Lecteur, d: Demande) {
    match d {
        Demande::Cible { id, repondre } => {
            let r = lecteur.verifier_cible(&id).await;
            let _ = repondre.send(r);
        }
        Demande::Texte { id, repondre } => {
            let r = lecteur.texte_complet(&id).await;
            let _ = repondre.send(r);
        }
        Demande::Oeil { motif, repondre } => {
            let r = lecteur.oeil(motif.as_deref()).await;
            let _ = repondre.send(r);
        }
        Demande::Applis { repondre } => {
            let r = lecteur.applis().await;
            let _ = repondre.send(r);
        }
    }
}

pub async fn boucle(
    partage: Partage,
    diffusion: broadcast::Sender<Value>,
    reveil: Arc<Notify>,
    mut cibles: mpsc::Receiver<Demande>,
    devtools: Arc<Devtools>,
) {
    // Le bus d'accessibilité peut disparaître ou se relancer (session, mise à jour) : on se reconnecte
    // sans perdre le modèle ni les identités. Une chute isolée du flux (une appli lancée ou fermée, le bus
    // qui redémarre) est NORMALE : elle se note dans le journal, sans réveiller Félix ; seule une série de
    // chutes rapprochées (3 en 5 min) ou un bus durablement indisponible devient une alerte grave (ntfy).
    let mut chutes: Vec<f64> = Vec::new();
    let mut indispo_depuis: Option<f64> = None;
    loop {
        let mut lecteur =
            match Lecteur::nouveau(partage.clone(), diffusion.clone(), devtools.clone()).await {
                Ok(l) => l,
                Err(e) => {
                    let depuis = *indispo_depuis.get_or_insert_with(maintenant);
                    let grave = maintenant() - depuis >= 60.0;
                    crate::alerte::alerter(
                        &mut *partage.write().await,
                        "AT-SPI",
                        grave,
                        format!("indisponible : {e} ; nouvel essai dans 10 s"),
                    );
                    tokio::time::sleep(Duration::from_secs(10)).await;
                    continue;
                }
            };
        if indispo_depuis.take().is_some() || !chutes.is_empty() {
            partage.write().await.tracer("AT-SPI : reconnecté au bus d'accessibilité".to_string());
        }
        // Les événements : une tâche à part, qui ne fait que marquer des fenêtres à relire.
        let evenements = {
            let conn = lecteur.conn.clone();
            let partage = partage.clone();
            let reveil = reveil.clone();
            tokio::spawn(async move { ecouter(conn, partage, reveil).await })
        };
        let mut shell_lu = 0.0f64;
        let ocr_disponible = crate::pixels::disponible();
        loop {
            if evenements.is_finished() {
                break;
            }
            partage.write().await.dernier_lecteur = maintenant();
            // Une cible à vérifier passe avant toute lecture : le pilote attend, la main levée.
            if let Ok(d) = cibles.try_recv() {
                repondre(&mut lecteur, d).await;
                continue;
            }
            let suivante = {
                let b = partage.read().await;
                b.prochaine()
            };
            if let Some(wid) = suivante {
                let generation = {
                    let mut b = partage.write().await;
                    b.derniers_essais.insert(wid, maintenant());
                    b.generations.get(&wid).copied().unwrap_or(0)
                };
                let mut lecture = if wid == SHELL {
                    lecteur.lire_shell().await
                } else {
                    lecteur.lire_fenetre(wid).await
                };
                // Secours à l'ancienne, pour la fenêtre ACTIVE seulement et jamais si elle se redessine en continu
                // (vidéo, jeu) : (a) muette → ses pixels sont lus en lignes de texte ; (b) des éléments connus par
                // leur nom mais sans position fiable (icônes du bureau…) → leur position vient de la ligne lue.
                // Pas de relecture des pixels si rien n'y a bougé depuis la dernière (Damage).
                let mut ocr_faite = false;
                if wid != SHELL && ocr_disponible {
                    let (muette, active, zone, bouge, anciens, anciens_oeil) = {
                        let b = partage.read().await;
                        match b.fenetres.get(&wid) {
                            Some(f) => {
                                // Muette = ni arbre ni DevTools ; une annonce décrit, elle ne positionne pas.
                                let muette = lecture
                                    .elements
                                    .as_ref()
                                    .map(|e| e.is_empty())
                                    .unwrap_or(true);
                                let par_pixels = f.elements.iter().any(|e| {
                                    e.source == "ocr"
                                        || e.source == "atspi+ocr"
                                        || e.source.starts_with("qwen")
                                });
                                let bouge = !par_pixels
                                    || f.dessin.dernier > f.observe_a.unwrap_or(0.0)
                                    || f.elements.is_empty();
                                let anciens: HashMap<String, (i32, i32, i32, i32)> = f
                                    .elements
                                    .iter()
                                    .filter(|e| e.source == "atspi+ocr")
                                    .map(|e| (e.cle_source.clone(), (e.x, e.y, e.w, e.h)))
                                    .collect();
                                let anciens_oeil: Vec<Element> = f
                                    .elements
                                    .iter()
                                    .filter(|e| e.source.starts_with("qwen"))
                                    .cloned()
                                    .collect();
                                (
                                    muette,
                                    f.active && !f.dessin.continu,
                                    f.boite_visible(),
                                    bouge,
                                    anciens,
                                    anciens_oeil,
                                )
                            }
                            None => (false, false, None, false, HashMap::new(), vec![]),
                        }
                    };
                    let mal: Vec<usize> = match (&lecture.elements, zone) {
                        (Some(els), Some(z)) if !muette => els
                            .iter()
                            .enumerate()
                            .filter(|(_, l)| crate::pixels::mal_positionne(l, z))
                            .map(|(i, _)| i)
                            .collect(),
                        _ => vec![],
                    };
                    let mut garder = false;
                    if active && zone.is_some() && (muette || !mal.is_empty()) {
                        let (x, y, w, h) = zone.unwrap();
                        if bouge {
                            let t0 = Instant::now();
                            match crate::pixels::lire_zone_image(
                                std::env::var("DISPLAY").ok(),
                                x,
                                y,
                                w,
                                h,
                            )
                            .await
                            {
                                Ok((lignes, img, origine)) => {
                                    if muette {
                                        let mut els = crate::pixels::en_elements(wid, &lignes);
                                        let mut voie = "tesseract";
                                        let mut source = "ocr";
                                        let mut scene_oeil: Option<String> = None;
                                        // Ce que l'œil avait vu reste vrai tant que les pixels sous chaque boîte n'ont pas changé.
                                        let mut gardes = 0;
                                        for e in &anciens_oeil {
                                            if let Some(emp) = lecteur.empreintes.get(&e.cle_source)
                                            {
                                                let v = crate::pixels::vignette(
                                                    &img,
                                                    e.x - origine.0,
                                                    e.y - origine.1,
                                                    e.w,
                                                    e.h,
                                                );
                                                if crate::pixels::ecart(&v, emp) <= 0.08 {
                                                    els.push(Lu::from(e));
                                                    gardes += 1;
                                                }
                                            }
                                        }
                                        if gardes > 0 {
                                            source = "ocr+qwen";
                                            voie = "tesseract+oeil";
                                        }
                                        // Presque rien à lire (icônes, boutons graphiques) : l'œil regarde, au plus toutes les 20 s.
                                        let dernier =
                                            lecteur.dernier_oeil.get(&wid).copied().unwrap_or(0.0);
                                        if lignes.len() < 4
                                            && crate::qwen::actif()
                                            && maintenant() - dernier > 20.0
                                        {
                                            lecteur.dernier_oeil.insert(wid, maintenant());
                                            match crate::qwen::regarder(&img, origine, wid, &lignes)
                                                .await
                                            {
                                                Ok(vue) => {
                                                    for (k, v) in vue.empreintes {
                                                        lecteur.empreintes.insert(k, v);
                                                    }
                                                    if lecteur.empreintes.len() > 5000 {
                                                        lecteur.empreintes.clear();
                                                    }
                                                    els.extend(vue.elements);
                                                    scene_oeil = Some(vue.scene.clone());
                                                    voie = "tesseract+oeil";
                                                    source = if lignes.is_empty() {
                                                        "qwen"
                                                    } else {
                                                        "ocr+qwen"
                                                    };
                                                    let mut b = partage.write().await;
                                                    if !vue.scene.is_empty() {
                                                        b.tracer(format!(
                                                            "œil sur « {} » : {}",
                                                            wid,
                                                            vue.scene
                                                                .chars()
                                                                .take(160)
                                                                .collect::<String>()
                                                        ));
                                                    }
                                                }
                                                Err(e) => {
                                                    let mut b = partage.write().await;
                                                    if !lecteur.oeil_signale {
                                                        lecteur.oeil_signale = true;
                                                        crate::alerte::alerter(
                                                            &mut b,
                                                            "œil",
                                                            false,
                                                            format!(
                                                                "Qwen (vLLM) indisponible : {e}"
                                                            ),
                                                        );
                                                    }
                                                }
                                            }
                                        }
                                        lecture = Lecture {
                                            elements: Some(els),
                                            source: source.into(),
                                            voie,
                                            tronquee: false,
                                            lecture_ms: t0.elapsed().as_millis() as u32,
                                            observe_a: maintenant(),
                                            lisible: true,
                                            scene: scene_oeil,
                                        };
                                    } else if let Some(els) = lecture.elements.as_mut() {
                                        let n = crate::pixels::positionner(els, &lignes, &mal);
                                        if n > 0 {
                                            lecture.voie = "cache+ocr";
                                        }
                                        lecture.lecture_ms += t0.elapsed().as_millis() as u32;
                                    }
                                    ocr_faite = true;
                                }
                                Err(e) => {
                                    let mut b = partage.write().await;
                                    crate::alerte::alerter(&mut b, "OCR", false, e);
                                }
                            }
                        } else if muette {
                            // Rien n'a bougé : les lignes déjà lues restent vraies, et fraîches.
                            garder = true;
                        } else if let Some(els) = lecture.elements.as_mut() {
                            // Rien n'a bougé : les positions retrouvées la dernière fois restent valables.
                            for l in els.iter_mut() {
                                if let Some((x, y, w, h)) = anciens.get(&l.cle_source) {
                                    l.x = *x;
                                    l.y = *y;
                                    l.w = *w;
                                    l.h = *h;
                                    l.source = Some("atspi+ocr".into());
                                }
                            }
                        }
                    }
                    if garder {
                        let mut b = partage.write().await;
                        b.dernier_lecteur = maintenant();
                        if b.generations.get(&wid).copied().unwrap_or(0) == generation {
                            b.a_relire.remove(&wid);
                        }
                        if let Some(f) = b.fenetres.get_mut(&wid) {
                            if f.etat == "a_relire" {
                                f.etat = "observe";
                            }
                            f.observe_a = Some(maintenant());
                        }
                        b.repos.insert(wid, 5.0);
                        continue;
                    }
                }
                let mut b = partage.write().await;
                b.dernier_lecteur = maintenant();
                if ocr_faite {
                    // Une lecture par les pixels coûte une à trois secondes : pas plus d'une toutes les 5 s.
                    b.repos.insert(wid, 5.0);
                }
                if b.fenetres.contains_key(&wid) {
                    if b.generations.get(&wid).copied().unwrap_or(0) == generation {
                        b.a_relire.remove(&wid);
                    } else if let Some(f) = b.fenetres.get_mut(&wid) {
                        f.etat = "a_relire";
                    }
                    match b.enregistrer(wid, lecture) {
                        Some(d) => {
                            b.repos.insert(wid, if ocr_faite { 5.0 } else { 0.5 });
                            // Un CAPTCHA dans cette fenêtre : Félix est prévenu une fois par fenêtre et par demi-heure.
                            let captcha = b
                                .fenetres
                                .get(&wid)
                                .map(|f| f.deduction()["captcha"].as_bool().unwrap_or(false))
                                .unwrap_or(false);
                            if captcha {
                                let titre = b
                                    .fenetres
                                    .get(&wid)
                                    .map(|f| f.titre.clone())
                                    .unwrap_or_default();
                                let sujet = format!(
                                    "CAPTCHA « {} »",
                                    titre.chars().take(40).collect::<String>()
                                );
                                crate::alerte::alerter(&mut b, &sujet, true, "un CAPTCHA est affiché : à toi de le faire, je ne le résous pas".into());
                            }
                            let _ = diffusion.send(d);
                        }
                        None => {
                            let r = b.repos.get(&wid).copied().unwrap_or(0.5);
                            b.repos
                                .insert(wid, (r * 2.0).min(if ocr_faite { 8.0 } else { 4.0 }));
                        }
                    }
                } else {
                    b.a_relire.remove(&wid);
                }
                continue;
            }
            if maintenant() - shell_lu > 15.0 {
                shell_lu = maintenant();
                let mut b = partage.write().await;
                if b.fenetres.contains_key(&SHELL) {
                    b.programmer(SHELL);
                    continue;
                }
            }
            tokio::select! {
                _ = reveil.notified() => {}
                _ = tokio::time::sleep(Duration::from_millis(200)) => {}
                Some(d) = cibles.recv() => {
                    repondre(&mut lecteur, d).await;
                }
            }
        }
        let now = maintenant();
        chutes.retain(|t| now - t < 300.0);
        chutes.push(now);
        let grave = chutes.len() >= 3;
        crate::alerte::alerter(
            &mut *partage.write().await,
            "AT-SPI",
            grave,
            format!(
                "flux d'événements terminé (bus d'accessibilité relancé ?) : reconnexion dans 3 s ({} chute(s) en 5 min)",
                chutes.len()
            ),
        );
        tokio::time::sleep(Duration::from_secs(3)).await;
    }
}

/// Événements AT-SPI → fenêtres à relire (par pid de l'émetteur), focus courant.
async fn ecouter(conn: AccessibilityConnection, partage: Partage, reveil: Arc<Notify>) {
    for r in [
        conn.register_event::<ObjectEvents>().await,
        conn.register_event::<WindowEvents>().await,
        conn.register_event::<DocumentEvents>().await,
    ] {
        if let Err(e) = r {
            partage
                .write()
                .await
                .erreur(format!("abonnement AT-SPI : {e}"));
        }
    }
    let dbus = zbus::fdo::DBusProxy::new(conn.connection()).await.ok();
    let mut pids: HashMap<String, Option<u32>> = HashMap::new();
    let mut flux = conn.event_stream();
    let mut dernier_marquage: HashMap<Wid, f64> = HashMap::new();
    while let Some(ev) = flux.next().await {
        let ev = match ev {
            Ok(e) => e,
            Err(_) => continue,
        };
        let (interessant, focus): (bool, Option<ObjectRefOwned>) = match &ev {
            Event::Object(o) => match o {
                ObjectEvents::StateChanged(s) => {
                    let f = if s.state == State::Focused && s.enabled {
                        Some(s.item.clone())
                    } else {
                        None
                    };
                    (
                        matches!(
                            s.state,
                            State::Focused
                                | State::Showing
                                | State::Visible
                                | State::Checked
                                | State::Selected
                                | State::Expanded
                                | State::Enabled
                                | State::Sensitive
                                | State::Active
                                | State::Busy
                        ),
                        f,
                    )
                }
                ObjectEvents::ChildrenChanged(_)
                | ObjectEvents::TextChanged(_)
                | ObjectEvents::PropertyChange(_)
                | ObjectEvents::VisibleDataChanged(_)
                | ObjectEvents::ActiveDescendantChanged(_)
                | ObjectEvents::SelectionChanged(_)
                | ObjectEvents::RowInserted(_)
                | ObjectEvents::RowDeleted(_) => (true, None),
                _ => (false, None),
            },
            Event::Window(w) => (
                matches!(
                    w,
                    WindowEvents::Activate(_)
                        | WindowEvents::Deactivate(_)
                        | WindowEvents::Create(_)
                        | WindowEvents::Destroy(_)
                        | WindowEvents::PropertyChange(_)
                ),
                None,
            ),
            Event::Document(d) => (
                matches!(
                    d,
                    DocumentEvents::LoadComplete(_)
                        | DocumentEvents::Reload(_)
                        | DocumentEvents::ContentChanged(_)
                        | DocumentEvents::PageChanged(_)
                ),
                None,
            ),
            _ => (false, None),
        };
        if !interessant {
            continue;
        }
        let emetteur = ev.sender().to_string();
        let pid = match pids.get(&emetteur) {
            Some(p) => *p,
            None => {
                let p = match (&dbus, BusName::try_from(emetteur.clone())) {
                    (Some(d), Ok(bn)) => avec_delai(
                        Duration::from_millis(800),
                        d.get_connection_unix_process_id(bn),
                    )
                    .await
                    .and_then(|r| r.ok()),
                    _ => None,
                };
                pids.insert(emetteur.clone(), p);
                if pids.len() > 2000 {
                    pids.clear();
                }
                p
            }
        };
        if let Some(item) = focus {
            // Le focus : nom et rôle de l'objet, lus vite, jamais le contenu d'un champ.
            let desc = match conn.object_as_accessible(&item).await {
                Ok(p) => {
                    let nom = avec_delai(Duration::from_millis(300), p.name())
                        .await
                        .and_then(|r| r.ok())
                        .unwrap_or_default();
                    let role = avec_delai(Duration::from_millis(300), p.get_role_name())
                        .await
                        .and_then(|r| r.ok())
                        .unwrap_or_default();
                    json!({"role": role, "nom": nettoyer(&nom, 80), "pid": pid, "cle": cle(&item), "a": maintenant()})
                }
                Err(_) => json!({"cle": cle(&item), "pid": pid, "a": maintenant()}),
            };
            partage.write().await.focus = Some(desc);
        }
        let now = maintenant();
        let mut b = partage.write().await;
        let cibles: Vec<Wid> = match pid {
            Some(p) => b
                .fenetres
                .values()
                .filter(|f| f.pid == Some(p))
                .map(|f| f.wid)
                .collect(),
            None => vec![b.active],
        };
        let mut marque = false;
        for w in cibles {
            if w == 0 || now - dernier_marquage.get(&w).copied().unwrap_or(0.0) < 0.15 {
                continue;
            }
            dernier_marquage.insert(w, now);
            b.programmer(w);
            marque = true;
        }
        drop(b);
        if marque {
            reveil.notify_one();
        }
    }
}
