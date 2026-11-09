//! Le pont HTTP local (127.0.0.1 seulement, lecture seule sauf `/annonce`).
//!
//! - `GET /regard` : la page pour un agent (texte) ; `/regard.json` : la même, structurée.
//! - `GET /carte`, `/carte/cible?id=`, `/carte/flux` : le contrat qu'`ecran` connaît déjà.
//! - `POST /annonce` : une application déclare ce qu'elle affiche (TPE Tk…).
//! - `GET /sante` : état du service.

use std::collections::HashMap;
use std::convert::Infallible;
use std::time::Duration;

use axum::extract::{Query, State};
use axum::http::{header, StatusCode};
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use futures_util::stream::{self, Stream, StreamExt};
use serde_json::{json, Value};
use tokio::sync::{broadcast, mpsc, oneshot};

use std::sync::Arc;

use crate::arbre::Demande;
use crate::devtools::Devtools;
use crate::modele::{maintenant, Memoire};
use crate::regard::{self, Options};
use crate::Partage;

#[derive(Clone)]
pub struct Etat {
    pub partage: Partage,
    pub diffusion: broadcast::Sender<Value>,
    pub cibles: mpsc::Sender<Demande>,
    pub devtools: Arc<Devtools>,
    pub demarre: f64,
}

async fn texte_elem(State(e): State<Etat>, Query(q): Query<HashMap<String, String>>) -> Response {
    let Some(id) = q.get("id").cloned() else {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"erreur": "id manquant"})),
        )
            .into_response();
    };
    let (tx, rx) = oneshot::channel();
    if e.cibles
        .send(Demande::Texte { id, repondre: tx })
        .await
        .is_err()
    {
        return (
            StatusCode::CONFLICT,
            Json(json!({"erreur": "lecteur AT-SPI indisponible"})),
        )
            .into_response();
    }
    match tokio::time::timeout(Duration::from_secs(10), rx).await {
        Ok(Ok((code, v))) => (
            StatusCode::from_u16(code).unwrap_or(StatusCode::CONFLICT),
            Json(v),
        )
            .into_response(),
        _ => (
            StatusCode::CONFLICT,
            Json(json!({"erreur": "lecture du texte trop longue"})),
        )
            .into_response(),
    }
}

async fn oeil(State(e): State<Etat>, Query(q): Query<HashMap<String, String>>) -> Response {
    let (tx, rx) = oneshot::channel();
    let motif = q.get("fenetre").cloned().filter(|s| !s.is_empty());
    if e.cibles
        .send(Demande::Oeil {
            motif,
            repondre: tx,
        })
        .await
        .is_err()
    {
        return (
            StatusCode::CONFLICT,
            Json(json!({"erreur": "lecteur indisponible"})),
        )
            .into_response();
    }
    match tokio::time::timeout(Duration::from_secs(70), rx).await {
        Ok(Ok((code, v))) => (
            StatusCode::from_u16(code).unwrap_or(StatusCode::CONFLICT),
            Json(v),
        )
            .into_response(),
        _ => (
            StatusCode::CONFLICT,
            Json(json!({"erreur": "œil trop long"})),
        )
            .into_response(),
    }
}

async fn applis(State(e): State<Etat>) -> Response {
    let (tx, rx) = oneshot::channel();
    if e.cibles
        .send(Demande::Applis { repondre: tx })
        .await
        .is_err()
    {
        return (
            StatusCode::CONFLICT,
            Json(json!({"erreur": "lecteur AT-SPI indisponible"})),
        )
            .into_response();
    }
    match tokio::time::timeout(Duration::from_secs(15), rx).await {
        Ok(Ok(v)) => Json(v).into_response(),
        _ => (
            StatusCode::CONFLICT,
            Json(json!({"erreur": "diagnostic trop long"})),
        )
            .into_response(),
    }
}

fn texte(s: String) -> Response {
    ([(header::CONTENT_TYPE, "text/plain; charset=utf-8")], s).into_response()
}

async fn regard_page(State(e): State<Etat>, Query(q): Query<HashMap<String, String>>) -> Response {
    let o = options(&q);
    let memoriser = q.get("memoriser").map(|v| v != "0").unwrap_or(true);
    let mut b = e.partage.write().await;
    let page = regard::page(&b, &o);
    if memoriser {
        b.memoriser(&o.lecteur);
    }
    texte(page)
}

