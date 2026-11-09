//! Le modèle du bureau : ce qu'il y a à l'écran, où, depuis quand, et ce qui a changé.
//!
//! Règles qui ne se négocient pas :
//! - une identité d'élément (`eN`) suit l'objet exposé par l'application (bus + chemin) ;
//!   elle n'est jamais réattribuée à un autre objet pendant la vie du service ;
//! - rien n'est inventé : une fenêtre non lue est « indisponible », une lecture ancienne
//!   devient « périmée », une couverture partielle reste partielle ;
//! - chaque lecteur (claude, codex, charli…) garde sa propre mémoire du dernier regard,
//!   pour que « ce qui a changé » soit vrai pour lui.

use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::Serialize;
use serde_json::{json, Value};

use crate::x11::Geo;

pub type Wid = u32;
pub const AGE_MAX: f64 = 30.0;
pub const SHELL: Wid = 0;

pub fn maintenant() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

pub fn heure(t: f64) -> String {
    let secs = t as i64;
    let (h, m, s) = (((secs / 3600) % 24), ((secs / 60) % 60), (secs % 60));
    // Heure locale : décalage lu une fois (le service tourne sur le poste, pas ailleurs).
    let decalage = decalage_local();
    let h = ((h + decalage).rem_euclid(24)) as i64;
    format!("{h:02}:{m:02}:{s:02}")
}

fn decalage_local() -> i64 {
    // `date +%z` sans processus : on lit TZ via libc localtime_r.
    use std::sync::OnceLock;
    static D: OnceLock<i64> = OnceLock::new();
    *D.get_or_init(|| {
        let out = std::process::Command::new("date").arg("+%z").output().ok();
        let s = out
            .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
            .unwrap_or_default();
        let signe = if s.starts_with('-') { -1 } else { 1 };
        let h: i64 = s.get(1..3).and_then(|x| x.parse().ok()).unwrap_or(0);
        signe * h
    })
}

#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct Element {
    pub id: String,
    pub cle_source: String,
    pub role: String,
    pub role_atspi: String,
    pub nom: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub texte: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub valeur: Option<String>,
    pub etats: Vec<String>,
    pub x: i32,
    pub y: i32,
    pub w: i32,
    pub h: i32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub parent: Option<String>,
    pub profondeur: u8,
    pub enfants: u32,
    pub source: String,
    pub identite: String,
    pub visible_source: bool,
    pub hors_vue: bool,
    pub wid: String,
}

/// Un élément tel que le lit une source, avant attribution d'identité.
#[derive(Clone, Debug)]
pub struct Lu {
    pub cle_source: String,
    pub role: String,
    pub role_atspi: String,
    pub nom: String,
    pub texte: Option<String>,
    pub valeur: Option<String>,
    pub etats: Vec<String>,
    pub x: i32,
    pub y: i32,
    pub w: i32,
    pub h: i32,
    pub parent_cle: Option<String>,
    pub profondeur: u8,
    pub enfants: u32,
    /// Source propre à cet élément quand elle diffère de celle de la lecture (`atspi+ocr` : nom connu
    /// par l'arbre, position retrouvée sur les pixels).
    pub source: Option<String>,
}

impl From<&Element> for Lu {
    /// Un élément déjà connu, repris tel quel dans une nouvelle lecture (sans parent : il est reconstruit).
    fn from(x: &Element) -> Self {
        Lu {
            cle_source: x.cle_source.clone(),
            role: x.role.clone(),
            role_atspi: x.role_atspi.clone(),
            nom: x.nom.clone(),
            texte: x.texte.clone(),
            valeur: x.valeur.clone(),
            etats: x.etats.clone(),
            x: x.x,
            y: x.y,
            w: x.w,
            h: x.h,
            parent_cle: None,
            profondeur: x.profondeur,
            enfants: x.enfants,
            source: Some(x.source.clone()),
        }
    }
}

