//! Tests sans écran : le modèle (identités, deltas, diff par lecteur, hors vue) et le rendu.

use crate::modele::{maintenant, Bureau, Fenetre, Lecture, Lu, SHELL};
use crate::regard::{page, Options};
use crate::x11::Geo;

fn lu(cle: &str, role: &str, nom: &str, x: i32, y: i32) -> Lu {
    Lu {
        cle_source: cle.into(),
        role: role.into(),
        role_atspi: role.into(),
        nom: nom.into(),
        texte: None,
        valeur: None,
        etats: vec![],
        x,
        y,
        w: 100,
        h: 30,
        parent_cle: None,
        profondeur: 1,
        enfants: 0,
        source: None,
    }
}

#[test]
fn positionnement_par_les_pixels() {
    use crate::pixels::{mal_positionne, positionner, Ligne};
    let zone = (0, 0, 2560, 1440);
    let mut els = vec![
        lu(":1.14/icone/1", "texte", "Dossier personnel", 0, 0),
        lu(
            ":1.14/icone/2",
            "texte",
            "Reunion_Vidal_24-09-2026_deroule.pdf",
            0,
            0,
        ),
    ];
    for e in els.iter_mut() {
        e.w = 2560;
        e.h = 1440;
    }
    let mal: Vec<usize> = els
        .iter()
        .enumerate()
        .filter(|(_, l)| mal_positionne(l, zone))
        .map(|(i, _)| i)
        .collect();
    assert_eq!(mal, vec![0, 1]);
    let lignes = vec![
        Ligne {
            texte: "Dossier personnel".into(),
            x: 2340,
            y: 120,
            w: 150,
            h: 18,
            confiance: 90.0,
        },
        Ligne {
            texte: "Reunion_Vidal_24-".into(),
            x: 2330,
            y: 260,
            w: 160,
            h: 18,
            confiance: 85.0,
        },
        Ligne {
            texte: "Corbeille".into(),
            x: 2350,
            y: 40,
            w: 80,
            h: 18,
            confiance: 92.0,
        },
    ];
    assert_eq!(positionner(&mut els, &lignes, &mal), 2);
    assert_eq!((els[0].x, els[0].y), (2340, 120));
    assert_eq!(els[0].source.as_deref(), Some("atspi+ocr"));
    assert_eq!(
        (els[1].x, els[1].y),
        (2330, 260),
        "le nom long est retrouvé par sa première ligne"
    );
}

#[test]
fn image_reduite_et_empreintes() {
    use crate::pixels::{ecart, redimensionner, vignette, Image};
    let (w, h) = (2560u32, 1440u32);
    let mut rgb = vec![0u8; (w * h * 3) as usize];
    // Un rectangle clair en haut à gauche sur fond noir.
    for y in 0..200 {
        for x in 0..400 {
            let k = ((y * w + x) * 3) as usize;
            rgb[k] = 230;
            rgb[k + 1] = 230;
            rgb[k + 2] = 230;
        }
    }
    let img = Image { w, h, rgb };
    let petite = redimensionner(&img, 1288);
    assert_eq!(petite.w % 28, 0);
    assert!(petite.w <= 1288 && petite.h < 800);
    let claire = vignette(&img, 0, 0, 400, 200);
    let sombre = vignette(&img, 1000, 1000, 400, 200);
    assert!(claire.iter().all(|v| *v > 200));
    assert!(ecart(&claire, &claire) == 0.0);
    assert!(
        ecart(&claire, &sombre) > 0.5,
        "une empreinte différente doit s'écarter"
    );
}

#[test]
fn captcha_signale_jamais_resolu() {
    let mut b = bureau_avec_fenetre();
    b.enregistrer(
        7,
        lecture(vec![lu(
            ":1.9/a/1",
            "case",
            "Je ne suis pas un robot",
            120,
            120,
        )]),
    );
    let d = b.fenetres[&7].deduction();
    assert_eq!(d["captcha"], true);
    assert!(d["attend"].as_str().unwrap().contains("CAPTCHA"));
}

fn lecture(els: Vec<Lu>) -> Lecture {
    Lecture {
        elements: Some(els),
        source: "atspi".into(),
        voie: "cache",
        tronquee: false,
        lecture_ms: 3,
        observe_a: maintenant(),
        lisible: true,
        scene: None,
    }
}