async fn regard_json(State(e): State<Etat>, Query(q): Query<HashMap<String, String>>) -> Response {
    let o = options(&q);
    let memoriser = q.get("memoriser").map(|v| v != "0").unwrap_or(true);
    let mut b = e.partage.write().await;
    let d = b.diff(&o.lecteur);
    let page = regard::page(&b, &o);
    let v = json!({
        "ts": maintenant(), "revision": b.revision, "session": b.session, "lecteur": o.lecteur,
        "page": page,
        "diff": {"premier": d.premier, "depuis": d.depuis, "ajoutes": d.ajoutes, "modifies": d.modifies,
                 "retires": d.retires.iter().map(|e| &e.id).collect::<Vec<_>>(),
                 "fenetres_ouvertes": d.fenetres_ouvertes, "fenetres_fermees": d.fenetres_fermees},
        "fenetre_active": b.fenetre_active().map(|f| f.to_json(maintenant())),
        "contexte": b.contexte(),
    });
    if memoriser {
        b.memoriser(&o.lecteur);
    }
    Json(v).into_response()
}

fn options(q: &HashMap<String, String>) -> Options {
    Options {
        lecteur: q
            .get("lecteur")
            .cloned()
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "anonyme".into()),
        fenetre: q.get("fenetre").cloned().filter(|s| !s.is_empty()),
        element: q.get("element").cloned().filter(|s| !s.is_empty()),
        tout: q.get("tout").map(|v| v == "1").unwrap_or(false),
        lignes: q.get("lignes").and_then(|v| v.parse().ok()).unwrap_or(0),
    }
}

async fn carte(State(e): State<Etat>, Query(q): Query<HashMap<String, String>>) -> Response {
    let b = e.partage.read().await;
    Json(b.instantane(q.get("fenetre").map(|s| s.as_str()))).into_response()
}

async fn cible(State(e): State<Etat>, Query(q): Query<HashMap<String, String>>) -> Response {
    let Some(id) = q.get("id").cloned() else {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"erreur": "id manquant"})),
        )
            .into_response();
    };
    let (tx, rx) = oneshot::channel();
    if e.cibles
        .send(Demande::Cible { id, repondre: tx })
        .await
        .is_err()
    {
        return (
            StatusCode::CONFLICT,
            Json(json!({"erreur": "lecteur AT-SPI indisponible"})),
        )
            .into_response();
    }
    match tokio::time::timeout(Duration::from_secs(8), rx).await {
        Ok(Ok((code, v))) => (
            StatusCode::from_u16(code).unwrap_or(StatusCode::CONFLICT),
            Json(v),
        )
            .into_response(),
        _ => (
            StatusCode::CONFLICT,
            Json(json!({"erreur": "vérification de la cible trop longue"})),
        )
            .into_response(),
    }
}

fn concerne(d: &Value, motif: &str) -> bool {
    if motif.is_empty() {
        return true;
    }
    let m = motif.to_lowercase();
    match d.get("type").and_then(|t| t.as_str()) {
        Some("fenetre") | Some("fermeture") => {
            let f = d.get("fenetre").cloned().unwrap_or(Value::Null);
            let t = f
                .get("titre")
                .and_then(|x| x.as_str())
                .unwrap_or("")
                .to_lowercase();
            let a = f
                .get("app")
                .and_then(|x| x.as_str())
                .unwrap_or("")
                .to_lowercase();
            t.contains(&m) || a.contains(&m)
        }
        _ => true,
    }
}