#[derive(Clone, Debug)]
pub struct Lecture {
    pub elements: Option<Vec<Lu>>,
    pub source: String,
    /// Comment l'arbre a été obtenu : `cache`, `cache-ancien`, `parcours` (ou vide).
    pub voie: &'static str,
    pub tronquee: bool,
    pub lecture_ms: u32,
    pub observe_a: f64,
    pub lisible: bool,
    /// La scène en une phrase, quand l'œil a regardé.
    pub scene: Option<String>,
}

#[derive(Clone, Debug, Default)]
pub struct Dessin {
    pub dernier: f64,
    pub compte: u32,
    pub fenetre_debut: f64,
    pub continu: bool,
}

#[derive(Clone, Debug)]
pub struct Fenetre {
    pub wid: Wid,
    pub titre: String,
    pub app: String,
    pub pid: Option<u32>,
    pub geo: Option<Geo>,
    pub visible: Option<bool>,
    pub active: bool,
    pub elements: Vec<Element>,
    pub source: Option<String>,
    pub voie: &'static str,
    pub etat: &'static str, // observe | a_relire | perime | indisponible
    pub observe_a: Option<f64>,
    pub lu: Option<String>,
    pub tronquee: bool,
    pub lecture_ms: u32,
    pub annonce: Option<Value>,
    pub dessin: Dessin,
    pub generation: u64,
    pub scene: Option<String>,
}

impl Fenetre {
    pub fn nouvelle(
        wid: Wid,
        titre: String,
        app: String,
        pid: Option<u32>,
        geo: Option<Geo>,
    ) -> Self {
        Fenetre {
            wid,
            titre,
            app,
            pid,
            geo,
            visible: None,
            active: false,
            elements: vec![],
            source: None,
            voie: "",
            etat: "indisponible",
            observe_a: None,
            lu: None,
            tronquee: false,
            lecture_ms: 0,
            annonce: None,
            dessin: Dessin::default(),
            generation: 0,
            scene: None,
        }
    }

    pub fn wid_hex(&self) -> String {
        if self.wid == SHELL {
            "shell".into()
        } else {
            format!("{:#x}", self.wid)
        }
    }

    pub fn perime(&self, now: f64) -> bool {
        self.etat == "perime" || self.observe_a.map(|t| now - t > AGE_MAX).unwrap_or(false)
    }

    pub fn boite_visible(&self) -> Option<(i32, i32, i32, i32)> {
        self.geo.map(|g| g.visible())
    }

    pub fn deduction(&self) -> Value {
        deduire(
            &self.app,
            &self.titre,
            &self.elements,
            self.annonce.as_ref(),
        )
    }

    pub fn couverture(&self) -> Value {
        if self.elements.is_empty() && self.annonce.is_some() {
            return json!({"etat": "declaration", "raison": "texte déclaré par l'application, sans positions des contrôles"});
        }
        match self.etat {
            "indisponible" => {
                json!({"etat": "indisponible", "raison": "contenu non décrit par les sources disponibles"})
            }
            _ => {
                json!({"etat": "partielle", "raison": "éléments exposés ; images et occlusion non interprétées",
                        "tronquee": self.tronquee, "source": self.source})
            }
        }
    }

    pub fn to_json(&self, now: f64) -> Value {
        let perime = self.perime(now);
        let (x, y, l, h) = self
            .geo
            .map(|g| (json!(g.x), json!(g.y), json!(g.l), json!(g.h)))
            .unwrap_or((Value::Null, Value::Null, Value::Null, Value::Null));
        let fraicheur_etat = if perime && self.etat == "observe" {
            "perime"
        } else {
            self.etat
        };
        let mut deduction = self.deduction();
        if perime {
            if let Some(r) = deduction
                .get("resume")
                .and_then(|v| v.as_str())
                .map(|s| s.to_string())
            {
                deduction["resume"] = json!(format!(
                    "{} (lu à {}, périmé)",
                    r,
                    self.lu.clone().unwrap_or_default()
                ));
            }
        }
        json!({
            "wid": self.wid_hex(), "app": self.app, "titre": self.titre, "pid": self.pid,
            "x": x, "y": y, "l": l, "h": h,
            "visible_x": self.boite_visible().map(|b| b.0), "visible_y": self.boite_visible().map(|b| b.1),
            "visible_l": self.boite_visible().map(|b| b.2), "visible_h": self.boite_visible().map(|b| b.3),
            "elements": self.elements, "arbre": self.etat != "indisponible", "source": self.source, "voie": self.voie,
            "deduction": deduction, "ts": self.lu, "lu": self.lu, "lecture_ms": self.lecture_ms,
            "perime": perime, "active": self.active, "visible": self.visible,
            "fraicheur": {"etat": fraicheur_etat, "observe_a": self.observe_a},
            "age_ms": self.observe_a.map(|t| ((now - t).max(0.0) * 1000.0) as u64),
            "couverture": self.couverture(), "annonce": self.annonce, "scene": self.scene,
            "dessin": {"continu": self.dessin.continu, "dernier": self.dessin.dernier},
        })
    }
}

