"""Lot 1 — couche texte branchée dans `ecran` : texte, chercher, cliquer tN /
@"texte", lire --zone/tN/@, ligne ETAT et codes de sortie. Faux écran = capture
figée du comptoir ; les clics sont lus dans le journal du faux xdotool."""
import json
import os
import sys
import unittest
import unittest.mock

from PIL import Image, ImageDraw

from tests.outils import FIXTURES, ICI, Bac, lire

sys.path.insert(0, ICI)

COMPTOIR = os.path.join(FIXTURES, "comptoir-2560.png")
ATTENDUS = json.load(open(os.path.join(FIXTURES, "attendus.json")))
TOL = ATTENDUS["tolerance_px"]


def proche(p, q):
    return abs(p[0] - q[0]) <= TOL and abs(p[1] - q[1]) <= TOL


class Base(unittest.TestCase):
    def setUp(self):
        self.bac = Bac()
        self.bac.ecran_fixe(COMPTOIR)

    def tearDown(self):
        self.bac.fermer()


class TestTexte(Base):
    def test_texte_ecran_entier(self):
        r = self.bac.lancer("texte")
        self.assertEqual(r.returncode, 0, r.stderr)
        premiere, *reste = r.stdout.splitlines()
        self.assertEqual(premiere, "ETAT ok")
        self.assertIn("« Nouveau passage »", r.stdout)
        d = json.loads(lire(self.bac.fichier("texte.json")))
        self.assertGreater(len(d["lignes"]), 80)
        self.assertTrue(all(li["id"].startswith("t") for li in d["lignes"]))

    def test_texte_json_et_zone(self):
        r = self.bac.lancer("texte", "--zone", "2300", "0", "260", "70", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        ligne = next(li for li in d["lignes"] if "Nouveau passage" in li["texte"])
        self.assertTrue(proche((ligne["cx"], ligne["cy"]),
                               ATTENDUS["comptoir-2560.png"]["Nouveau passage"]))

    def test_etiquette_du_cadre_ignoree(self):
        from PIL import ImageFont
        import perception as P
        img = Image.open(COMPTOIR).convert("RGB")
        dessin = ImageDraw.Draw(img)
        police = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 15)
        dessin.rectangle((900, 1180, 1700, 1212), fill=(240, 138, 36))
        dessin.text((914, 1187), "CLAUDE PILOTE L'ÉCRAN — bouge la souris pour l'arrêter",
                    fill=(12, 12, 12), font=police)
        chemin = os.path.join(self.bac.racine, "avec-cadre.png")
        img.save(chemin)
        # Témoin : sans filtre, l'OCR lit bien l'étiquette…
        self.assertTrue(P.chercher(P.ocr(img.crop((880, 1170, 1720, 1222))), "bouge la souris"))
        # … et `ecran texte` la retire.
        self.bac.extra["PILOTE_CAPTURE_FIXE"] = chemin
        r = self.bac.lancer("texte")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("bouge la souris", r.stdout)
        self.assertIn("Nouveau passage", r.stdout)