async fn flux(
    State(e): State<Etat>,
    Query(q): Query<HashMap<String, String>>,
) -> Sse<impl Stream<Item = Result<Event, Infallible>>> {
    let motif = q.get("fenetre").cloned().unwrap_or_default();
    let duree: Option<f64> = q
        .get("duree")
        .and_then(|v| v.parse().ok())
        .map(|d: f64| d.min(600.0));
    // Abonnement et instantané sous le même verrou : aucun delta perdu entre les deux.
    let (rx, premier) = {
        let b = e.partage.read().await;
        let rx = e.diffusion.subscribe();
        let mut inst = b.instantane(if motif.is_empty() { None } else { Some(&motif) });
        inst["type"] = json!("instantane");
        (rx, inst)
    };
    let m = motif.clone();
    let suite = stream::unfold(rx, move |mut rx| {
        let m = m.clone();
        async move {
            loop {
                match rx.recv().await {
                    Ok(v) => {
                        if concerne(&v, &m) {
                            return Some((
                                Ok::<Event, Infallible>(
                                    Event::default().event("changement").data(v.to_string()),
                                ),
                                rx,
                            ));
                        }
                    }
                    Err(broadcast::error::RecvError::Lagged(_)) => {
                        return Some((
                            Ok(Event::default()
                                .event("resync")
                                .data(json!({"type": "resync"}).to_string())),
                            rx,
                        ));
                    }
                    Err(broadcast::error::RecvError::Closed) => return None,
                }
            }
        }
    });
    let tout = stream::once(async move {
        Ok::<Event, Infallible>(
            Event::default()
                .event("instantane")
                .data(premier.to_string()),
        )
    })
    .chain(suite);
    let fin = tokio::time::sleep(Duration::from_secs_f64(duree.unwrap_or(1e9)));
    Sse::new(tout.take_until(fin)).keep_alive(KeepAlive::new().interval(Duration::from_secs(1)))
}

async fn annonce(State(e): State<Etat>, Json(v): Json<Value>) -> Response {
    let titre = v
        .get("titre")
        .and_then(|x| x.as_str())
        .unwrap_or("")
        .to_string();
    let app = v
        .get("app")
        .and_then(|x| x.as_str())
        .unwrap_or("")
        .to_string();
    if titre.is_empty() && app.is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"erreur": "titre ou app requis"})),
        )
            .into_response();
    }
    let mut b = e.partage.write().await;
    let mut a = v.clone();
    a["recue_a"] = json!(maintenant());
    if !titre.is_empty() {
        b.annonces.insert(titre.clone(), a.clone());
    }
    if !app.is_empty() {
        b.annonces.insert(app.clone(), a.clone());
    }
    let wids: Vec<u32> = b
        .fenetres
        .values()
        .filter(|f| (!titre.is_empty() && f.titre == titre) || (!app.is_empty() && f.app == app))
        .map(|f| f.wid)
        .collect();
    let mut deltas = vec![];
    for w in wids {
        if let Some(f) = b.fenetres.get_mut(&w) {
            if f.annonce.as_ref() != Some(&a) {
                f.annonce = Some(a.clone());
                if let Some(d) = b.publier_meta(w) {
                    deltas.push(d);
                }
            }
        }
    }
    drop(b);
    for d in deltas {
        let _ = e.diffusion.send(d);
    }
    Json(json!({"ok": true})).into_response()
}

async fn sante(State(e): State<Etat>) -> Response {
    let pages = e.devtools.pages_resume().await;
    let b = e.partage.read().await;
    let lecteurs: HashMap<&String, &Memoire> = b.lecteurs.iter().collect();
    Json(json!({
        "ok": true, "session": b.session, "revision": b.revision, "depuis_s": maintenant() - e.demarre,
        "fenetres": b.fenetres.len(), "a_relire": b.a_relire.len(),
        "devtools": {"ports": e.devtools.ports, "pages": pages},
        "elements": b.fenetres.values().map(|f| f.elements.len()).sum::<usize>(),
        "lecteurs": lecteurs.iter().map(|(k, m)| (k.to_string(), json!({"revision": m.revision, "instant": m.instant}))).collect::<HashMap<_, _>>(),
        "erreurs": b.erreurs,
    }))
    .into_response()
}

pub async fn servir(etat: Etat, port: u16) -> std::io::Result<()> {
    let app = Router::new()
        .route("/regard", get(regard_page))
        .route("/regard.json", get(regard_json))
        .route("/carte", get(carte))
        .route("/carte/cible", get(cible))
        .route("/carte/flux", get(flux))
        .route("/annonce", post(annonce))
        .route("/texte", get(texte_elem))
        .route("/oeil", post(oeil))
        .route("/applis", get(applis))
        .route("/sante", get(sante))
        .with_state(etat);
    let ecoute = tokio::net::TcpListener::bind(("127.0.0.1", port)).await?;
    axum::serve(ecoute, app).await
}