/// La mémoire d'un lecteur : ce qu'il a vu au dernier regard.
#[derive(Clone, Debug, Default)]
pub struct Memoire {
    pub revision: u64,
    pub instant: f64,
    pub elements: HashMap<String, Element>,
    pub fenetres: HashMap<Wid, String>,
    pub active: Wid,
}

#[derive(Clone, Debug, Default)]
pub struct Diff {
    pub premier: bool,
    pub depuis: Option<f64>,
    pub ajoutes: HashSet<String>,
    pub modifies: HashSet<String>,
    pub retires: Vec<Element>,
    pub fenetres_ouvertes: Vec<String>,
    pub fenetres_fermees: Vec<String>,
    pub titres: Vec<(String, String)>,
    pub active_avant: Option<String>,
}

#[derive(Clone, Debug, Serialize)]
pub struct Trace {
    pub revision: u64,
    pub ts: f64,
    pub texte: String,
}

pub struct Bureau {
    pub session: String,
    pub revision: u64,
    pub fenetres: BTreeMap<Wid, Fenetre>,
    pub active: Wid,
    pub pointeur: Option<(i32, i32)>,
    pub empilement: Vec<Wid>,
    pub largeur: i32,
    pub hauteur: i32,
    pub focus: Option<Value>,
    pub pilote: Value,
    pub erreurs: VecDeque<String>,
    pub journal: VecDeque<Trace>,
    pub lecteurs: HashMap<String, Memoire>,
    pub annonces: HashMap<String, Value>,
    /// Fenêtres à relire (instant de la demande), générations (une lecture dépassée est relancée),
    /// et derniers essais : le lecteur pioche là-dedans, l'active d'abord.
    pub a_relire: HashMap<Wid, f64>,
    pub generations: HashMap<Wid, u64>,
    pub derniers_essais: HashMap<Wid, f64>,
    /// Période minimale de relecture par fenêtre : courte quand le contenu bouge, allongée quand
    /// des relectures successives ne changent rien (une appli bavarde ne coûte plus un cœur).
    pub repos: HashMap<Wid, f64>,
    /// Signes de vie pour le chien de garde : dernier événement X11, dernier tour du lecteur.
    pub derniere_x11: f64,
    pub dernier_lecteur: f64,
    pub ocr_signale: bool,
    refs: HashMap<String, String>,
    compteur: u64,
}

impl Bureau {
    pub fn nouveau() -> Self {
        let session = format!(
            "{:x}{:x}",
            std::process::id(),
            (maintenant() * 1000.0) as u64
        );
        Bureau {
            session,
            revision: 0,
            fenetres: BTreeMap::new(),
            active: 0,
            pointeur: None,
            empilement: vec![],
            largeur: 0,
            hauteur: 0,
            focus: None,
            pilote: Value::Null,
            erreurs: VecDeque::new(),
            journal: VecDeque::new(),
            lecteurs: HashMap::new(),
            annonces: HashMap::new(),
            a_relire: HashMap::new(),
            generations: HashMap::new(),
            derniers_essais: HashMap::new(),
            repos: HashMap::new(),
            derniere_x11: 0.0,
            dernier_lecteur: 0.0,
            ocr_signale: false,
            refs: HashMap::new(),
            compteur: 0,
        }
    }

