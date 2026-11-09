"""`ecran` en boîte noire, sans écran : faux xdotool/systemctl, état jetable."""
import os
import io
import json
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from tests.outils import ICI, LANCEUR, Bac, lire

sys.path.insert(0, ICI)


class TestRefus(unittest.TestCase):
    """Arguments invalides : refus AVANT toute action (ex-section 1 de tests.sh)."""

    CAS = ["voir --zone", "voir --zone 10 20 30", "voir --fenetre", "voir --zzz",
           "cliquer 1 --doulbe", "cliquer 1 --droit --milieu", "attendre abc",
           "attendre -1", "defiler bas -3", "defiler bas 999999",
           "defiler bas 3 --a 100", "cliquer abc def", "cliquer 99999 99999",
           "taper", "touche --foo", "fenetre 0xdeadbeef"]

    def setUp(self):
        self.bac = Bac()

    def tearDown(self):
        self.bac.fermer()

    def test_refus_sans_action(self):
        for cas in self.CAS:
            with self.subTest(cas=cas):
                r = self.bac.lancer(*cas.split())
                self.assertNotEqual(r.returncode, 0, r.stdout)
                self.assertNotIn("cadre non affiché", r.stderr)
                self.assertEqual(self.bac.injections(), [], r.stderr)

    def test_commande_inconnue(self):
        r = self.bac.lancer("zzz")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("commande inconnue", r.stderr)


class TestStop(unittest.TestCase):
    def setUp(self):
        self.bac = Bac()
        with open(self.bac.fichier("stop"), "w") as f:
            f.write("[cadre] souris bougée par Félix (1,1 → 300,300) à 12:00:00")

    def tearDown(self):
        self.bac.fermer()

    def test_action_refusee_et_stop_conserve(self):
        for cas in (["souris", "10", "10"], ["cliquer", "500", "500"], ["taper", "abc"],
                    ["touche", "Return"], ["defiler", "bas"]):
            with self.subTest(cas=cas):
                r = self.bac.lancer(*cas)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("ARRÊTÉ", r.stderr)
                self.assertEqual(self.bac.injections(), [])
                self.assertTrue(os.path.exists(self.bac.fichier("stop")))

    def test_etat_montre_le_stop(self):
        r = self.bac.lancer("etat")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("STOP", r.stdout)
        self.assertIn("[cadre]", r.stdout)


class TestCommun(unittest.TestCase):
    def setUp(self):
        self.bac = Bac()
        import commun
        self.commun = commun
        self._etat = commun.ETAT
        commun.ETAT = self.bac.etat

    def tearDown(self):
        self.commun.ETAT = self._etat
        self.bac.fermer()

    def test_occupe_orphelin_ignore_et_efface(self):
        with open(self.bac.fichier("occupe"), "w") as f:
            f.write("4194304")                       # pid inexistant
        self.assertFalse(self.commun.occupe_valide())
        self.assertFalse(os.path.exists(self.bac.fichier("occupe")))

    def test_occupe_vieux_ignore(self):
        with open(self.bac.fichier("occupe"), "w") as f:
            f.write(str(os.getpid()))
        vieux = time.time() - self.commun.AGE_OCCUPE - 5
        os.utime(self.bac.fichier("occupe"), (vieux, vieux))
        self.assertFalse(self.commun.occupe_valide())

    def test_occupe_vivant_honore(self):
        with open(self.bac.fichier("occupe"), "w") as f:
            f.write(str(os.getpid()))
        self.assertTrue(self.commun.occupe_valide())

    def test_ecriture_atomique_sans_residu(self):
        self.commun.ecrire("x", "contenu")
        self.assertEqual(lire(self.bac.fichier("x")), "contenu")
        self.assertEqual([f for f in os.listdir(self.bac.etat) if ".tmp" in f], [])