fn bureau_avec_fenetre() -> Bureau {
    let mut b = Bureau::nouveau();
    b.largeur = 2560;
    b.hauteur = 1440;
    let mut f = Fenetre::nouvelle(
        7,
        "Essai".into(),
        "appli".into(),
        Some(42),
        Some(Geo {
            x: 100,
            y: 100,
            l: 800,
            h: 600,
            marges: (0, 0, 0, 0),
        }),
    );
    f.active = true;
    b.fenetres.insert(7, f);
    b.active = 7;
    b.empilement = vec![7];
    b
}

#[test]
fn identite_stable_et_jamais_reattribuee() {
    let mut b = bureau_avec_fenetre();
    b.enregistrer(
        7,
        lecture(vec![
            lu(":1.9/a/1", "bouton", "Envoyer", 120, 120),
            lu(":1.9/a/2", "champ", "Nom", 120, 200),
        ]),
    );
    let ids: Vec<String> = b.fenetres[&7]
        .elements
        .iter()
        .map(|e| e.id.clone())
        .collect();
    assert_eq!(ids, vec!["e1", "e2"]);
    // Le bouton disparaît puis revient : même objet, même identité ; un nouvel objet reçoit e3.
    b.enregistrer(7, lecture(vec![lu(":1.9/a/2", "champ", "Nom", 120, 200)]));
    b.enregistrer(
        7,
        lecture(vec![
            lu(":1.9/a/1", "bouton", "Envoyer", 120, 120),
            lu(":1.9/a/3", "lien", "Aide", 120, 300),
        ]),
    );
    let ids: Vec<String> = b.fenetres[&7]
        .elements
        .iter()
        .map(|e| e.id.clone())
        .collect();
    assert_eq!(ids, vec!["e1", "e3"]);
}

#[test]
fn relecture_identique_ne_produit_aucun_delta() {
    let mut b = bureau_avec_fenetre();
    let d1 = b.enregistrer(
        7,
        lecture(vec![lu(":1.9/a/1", "bouton", "Envoyer", 120, 120)]),
    );
    assert!(d1.is_some());
    let rev = b.revision;
    std::thread::sleep(std::time::Duration::from_millis(5));
    let d2 = b.enregistrer(
        7,
        lecture(vec![lu(":1.9/a/1", "bouton", "Envoyer", 120, 120)]),
    );
    assert!(
        d2.is_none(),
        "une relecture identique ne doit pas publier de révision"
    );
    assert_eq!(b.revision, rev);
}

#[test]
fn delta_detaille_ajouts_modifications_retraits() {
    let mut b = bureau_avec_fenetre();
    b.enregistrer(
        7,
        lecture(vec![
            lu(":1.9/a/1", "bouton", "Envoyer", 120, 120),
            lu(":1.9/a/2", "champ", "Nom", 120, 200),
        ]),
    );
    let mut modifie = lu(":1.9/a/2", "champ", "Nom", 120, 200);
    modifie.texte = Some("Félix".into());
    let d = b
        .enregistrer(
            7,
            lecture(vec![modifie, lu(":1.9/a/3", "lien", "Aide", 120, 300)]),
        )
        .expect("delta");
    assert_eq!(d["type"], "fenetre");
    assert_eq!(d["ajoutes"].as_array().unwrap().len(), 1);
    assert_eq!(d["modifies"].as_array().unwrap().len(), 1);
    assert_eq!(
        d["retires"].as_array().unwrap(),
        &vec![serde_json::json!("e1")]
    );
    assert_eq!(
        d["precedent"].as_u64().unwrap() + 1,
        d["revision"].as_u64().unwrap()
    );
}

#[test]
fn hors_vue_quand_le_centre_sort_de_la_fenetre() {
    let mut b = bureau_avec_fenetre();
    b.enregistrer(
        7,
        lecture(vec![
            lu(":1.9/a/1", "bouton", "Visible", 120, 120),
            lu(":1.9/a/2", "bouton", "Défilé", 120, 900),
        ]),
    );
    let els = &b.fenetres[&7].elements;
    assert!(!els[0].hors_vue);
    assert!(
        els[1].hors_vue,
        "un élément sous le bord de la fenêtre est hors vue"
    );
}

#[test]
fn diff_par_lecteur_independant() {
    let mut b = bureau_avec_fenetre();
    b.enregistrer(
        7,
        lecture(vec![lu(":1.9/a/1", "bouton", "Envoyer", 120, 120)]),
    );
    assert!(b.diff("claude").premier);
    b.memoriser("claude");
    b.enregistrer(
        7,
        lecture(vec![
            lu(":1.9/a/1", "bouton", "Envoyer", 120, 120),
            lu(":1.9/a/2", "lien", "Aide", 120, 300),
        ]),
    );
    let dc = b.diff("claude");
    assert_eq!(dc.ajoutes.len(), 1);
    assert!(dc.retires.is_empty());
    // Codex n'a jamais regardé : pour lui, tout est nouveau ; la mémoire de Claude n'a pas bougé.
    assert!(b.diff("codex").premier);
    b.enregistrer(7, lecture(vec![lu(":1.9/a/2", "lien", "Aide", 120, 300)]));
    let dc = b.diff("claude");
    assert_eq!(dc.retires.len(), 1);
    assert_eq!(dc.retires[0].id, "e1");
}