    /// Demande une relecture (coalescée) et marque l'ancienne lecture « à relire ».
    pub fn programmer(&mut self, wid: Wid) {
        *self.generations.entry(wid).or_insert(0) += 1;
        self.a_relire.entry(wid).or_insert_with(maintenant);
        if let Some(f) = self.fenetres.get_mut(&wid) {
            if f.etat == "observe" {
                f.etat = "a_relire";
            }
        }
    }

    /// La prochaine fenêtre à lire. Équité : une fenêtre n'est pas relue avant sa période minimale
    /// (0,5 s pour l'active, 2 s derrière), l'active passe devant quand elle y a droit, sinon la
    /// fenêtre lue depuis le plus longtemps. Une application bavarde ne monopolise plus le lecteur.
    pub fn prochaine(&self) -> Option<Wid> {
        let now = maintenant();
        let mut pretes: Vec<(Wid, f64)> = self
            .a_relire
            .iter()
            .filter(|(w, t)| {
                let dernier = self.derniers_essais.get(*w).copied().unwrap_or(0.0);
                let repos = self.repos.get(*w).copied().unwrap_or(0.5);
                let periode = if **w == self.active {
                    repos
                } else {
                    repos.max(2.0)
                };
                now - **t >= 0.1 && now - dernier >= periode
            })
            .map(|(w, _)| (*w, self.derniers_essais.get(w).copied().unwrap_or(0.0)))
            .collect();
        pretes.sort_by(|a, b| {
            (a.0 != self.active)
                .cmp(&(b.0 != self.active))
                .then(a.1.partial_cmp(&b.1).unwrap_or(std::cmp::Ordering::Equal))
        });
        pretes.first().map(|p| p.0)
    }

    pub fn id_pour(&mut self, cle: &str) -> String {
        if let Some(id) = self.refs.get(cle) {
            return id.clone();
        }
        self.compteur += 1;
        let id = format!("e{}", self.compteur);
        self.refs.insert(cle.to_string(), id.clone());
        id
    }

    /// Le registre des identités n'oublie que ce qui n'est plus à l'écran, et seulement quand il
    /// devient gros : un objet revenu après l'oubli reçoit un nouveau numéro, jamais un ancien.
    pub fn elaguer_refs(&mut self) {
        if self.refs.len() < 50_000 {
            return;
        }
        let vivantes: HashSet<&str> = self
            .fenetres
            .values()
            .flat_map(|f| f.elements.iter().map(|e| e.cle_source.as_str()))
            .collect();
        let garder: HashSet<String> = vivantes.into_iter().map(|s| s.to_string()).collect();
        self.refs.retain(|k, _| garder.contains(k));
    }

    pub fn tracer(&mut self, texte: String) {
        self.journal.push_back(Trace {
            revision: self.revision,
            ts: maintenant(),
            texte,
        });
        while self.journal.len() > 300 {
            self.journal.pop_front();
        }
    }

    pub fn erreur(&mut self, texte: String) {
        self.erreurs
            .push_back(format!("{} {}", heure(maintenant()), texte));
        while self.erreurs.len() > 20 {
            self.erreurs.pop_front();
        }
    }

    pub fn fenetre_active(&self) -> Option<&Fenetre> {
        self.fenetres.get(&self.active)
    }

    pub fn contexte(&self) -> Value {
        let visibles: serde_json::Map<String, Value> = self
            .fenetres
            .values()
            .filter(|f| f.wid != SHELL)
            .map(|f| (f.wid_hex(), json!(f.visible)))
            .collect();
        json!({
            "fenetre_active": format!("{:#x}", self.active),
            "pointeur": self.pointeur.map(|(x, y)| json!({"x": x, "y": y})),
            "empilement": self.empilement.iter().map(|w| format!("{w:#x}")).collect::<Vec<_>>(),
            "visibles": visibles,
            "focus": self.focus,
            "pilote": self.pilote,
            "ecran": {"largeur": self.largeur, "hauteur": self.hauteur},
        })
    }

