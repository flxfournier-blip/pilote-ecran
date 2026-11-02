"""Lot 2 — image lisible : cadrage (fonction pure) et conversion `--img` par ecran.json."""
import json
import io
import os
import sys
import time
import unittest
from contextlib import ExitStack, nullcontext, redirect_stdout, redirect_stderr
from unittest import mock
from PIL import Image

from tests.outils import FIXTURES, ICI, Bac

sys.path.insert(0, ICI)
import ecran  # noqa: E402

ECRAN = (2560, 1440)


class TestCadrer(unittest.TestCase):
    def test_fenetre_demi_ecran_echelle_1(self):
        b, o, e = ecran.cadrer(ECRAN, (0, 0), "fenetre", (0, 69, 1280, 1304))
        self.assertEqual((b, o, e), ((0, 69, 1280, 1373), (0, 69), 1.0))

    def test_fenetre_agrandie_reduite_sous_2000(self):
        b, o, e = ecran.cadrer(ECRAN, (0, 0), "fenetre", (0, 32, 2560, 1341))
        self.assertEqual(e, 0.75)
        self.assertLess(round((b[2] - b[0]) * e), 2000)

    def test_fenetre_debordante_rognee(self):
        b, o, e = ecran.cadrer(ECRAN, (0, 0), "fenetre", (-60, 100, 1500, 900))
        self.assertEqual(b[0], 0)
        self.assertEqual(o, (0, 100))

    def test_tuile(self):
        self.assertEqual(ecran.cadrer(ECRAN, (0, 0), "tuile", tuile=((2, 2), 4))[:2], ((1280, 720, 2560, 1440), (1280, 720)))
        with self.assertRaises(SystemExit):
            ecran.cadrer(ECRAN, (0, 0), "tuile", tuile=((2, 2), 5))

    def test_autour_pres_du_bord(self):
        b, o, e = ecran.cadrer(ECRAN, (0, 0), "autour", point=(2500, 30), autour=(1200, 600))
        self.assertEqual(b, (1360, 0, 2560, 600))
        self.assertEqual(e, 1.0)

    def test_plein_et_apercu(self):
        self.assertEqual(ecran.cadrer(ECRAN, (0, 0), "plein")[2], 0.75)
        self.assertEqual(ecran.cadrer(ECRAN, (0, 0), "apercu")[2], 0.5)

    def test_sans_fenetre_ecran_entier(self):
        self.assertEqual(ecran.cadrer(ECRAN, (0, 0), "fenetre", None)[2], 0.75)


class TestImgConversion(unittest.TestCase):
    def setUp(self):
        self.bac = Bac()
        self.bac.ecran_fixe(os.path.join(FIXTURES, "comptoir-2560.png"))

    def tearDown(self):
        self.bac.fermer()

    def sidecar(self, origine, echelle, age=0):
        with open(self.bac.fichier("ecran.json"), "w") as f:
            json.dump({"ts": time.time() - age, "origine": origine, "echelle": echelle, "taille": [100, 100]}, f)

    def test_img_origine_et_echelle(self):
        self.sidecar([0, 32], 0.75)
        r = self.bac.lancer("souris", "300", "150", "--img")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("xdotool mousemove 400 232", self.bac.appels())

    def test_img_sans_sidecar_refuse(self):
        r = self.bac.lancer("souris", "300", "150", "--img")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.bac.injections(), [])


