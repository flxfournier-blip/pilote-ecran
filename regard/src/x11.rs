//! La couche X11 : les fenêtres clientes (EWMH), leur géométrie écran, l'active, l'empilement,
//! le pointeur, et l'extension Damage (« cette fenêtre vient de se redessiner, là »).
//! Aucun pixel n'est lu : seulement des propriétés et des événements du serveur X.
//! Un fil bloquant possède la connexion et pousse des `Evenement` typés vers le modèle.

use std::collections::HashMap;
use std::time::{Duration, Instant};

use tokio::sync::mpsc::UnboundedSender;
use x11rb::connection::Connection;
use x11rb::properties::WmClass;
use x11rb::protocol::damage::{self, ConnectionExt as _, ReportLevel};
use x11rb::protocol::xproto::{
    self, Atom, AtomEnum, ChangeWindowAttributesAux, ConnectionExt as _, EventMask, MapState,
    Window,
};
use x11rb::protocol::Event;
use x11rb::rust_connection::RustConnection;
use x11rb::NONE;

/// Géométrie écran d'une fenêtre cliente : origine X11 du client, taille, et marges CSD
/// (`_GTK_FRAME_EXTENTS` : ombres dessinées DANS la fenêtre cliente, hors de la zone visible).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
pub struct Geo {
    pub x: i32,
    pub y: i32,
    pub l: i32,
    pub h: i32,
    /// gauche, droite, haut, bas
    pub marges: (i32, i32, i32, i32),
}

impl Geo {
    /// La boîte réellement visible à l'écran (sans les ombres CSD).
    pub fn visible(&self) -> (i32, i32, i32, i32) {
        let (g, d, h, b) = self.marges;
        (
            self.x + g,
            self.y + h,
            (self.l - g - d).max(0),
            (self.h - h - b).max(0),
        )
    }
}

#[derive(Clone, Debug)]
pub enum Evenement {
    Ecran {
        largeur: i32,
        hauteur: i32,
    },
    Ouverte {
        wid: Window,
        titre: String,
        classe: String,
        pid: Option<u32>,
        geo: Option<Geo>,
    },
    Fermee {
        wid: Window,
    },
    Titre {
        wid: Window,
        titre: String,
    },
    Geometrie {
        wid: Window,
        geo: Geo,
    },
    Active {
        wid: Window,
    },
    Empilement(Vec<Window>),
    Pointeur {
        x: i32,
        y: i32,
    },
    Visibles(Vec<(Window, bool)>),
    /// Une zone redessinée (coordonnées écran), agrégée côté modèle.
    Dessin {
        wid: Window,
        x: i32,
        y: i32,
        l: i32,
        h: i32,
    },
    Erreur(String),
}

struct Atomes {
    net_active: Atom,
    net_clients: Atom,
    net_stack: Atom,
    net_name: Atom,
    net_pid: Atom,
    utf8: Atom,
    gtk_extents: Atom,
}

fn atome(conn: &RustConnection, nom: &[u8]) -> Result<Atom, Box<dyn std::error::Error>> {
    Ok(conn.intern_atom(false, nom)?.reply()?.atom)
}

fn prop_u32(conn: &RustConnection, w: Window, atom: Atom) -> Vec<u32> {
    conn.get_property(false, w, atom, AtomEnum::ANY, 0, u32::MAX)
        .ok()
        .and_then(|c| c.reply().ok())
        .and_then(|r| r.value32().map(|it| it.collect()))
        .unwrap_or_default()
}

fn prop_texte(conn: &RustConnection, w: Window, atom: Atom) -> Option<String> {
    let r = conn
        .get_property(false, w, atom, AtomEnum::ANY, 0, u32::MAX)
        .ok()?
        .reply()
        .ok()?;
    if r.format != 8 || r.value.is_empty() {
        return None;
    }
    Some(String::from_utf8_lossy(&r.value).to_string())
}

fn nettoyer(s: &str, n: usize) -> String {
    let s: String = s.chars().filter(|c| !c.is_control()).collect();
    let s = s.trim();
    if s.chars().count() > n {
        let coupe: String = s.chars().take(n).collect();
        format!("{coupe}…")
    } else {
        s.to_string()
    }
}

/// Le fil X11 : à lancer dans `std::thread`. Ne rend jamais la main ; en cas d'erreur de
/// connexion, l'erreur est envoyée puis le fil attend avant de retenter.
pub fn fil(display: Option<String>, tx: UnboundedSender<Evenement>) {
    loop {
        if let Err(e) = boucle(display.as_deref(), &tx) {
            let _ = tx.send(Evenement::Erreur(format!("X11 : {e}")));
        }
        std::thread::sleep(Duration::from_secs(5));
    }
}