    /// L'instantané JSON compatible avec `ecran` (`/carte`).
    pub fn instantane(&self, motif: Option<&str>) -> Value {
        let now = maintenant();
        let m = motif.unwrap_or("").to_lowercase();
        let mut fen: Vec<&Fenetre> = self
            .fenetres
            .values()
            .filter(|f| {
                m.is_empty()
                    || f.titre.to_lowercase().contains(&m)
                    || f.app.to_lowercase().contains(&m)
            })
            .collect();
        fen.sort_by_key(|f| (!f.active, f.wid == SHELL, f.app.to_lowercase()));
        json!({
            "ts": heure(now), "session": self.session, "revision": self.revision,
            "fenetres": fen.iter().map(|f| f.to_json(now)).collect::<Vec<_>>(),
            "a_rafraichir": self.fenetres.values().filter(|f| f.etat == "a_relire").count(),
            "contexte": self.contexte(), "sans_capture": true,
            "erreurs": self.erreurs,
        })
    }

    fn publier(&mut self, mut d: Value) -> Value {
        let precedent = self.revision;
        self.revision += 1;
        if let Some(o) = d.as_object_mut() {
            o.insert("session".into(), json!(self.session));
            o.insert("revision".into(), json!(self.revision));
            o.insert("precedent".into(), json!(precedent));
            o.insert("ts".into(), json!(maintenant()));
        }
        d
    }

    pub fn publier_contexte(&mut self) -> Value {
        let c = self.contexte();
        self.publier(json!({"type": "contexte", "contexte": c}))
    }