class TestVoirInstantane(unittest.TestCase):
    """Une interface change pendant AT-SPI ; aucun accès au vrai bureau."""

    def setUp(self):
        self.bac = Bac()
        self.addCleanup(self.bac.fermer)
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(mock.patch.object(ecran.commun, "ETAT", self.bac.etat))
        self.couleur = "blue"
        self.horloge = 1000.0
        self.sortie, self.erreurs = io.StringIO(), io.StringIO()
        for nom, valeur in {
            "agir": lambda **kw: nullcontext(), "ecran_taille": lambda: (300, 200),
            "activer_accessibilite": lambda: None, "atspi": lambda: object(),
            "choisir_fenetres_atspi": lambda *a: ([("Test", "Avant", object(), "1")], ("1", "Avant", 1)),
            "fenetre_active": lambda: ("1", "Avant", 1),
            "geometrie_x11": lambda wid: (0, 0, 300, 200),
            "capture": self.capture, "parcourir": self.parcourir,
            "lire_ecran": self.ocr, "ligne_etat": lambda: "ETAT test",
            "agent_courant": lambda: "test",
        }.items():
            self.patches.enter_context(mock.patch.object(ecran, nom, side_effect=valeur))
        self.patches.enter_context(mock.patch.object(ecran.time, "time", side_effect=lambda: self.horloge))

    def capture(self, *args):
        img = Image.new("RGB", (300, 200), self.couleur)
        return img, 1.0, img

    def parcourir(self, atspi, w, largeur, hauteur, deadline, elements, vus, wid):
        self.couleur = "red"  # Réponse arrivée pendant le parcours.
        elements.append({"id": 7, "x": 10, "y": 10, "w": 80, "h": 20,
                         "nom": "Réponse", "texte": "", "etats": [],
                         "role": "bouton", "role_atspi": "push button", "wid": wid})
        return 1, False

    def ocr(self, zone, img):
        self.horloge += 3  # L'horodatage doit rester celui de la capture.
        texte = "Réponse reçue" if img.getpixel((0, 0)) == (255, 0, 0) else "Préparation"
        li = {"id": "t1", "texte": texte, "x": 10, "y": 40, "l": 100, "h": 20,
              "cx": 60, "cy": 50, "conf": 99, "mots": []}
        return img, [], [li]

    def voir(self):
        with redirect_stdout(self.sortie), redirect_stderr(self.erreurs):
            ecran.cmd_voir(["--sans-marques"])

    def donnees(self, nom):
        with open(self.bac.fichier(nom)) as f:
            return json.load(f)

    def test_image_et_ocr_montrent_la_reponse_arrivee_pendant_atspi(self):
        self.voir()
        with Image.open(self.bac.fichier("ecran.png")) as img:
            self.assertEqual(img.getpixel((0, 0)), (255, 0, 0))
        self.assertIn("Réponse reçue", self.sortie.getvalue())
        self.assertNotIn("Préparation", self.sortie.getvalue())

    def test_changement_de_fenetre_ecarte_les_anciens_reperes(self):
        ecran.fenetre_active.side_effect = lambda: ("2", "Après", 2)
        self.voir()
        self.assertEqual(self.donnees("elements.json")["elements"], [])
        self.assertEqual(self.donnees("elements.json")["wid"], "2")
        self.assertIn("fenêtre changée", self.erreurs.getvalue())
        self.assertIn("Réponse reçue", self.sortie.getvalue())

    def test_horodatage_capture_commun_malgre_delai_ocr(self):
        self.voir()
        for nom in ("ecran.json", "elements.json", "texte.json"):
            self.assertEqual(self.donnees(nom)["ts"], 1000.0)
        self.assertIn("Capture prise il y a 3.0 s", self.sortie.getvalue())

    def test_deplacement_apres_capture_ne_filtre_pas_le_texte_visible(self):
        # Le deuxième appel aurait déplacé la fenêtre hors du texte de la capture.
        ecran.geometrie_x11.side_effect = [(0, 0, 300, 200), (200, 100, 100, 100)]
        self.voir()
        self.assertIn("Réponse reçue", self.sortie.getvalue())
        ecran.geometrie_x11.assert_called_once_with("1")

    def test_rapide_sans_arbre_ni_ocr_et_anciens_ids_invalides(self):
        self.voir()
        with mock.patch.object(ecran, "capture_texte", side_effect=lambda zone: self.capture()[2].crop((0, 0, 200, 100))):
            with redirect_stdout(self.sortie):
                ecran.cmd_voir(["--rapide", "--zone", "0", "0", "200", "100"])
        self.assertEqual(ecran.lire_ecran.call_count, 1)
        self.assertEqual(ecran.parcourir.call_count, 1)
        self.assertEqual(self.donnees("elements.json")["elements"], [])
        self.assertFalse(os.path.exists(self.bac.fichier("texte.json")))
        self.assertEqual(self.donnees("ecran.json")["taille"], [200, 100])

    def test_capture_rapide_en_echec_ne_conserve_pas_les_reperes(self):
        self.voir()
        with mock.patch.object(ecran, "capture_texte", side_effect=SystemExit("capture impossible")):
            with self.assertRaises(SystemExit):
                ecran.cmd_voir(["--rapide"])
        for nom in ("elements.json", "texte.json", "ecran.json"):
            self.assertFalse(os.path.exists(self.bac.fichier(nom)))

    def test_vue_action_reutilise_derniere_capture_et_repli_si_focus_change(self):
        self.couleur = "red"
        ecran.fenetre_active.side_effect = [("1", "Avant", 1), ("2", "Après", 2),
                                           ("1", "Avant", 1), ("2", "Après", 2),
                                           ("2", "Après", 2)]
        # Le vieux cadrage couperait l'image ; un changement de focus exige le plein écran.
        ecran.geometrie_x11.side_effect = lambda wid: (10, 10, 100, 100)
        with mock.patch.object(ecran, "capture_texte", side_effect=lambda: self.capture()[2]) as capturer:
            with redirect_stdout(self.sortie):
                ecran.verdict(Image.new("RGB", (300, 200), "blue"), voir=True)
        self.assertEqual(capturer.call_count, 2)
        self.assertEqual(self.donnees("ecran.json")["taille"], [300, 200])
        with Image.open(self.bac.fichier("ecran.png")) as img:
            self.assertEqual(img.getpixel((150, 150)), (255, 0, 0))
        self.assertIn("fenêtre changée", self.sortie.getvalue())


if __name__ == "__main__":
    unittest.main()
