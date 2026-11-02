"""veille.py sans écran : bus et rotation, audio (pactl), processus, GPU, machine « figé », seaux AT-SPI."""
import json
import os
import queue
import tempfile
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

import veille


class Bus(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.bus = veille.Bus(os.path.join(self.d, "veille.jsonl"))

    def test_ligne_json_et_champs_vides_omis(self):
        d = self.bus.emettre("gpu", "charge", "GPU : fort (70 %)", util=70, vide="", rien=None)
        ligne = json.loads(open(self.bus.fichier, encoding="utf-8").read().splitlines()[-1])
        self.assertEqual(ligne["msg"], "GPU : fort (70 %)")
        self.assertEqual(ligne["util"], 70)
        self.assertNotIn("vide", ligne)
        self.assertNotIn("rien", ligne)
        self.assertRegex(ligne["ts"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        self.assertEqual(d["src"], "gpu")

    def test_rotation_garde_les_dernieres_lignes(self):
        ancien = (veille.MAX_OCTETS, veille.GARDER)
        veille.MAX_OCTETS, veille.GARDER = 2000, 5
        try:
            for i in range(60):
                self.bus.emettre("proc", "plus", f"processus : + x{i}")
            self.assertTrue(self.bus.tourner())
            lignes = open(self.bus.fichier, encoding="utf-8").read().splitlines()
            self.assertEqual(len(lignes), 5)
            self.assertIn("x59", lignes[-1])
        finally:
            veille.MAX_OCTETS, veille.GARDER = ancien


PACTL = """Sink Input #57
\tDriver: PipeWire
\tCorked: no
\tProperties:
\t\tapplication.name = "Firefox"
\t\tmedia.name = "AudioStream"
Sink Input #58
\tCorked: yes
\tProperties:
\t\tmedia.name = "audio stream #1"
\t\tapplication.name = "Hogwarts Legacy"
"""


class AudioTest(unittest.TestCase):
    def test_parse_pactl(self):
        b = veille.parse_pactl(PACTL)
        self.assertEqual([(x["index"], x["app"], x["media"], x["corked"]) for x in b],
                         [(57, "Firefox", "AudioStream", False), (58, "Hogwarts Legacy", "audio stream #1", True)])

    def test_sans_nom_d_application_le_binaire_puis_le_flux(self):
        texte = 'Source Output #9\n\tCorked: no\n\tProperties:\n\t\tapplication.process.binary = "parec"\n' \
                'Source Output #10\n\tCorked: no\n\tProperties:\n\t\tmedia.name = "Monitor"\n'
        self.assertEqual(sorted(veille.etat_audio(veille.parse_pactl(texte))),
                         [("Monitor", "Monitor", 10), ("parec", "", 9)])

    def test_diff_audio(self):
        avant = veille.etat_audio(veille.parse_pactl(PACTL))
        apres = dict(avant)
        apres[("Firefox", "AudioStream", 57)] = True          # pause
        apres[("Hogwarts Legacy", "audio stream #1", 58)] = False  # reprise
        apres[("Spotify", "Nova", 60)] = False                # nouveau flux qui joue
        del apres[("Firefox", "AudioStream", 57)]
        apres[("Firefox", "AudioStream", 57)] = True
        evts = veille.diff_audio(avant, apres)
        self.assertIn(("pause", "Firefox", "AudioStream"), evts)
        self.assertIn(("reprise", "Hogwarts Legacy", "audio stream #1"), evts)
        self.assertIn(("joue", "Spotify", "Nova"), evts)
        self.assertEqual(veille.diff_audio(apres, avant), [("reprise", "Firefox", "AudioStream"),
                                                           ("pause", "Hogwarts Legacy", "audio stream #1"),
                                                           ("fin", "Spotify", "Nova")])

    def test_annonce_regroupe_par_application(self):
        bus = veille.Bus(os.path.join(tempfile.mkdtemp(), "v.jsonl"))
        a = veille.Audio(bus)
        a.annoncer([("joue", "Hogwarts Legacy", "audio stream #1"), ("joue", "Hogwarts Legacy", "audio stream #2"),
                    ("fin", "Firefox", "AudioStream")], veille.Audio.MSG, "audio")
        lignes = [json.loads(l) for l in open(bus.fichier, encoding="utf-8")]
        self.assertEqual([l["msg"] for l in lignes], ["audio : Hogwarts Legacy joue (2 flux)", "audio : fin Firefox « AudioStream »"])


class ProcessusTest(unittest.TestCase):
    def setUp(self):
        self.bus = veille.Bus(os.path.join(tempfile.mkdtemp(), "v.jsonl"))
        self.releves = [{}]
        self.p = veille.Processus(self.bus, lire=lambda: dict(self.releves[0]), periode=2)

    def test_naissance_au_second_releve_mort_avec_duree(self):
        self.releves[:] = [{1: ("systemd", 0), 500: ("steam", 1)}]
        self.p = veille.Processus(self.bus, lire=lambda: dict(self.releves[0]), periode=2)
        self.releves[0][600] = ("HogwartsLegacy.", 500)
        self.releves[0][601] = ("grep", 500)          # utilitaire : jamais annoncé
        self.releves[0][602] = ("kworker/0:1", 2)     # fil du noyau : jamais annoncé
        self.assertEqual(self.p.pas(), ([], []))      # 1er relevé : candidat seulement
        n, m = self.p.pas()
        self.assertEqual(n, [(600, "HogwartsLegacy.", "steam")])
        self.assertEqual(m, [])
        del self.releves[0][600]
        del self.releves[0][601]
        n, m = self.p.pas()
        self.assertEqual((n, m), ([], [(600, "HogwartsLegacy.", 4)]))

    def test_mort_d_un_processus_present_au_demarrage(self):
        self.releves[:] = [{1: ("systemd", 0), 500: ("steam", 1)}]
        self.p = veille.Processus(self.bus, lire=lambda: dict(self.releves[0]), periode=2)
        del self.releves[0][500]
        n, m = self.p.pas()
        self.assertEqual((n, m), ([], [(500, "steam", None)]))
        self.p.annoncer(n, m)
        ligne = json.loads(open(self.bus.fichier, encoding="utf-8").read().splitlines()[-1])
        self.assertEqual(ligne["msg"], "processus : − steam (pid 500, présent au démarrage)")

    def test_annonce_resume_au_dela_de_six(self):
        self.p.annoncer([(i, "node", "code") for i in range(9)], [])
        ligne = json.loads(open(self.bus.fichier, encoding="utf-8").read().splitlines()[-1])
        self.assertEqual(ligne["msg"], "processus : + 9 (node ×9)")
        self.assertEqual(ligne["n"], 9)


class GpuTest(unittest.TestCase):
    def test_bandes(self):
        self.assertEqual([veille.bande_gpu(u) for u in (0, 9, 10, 59, 60, 89, 90, 100)],
                         ["repos", "repos", "actif", "actif", "fort", "fort", "saturé", "saturé"])


class EcranTest(unittest.TestCase):
    def test_etats_confirmes_par_deux_releves_et_fige(self):
        m = veille.MachineEcran()
        t = 1000.0
        self.assertEqual(m.observer(0.0, t), [("etat", "calme", 0.0)])
        self.assertEqual(m.observer(60.0, t + 2), [])                       # 1er relevé vidéo : candidat
        self.assertEqual(m.observer(55.0, t + 4), [("etat", "vidéo", 55.0)])  # confirmé
        self.assertEqual(m.observer(0.0, t + 6), [])
        for i in range(1, 15):
            evts = m.observer(0.0, t + 6 + 2 * i)
            if i == 1:
                self.assertEqual(evts, [("etat", "calme", 0.0)])
            elif t + 6 + 2 * i - (t + 4) >= veille.FIGE_APRES:
                break
            else:
                self.assertEqual(evts, [])
        self.assertEqual(evts, [("fige", 30, True)])                         # « vidéo » juste avant : suspect
        self.assertEqual(m.observer(0.0, t + 40), [])                        # pas de rappel avant RAPPEL_FIGE
        self.assertEqual(m.observer(3.0, t + 42), [("reprise", 3.0)])
        self.assertEqual(m.observer(3.0, t + 44), [("etat", "activité", 3.0)])

    def test_bureau_immobile_n_est_pas_fige(self):
        m = veille.MachineEcran()
        m.observer(0.0, 0.0)
        self.assertEqual(m.observer(0.0, 31.0), [("fige", 31, False)])
        m2 = veille.MachineEcran()
        m2.observer(0.0, 0.0)
        self.assertEqual(m2.observer(0.0, 31.0, indices_jeu=True), [("fige", 31, True)])

    def test_sans_damage_et_images_desactivees_ne_capture_jamais(self):
        with patch.object(veille, "IMAGES_AUTORISEES", False):
            for dessin in (None, SimpleNamespace(actif=False)):
                with self.subTest(damage_present=dessin is not None):
                    bus = Mock()
                    ecran = veille.Ecran(bus, dessin=dessin)
                    ecran.mesurer = Mock(side_effect=AssertionError("capture interdite"))
                    self.assertTrue(ecran.tic())
                    self.assertTrue(ecran.tic())
                    ecran.mesurer.assert_not_called()
                    bus.emettre.assert_called_once()
                    self.assertEqual(bus.emettre.call_args.args[:2], ("ecran", "indisponible"))


class DessinTest(unittest.TestCase):
    def test_transitions_d_une_fenetre(self):
        a = veille.AgregeDessin()
        a.noter(7, 10, 10, 100, 20, 0.1)
        a.noter(7, 50, 15, 100, 20, 0.2)
        self.assertEqual(a.vider(0.25), ([(7, "debut", {"bbox": (10, 10, 150, 35), "n": 2})], 140 * 25))
        self.assertEqual(a.vider(0.5), ([], 0))                              # rien redessiné : silence
        a.noter(7, 0, 0, 10, 10, 0.6)
        self.assertEqual(a.vider(0.75), ([(7, "zone", {"bbox": (0, 0, 10, 10), "n": 1})], 100))   # lent : la zone est lisible
        self.assertEqual(a.vider(2.0), ([], 0))
        self.assertEqual(a.vider(2.7), ([(7, "arret", {"etait": "actif", "duree": 2})], 0))
        self.assertEqual(a.vider(3.0), ([], 0))

    def test_video_en_continu_puis_arret(self):
        a = veille.AgregeDessin()
        t = 0.0
        sorties = []
        for pas in range(12):                                                 # 3 s à 16 redessins par 250 ms
            t = 0.25 * (pas + 1)
            for _ in range(16):
                a.noter(9, 0, 0, 1920, 1080, t - 0.01)
            s, aire = a.vider(t)
            self.assertEqual(aire, 1920 * 1080)                               # l'aire d'une période : la boîte, pas 16 fois la boîte
            sorties += s
        genres = [g for _, g, _ in sorties]
        self.assertEqual(genres[0], "debut")
        self.assertEqual(genres.count("continu"), 1)                          # annoncé une fois, rappel à 30 s
        self.assertNotIn("zone", genres)
        self.assertEqual(a.vider(t + 2.5), ([(9, "arret", {"etait": "continu", "duree": 5})], 0))

    def test_pct_recent_depuis_les_zones(self):
        bus = veille.Bus(os.path.join(tempfile.mkdtemp(), "v.jsonl"))
        d = veille.Dessin(bus)
        now = __import__("time").monotonic()
        d.aires = [(now - 10, 10 ** 9), (now - 0.5, 2560 * 1440 // 4), (now - 0.2, 2560 * 1440 // 2)]
        self.assertAlmostEqual(d.pct_recent(2.0), 50.0, places=3)             # la pire période des 2 dernières secondes
        d.aires = [(now, 10 ** 9)]
        self.assertEqual(d.pct_recent(2.0), 100.0)
        bus.emettre("dessin", "continu", "en continu : firefox « vidéo »", app="firefox", titre="vidéo", wid="0x9", taux=60)
        bus.emettre("dessin", "zone", "redessine : charli.py « Charli »", app="charli.py", titre="Charli", wid="0x7")
        self.assertEqual(bus.etat.instantane()["dessin"]["0x9"]["etat"], "continu")
        bus.emettre("dessin", "arret", "s'arrête : firefox", app="firefox", titre="vidéo", wid="0x9", duree=5, etait="continu")
        self.assertEqual(sorted(bus.etat.instantane()["dessin"]), ["0x7"])


class CarteTest(unittest.TestCase):
    ELS = [{"role": "liste", "role_atspi": "list", "nom": "Panneau latéral", "texte": "", "x": 10, "y": 50, "w": 200, "h": 900, "etats": []},
           {"role": "champ", "role_atspi": "entry", "nom": "Rechercher", "texte": "ordon", "x": 300, "y": 60, "w": 400, "h": 30, "etats": ["focus"]},
           {"role": "bouton", "role_atspi": "push button", "nom": "Ouvrir", "texte": "", "x": 720, "y": 60, "w": 80, "h": 30, "etats": []}]

    def test_deduire(self):
        d = veille.deduire("org.gnome.Nautilus", "Dossier personnel", self.ELS)
        self.assertEqual(d["genre"], "gestionnaire de fichiers")
        self.assertEqual(d["attend"], "une saisie dans « Rechercher » (contient « ordon »)")
        self.assertEqual(d["focus"], "champ « Rechercher » = ordon [focus]")
        self.assertEqual(d["resume"], "gestionnaire de fichiers, 3 éléments, 1 champ, boutons : Ouvrir ; attend une saisie dans « Rechercher » (contient « ordon »)")
        self.assertEqual(veille.deduire("steam", "Steam Settings", None)["genre"], "sans arbre lisible (Steam, jeu, vidéo, ou Chrome/Electron en arrière-plan)")
        self.assertEqual(veille.deduire("Google-chrome", "CNDA", [{"role": "page", "role_atspi": "document web", "etats": []}])["genre"], "navigateur")
        self.assertEqual(veille.deduire("x", "y", [{"role": "dialogue", "role_atspi": "dialog", "nom": "Enregistrer ?", "etats": []}])["attend"],
                         "une réponse : « Enregistrer ? »")

    def test_carte_tenue_a_jour_sur_evenement(self):
        bus = veille.Bus(os.path.join(tempfile.mkdtemp(), "v.jsonl"))

        class FauxLecteur:
            def elements_fenetre(self, wid, delai=0.5):
                return list(CarteTest.ELS) if wid == 0x42 else None

        class FauxFenetres:
            noms = {0x42: ("Dossier personnel", "org.gnome.Nautilus"), 0x43: ("Steam Settings", "steam")}
            geometries = {0x42: (43, 32, 1233, 1085)}
            actif = 0x42

        c = veille.Carte(bus, FauxLecteur(), FauxFenetres())
        bus.declencheurs.append(c.sur_evenement)
        bus.emettre("fenetre", "ouverte", "…", app="org.gnome.Nautilus", titre="Dossier personnel", pid=1, wid="0x42")
        bus.emettre("fenetre", "ouverte", "…", app="steam", titre="Steam Settings", pid=2, wid="0x43")
        self.assertEqual(sorted(c.a_rafraichir), [0x42, 0x43])
        c.a_rafraichir = {w: 0.0 for w in c.a_rafraichir}               # assez vieux pour être relus
        c.tic()
        d = c.instantane()
        self.assertEqual([f["wid"] for f in d["fenetres"]], ["0x42", "0x43"])   # l'active d'abord
        f = d["fenetres"][0]
        self.assertTrue(f["active"] and f["arbre"])
        self.assertEqual((f["x"], f["y"], f["l"], f["h"]), (43, 32, 1233, 1085))
        self.assertEqual(f["elements"][1]["nom"], "Rechercher")
        self.assertEqual(d["fenetres"][1]["deduction"]["genre"], "sans arbre lisible (Steam, jeu, vidéo, ou Chrome/Electron en arrière-plan)")
        self.assertEqual(bus.etat.instantane()["fenetres"]["0x42"]["deduction"][:33], "gestionnaire de fichiers, 3 éléme")
        self.assertEqual(len(c.instantane("steam")["fenetres"]), 1)
        bus.emettre("fenetre", "fermee", "…", wid="0x43")
        self.assertEqual(len(c.instantane()["fenetres"]), 1)
        bus.emettre("dessin", "zone", "…", app="org.gnome.Nautilus", titre="Dossier personnel", wid="0x42")
        self.assertIn(0x42, c.a_rafraichir)

    def test_arriere_plan_garde_la_derniere_lecture_et_annonce_d_une_appli_muette(self):
        import http.client
        bus = veille.Bus(os.path.join(tempfile.mkdtemp(), "v.jsonl"))
        lectures = {0x42: list(CarteTest.ELS)}

        class FauxLecteur:
            def elements_fenetre(self, wid, delai=0.5):
                return lectures.get(wid)

        class FauxFenetres:
            noms = {0x42: ("CNDA - Google Chrome", "Google-chrome"), 0x43: ("TPE — PharmIOS (simulateur)", "Tk")}
            geometries = {}
            actif = 0x42

        c = veille.Carte(bus, FauxLecteur(), FauxFenetres())
        pont = veille.Pont(bus, port=0, carte=c)
        pont.start()
        c.rafraichir(0x42)
        lectures[0x42] = None                                              # Chrome passe en arrière-plan : arbre vide
        c.rafraichir(0x42)
        f = c.instantane()["fenetres"][0]
        self.assertTrue(f["perime"])
        self.assertEqual(len(f["elements"]), 3)
        self.assertIn("en arrière-plan)", f["deduction"]["resume"])
        conn = http.client.HTTPConnection("127.0.0.1", pont.port, timeout=5)
        corps = json.dumps({"app": "tpe_simule", "titre": "TPE — PharmIOS (simulateur)", "lignes": ["MONTANT", "12,50 EUR", "", "INSEREZ OU", "PRESENTEZ CARTE"],
                            "etat": {"etape": "carte", "montant_cts": 1250}})
        conn.request("POST", "/annonce", body=corps, headers={"Content-Type": "application/json"})
        self.assertEqual(json.loads(conn.getresponse().read()), {"ok": True, "change": True})
        conn.request("POST", "/annonce", body=corps, headers={"Content-Type": "application/json"})
        self.assertEqual(json.loads(conn.getresponse().read()), {"ok": True, "change": False})    # identique : pas de nouvel événement
        self.assertEqual([l["ev"] for l in map(json.loads, open(bus.fichier, encoding="utf-8")) if l["src"] == "annonce"], ["affiche"])
        c.rafraichir(0x43)
        tpe = [x for x in c.instantane()["fenetres"] if x["wid"] == "0x43"][0]
        self.assertEqual(tpe["annonce"]["lignes"][1], "12,50 EUR")
        self.assertIn("déclare : MONTANT / 12,50 EUR / INSEREZ OU / PRESENTEZ CARTE", tpe["deduction"]["resume"])
        pont.serveur.shutdown()


class CarteVivanteTest(unittest.TestCase):
    """Contrat client : suivre et reconstruire le bureau sans image ni identités fictives."""

    def setUp(self):
        repertoire = tempfile.TemporaryDirectory()
        self.addCleanup(repertoire.cleanup)
        self.bus = veille.Bus(os.path.join(repertoire.name, "v.jsonl"))
        self.lectures = {0x42: [self.element("champ", "Recherche", "12"),
                                self.element("bouton", "Ouvrir", x=220)]}
        self.lecteur = SimpleNamespace(elements_fenetre=lambda wid, delai=1: deepcopy(self.lectures.get(wid)),
                                       verifier_cible=Mock(return_value=True))
        self.fenetres = SimpleNamespace(noms={0x42: ("Document test", "editeur-test")},
                                        geometries={0x42: (20, 30, 800, 600)}, actif=0x42)
        self.carte = veille.Carte(self.bus, self.lecteur, self.fenetres)
        self.bus.declencheurs.append(self.carte.sur_evenement)

    @staticmethod
    def element(cle, nom, texte="", x=40):
        return {"cle_source": "atspi:bus-test:/objet/" + cle, "source": "atspi",
                "role": "champ" if cle == "champ" else "bouton",
                "role_atspi": "entry" if cle == "champ" else "push button",
                "nom": nom, "texte": texte, "x": x, "y": 80, "w": 120, "h": 30,
                "etats": ["actif"]}

    def abonner(self, motif=None):
        q, instantane = self.carte.abonner(motif)
        self.addCleanup(self.carte.desabonner, q)
        return q, instantane

    @staticmethod
    def drainer(q):
        evts = []
        while True:
            try:
                evts.append(q.get_nowait())
            except queue.Empty:
                return evts

    def fenetre(self):
        return next(f for f in self.carte.instantane()["fenetres"] if f["wid"] == "0x42")

    def demarrer_pont(self):
        oeil = SimpleNamespace(regarder_maintenant=Mock(side_effect=AssertionError("aucune image nécessaire")))
        pont = veille.Pont(self.bus, port=0, carte=self.carte, oeil=oeil)
        pont.start()

        def fermer():
            pont.serveur.shutdown()
            pont.serveur.server_close()
            pont.join(timeout=2)

        self.addCleanup(fermer)
        return pont, oeil

    def test_identite_native_stable_malgre_renommage_valeur_deplacement_et_reordre(self):
        self.carte.rafraichir(0x42)
        initial = self.fenetre()["elements"]
        ids = {e["nom"]: e["id"] for e in initial}
        self.assertEqual(len(set(ids.values())), 2)
        self.assertTrue(all(e["identite"] == "native" for e in initial))
        q, snapshot = self.abonner()
        self.assertTrue(snapshot["session"])
        self.assertIsInstance(snapshot["revision"], int)
        self.assertIn("contexte", snapshot)

        self.lectures[0x42] = [self.element("bouton", "Enregistrer", x=350),
                               self.element("champ", "Résultat", "42", x=50)]
        self.carte.rafraichir(0x42)
        delta = self.drainer(q)[-1]
        self.assertEqual(delta["type"], "fenetre")
        self.assertEqual(delta["session"], snapshot["session"])
        self.assertEqual(delta["precedent"], snapshot["revision"])
        self.assertGreater(delta["revision"], delta["precedent"])
        self.assertEqual(delta["ajoutes"], [])
        self.assertEqual(delta["retires"], [])
        self.assertEqual(delta["ordre"], [ids["Ouvrir"], ids["Recherche"]])
        modifications = {e["id"]: e for e in delta["modifies"]}
        self.assertEqual(modifications[ids["Recherche"]]["texte"], "42")
        self.assertEqual(modifications[ids["Ouvrir"]]["nom"], "Enregistrer")
        self.assertEqual(modifications[ids["Ouvrir"]]["x"], 350)
        self.assertNotIn("elements", delta["fenetre"])
        self.assertIn("ts", delta)

    def test_snapshot_et_deltas_reconstruisent_ajouts_retraits_ordre_et_fermeture(self):
        self.carte.rafraichir(0x42)
        q, initial = self.abonner()
        reconstruction = {f["wid"]: deepcopy(f) for f in initial["fenetres"]}
        revision = initial["revision"]
        self.lectures[0x42] = [self.element("annuler", "Annuler", x=500),
                               self.element("champ", "Recherche", "42")]
        self.carte.rafraichir(0x42)
        for delta in self.drainer(q):
            self.assertEqual(delta["precedent"], revision)
            revision = delta["revision"]
            self.assertEqual(delta["type"], "fenetre")
            avant = reconstruction.get(delta["wid"], {"elements": []})
            elements = {e["id"]: e for e in avant["elements"]}
            for identifiant in delta["retires"]:
                del elements[identifiant]
            for element in delta["ajoutes"] + delta["modifies"]:
                elements[element["id"]] = element
            reconstruction[delta["wid"]] = dict(delta["fenetre"],
                                                elements=[elements[i] for i in delta["ordre"]])
        actualise = self.fenetre()
        self.assertEqual(reconstruction["0x42"]["elements"], actualise["elements"])
        for cle in ("app", "titre", "x", "y", "l", "h", "perime", "active"):
            self.assertEqual(reconstruction["0x42"][cle], actualise[cle], cle)
        self.assertEqual(revision, self.carte.instantane()["revision"])
        self.bus.emettre("fenetre", "fermee", "fermée", wid="0x42")
        fermeture = self.drainer(q)[-1]
        self.assertEqual(fermeture["type"], "fermeture")
        self.assertEqual(fermeture["wid"], "0x42")
        self.assertEqual(fermeture["precedent"], revision)
        reconstruction.pop(fermeture["wid"])
        self.assertEqual(list(reconstruction.values()), self.carte.instantane()["fenetres"])

    def test_abonnement_snapshot_independant_et_desabonnement(self):
        self.carte.rafraichir(0x42)
        q, snapshot = self.abonner()
        snapshot["fenetres"][0]["elements"][0]["nom"] = "modifié par le client"
        self.assertEqual(self.fenetre()["elements"][0]["nom"], "Recherche")
        self.carte.desabonner(q)
        self.lectures[0x42][0]["texte"] = "42"
        self.carte.rafraichir(0x42)
        self.assertTrue(q.empty())

    def test_abonnement_pendant_une_lecture_ne_perd_pas_la_mise_a_jour(self):
        import threading
        self.carte.rafraichir(0x42)
        lecture_commencee, autoriser_lecture = threading.Event(), threading.Event()
        self.addCleanup(autoriser_lecture.set)

        def lecteur(wid, delai=1):
            lecture_commencee.set()
            if not autoriser_lecture.wait(timeout=3):
                raise AssertionError("la lecture simulée n'a pas été débloquée")
            return [self.element("champ", "Recherche", "42")]

        self.carte.lecteur = SimpleNamespace(elements_fenetre=lecteur)
        fil = threading.Thread(target=self.carte.rafraichir, args=(0x42,), daemon=True)
        fil.start()
        self.assertTrue(lecture_commencee.wait(timeout=2))
        q, snapshot = self.abonner()
        autoriser_lecture.set()
        fil.join(timeout=2)
        self.assertFalse(fil.is_alive())
        self.assertEqual(snapshot["fenetres"][0]["elements"][0]["texte"], "12")
        delta = self.drainer(q)[-1]
        self.assertEqual(delta["precedent"], snapshot["revision"])
        self.assertEqual(delta["modifies"][0]["texte"], "42")
        self.assertEqual(delta["revision"], self.carte.instantane()["revision"])

    def test_abonnement_filtre_ne_livre_que_la_fenetre_demandee(self):
        import http.client
        self.fenetres.noms[0x43] = ("Autre fenêtre", "autre-test")
        self.lectures[0x43] = [self.element("champ", "Autre", "0")]
        self.carte.rafraichir(0x42)
        self.carte.rafraichir(0x43)
        pont, _ = self.demarrer_pont()
        connexion = http.client.HTTPConnection("127.0.0.1", pont.port, timeout=3)
        self.addCleanup(connexion.close)
        connexion.request("GET", "/carte/flux?fenetre=DOCUMENT&duree=2")
        reponse = connexion.getresponse()
        self.addCleanup(reponse.close)
        _, snapshot = self.lire_sse(reponse)
        self.assertEqual([f["wid"] for f in snapshot["fenetres"]], ["0x42"])
        self.lectures[0x43][0]["texte"] = "1"
        self.carte.rafraichir(0x43)
        self.lectures[0x42][0]["texte"] = "42"
        self.carte.rafraichir(0x42)
        evenement, delta = self.lire_sse(reponse)
        self.assertEqual(evenement, "changement")
        self.assertEqual(delta["wid"], "0x42")
        self.assertEqual(delta["precedent"], snapshot["revision"])

        self.bus.emettre("fenetre", "fermee", "autre fermée", wid="0x43")
        self.lectures[0x42][0]["texte"] = "43"
        self.carte.rafraichir(0x42)
        _, suivant = self.lire_sse(reponse)
        self.assertEqual(suivant["type"], "fenetre")
        self.assertEqual(suivant["wid"], "0x42")
        self.assertEqual(suivant["precedent"], delta["revision"])

    def test_arbre_perdu_conserve_donnees_mais_les_marque_perimees(self):
        self.carte.rafraichir(0x42)
        avant = self.fenetre()
        self.lectures[0x42] = None
        self.carte.rafraichir(0x42)
        apres = self.fenetre()
        self.assertEqual(apres["elements"], avant["elements"])
        self.assertTrue(apres["perime"])
        self.assertEqual(apres["fraicheur"]["etat"], "perime")
        self.assertEqual(apres["fraicheur"]["observe_a"], avant["fraicheur"]["observe_a"])

    def test_identite_sans_reference_native_est_explicitement_estimee(self):
        self.lectures[0x42][0].pop("cle_source")
        self.lectures[0x42][1]["cle_source"] = "atspi:bus-test:structure:0/1"
        self.carte.rafraichir(0x42)
        elements = self.fenetre()["elements"]
        self.assertTrue(all(e["id"] for e in elements))
        self.assertEqual([e["identite"] for e in elements], ["estimee", "estimee"])

    def test_atspi_texte_focus_structure_et_valeur_replanifient_sans_damage(self):
        self.carte.rafraichir(0x42)
        accessibilite = veille.Accessibilite(self.bus)
        accessibilite.Atspi = SimpleNamespace(StateType=SimpleNamespace(EDITABLE=1))
        application = SimpleNamespace(get_name=lambda: "editeur-test")
        source = SimpleNamespace(get_process_id=lambda: 123, get_application=lambda: application,
                                 get_role_name=lambda: "label", get_name=lambda: "Résultat",
                                 get_state_set=lambda: SimpleNamespace(contains=lambda _: False))
        for evenement in ("object:text-changed:insert", "object:state-changed:focused",
                          "object:children-changed:remove", "object:property-change:accessible-value",
                          "object:bounds-changed"):
            with self.subTest(evenement=evenement):
                self.carte.a_rafraichir.clear()
                accessibilite.traiter(SimpleNamespace(type=evenement, source=source, detail1=1,
                                                      detail2=2, any_data="42"))
                accessibilite._vider_modifications()
                self.assertIn(0x42, self.carte.a_rafraichir)

    def test_contexte_stop_est_diffuse_sans_relire_ni_image(self):
        self.carte.rafraichir(0x42)
        q, initial = self.abonner()
        self.bus.emettre("pilote", "stop", "arrêt humain", stop="clic de Félix")
        delta = self.drainer(q)[-1]
        self.assertEqual(delta["type"], "contexte")
        self.assertEqual(delta["precedent"], initial["revision"])
        self.assertEqual(delta["contexte"]["pilote"]["stop"], "clic de Félix")
        self.assertEqual(self.carte.instantane()["contexte"]["pilote"]["stop"], "clic de Félix")

    def test_debordement_signale_resynchronisation_au_lieu_de_perdre_silencieusement(self):
        self.carte.rafraichir(0x42)
        q, _ = self.abonner()
        self.assertGreater(q.maxsize, 0)
        for i in range(q.maxsize + 1):
            self.lectures[0x42][0]["texte"] = str(i)
            self.carte.rafraichir(0x42)
        evts = self.drainer(q)
        self.assertTrue(any(e["type"] == "resync" for e in evts), evts[:2])

    @staticmethod
    def lire_sse(reponse):
        """Lit un événement SSE complet, en tolérant commentaires et heartbeat."""
        evenement, donnees = None, []
        while True:
            ligne = reponse.fp.readline().decode("utf-8")
            if not ligne:
                raise AssertionError("flux fermé avant réception de l'événement")
            if ligne in ("\n", "\r\n"):
                if donnees:
                    return evenement, json.loads("\n".join(donnees))
                continue
            if ligne.startswith("event:"):
                evenement = ligne[6:].strip()
            elif ligne.startswith("data:"):
                donnees.append(ligne[5:].strip())

    def test_flux_http_snapshot_puis_delta_sans_oeil(self):
        import http.client
        self.carte.rafraichir(0x42)
        pont, oeil = self.demarrer_pont()
        connexion = http.client.HTTPConnection("127.0.0.1", pont.port, timeout=3)
        self.addCleanup(connexion.close)
        connexion.request("GET", "/carte/flux?duree=1")
        reponse = connexion.getresponse()
        self.addCleanup(reponse.close)
        self.assertEqual(reponse.status, 200)
        self.assertEqual(reponse.getheader("Content-Type"), "text/event-stream; charset=utf-8")
        evenement, snapshot = self.lire_sse(reponse)
        self.assertEqual(evenement, "instantane")
        self.assertEqual(snapshot["fenetres"][0]["elements"][0]["texte"], "12")
        self.lectures[0x42][0]["texte"] = "42"
        self.carte.rafraichir(0x42)
        evenement, delta = self.lire_sse(reponse)
        self.assertEqual(evenement, "changement")
        self.assertEqual(delta["precedent"], snapshot["revision"])
        self.assertEqual(delta["modifies"][0]["texte"], "42")
        reste = reponse.read()                 # le flux borné se termine, sans lecture d'écran
        self.assertTrue(all(not l or l.startswith(b":") for l in reste.splitlines()))
        oeil.regarder_maintenant.assert_not_called()

    def test_flux_filtre_renommage_entrant_fournit_tous_les_elements(self):
        import http.client
        self.carte.rafraichir(0x42)
        pont, _ = self.demarrer_pont()
        connexion = http.client.HTTPConnection("127.0.0.1", pont.port, timeout=3)
        self.addCleanup(connexion.close)
        connexion.request("GET", "/carte/flux?fenetre=CIBLE&duree=2")
        reponse = connexion.getresponse()
        self.addCleanup(reponse.close)
        _, initial = self.lire_sse(reponse)
        self.assertEqual(initial["fenetres"], [])
        self.fenetres.noms[0x42] = ("Cible renommée", "editeur-test")
        self.carte.rafraichir(0x42)
        evenement, snapshot = self.lire_sse(reponse)
        self.assertEqual(evenement, "instantane")
        self.assertTrue(snapshot["resynchronise"])
        self.assertEqual([e["nom"] for e in snapshot["fenetres"][0]["elements"]], ["Recherche", "Ouvrir"])
        self.assertEqual(snapshot["revision"], self.carte.instantane()["revision"])

    def test_flux_resynchronise_apres_debordement_et_reprend_la_chaine(self):
        import http.client
        self.carte.rafraichir(0x42)
        abonner = self.carte.abonner
        abonnements = []

        def abonnement(motif=None):
            q, snapshot = abonner(motif)
            abonnements.append(q)
            if len(abonnements) == 1:
                q.put_nowait({"type": "resync"})
            return q, snapshot

        pont, _ = self.demarrer_pont()
        connexion = http.client.HTTPConnection("127.0.0.1", pont.port, timeout=3)
        self.addCleanup(connexion.close)
        with patch.object(self.carte, "abonner", side_effect=abonnement):
            connexion.request("GET", "/carte/flux?duree=2")
            reponse = connexion.getresponse()
            self.addCleanup(reponse.close)
            self.assertEqual(self.lire_sse(reponse)[0], "instantane")
            evenement, snapshot = self.lire_sse(reponse)
            self.assertEqual(evenement, "instantane")
            self.assertTrue(snapshot["resynchronise"])
            self.assertEqual(len(abonnements), 2)
            self.lectures[0x42][0]["texte"] = "42"
            self.carte.rafraichir(0x42)
            evenement, delta = self.lire_sse(reponse)
            self.assertEqual(evenement, "changement")
            self.assertEqual(delta["precedent"], snapshot["revision"])
            self.assertEqual(delta["modifies"][0]["texte"], "42")

    def test_cible_http_refuse_element_perime_ou_fenetre_inactive(self):
        import http.client
        from urllib.parse import urlencode
        self.carte.rafraichir(0x42)
        identifiant = self.fenetre()["elements"][0]["id"]
        pont, _ = self.demarrer_pont()
        connexion = http.client.HTTPConnection("127.0.0.1", pont.port, timeout=3)
        self.addCleanup(connexion.close)

        def demander():
            connexion.request("GET", "/carte/cible?" + urlencode({"id": identifiant}))
            reponse = connexion.getresponse()
            return reponse.status, json.loads(reponse.read())

        self.assertEqual(demander()[0], 200)
        self.fenetres.actif = 0x99
        self.assertEqual(demander()[0], 409)
        self.fenetres.actif = 0x42
        self.lectures[0x42] = None
        self.carte.rafraichir(0x42)
        self.assertEqual(demander()[0], 409)

    def test_cible_refuse_centre_hors_fenetre_element_invisible_et_identite_estimee(self):
        normal = deepcopy(self.lectures[0x42])
        cas = [("centre hors fenêtre", {"x": 900}),
               ("masqué par sa source", {"visible_source": False}),
               ("identité estimée", {"identite": "estimee"})]
        for nom, changement in cas:
            with self.subTest(cas=nom):
                self.lectures[0x42] = deepcopy(normal)
                self.lectures[0x42][0].update(changement)
                self.carte.rafraichir(0x42)
                identifiant = self.fenetre()["elements"][0]["id"]
                self.assertEqual(self.carte.cible(identifiant)[0], 409)

    def test_cible_verifie_fenetre_visible_et_recouvrement_par_fenetre_superieure(self):
        self.fenetres.visibles = {0x42: True, 0x43: True}
        self.fenetres.empilement = [0x42, 0x43]
        self.fenetres.geometries[0x43] = (80, 80, 100, 100)  # recouvre le centre (100, 95)
        self.carte.rafraichir(0x42)
        identifiant = self.fenetre()["elements"][0]["id"]
        self.assertEqual(self.carte.cible(identifiant)[0], 409)
        self.fenetres.visibles[0x43] = False                 # une fenêtre réduite ne recouvre rien
        self.assertEqual(self.carte.cible(identifiant)[0], 200)
        self.fenetres.visibles[0x42] = False
        self.assertEqual(self.carte.cible(identifiant)[0], 409)

    def test_cible_refuse_contenu_modifie_lors_de_la_relecture(self):
        self.carte.rafraichir(0x42)
        avant = self.fenetre()["elements"][1]
        self.lecteur._dans_la_boucle = Mock(side_effect=lambda fn, attente: {"valeur": fn()})
        self.lectures[0x42][1]["nom"] = "Supprimer"
        self.assertEqual(self.carte.cible(avant["id"])[0], 409)
        apres = self.fenetre()["elements"][1]
        self.assertEqual(apres["id"], avant["id"])
        self.assertEqual(apres["nom"], "Supprimer")
        self.lecteur._dans_la_boucle.assert_called()

    def test_cible_exige_confirmation_positive_du_hit_test_source(self):
        self.carte.rafraichir(0x42)
        identifiant = self.fenetre()["elements"][0]["id"]
        for resultat, attendu in ((False, 409), (None, 409), (True, 200)):
            with self.subTest(confirmation=resultat):
                self.lecteur.verifier_cible.reset_mock()
                self.lecteur.verifier_cible.return_value = resultat
                self.assertEqual(self.carte.cible(identifiant)[0], attendu)
                self.lecteur.verifier_cible.assert_called_once()
                self.assertEqual(self.lecteur.verifier_cible.call_args.args[0], 0x42)
                self.assertEqual(self.lecteur.verifier_cible.call_args.args[1]["id"], identifiant)


class AtspiTest(unittest.TestCase):
    def test_seaux_vides_en_un_evenement_par_champ(self):
        bus = veille.Bus(os.path.join(tempfile.mkdtemp(), "v.jsonl"))
        a = veille.Accessibilite(bus)
        a.compter(("Terminal", "terminal", "", False), 12, 0, "Ran 131 tests")
        a.compter(("Terminal", "terminal", "", False), 5, 2, "OK")
        a.compter(("Firefox", "entry", "Rechercher", True), 7, 0, "")
        a.ignores = 3
        sortie = a.vider()
        self.assertEqual([s["msg"] for s in sortie],
                         ["texte : +17/−2 car. dans Terminal terminal : « OK »",
                          "texte : +7/−0 car. dans Firefox champ « Rechercher »",
                          "texte : 3 événements non détaillés (flot > 50/s)"])
        self.assertTrue(sortie[1]["editable"])
        self.assertNotIn("extrait", sortie[1])
        self.assertEqual(a.vider(), [])

    def test_un_libelle_reecrit_a_l_identique_est_tu_puis_resume(self):
        bus = veille.Bus(os.path.join(tempfile.mkdtemp(), "v.jsonl"))
        a = veille.Accessibilite(bus)
        cle = ("charli.py", "label", "", False)
        a.compter(cle, 1366, 1367, "CONTEXTE Fenêtre")
        self.assertEqual(len(a.vider()), 1)
        resumes = []
        for _ in range(veille.REPETITIONS_RESUME * 2):
            a.compter(cle, 1366, 1367, "CONTEXTE Fenêtre")
            resumes += a.vider()
        self.assertEqual([r["msg"] for r in resumes], ["texte : charli.py texte réécrit à l'identique ×30",
                                                       "texte : charli.py texte réécrit à l'identique ×60"])
        a.compter(cle, 10, 0, "autre chose")
        self.assertEqual(a.vider()[0]["msg"], "texte : +10/−0 car. dans charli.py texte : « autre chose »")

    def test_nettoyer_et_libelle(self):
        self.assertEqual(veille._nettoyer("  a\n\n b\tc  ", 3), "a b")
        self.assertEqual(veille.libelle("push button"), "bouton")
        self.assertEqual(veille.libelle("terminal"), "terminal")
        self.assertEqual(veille.libelle("rôle-inconnu"), "rôle-inconnu")


class EtatTest(unittest.TestCase):
    def test_reconstruit_l_etat_depuis_les_evenements(self):
        bus = veille.Bus(os.path.join(tempfile.mkdtemp(), "v.jsonl"))
        bus.emettre("fenetre", "ouverte", "…", app="firefox", titre="GIE", pid=12, wid="0x1")
        bus.emettre("fenetre", "activee", "…", app="firefox", titre="GIE", pid=12, wid="0x1")
        bus.emettre("fenetre", "titre", "…", app="firefox", titre="GIE — connecté", wid="0x1")
        bus.emettre("gpu", "initial", "…", util=93, mem=9000, bande="saturé")
        bus.emettre("gpu", "plus", "…", pid=5, comm="HogwartsLegacy.")
        bus.emettre("gpu", "memoire", "…", mem=2000)
        bus.emettre("audio", "joue", "…", app="Firefox", flux=1)
        bus.emettre("micro", "joue", "…", app="Zoom", flux=1)
        bus.emettre("pilote", "stop", "…", stop="[vigie] clic")
        bus.emettre("ecran", "etat", "…", etat="vidéo", pct=66.0)
        bus.emettre("ecran", "fige", "…", depuis=30)
        d = bus.etat.instantane()
        self.assertEqual(d["fenetre_active"]["titre"], "GIE — connecté")
        self.assertEqual(d["fenetres"]["0x1"]["titre"], "GIE — connecté")
        self.assertEqual(d["gpu"], {"bande": "saturé", "util": 93, "mem": 2000, "procs": {"5": "HogwartsLegacy."}})
        self.assertEqual(d["audio"], {"Firefox": "joue"})
        self.assertEqual(d["micro"], ["Zoom"])
        self.assertEqual(d["pilote"], {"stop": "[vigie] clic", "agent": None})
        self.assertEqual(d["ecran"], {"etat": "vidéo", "pct": 66.0, "fige_depuis_s": 30, "suspect": False})
        bus.emettre("ecran", "reprise", "…", pct=5.0)
        bus.emettre("audio", "fin", "…", app="Firefox")
        bus.emettre("pilote", "reprise", "…")
        bus.emettre("fenetre", "fermee", "…", wid="0x1")
        d = bus.etat.instantane()
        self.assertIsNone(d["ecran"]["fige_depuis_s"])
        self.assertEqual(d["audio"], {})
        self.assertIsNone(d["pilote"]["stop"])
        self.assertEqual(d["fenetres"], {})
        self.assertEqual(len(d["derniers"]), 15)
        self.assertEqual(d["derniers"][-1]["src"], "fenetre")


class PontTest(unittest.TestCase):
    def test_etat_evenements_texte_et_flux(self):
        import http.client
        bus = veille.Bus(os.path.join(tempfile.mkdtemp(), "v.jsonl"))
        pont = veille.Pont(bus, port=0, lecteur_texte=lambda tout=False: {"fenetre": "X", "lignes": ["bouton « OK »"]})
        pont.start()
        bus.emettre("gpu", "charge", "GPU : fort", util=70, bande="fort", mem=1)
        bus.emettre("ecran", "etat", "écran : calme", etat="calme", pct=0.0)
        c = http.client.HTTPConnection("127.0.0.1", pont.port, timeout=5)
        c.request("GET", "/etat")
        self.assertEqual(json.loads(c.getresponse().read())["gpu"]["bande"], "fort")
        c.request("GET", "/evenements?n=5&src=ecran")
        self.assertEqual([e["ev"] for e in json.loads(c.getresponse().read())], ["etat"])
        c.request("GET", "/texte")
        self.assertEqual(json.loads(c.getresponse().read())["lignes"], ["bouton « OK »"])
        c.request("GET", "/rien")
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 404)
        f = http.client.HTTPConnection("127.0.0.1", pont.port, timeout=5)
        f.request("GET", "/flux?src=audio")
        r = f.getresponse()
        self.assertEqual(r.getheader("Content-Type"), "text/event-stream; charset=utf-8")
        self.assertEqual(r.fp.readline(), b": pont veille\n")
        r.fp.readline()
        bus.emettre("gpu", "charge", "ignoré par le filtre", util=1)
        bus.emettre("audio", "joue", "audio : Spotify joue", app="Spotify")
        ligne = r.fp.readline()
        self.assertTrue(ligne.startswith(b"data: "), ligne)
        self.assertEqual(json.loads(ligne[6:])["msg"], "audio : Spotify joue")
        f.close()
        pont.serveur.shutdown()
        self.assertEqual(veille.compacter({"role": "champ", "nom": "Recherche", "texte": "abc", "etats": ["focus"]}),
                         "champ « Recherche » = abc [focus]")


class OeilTest(unittest.TestCase):
    REPONSE = ('```json\n{"scene": "Un terminal affiche le résultat de tests", "type": "terminal", '
               '"elements": [{"type": "texte", "libelle": "Ran 16 tests", "zone": "bas", "etat": null}, "pas un dict", '
               '{"type": "champ", "libelle": "Email: felix.x@example.org, code 2233551054", "zone": "centre"}], '
               '"evenement": "tests terminés", "alerte": null, "texte_cle": ["OK", 12, "prix 12,50 € à 18:42"]}\n```')

    def test_masquer(self):
        self.assertEqual(veille.Oeil.masquer("mail a.b+c@x-y.fr, code 2233551054, prix 12,50 € à 18:42, 185 600 tokens"),
                         "mail [adresse mail], code [numéro], prix 12,50 € à 18:42, [numéro] tokens")

    def test_extraire_json(self):
        self.assertEqual(veille.extraire_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(veille.extraire_json('Voici : {"a": {"b": 2}} fin'), {"a": {"b": 2}})
        self.assertEqual(veille.extraire_json('<think>\nje {réfléchis}\n</think>\n{"a": 3}'), {"a": 3})
        self.assertIsNone(veille.extraire_json("rien"))

    def test_declencheurs(self):
        self.assertTrue(veille.Oeil.interesse({"src": "ecran", "ev": "fige"}))
        self.assertFalse(veille.Oeil.interesse({"src": "atspi", "ev": "texte"}))

    def _faux_moteur(self, reponse):
        import http.server
        import threading

        class Faux(http.server.BaseHTTPRequestHandler):
            requetes = []

            def do_POST(self):
                Faux.requetes.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)))))
                corps = json.dumps({"choices": [{"message": {"content": reponse}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(corps)))
                self.end_headers()
                self.wfile.write(corps)

            def log_message(self, *_):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Faux)
        srv.Faux = Faux
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    def test_regard_publie_une_scene_structuree_avec_les_faits_et_sans_lecture_lettre_a_lettre(self):
        from PIL import Image
        bus = veille.Bus(os.path.join(tempfile.mkdtemp(), "v.jsonl"))
        srv = self._faux_moteur(self.REPONSE)
        oeil = veille.Oeil(bus, url=f"http://127.0.0.1:{srv.server_address[1]}/v1", capturer=lambda: Image.new("RGB", (64, 36), "black"))
        bus.declencheurs.append(oeil.sur_evenement)
        bus.emettre("fenetre", "activee", "…", app="steam", titre="Steam Settings", pid=1, wid="0x2")
        bus.emettre("ecran", "fige", "écran : immobile depuis 30 s", depuis=30, suspect=False)
        self.assertEqual(oeil.demande, "…")                                   # l'immobilité d'un bureau ne déclenche rien
        bus.emettre("ecran", "fige", "écran : FIGÉ depuis 30 s", depuis=30, suspect=True)
        self.assertEqual(oeil.demande, "écran : FIGÉ depuis 30 s")
        scene = oeil.regarder_maintenant("test")
        self.assertEqual(scene["scene"], "Un terminal affiche le résultat de tests")
        self.assertEqual(scene["elements"], [{"type": "texte", "libelle": "Ran 16 tests", "zone": "bas", "etat": None},
                                             {"type": "champ", "libelle": "Email: [adresse mail], code [numéro]", "zone": "centre", "etat": None}])
        self.assertEqual(scene["texte_cle"], ["OK", "prix 12,50 € à 18:42"])
        texte_envoye = srv.Faux.requetes[-1]["messages"][1]["content"][0]["text"]
        self.assertIn("FAITS CERTAINS", texte_envoye)
        self.assertIn("GNOME", texte_envoye)
        self.assertIn("« steam », titre « Steam Settings »", texte_envoye)
        self.assertFalse(srv.Faux.requetes[-1]["chat_template_kwargs"]["enable_thinking"])
        d = bus.etat.instantane()
        self.assertEqual(d["scene"]["evenement"], "tests terminés")
        self.assertTrue(d["derniers"][-1]["msg"].startswith("œil : Un terminal affiche le résultat de tests — tests terminés (2 éléments"))
        srv.shutdown()

    def test_moteur_injoignable_une_seule_erreur(self):
        from PIL import Image
        bus = veille.Bus(os.path.join(tempfile.mkdtemp(), "v.jsonl"))
        oeil = veille.Oeil(bus, url="http://127.0.0.1:1/v1", capturer=lambda: Image.new("RGB", (8, 8), "black"))
        self.assertIn("erreur", oeil.regarder("a"))
        self.assertIn("erreur", oeil.regarder("b"))
        lignes = [json.loads(l) for l in open(bus.fichier, encoding="utf-8")]
        self.assertEqual([l["ev"] for l in lignes], ["erreur"])
        self.assertTrue(oeil.tic())                                   # moteur indisponible : pas de nouveau fil


if __name__ == "__main__":
    unittest.main()