    /// Enregistre une lecture et produit le delta (None si rien n'a changé).
    pub fn enregistrer(&mut self, wid: Wid, lecture: Lecture) -> Option<Value> {
        let (largeur, hauteur) = (self.largeur, self.hauteur);
        let f = self.fenetres.get(&wid)?;
        let wid_hex = f.wid_hex();
        let boite = f.boite_visible();
        let anciens: Vec<Element> = f.elements.clone();
        let meta_avant = f.to_json(maintenant());
        let lu_a = heure(lecture.observe_a);
        let mut nouveaux: Vec<Element> = Vec::new();
        let mut etat = f.etat;
        let mut source = f.source.clone();
        let mut observe_a = f.observe_a;
        let mut lu = f.lu.clone();
        match lecture.elements {
            Some(lus) => {
                let mut ids: HashMap<String, String> = HashMap::new();
                for l in &lus {
                    let id = self.id_pour(&l.cle_source);
                    ids.insert(l.cle_source.clone(), id);
                }
                for l in lus {
                    let id = ids[&l.cle_source].clone();
                    let (cx, cy) = (l.x + l.w / 2, l.y + l.h / 2);
                    let hors_vue = match boite {
                        Some((bx, by, bl, bh)) => {
                            !(cx >= bx && cx < bx + bl && cy >= by && cy < by + bh)
                        }
                        None => false,
                    } || cx < 0
                        || cy < 0
                        || (largeur > 0 && cx >= largeur)
                        || (hauteur > 0 && cy >= hauteur);
                    nouveaux.push(Element {
                        id,
                        cle_source: l.cle_source.clone(),
                        role: l.role,
                        role_atspi: l.role_atspi,
                        nom: l.nom,
                        texte: l.texte,
                        valeur: l.valeur,
                        etats: l.etats,
                        x: l.x,
                        y: l.y,
                        w: l.w,
                        h: l.h,
                        parent: l.parent_cle.as_ref().and_then(|p| ids.get(p).cloned()),
                        profondeur: l.profondeur,
                        enfants: l.enfants,
                        source: l.source.clone().unwrap_or_else(|| lecture.source.clone()),
                        // Une ligne lue sur les pixels n'a pas d'objet derrière elle : identité estimée, vérifiée
                        // par une relecture de la zone avant tout clic.
                        identite: if l.cle_source.starts_with("ocr|")
                            || l.cle_source.starts_with("qwen|")
                        {
                            "estimee".into()
                        } else {
                            "native".into()
                        },
                        visible_source: true,
                        hors_vue,
                        wid: wid_hex.clone(),
                    });
                }
                etat = "observe";
                source = Some(lecture.source.clone());
                observe_a = Some(lecture.observe_a);
                lu = Some(lu_a);
            }
            None => {
                // Pas de lecture possible : on garde l'ancienne, datée, jamais réinventée.
                nouveaux = anciens.clone();
                if nouveaux.is_empty() {
                    etat = "indisponible";
                    source = None;
                } else {
                    etat = "perime";
                }
            }
        }
        self.elaguer_refs();
        let f = self.fenetres.get_mut(&wid)?;
        f.elements = nouveaux;
        f.etat = etat;
        f.source = source;
        if !lecture.voie.is_empty() {
            f.voie = lecture.voie;
        }
        if lecture.scene.is_some() {
            f.scene = lecture.scene.clone();
        }
        f.observe_a = observe_a;
        f.lu = lu;
        f.tronquee = lecture.tronquee;
        f.lecture_ms = lecture.lecture_ms;
        let avant: HashMap<&str, &Element> = anciens.iter().map(|e| (e.id.as_str(), e)).collect();
        let apres: HashMap<&str, &Element> =
            f.elements.iter().map(|e| (e.id.as_str(), e)).collect();
        let ajoutes: Vec<&Element> = f
            .elements
            .iter()
            .filter(|e| !avant.contains_key(e.id.as_str()))
            .collect();
        let modifies: Vec<&Element> = f
            .elements
            .iter()
            .filter(|e| {
                avant
                    .get(e.id.as_str())
                    .map(|a| **a != **e)
                    .unwrap_or(false)
            })
            .collect();
        let retires: Vec<&str> = anciens
            .iter()
            .map(|e| e.id.as_str())
            .filter(|i| !apres.contains_key(i))
            .collect();
        let ordre: Vec<&str> = f.elements.iter().map(|e| e.id.as_str()).collect();
        let ordre_avant: Vec<&str> = anciens.iter().map(|e| e.id.as_str()).collect();
        let mut meta = f.to_json(maintenant());
        meta.as_object_mut().map(|o| o.remove("elements"));
        // Une relecture identique n'est pas un changement : les horodatages et durées ne comptent pas.
        let stable = |mut v: Value| {
            if let Some(o) = v.as_object_mut() {
                for k in [
                    "elements",
                    "age_ms",
                    "observe_a",
                    "lu",
                    "ts",
                    "lecture_ms",
                    "fraicheur",
                    "dessin",
                ] {
                    o.remove(k);
                }
            }
            v
        };
        if ajoutes.is_empty()
            && modifies.is_empty()
            && retires.is_empty()
            && ordre == ordre_avant
            && stable(meta_avant) == stable(meta.clone())
        {
            return None;
        }
        let d = json!({"type": "fenetre", "wid": wid_hex, "fenetre": meta,
                       "ajoutes": ajoutes, "modifies": modifies, "retires": retires, "ordre": ordre});
        Some(self.publier(d))
    }

    pub fn fermer(&mut self, wid: Wid) -> Option<Value> {
        let f = self.fenetres.remove(&wid)?;
        let mut meta = f.to_json(maintenant());
        meta.as_object_mut().map(|o| o.remove("elements"));
        self.tracer(format!("fenêtre fermée : {} « {} »", f.app, f.titre));
        Some(self.publier(json!({"type": "fermeture", "wid": f.wid_hex(), "fenetre": meta})))
    }

    pub fn publier_meta(&mut self, wid: Wid) -> Option<Value> {
        let f = self.fenetres.get(&wid)?;
        let mut meta = f.to_json(maintenant());
        meta.as_object_mut().map(|o| o.remove("elements"));
        let ordre: Vec<String> = f.elements.iter().map(|e| e.id.clone()).collect();
        let wid_hex = f.wid_hex();
        Some(
            self.publier(json!({"type": "fenetre", "wid": wid_hex, "fenetre": meta,
                                 "ajoutes": [], "modifies": [], "retires": [], "ordre": ordre})),
        )
    }

