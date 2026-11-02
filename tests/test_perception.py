"""Couche texte sur capture figée (comptoir PharmIOS, thème sombre, 2560×1440).

    /usr/bin/python3 -m unittest discover -s ~/jarvis/pilote-ecran/tests -t ~/jarvis/pilote-ecran
"""
import json
import os
import sys
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from tests.outils import FIXTURES, ICI

sys.path.insert(0, ICI)
import perception as P  # noqa: E402

ATTENDUS = json.load(open(os.path.join(FIXTURES, "attendus.json")))
TOL = ATTENDUS["tolerance_px"]


class TestComptoir(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.img = Image.open(os.path.join(FIXTURES, "comptoir-2560.png")).convert("RGB")
        cls.mots = P.ocr(cls.img)

    def test_boutons_trouves_au_bon_endroit(self):
        for motif, (x, y) in ATTENDUS["comptoir-2560.png"].items():
            with self.subTest(motif=motif):
                r = P.chercher(self.mots, motif)
                self.assertTrue(r, f"« {motif} » introuvable")
                self.assertTrue(any(abs(o["cx"] - x) <= TOL and abs(o["cy"] - y) <= TOL for o in r),
                                f"« {motif} » : {[(o['cx'], o['cy']) for o in r]} ≠ {x},{y}")

    def test_absent_rend_vide_et_trois_proches(self):
        for motif in ATTENDUS["absents"]:
            with self.subTest(motif=motif):
                self.assertEqual(P.chercher(self.mots, motif), [])
                self.assertEqual(len(P.proches(self.mots, motif)), 3)

    def test_confiance_minimale(self):
        self.assertTrue(all(m["conf"] >= P.CONF_MIN for m in self.mots))

    def test_bouton_colore_lu(self):
        # « Carte bleue » (texte sombre sur vert) et « Envoyer » (blanc sur vert)
        # échappent au seuillage global : seconde passe par zone colorée.
        sources = {o["source"] for m in ("Carte bleue", "Envoyer")
                   for o in P.chercher(self.mots, m)}
        self.assertIn("ocr-couleur", sources)

    def test_capture_reduite_illisible(self):
        # L'ancien `voir` réduisait ×0,5 : c'est ce qui a fait piloter à
        # l'aveugle le 25/09. Ce test DOIT échouer à lire : s'il se met à
        # réussir, c'est que la fixture a changé, pas que la réduction est bonne.
        # 28/10 : il porte sur TESSERACT ; Indra (complément, cf. _indra) lit
        # une capture réduite sans peine (470 mots), on l'écarte ici.
        reduite = self.img.resize((1280, 720), Image.LANCZOS)
        with patch.object(P, "MOTEUR_OCR", "tesseract"):
            mots = P.ocr(reduite, agrandir=1, couleurs=False)
        self.assertLess(len(mots), 50)
        self.assertEqual(P.chercher(mots, "Dépose une ordonnance"), [])

    def test_origine_decale_les_coordonnees(self):
        zone = (2300, 0, 260, 70)
        crop = self.img.crop((zone[0], zone[1], zone[0] + zone[2], zone[1] + zone[3]))
        r = P.chercher(P.ocr(crop, origine=zone[:2]), "Nouveau passage")
        x, y = ATTENDUS["comptoir-2560.png"]["Nouveau passage"]
        self.assertTrue(r and abs(r[0]["cx"] - x) <= TOL and abs(r[0]["cy"] - y) <= TOL, r)

    def test_lignes_gauche_a_droite(self):
        textes = [li["texte"] for li in P.lignes(self.mots)]
        self.assertTrue(any("Carte Vitale" in t for t in textes), textes[:20])


class TestComptoirReel(unittest.TestCase):
    """Captures RÉELLES : fenêtre du comptoir (Chrome --app agrandi, origine
    0,69) et écran entier d'un nouveau passage (texte d'invite gris)."""

    def test_boutons_trouves(self):
        self.verifier("comptoir-app-2560x1304.png")

    def test_texte_gris_sur_ecran_entier(self):
        self.verifier("comptoir-nouveau-passage.png")

    def test_theme_chatgpt_capsules_claires(self):
        self.verifier("comptoir-theme-chatgpt-1280.png")

    def verifier(self, nom):
        att = ATTENDUS[nom]
        img = Image.open(os.path.join(FIXTURES, nom)).convert("RGB")
        mots = P.ocr(img, origine=tuple(att["_origine"]))
        for motif, (x, y) in ((m, v) for m, v in att.items() if not m.startswith("_")):
            with self.subTest(motif=motif):
                r = P.chercher(mots, motif)
                self.assertTrue(any(abs(o["cx"] - x) <= TOL and abs(o["cy"] - y) <= TOL for o in r),
                                f"« {motif} » : {[(o['cx'], o['cy']) for o in r]}")


class TestNormalisation(unittest.TestCase):
    def test_accents_casse_ponctuation(self):
        self.assertEqual(P.normaliser("ESPÈCES"), P.normaliser("espèces"))
        self.assertEqual(P.normaliser("Charli."), "charli")
        self.assertEqual(P.normaliser("Œdème"), "oedeme")
        self.assertEqual(P.normaliser("Voir l'ordonnance").split(), ["voir", "l", "ordonnance"])

    def test_chercher_saute_un_petit_mot(self):
        mots = [P._mot(t, 90, x, 10, l, 10, "ocr")
                for t, x, l in (("Valider", 0, 40), ("la", 45, 12), ("facture", 62, 45))]
        self.assertEqual(len(P.chercher(mots, "Valider facture")), 1)
        self.assertEqual(P.chercher(mots, "facture Valider"), [])


class TestDiff(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.img = Image.open(os.path.join(FIXTURES, "comptoir-2560.png")).convert("RGB")

    def test_zone_changee(self):
        b = self.img.copy()
        ImageDraw.Draw(b).rectangle((1200, 640, 1900, 950), fill=(250, 250, 250))
        d = P.diff(self.img, b)
        self.assertGreater(d["pct"], 1)
        self.assertEqual(len(d["zones"]), 1)
        x, y, l, h = d["zones"][0]
        self.assertLessEqual(abs(x - 1200) + abs(y - 640), 4)

    def test_bruit_ignore(self):
        b = self.img.copy()
        ImageDraw.Draw(b).rectangle((500, 500, 502, 502), fill=(255, 255, 255))
        self.assertEqual(P.diff(self.img, b)["zones"], [])

    def test_un_chiffre_qui_change_est_vu(self):
        # « Compté 3 fois » → « 4 » : ~12×18 px. Invisible avec l'ancien seuil.
        b = self.img.copy()
        ImageDraw.Draw(b).rectangle((700, 700, 711, 717), fill=(255, 255, 255))
        self.assertEqual(len(P.diff(self.img, b)["zones"]), 1)

    def test_exclusion(self):
        b = self.img.copy()
        ImageDraw.Draw(b).rectangle((0, 0, 2559, 30), fill=(255, 0, 0))
        self.assertEqual(P.diff(self.img, b, exclure=[(0, 0, 2560, 32)])["zones"], [])

    def test_identique(self):
        self.assertEqual(P.diff(self.img, self.img), {"pct": 0.0, "zones": []})

    def test_stable(self):
        images = iter([self.img, self.img.copy(), self.img.copy()])
        img, ok, _ = P.stable(lambda: next(images), max_s=1, pas=0.01)
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