class TestEtiquette(unittest.TestCase):
    def test_dans_la_barre_du_haut(self):
        import commun
        x, y, h = commun.position_etiquette(32, 560, 1373)
        self.assertEqual((y, h), (0, 32))
        self.assertLessEqual(x + 560, 1150, "l'étiquette déborderait sur l'horloge")

    def test_repli_sans_barre(self):
        import commun
        x, y, h = commun.position_etiquette(0, 560, 1440)
        self.assertEqual(y, 1440 - 36)

    def test_nom_de_l_agent(self):
        import commun
        bac = Bac()
        ancien = commun.ETAT
        try:
            commun.ETAT = bac.etat
            with open(bac.fichier("pilote"), "w") as f:
                f.write('{"agent": "codex"}')
            self.assertEqual(commun.agent_pilote(), "codex")
        finally:
            commun.ETAT = ancien
            bac.fermer()


class TestAffichage(unittest.TestCase):
    """08/11/2026 : la session graphique a redémarré sur :1, les services gardaient :0."""

    def setUp(self):
        import tempfile
        self.dossier = tempfile.mkdtemp(prefix="x11-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dossier, ignore_errors=True)

    def prise(self, n, age=0):
        p = os.path.join(self.dossier, f"X{n}")
        open(p, "w").close()
        t = time.time() - age
        os.utime(p, (t, t))

    def test_display_vivant_garde(self):
        import commun
        self.prise(0, age=100)
        self.prise(1)
        self.assertEqual(commun.affichage(":0", self.dossier), ":0")
        self.assertEqual(commun.affichage(":0.0", self.dossier), ":0.0")

    def test_display_mort_remplace_par_la_prise_vivante(self):
        import commun
        self.prise(1)
        self.assertEqual(commun.affichage(":0", self.dossier), ":1")

    def test_sans_display_la_plus_recente(self):
        import commun
        self.prise(0, age=3600)
        self.prise(2)
        self.assertEqual(commun.affichage("", self.dossier), ":2")

    def test_aucune_prise_repli(self):
        import commun
        self.assertEqual(commun.affichage(":3", self.dossier), ":3")
        self.assertEqual(commun.affichage("", self.dossier), ":0")

    def test_distant_non_juge(self):
        import commun
        self.assertEqual(commun.affichage("hote:5", self.dossier), "hote:5")


class TestRecetteRefuse(unittest.TestCase):
    """La recette écran ne part ni sous STOP, ni sur un écran réservé, ni sans GO."""

    def setUp(self):
        self.bac = Bac()

    def tearDown(self):
        self.bac.fermer()

    def recette(self):
        return subprocess.run([os.path.join(ICI, "recette-ecran.sh")], env=self.bac.env(),
                              capture_output=True, text=True, timeout=30)

    def poser(self, nom, contenu=""):
        with open(self.bac.fichier(nom), "w") as f:
            f.write(contenu)

    def test_sous_stop_meme_avec_go(self):
        self.poser("go-recette")
        self.poser("stop", "[cadre] souris bougée par Félix")
        r = self.recette()
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("STOP en vigueur", r.stdout)
        self.assertTrue(os.path.exists(self.bac.fichier("stop")))
        self.assertTrue(os.path.exists(self.bac.fichier("go-recette")), "GO consommé pour rien")
        self.assertEqual(self.bac.appels(), [])

    def test_ecran_reserve(self):
        self.poser("go-recette")
        self.poser("pilote", '{"agent": "codex"}')
        self.poser("actif", "1")
        r = self.recette()
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("codex", r.stdout)
        self.assertEqual(self.bac.appels(), [])

    def test_sans_go(self):
        r = self.recette()
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("pas de GO", r.stdout)
        self.assertEqual(self.bac.appels(), [])


class TestGeometrie(unittest.TestCase):
    def test_xwininfo_et_non_xdotool(self):
        # Fenêtre à barre mutter : xdotool dit Y=106, la vraie origine est 69.
        import ecran
        sortie = ("  Absolute upper-left X:  0\n  Absolute upper-left Y:  69\n"
                  "  Width: 2560\n  Height: 1304\n")
        def faux_sh(*args, **kw):
            return sortie if args[0] == "xwininfo" else ""
        with mock.patch.object(ecran, "sh", side_effect=faux_sh):
            self.assertEqual(ecran.geometrie_x11("0x04200003"), (0, 69, 2560, 1304))


class TestDeconfiner(unittest.TestCase):
    def test_relance_le_lanceur(self):
        import ecran
        vu = {}
        with mock.patch.dict(os.environ, {"PILOTE_DECONFINE": ""}), \
                mock.patch("builtins.open", mock.mock_open(read_data="snap.code.code (enforce)")), \
                mock.patch("os.execvp", side_effect=lambda p, cmd: vu.setdefault("cmd", cmd)):
            ecran.deconfiner()
        self.assertEqual(vu["cmd"][0], "systemd-run")
        self.assertIn(os.path.realpath(LANCEUR), vu["cmd"])

    def test_hors_snap_ne_relance_pas(self):
        import ecran
        with mock.patch("builtins.open", mock.mock_open(read_data="unconfined")), \
                mock.patch("os.execvp") as ex:
            ecran.deconfiner()
        ex.assert_not_called()


class TestIdentitesAtspi(unittest.TestCase):
    def test_enfant_annonce_mais_null_signale_lecture_incomplete(self):
        import ecran
        atspi = SimpleNamespace(StateType=SimpleNamespace(SHOWING=1, VISIBLE=2))
        racine = self.noeud(path="/racine", enfants=[None])
        elements = []
        with mock.patch.object(ecran, "repere", return_value=(0, 0, 0)):
            _, coupe = ecran.parcourir(atspi, racine, 1920, 1080, time.monotonic() + 1, elements, set(), "42")
        self.assertTrue(coupe)
        self.assertEqual(elements, [])

    def noeud(self, nom="Bouton", path="/objet/1", enfants=(), x=10):
        acc = SimpleNamespace(
            get_name=lambda: nom, get_role_name=lambda: "push button",
            get_state_set=lambda: SimpleNamespace(contains=lambda _: True),
            get_component_iface=lambda: SimpleNamespace(get_extents=lambda _: SimpleNamespace(
                x=x, y=20, width=30, height=20)),
            get_child_count=lambda: len(enfants), get_child_at_index=lambda n: enfants[n])
        if path:
            acc.app, acc.path = SimpleNamespace(bus_name=":1.42"), path
        return acc

    def parcourir(self, enfants, wid="42", native=True):
        import ecran
        racine = self.noeud(path="/racine" if native else None, enfants=enfants)
        atspi = SimpleNamespace(StateType=SimpleNamespace(SHOWING=1, VISIBLE=2))
        elements = []
        with mock.patch.object(ecran, "repere", return_value=(0, 0, 0)), \
                mock.patch.object(ecran, "etats_de", return_value=[]):
            ecran.parcourir(atspi, racine, 1920, 1080, time.monotonic() + 1, elements, set(), wid)
        return elements

    def test_native_survit_nom_position_et_reorganisation(self):
        avant = self.parcourir([self.noeud(), self.noeud("Autre", "/objet/2", x=100)])
        apres = self.parcourir([self.noeud("Autre", "/objet/2", x=100), self.noeud("Modifié", x=200)])
        self.assertEqual(avant[0]["cle_source"], apres[1]["cle_source"])
        self.assertEqual(avant[0]["cle_source"], "atspi::1.42:/objet/1")
        self.assertEqual([e["id"] for e in apres], [1, 2], "les ids de clic restent ceux du parcours")
        self.assertTrue(all(e["source"] == "atspi" for e in apres))

    def test_repli_structurel_explicite_sans_contenu(self):
        avant = self.parcourir([self.noeud(path=None)], native=False)
        apres = self.parcourir([self.noeud("Valeur changée", path=None, x=300)], native=False)
        autre = self.parcourir([self.noeud(path=None)], wid="43", native=False)
        self.assertEqual(avant[0]["cle_source"], "atspi:structure:x11:42:0")
        self.assertEqual(avant[0]["cle_source"], apres[0]["cle_source"])
        self.assertNotEqual(avant[0]["cle_source"], autre[0]["cle_source"])


class TestCarteFluxCli(unittest.TestCase):
    def test_delta_timestamp_numerique_et_qualite_de_la_carte(self):
        import ecran
        element = {"id": "objet-1", "role": "bouton", "nom": "Tester", "identite": "estimee"}
        fenetre = {"wid": "0x1", "titre": "Test", "perime": True, "age_ms": 7000,
                   "fraicheur": {"etat": "observe"},
                   "couverture": {"etat": "partielle", "raison": "images non interprétées"},
                   "elements": [element]}
        sortie = io.StringIO()
        with redirect_stdout(sortie):
            ecran.afficher_carte({"ts": 0.0, "fenetres": [fenetre]})
            ecran.afficher_changement_carte({"ts": 0.0, "revision": 2, "type": "fenetre", "wid": "0x1",
                                            "fenetre": fenetre, "modifies": [element]})
        texte = sortie.getvalue()
        self.assertIn("01:00:00 révision 2", texte)
        self.assertIn("fraîcheur : périmée (âge 7000 ms)", texte)
        self.assertIn("couverture : partielle — images non interprétées", texte)
        self.assertIn("identité estimée", texte)
        self.assertNotIn("arbre vide en arrière-plan", texte)

    def test_options_dans_les_deux_ordres(self):
        import ecran
        for args in (["--carte", "--suivre", "3", "--json", "--fenetre", "Calculatrice"],
                     ["--fenetre", "Calculatrice", "--suivre", "3", "--carte", "--json"]):
            with self.subTest(args=args), mock.patch.object(ecran, "veille_carte") as carte:
                ecran.cmd_veille(args)
                carte.assert_called_once_with(True, "Calculatrice", 3)

    def test_duree_par_defaut_et_carte_simple(self):
        import ecran
        with mock.patch.object(ecran, "veille_carte") as carte:
            ecran.cmd_veille(["--carte", "--suivre"])
            carte.assert_called_once_with(False, "", 60)
        with mock.patch.object(ecran, "veille_carte") as carte:
            ecran.cmd_veille(["--carte"])
            carte.assert_called_once_with(False, "", None)

    def test_flux_json_instantane_delta_et_resynchronisation(self):
        import ecran
        donnees = [
            ("instantane", {"session": "s", "revision": 1, "fenetres": []}),
            ("changement", {"session": "s", "revision": 2, "precedent": 1, "type": "contexte", "contexte": {"titre": "Écran"}}),
            ("instantane", {"session": "s", "revision": 5, "resynchronise": True, "fenetres": []}),
        ]
        flux = ": heartbeat\n\n" + "".join(
            f"event: {ev}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n" for ev, d in donnees)
        sortie = io.StringIO()
        with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(flux.encode())) as ouvrir, redirect_stdout(sortie):
            ecran.suivre_carte(True, "Éditeur", 3)
        lignes = [json.loads(l) for l in sortie.getvalue().splitlines()]
        self.assertEqual(lignes, [{"evenement": ev, **d} for ev, d in donnees])
        url = ouvrir.call_args.args[0].full_url
        self.assertIn("/carte/flux?", url)
        self.assertIn("duree=3", url)
        self.assertIn("fenetre=%C3%89diteur", url)

    def test_option_inconnue_ne_disparait_pas_apres_carte(self):
        import ecran
        with self.assertRaises(SystemExit), mock.patch.object(ecran, "veille_carte") as carte:
            ecran.cmd_veille(["--carte", "--inconnu"])
        carte.assert_not_called()


if __name__ == "__main__":
    unittest.main()