    /// Ce qui a changé pour un lecteur depuis son dernier regard.
    pub fn diff(&self, lecteur: &str) -> Diff {
        let mut d = Diff::default();
        let m = match self.lecteurs.get(lecteur) {
            Some(m) => m,
            None => {
                d.premier = true;
                return d;
            }
        };
        d.depuis = Some(m.instant);
        let mut vus: HashSet<&str> = HashSet::new();
        for f in self.fenetres.values() {
            for e in &f.elements {
                vus.insert(&e.id);
                match m.elements.get(&e.id) {
                    None => {
                        d.ajoutes.insert(e.id.clone());
                    }
                    Some(a) => {
                        if a.nom != e.nom
                            || a.texte != e.texte
                            || a.valeur != e.valeur
                            || a.etats != e.etats
                            || (a.x, a.y, a.w, a.h) != (e.x, e.y, e.w, e.h)
                        {
                            d.modifies.insert(e.id.clone());
                        }
                    }
                }
            }
            match m.fenetres.get(&f.wid) {
                None => d
                    .fenetres_ouvertes
                    .push(format!("{} « {} »", f.app, f.titre)),
                Some(t) if *t != f.titre => d.titres.push((t.clone(), f.titre.clone())),
                _ => {}
            }
        }
        for (w, t) in &m.fenetres {
            if !self.fenetres.contains_key(w) {
                d.fenetres_fermees.push(t.clone());
            }
        }
        let mut retires: Vec<Element> = m
            .elements
            .values()
            .filter(|e| !vus.contains(e.id.as_str()))
            .cloned()
            .collect();
        retires.sort_by(|a, b| a.id.len().cmp(&b.id.len()).then(a.id.cmp(&b.id)));
        d.retires = retires;
        if m.active != self.active {
            d.active_avant = m.fenetres.get(&m.active).cloned();
        }
        d
    }

    pub fn memoriser(&mut self, lecteur: &str) {
        // Un lecteur qui n'a pas regardé depuis un jour est oublié (sa mémoire est une copie des éléments).
        let now = maintenant();
        self.lecteurs.retain(|_, m| now - m.instant < 86_400.0);
        let mut m = Memoire {
            revision: self.revision,
            instant: now,
            ..Default::default()
        };
        for f in self.fenetres.values() {
            m.fenetres.insert(f.wid, f.titre.clone());
            for e in &f.elements {
                m.elements.insert(e.id.clone(), e.clone());
            }
        }
        m.active = self.active;
        self.lecteurs.insert(lecteur.to_string(), m);
    }

    pub fn trouver(&self, id: &str) -> Option<(&Fenetre, &Element)> {
        for f in self.fenetres.values() {
            if let Some(e) = f.elements.iter().find(|e| e.id == id) {
                return Some((f, e));
            }
        }
        None
    }

    /// Les fenêtres visibles au-dessus de `wid` qui recouvrent le point.
    pub fn recouvert(&self, wid: Wid, x: i32, y: i32) -> Option<&Fenetre> {
        let pos = self.empilement.iter().position(|w| *w == wid)?;
        for autre in &self.empilement[pos + 1..] {
            if let Some(f) = self.fenetres.get(autre) {
                if f.visible == Some(false) {
                    continue;
                }
                if let Some((bx, by, bl, bh)) = f.boite_visible() {
                    if x >= bx && x < bx + bl && y >= by && y < by + bh {
                        return Some(f);
                    }
                }
            }
        }
        None
    }
}