#[test]
fn recouvrement_par_une_fenetre_au_dessus() {
    let mut b = bureau_avec_fenetre();
    let mut haut = Fenetre::nouvelle(
        9,
        "Dialogue".into(),
        "appli".into(),
        Some(42),
        Some(Geo {
            x: 300,
            y: 300,
            l: 200,
            h: 200,
            marges: (0, 0, 0, 0),
        }),
    );
    haut.visible = Some(true);
    b.fenetres.insert(9, haut);
    b.empilement = vec![7, 9];
    assert!(b.recouvert(7, 350, 350).is_some());
    assert!(b.recouvert(7, 150, 150).is_none());
    assert!(
        b.recouvert(9, 350, 350).is_none(),
        "la fenêtre du dessus n'est recouverte par personne"
    );
}

#[test]
fn page_marque_les_changements_et_les_references() {
    let mut b = bureau_avec_fenetre();
    b.enregistrer(
        7,
        lecture(vec![lu(":1.9/a/1", "bouton", "Envoyer", 120, 120)]),
    );
    b.memoriser("claude");
    let mut focus = lu(":1.9/a/2", "champ", "Nom", 120, 200);
    focus.etats = vec!["focus".into()];
    b.enregistrer(
        7,
        lecture(vec![lu(":1.9/a/1", "bouton", "Envoyer", 120, 120), focus]),
    );
    let p = page(
        &b,
        &Options {
            lecteur: "claude".into(),
            ..Default::default()
        },
    );
    assert!(p.contains("✚ 1 nouveaux"), "{p}");
    assert!(p.contains("✚ champ « Nom » (focus)  e2"), "{p}");
    assert!(p.contains("bouton « Envoyer »  e1"), "{p}");
    assert!(p.contains("attend une saisie dans « Nom »"), "{p}");
}

#[test]
fn budget_garde_le_focus_et_les_dialogues() {
    let mut b = bureau_avec_fenetre();
    let mut els: Vec<Lu> = (0..300)
        .map(|i| {
            lu(
                &format!(":1.9/t/{i}"),
                "texte",
                &format!("ligne {i}"),
                120,
                120 + i,
            )
        })
        .collect();
    let mut d = lu(
        ":1.9/d",
        "dialogue",
        "Enregistrer les modifications ?",
        200,
        200,
    );
    d.profondeur = 1;
    els.push(d);
    let mut f = lu(":1.9/f", "champ", "Réponse", 200, 240);
    f.etats = vec!["focus".into()];
    els.push(f);
    b.enregistrer(7, lecture(els));
    let p = page(
        &b,
        &Options {
            lecteur: "x".into(),
            lignes: 40,
            ..Default::default()
        },
    );
    assert!(
        p.contains("dialogue « Enregistrer les modifications ? »"),
        "{p}"
    );
    assert!(p.contains("champ « Réponse » (focus)"), "{p}");
    assert!(p.contains("non montrés"), "{p}");
    assert!(p.lines().count() < 70, "{}", p.lines().count());
}

#[test]
fn fenetre_muette_renvoie_au_secours() {
    let b = bureau_avec_fenetre();
    let p = page(
        &b,
        &Options {
            lecteur: "x".into(),
            ..Default::default()
        },
    );
    assert!(p.contains("muette"), "{p}");
    assert!(p.contains("ecran voir"), "{p}");
}

#[test]
fn shell_et_contexte_dans_l_instantane() {
    let mut b = bureau_avec_fenetre();
    b.fenetres.insert(
        SHELL,
        Fenetre::nouvelle(
            SHELL,
            "barre du haut".into(),
            "gnome-shell".into(),
            None,
            None,
        ),
    );
    b.programmer(7);
    assert_eq!(
        b.prochaine(),
        None,
        "une demande de moins de 100 ms n'est pas encore prête"
    );
    let inst = b.instantane(None);
    assert_eq!(inst["sans_capture"], true);
    assert_eq!(inst["contexte"]["fenetre_active"], "0x7");
    assert_eq!(inst["fenetres"][0]["wid"], "0x7", "l'active en premier");
}