struct Suivi {
    titre: String,
    classe: String,
    damage: Option<damage::Damage>,
    geo: Option<Geo>,
    dessin_depuis: Instant,
}

fn boucle(
    display: Option<&str>,
    tx: &UnboundedSender<Evenement>,
) -> Result<(), Box<dyn std::error::Error>> {
    let (conn, ecran) = x11rb::connect(display)?;
    let racine = conn.setup().roots[ecran].root;
    let setup = &conn.setup().roots[ecran];
    tx.send(Evenement::Ecran {
        largeur: setup.width_in_pixels as i32,
        hauteur: setup.height_in_pixels as i32,
    })?;
    let atomes = Atomes {
        net_active: atome(&conn, b"_NET_ACTIVE_WINDOW")?,
        net_clients: atome(&conn, b"_NET_CLIENT_LIST")?,
        net_stack: atome(&conn, b"_NET_CLIENT_LIST_STACKING")?,
        net_name: atome(&conn, b"_NET_WM_NAME")?,
        net_pid: atome(&conn, b"_NET_WM_PID")?,
        utf8: atome(&conn, b"UTF8_STRING")?,
        gtk_extents: atome(&conn, b"_GTK_FRAME_EXTENTS")?,
    };
    let damage_ok = conn
        .damage_query_version(1, 1)
        .ok()
        .and_then(|c| c.reply().ok())
        .is_some();
    if !damage_ok {
        let _ = tx.send(Evenement::Erreur(
            "extension Damage absente : pas de détection des redessins".into(),
        ));
    }
    conn.change_window_attributes(
        racine,
        &ChangeWindowAttributesAux::new().event_mask(EventMask::PROPERTY_CHANGE),
    )?;
    conn.flush()?;

    let infos = |w: Window| -> (String, String, Option<u32>) {
        let titre = prop_texte(&conn, w, atomes.net_name)
            .or_else(|| prop_texte(&conn, w, AtomEnum::WM_NAME.into()))
            .unwrap_or_default();
        let classe = WmClass::get(&conn, w)
            .ok()
            .and_then(|c| c.reply().ok())
            .flatten()
            .map(|c| {
                let cl = String::from_utf8_lossy(c.class()).to_string();
                if cl.is_empty() {
                    String::from_utf8_lossy(c.instance()).to_string()
                } else {
                    cl
                }
            })
            .unwrap_or_default();
        let pid = prop_u32(&conn, w, atomes.net_pid).first().copied();
        (nettoyer(&titre, 100), classe, pid)
    };
    let geometrie = |w: Window| -> Option<Geo> {
        let g = conn.get_geometry(w).ok()?.reply().ok()?;
        let t = conn
            .translate_coordinates(w, racine, 0, 0)
            .ok()?
            .reply()
            .ok()?;
        let m = prop_u32(&conn, w, atomes.gtk_extents);
        let marges = if m.len() == 4 {
            (m[0] as i32, m[1] as i32, m[2] as i32, m[3] as i32)
        } else {
            (0, 0, 0, 0)
        };
        Some(Geo {
            x: t.dst_x as i32,
            y: t.dst_y as i32,
            l: g.width as i32,
            h: g.height as i32,
            marges,
        })
    };
    let suivre = |w: Window| -> Option<damage::Damage> {
        let _ = conn.change_window_attributes(
            w,
            &ChangeWindowAttributesAux::new()
                .event_mask(EventMask::PROPERTY_CHANGE | EventMask::STRUCTURE_NOTIFY),
        );
        if !damage_ok {
            return None;
        }
        let id = conn.generate_id().ok()?;
        conn.damage_create(id, w, ReportLevel::NON_EMPTY)
            .ok()?
            .check()
            .ok()?;
        Some(id)
    };

    let mut suivis: HashMap<Window, Suivi> = HashMap::new();
    let clients: Vec<Window> = prop_u32(&conn, racine, atomes.net_clients);
    for &w in &clients {
        let (titre, classe, pid) = infos(w);
        let geo = geometrie(w);
        let damage = suivre(w);
        suivis.insert(
            w,
            Suivi {
                titre: titre.clone(),
                classe: classe.clone(),
                damage,
                geo,
                dessin_depuis: Instant::now(),
            },
        );
        tx.send(Evenement::Ouverte {
            wid: w,
            titre,
            classe,
            pid,
            geo,
        })?;
    }
    tx.send(Evenement::Empilement(prop_u32(
        &conn,
        racine,
        atomes.net_stack,
    )))?;
    let mut active = prop_u32(&conn, racine, atomes.net_active)
        .first()
        .copied()
        .unwrap_or(0);
    tx.send(Evenement::Active { wid: active })?;
    conn.flush()?;

    let mut dernier_releve = Instant::now() - Duration::from_secs(1);
    loop {
        if dernier_releve.elapsed() >= Duration::from_millis(250) {
            dernier_releve = Instant::now();
            if let Ok(p) = conn.query_pointer(racine).and_then(|c| Ok(c.reply())) {
                if let Ok(p) = p {
                    tx.send(Evenement::Pointeur {
                        x: p.root_x as i32,
                        y: p.root_y as i32,
                    })?;
                }
            }
            let visibles: Vec<(Window, bool)> = suivis
                .keys()
                .map(|&w| {
                    let v = conn
                        .get_window_attributes(w)
                        .ok()
                        .and_then(|c| c.reply().ok())
                        .map(|a| a.map_state == MapState::VIEWABLE)
                        .unwrap_or(false);
                    (w, v)
                })
                .collect();
            tx.send(Evenement::Visibles(visibles))?;
        }
        let ev = match conn.poll_for_event()? {
            Some(ev) => ev,
            None => {
                std::thread::sleep(Duration::from_millis(12));
                continue;
            }
        };
        match ev {
            Event::DamageNotify(e) => {
                let wid = e.drawable;
                let _ = conn.damage_subtract(e.damage, NONE, NONE);
                if let Some(s) = suivis.get_mut(&wid) {
                    // Le premier rapport d'une fenêtre (toute sa surface) n'est pas un redessin.
                    if s.dessin_depuis.elapsed() < Duration::from_millis(500) {
                        continue;
                    }
                    let (ox, oy) = s.geo.map(|g| (g.x, g.y)).unwrap_or((0, 0));
                    tx.send(Evenement::Dessin {
                        wid,
                        x: ox + e.area.x as i32,
                        y: oy + e.area.y as i32,
                        l: e.area.width as i32,
                        h: e.area.height as i32,
                    })?;
                }
            }
            Event::ConfigureNotify(e) => {
                if let Some(s) = suivis.get_mut(&e.window) {
                    if let Some(g) = geometrie(e.window) {
                        if s.geo != Some(g) {
                            s.geo = Some(g);
                            tx.send(Evenement::Geometrie {
                                wid: e.window,
                                geo: g,
                            })?;
                        }
                    }
                }
            }
            Event::PropertyNotify(e) if e.window == racine => {
                if e.atom == atomes.net_active {
                    let a = prop_u32(&conn, racine, atomes.net_active)
                        .first()
                        .copied()
                        .unwrap_or(0);
                    if a != active {
                        active = a;
                        tx.send(Evenement::Active { wid: a })?;
                    }
                } else if e.atom == atomes.net_stack {
                    tx.send(Evenement::Empilement(prop_u32(
                        &conn,
                        racine,
                        atomes.net_stack,
                    )))?;
                } else if e.atom == atomes.net_clients {
                    let nouveaux: Vec<Window> = prop_u32(&conn, racine, atomes.net_clients);
                    for &w in &nouveaux {
                        if !suivis.contains_key(&w) {
                            let (titre, classe, pid) = infos(w);
                            let geo = geometrie(w);
                            let damage = suivre(w);
                            suivis.insert(
                                w,
                                Suivi {
                                    titre: titre.clone(),
                                    classe: classe.clone(),
                                    damage,
                                    geo,
                                    dessin_depuis: Instant::now(),
                                },
                            );
                            tx.send(Evenement::Ouverte {
                                wid: w,
                                titre,
                                classe,
                                pid,
                                geo,
                            })?;
                        }
                    }
                    let fermees: Vec<Window> = suivis
                        .keys()
                        .filter(|w| !nouveaux.contains(w))
                        .copied()
                        .collect();
                    for w in fermees {
                        if let Some(s) = suivis.remove(&w) {
                            if let Some(d) = s.damage {
                                let _ = conn.damage_destroy(d);
                            }
                        }
                        tx.send(Evenement::Fermee { wid: w })?;
                    }
                    conn.flush()?;
                }
            }
            Event::PropertyNotify(e) => {
                if (e.atom == atomes.net_name || e.atom == Atom::from(AtomEnum::WM_NAME))
                    && suivis.contains_key(&e.window)
                {
                    let (titre, _, _) = infos(e.window);
                    let s = suivis.get_mut(&e.window).unwrap();
                    if !titre.is_empty() && titre != s.titre {
                        s.titre = titre.clone();
                        tx.send(Evenement::Titre {
                            wid: e.window,
                            titre,
                        })?;
                    }
                }
            }
            _ => {}
        }
        let _ = &atomes.utf8;
        let _ = xproto::PropMode::REPLACE;
    }
}
