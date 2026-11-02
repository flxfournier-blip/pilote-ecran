"""Lot 6 — vigie evdev : clics et molette de Félix → STOP toujours ; clavier → STOP seulement pendant une frappe
d'agent (décision de Félix du 26/09/2026, « bornée, fail-open »). Sans périphérique réel."""
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from tests.outils import FIXTURES, ICI, Bac, lire

sys.path.insert(0, ICI)
import vigie  # noqa: E402

BTN_LEFT, BTN_RIGHT, KEY_A, KEY_ENTER, REL_X, REL_WHEEL = 0x110, 0x111, 30, 28, 0x00, 0x08


class TestMotif(unittest.TestCase):
    def test_clic_arrete_toujours(self):
        self.assertIsNotNone(vigie.motif_arret(vigie.EV_KEY, BTN_LEFT, 1, False))
        self.assertIsNotNone(vigie.motif_arret(vigie.EV_KEY, BTN_RIGHT, 1, True))

    def test_relachement_et_repetition_ignores(self):
        self.assertIsNone(vigie.motif_arret(vigie.EV_KEY, BTN_LEFT, 0, False))
        self.assertIsNone(vigie.motif_arret(vigie.EV_KEY, KEY_A, 2, True))

    def test_molette_arrete_mouvement_non(self):
        self.assertIsNotNone(vigie.motif_arret(vigie.EV_REL, REL_WHEEL, -1, False))
        self.assertIsNotNone(vigie.motif_arret(vigie.EV_REL, vigie.REL_WHEEL_HI_RES, 120, False))
        self.assertIsNone(vigie.motif_arret(vigie.EV_REL, REL_X, 15, False))   # le cadre juge le déplacement

    def test_mouvement_physique_arrete_pendant_deplacement_agent(self):
        for axe in (vigie.REL_X, vigie.REL_Y):
            for delta in (-1, 1, 80):
                with self.subTest(axe=axe, delta=delta):
                    self.assertIsNotNone(vigie.motif_arret(vigie.EV_REL, axe, delta, False, True))
                    self.assertIsNone(vigie.motif_arret(vigie.EV_REL, axe, delta, False, False))

    def test_mouvement_nul_ne_declenche_pas_stop(self):
        for axe in (vigie.REL_X, vigie.REL_Y):
            self.assertIsNone(vigie.motif_arret(vigie.EV_REL, axe, 0, False, True))

    def test_deplacement_ne_bloque_pas_le_clavier_hors_frappe_agent(self):
        self.assertIsNone(vigie.motif_arret(vigie.EV_KEY, KEY_A, 1, False, True))
        self.assertIsNotNone(vigie.motif_arret(vigie.EV_KEY, BTN_LEFT, 1, False, True))

    def test_clavier_seulement_pendant_une_frappe(self):
        self.assertIsNone(vigie.motif_arret(vigie.EV_KEY, KEY_A, 1, False))    # Félix écrit à Claude : libre
        self.assertIsNotNone(vigie.motif_arret(vigie.EV_KEY, KEY_ENTER, 1, True))

    def test_motif_sans_code_de_touche(self):
        m = vigie.motif_arret(vigie.EV_KEY, KEY_A, 1, True)
        self.assertNotIn("30", m)


class TestMarqueur(unittest.TestCase):
    def setUp(self):
        self.bac = Bac()
        self.patch = mock.patch("commun.ETAT", self.bac.etat)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.bac.fermer()

    def ecrire(self, texte):
        with open(self.bac.fichier("en-cours"), "w") as f:
            f.write(texte)

    def test_absent(self):
        self.assertFalse(vigie.frappe_agent_en_cours())

    def test_pid_vivant(self):
        self.ecrire(f"taper {os.getpid()}")
        self.assertTrue(vigie.frappe_agent_en_cours())
        self.ecrire(f"touche {os.getpid()}")
        self.assertTrue(vigie.frappe_agent_en_cours())

    def test_pid_mort_ou_commande_autre(self):
        p = subprocess.Popen(["true"]); p.wait()
        self.ecrire(f"taper {p.pid}")
        self.assertFalse(vigie.frappe_agent_en_cours())
        self.ecrire(f"cliquer {os.getpid()}")
        self.assertFalse(vigie.frappe_agent_en_cours())
        self.ecrire("n'importe quoi")
        self.assertFalse(vigie.frappe_agent_en_cours())

    def test_peripheriques_env(self):
        with mock.patch.dict(os.environ, {"PILOTE_VIGIE_DEVICES": "aucun"}):
            self.assertEqual(vigie.peripheriques(), [])
        with mock.patch.dict(os.environ, {"PILOTE_VIGIE_DEVICES": "/a:/b"}):
            self.assertEqual(vigie.peripheriques(), ["/a", "/b"])


class TestIntegration(unittest.TestCase):
    """`taper` pose `en-cours` pendant la frappe puis l'efface ; `etat` dit si la vigie tourne."""

    def setUp(self):
        self.bac = Bac()
        self.bac.ecran_fixe(os.path.join(FIXTURES, "comptoir-2560.png"))

    def tearDown(self):
        self.bac.fermer()

    def test_en_cours_efface_apres_taper(self):
        r = self.bac.lancer("taper", "abc", PILOTE_AGENT="claude")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(self.bac.fichier("en-cours")))

    def test_en_cours_pose_pendant_la_frappe(self):
        # Le faux xdotool recopie `en-cours` au moment de la frappe.
        r = self.bac.lancer("taper", "abc", PILOTE_AGENT="claude", FAUX_COPIE_EN_COURS="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("taper ", lire(os.path.join(self.bac.racine, "en-cours-vu")))

    def test_etat_vigie(self):
        r = self.bac.lancer("etat")
        self.assertIn("vigie : ABSENTE", r.stdout)
        with open(self.bac.fichier("vigie"), "w") as f:
            f.write(f"{os.getpid()} {time.time():.0f} 3")
        r = self.bac.lancer("etat")
        self.assertIn("vigie : active (3", r.stdout)


if __name__ == "__main__":
    unittest.main()
