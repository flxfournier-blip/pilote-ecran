"""cadre.py en vrai, mais hors écran (QT_QPA_PLATFORM=offscreen) : rien ne
s'affiche chez Félix, la logique du garde-fou tourne telle quelle."""
import os
import subprocess
import time
import unittest

from tests.outils import ICI, PYTHON_CADRE, Bac, lire

CADRE = os.path.join(ICI, "cadre.py")


@unittest.skipUnless(os.path.exists(PYTHON_CADRE), "venv PySide6 de charli-app absent")
class TestCadre(unittest.TestCase):
    def setUp(self):
        self.bac = Bac()
        self.proc = None

    def tearDown(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self.bac.fermer()

    def demarrer(self):
        env = self.bac.env(QT_QPA_PLATFORM="offscreen")
        self.proc = subprocess.Popen([PYTHON_CADRE, CADRE], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            if os.path.exists(self.bac.fichier("pret")):
                return
            time.sleep(0.1)
        self.fail("le cadre n'a pas écrit `pret` en 5 s")

    def vieillir(self, nom, secondes, contenu="x"):
        p = self.bac.fichier(nom)
        with open(p, "w") as f:
            f.write(contenu)
        t = time.time() - secondes
        os.utime(p, (t, t))

    def test_actif_perime_ne_tue_pas_le_cadre_relance(self):
        # 25/09 : relancé après une pause, le cadre lisait l'ancien `actif`
        # et mourait en 200 ms — l'action partait sans kill-switch.
        self.vieillir("actif", 1000)
        self.demarrer()
        time.sleep(1.5)
        self.assertIsNone(self.proc.poll(), "le cadre s'est arrêté tout seul")

    def test_stop_d_ecran_lu_sans_etre_ecrase(self):
        self.demarrer()
        with open(self.bac.fichier("stop"), "w") as f:
            f.write("[ecran] souris bougée pendant l'action")
        self.proc.wait(timeout=5)
        self.assertEqual(lire(self.bac.fichier("stop")),
                         "[ecran] souris bougée pendant l'action")

    def test_souris_recente_gardee(self):
        # Un geste de Félix pendant la pause doit compter au redémarrage.
        self.vieillir("souris", 60, "1234 567")
        self.demarrer()
        self.assertEqual(lire(self.bac.fichier("souris")), "1234 567")

    def test_souris_ancienne_remplacee(self):
        self.vieillir("souris", 1000, "1234 567")
        self.demarrer()
        self.assertNotEqual(lire(self.bac.fichier("souris")), "1234 567")

    def test_mouvement_ecrit_stop_prefixe(self):
        # Hors écran le pointeur est en 0,0 : une référence loin de là = geste.
        self.demarrer()
        with open(self.bac.fichier("souris"), "w") as f:
            f.write("900 900")
        self.proc.wait(timeout=5)
        self.assertTrue(lire(self.bac.fichier("stop")).startswith("[cadre] "))


if __name__ == "__main__":
    unittest.main()