/// Ce qu'on peut dire d'une fenêtre sans la regarder : genre, ce qu'elle attend, où est le focus.
pub fn deduire(app: &str, titre: &str, els: &[Element], annonce: Option<&Value>) -> Value {
    let a = app.to_lowercase();
    let t = titre.to_lowercase();
    let a_role = |r: &str| els.iter().any(|e| e.role_atspi == r);
    let genre = if els.is_empty() && annonce.is_none() {
        "sans arbre lisible (Steam, jeu, vidéo, Tk, ou Chrome/Electron en arrière-plan)"
    } else if a.contains("chrome")
        || a.contains("firefox")
        || a.contains("chromium")
        || a_role("document web")
    {
        "navigateur"
    } else if a.starts_with("code") || a.contains("vscode") {
        "éditeur de code"
    } else if a.contains("terminal") || a_role("terminal") {
        "terminal"
    } else if a.contains("nautilus") || a.contains("thunar") {
        "gestionnaire de fichiers"
    } else if a.contains("steam") {
        "Steam"
    } else if a.contains("mailspring") || a.contains("thunderbird") || t.contains("mail") {
        "courrier"
    } else if a.contains("whatsapp") || a.contains("telegram") || a.contains("signal") {
        "messagerie"
    } else if a == "tk" || a.contains("charli") {
        "application maison"
    } else {
        "application"
    };
    let focus = els.iter().find(|e| e.etats.iter().any(|s| s == "focus"));
    let dialogue = els.iter().find(|e| {
        (e.role_atspi == "dialog" || e.role_atspi == "alert")
            && (!e.nom.is_empty() || e.texte.is_some())
    });
    let captcha = els
        .iter()
        .any(|e| est_captcha(&e.nom) || e.texte.as_deref().map(est_captcha).unwrap_or(false));
    let attend = if captcha {
        Some("un CAPTCHA : à Félix (je le signale, je ne le résous pas)".to_string())
    } else if let Some(d) = dialogue {
        Some(format!(
            "une réponse : « {} »",
            if d.nom.is_empty() {
                d.texte.clone().unwrap_or_default()
            } else {
                d.nom.clone()
            }
        ))
    } else if let Some(f) = focus.filter(|f| f.role == "champ") {
        let mut s = format!(
            "une saisie dans « {} »",
            if f.nom.is_empty() { "champ" } else { &f.nom }
        );
        if let Some(t) = &f.texte {
            s += &format!(" (contient « {} »)", t.chars().take(40).collect::<String>());
        }
        Some(s)
    } else {
        None
    };
    let boutons: Vec<&str> = els
        .iter()
        .filter(|e| e.role == "bouton" && !e.nom.is_empty())
        .map(|e| e.nom.as_str())
        .take(6)
        .collect();
    let champs = els.iter().filter(|e| e.role == "champ").count();
    let mut resume = format!("{genre}, {} éléments", els.len());
    if champs > 0 {
        resume += &format!(", {champs} champ{}", if champs > 1 { "s" } else { "" });
    }
    if !boutons.is_empty() {
        resume += &format!(", boutons : {}", boutons.join(", "));
    }
    if let Some(a) = &attend {
        resume += &format!(" ; attend {a}");
    }
    if els.is_empty() {
        if let Some(l) = annonce
            .and_then(|a| a.get("lignes"))
            .and_then(|l| l.as_array())
        {
            let txt: Vec<&str> = l
                .iter()
                .filter_map(|x| x.as_str())
                .filter(|x| !x.is_empty())
                .collect();
            resume = format!(
                "{genre} ; déclare : {}",
                txt.join(" / ").chars().take(160).collect::<String>()
            );
        }
    }
    json!({"genre": genre, "attend": attend, "captcha": captcha,
           "focus": focus.map(|f| compacter(f)), "resume": resume})
}

/// Un CAPTCHA se reconnaît à ses mots ; il est signalé, jamais résolu.
pub fn est_captcha(s: &str) -> bool {
    let t = s.to_lowercase();
    t.contains("captcha")
        || t.contains("pas un robot")
        || t.contains("not a robot")
        || t.contains("vous êtes humain")
        || t.contains("you are human")
        || t.contains("vérification de sécurité")
        || t.contains("security check")
}

pub fn compacter(e: &Element) -> String {
    let mut l = e.role.clone();
    if !e.nom.is_empty() {
        l += &format!(" « {} »", e.nom);
    }
    if let Some(t) = &e.texte {
        l += &format!(" = {t}");
    }
    if !e.etats.is_empty() {
        l += &format!(" [{}]", e.etats.join(", "));
    }
    l
}