class TestChercher(Base):
    def test_plusieurs_motifs_json(self):
        r = self.bac.lancer("chercher", "Nouveau passage", "Carte bleue", "Envoyer", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        res = json.loads(r.stdout)["resultats"]
        for motif in ("Nouveau passage", "Carte bleue", "Envoyer"):
            with self.subTest(motif=motif):
                self.assertTrue(any(proche((o["cx"], o["cy"]), ATTENDUS["comptoir-2560.png"][motif])
                                    for o in res[motif]), res[motif])

    def test_introuvable_exit_2_et_proches(self):
        r = self.bac.lancer("chercher", "Valider la facture")
        self.assertEqual(r.returncode, 2)
        self.assertIn("INTROUVABLE", r.stdout)
        self.assertEqual(r.stdout.count("proche :"), 3)

    def test_occurrences_multiples_numerotees(self):
        r = self.bac.lancer("chercher", "Espèces")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("@3", r.stdout)
        self.assertIn("--n K", r.stdout)


class TestCliquer(Base):
    def test_cliquer_texte(self):
        r = self.bac.lancer("cliquer", "@Nouveau passage")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.bac.clics()), 1)
        self.assertTrue(proche(self.bac.clics()[0],
                               ATTENDUS["comptoir-2560.png"]["Nouveau passage"]))
        self.assertIn("Nouveau passage", r.stdout)

    def test_ambigu_refuse_sans_n(self):
        r = self.bac.lancer("cliquer", "@Espèces")
        self.assertEqual(r.returncode, 2)
        self.assertIn("ambigu", r.stderr)
        self.assertEqual(self.bac.injections(), [])

    def test_ambigu_avec_n(self):
        r = self.bac.lancer("cliquer", "@Espèces", "--n", "3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(proche(self.bac.clics()[0], (296, 1376)), self.bac.clics())

    def test_zone_leve_l_ambiguite(self):
        r = self.bac.lancer("cliquer", "@Espèces", "--zone", "0", "1300", "1400", "140")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(proche(self.bac.clics()[0], (296, 1376)), self.bac.clics())

    def test_introuvable_rien_ne_part(self):
        r = self.bac.lancer("cliquer", "@Valider la facture")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.bac.injections(), [])

    def test_cliquer_ligne_tN(self):
        self.bac.lancer("texte")
        d = json.loads(lire(self.bac.fichier("texte.json")))
        li = next(x for x in d["lignes"] if x["texte"] == "Nouveau passage")
        r = self.bac.lancer("cliquer", li["id"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.bac.clics(), [(li["cx"], li["cy"])])

    def test_ligne_tN_changee_refusee(self):
        self.bac.lancer("texte")
        d = json.loads(lire(self.bac.fichier("texte.json")))
        li = next(x for x in d["lignes"] if x["texte"] == "Nouveau passage")
        img = Image.open(COMPTOIR).convert("RGB")
        ImageDraw.Draw(img).rectangle((li["x"], li["y"], li["x"] + li["l"], li["y"] + li["h"]),
                                      fill=(255, 255, 255))
        autre = os.path.join(self.bac.racine, "change.png")
        img.save(autre)
        self.bac.extra["PILOTE_CAPTURE_FIXE"] = autre
        r = self.bac.lancer("cliquer", li["id"])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("a changé", r.stderr)
        self.assertEqual(self.bac.injections(), [])

    def test_sous_stop_exit_3_sans_clic(self):
        with open(self.bac.fichier("stop"), "w") as f:
            f.write("[cadre] geste de Félix")
        r = self.bac.lancer("cliquer", "@Nouveau passage")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(self.bac.injections(), [])

    def test_reserve_par_autre_exit_4(self):
        with open(self.bac.fichier("pilote"), "w") as f:
            f.write('{"agent": "codex"}')
        with open(self.bac.fichier("actif"), "w") as f:
            f.write("1")
        r = self.bac.lancer("cliquer", "500", "500")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertEqual(self.bac.injections(), [])


class TestLire(Base):
    def test_lire_zone(self):
        r = self.bac.lancer("lire", "--zone", "2300", "0", "260", "70")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("ETAT "))
        self.assertIn("Nouveau passage", r.stdout)

    def test_lire_autour_d_une_ancre(self):
        r = self.bac.lancer("lire", "@Dépose une ordonnance", "--autour", "2900x120")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Envoyer", r.stdout)

    def test_lire_sous_stop_permis(self):
        with open(self.bac.fichier("stop"), "w") as f:
            f.write("[cadre] geste de Félix")
        r = self.bac.lancer("lire", "--zone", "2300", "0", "260", "70")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("ETAT STOP"))


class TestSecuriteCaptureFixe(unittest.TestCase):
    def test_ignoree_sur_le_vrai_dossier_d_etat(self):
        # Une image inventée ne doit jamais guider un clic sur le VRAI écran.
        import sys
        from tests.outils import ICI
        sys.path.insert(0, ICI)
        import ecran
        vrai = f"/run/user/{os.getuid()}/pilote-ecran"
        ancien = ecran.ETAT
        try:
            ecran.ETAT = vrai
            os.environ["PILOTE_CAPTURE_FIXE"] = COMPTOIR
            with unittest.mock.patch("perception.capturer", return_value="REEL"):
                self.assertEqual(ecran.capture_texte(), "REEL")
        finally:
            ecran.ETAT = ancien
            os.environ.pop("PILOTE_CAPTURE_FIXE", None)




if __name__ == "__main__":
    unittest.main()
