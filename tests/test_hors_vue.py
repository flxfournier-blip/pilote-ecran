"""01/11/2026 — un élément AT-SPI défilé hors du cadre de sa fenêtre ne doit jamais être cliqué : le CIC annonçait son
bouton « Valider » en y = 1391 sous une fenêtre finissant en 1373, et le clic partait sur le dock (qui lançait Zoom)."""
import os
import sys
import unittest

ICI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ICI)

import ecran  # noqa: E402

ECRAN = (2560, 1440)
FENETRE = (0, 32, 2560, 1341)          # Firefox maximisé : visible de y = 32 à 1373


def element(x, y, w=88, h=36):
    return {"x": x, "y": y, "w": w, "h": h}


class TestHorsVue(unittest.TestCase):
    def test_bouton_sous_le_bas_de_la_fenetre(self):
        # Centre en 1361,1391 : sous le bord visible (1373) → hors vue.
        self.assertTrue(ecran.hors_vue(element(1317, 1373), FENETRE, ECRAN))

    def test_bouton_visible(self):
        self.assertFalse(ecran.hors_vue(element(1317, 749), FENETRE, ECRAN))

    def test_hors_ecran_meme_sans_geometrie(self):
        self.assertTrue(ecran.hors_vue(element(2600, 100), None, ECRAN))
        self.assertTrue(ecran.hors_vue(element(100, -80), None, ECRAN))

    def test_sans_geometrie_dans_l_ecran(self):
        self.assertFalse(ecran.hors_vue(element(100, 100), None, ECRAN))

    def test_a_gauche_de_la_fenetre(self):
        self.assertTrue(ecran.hors_vue(element(10, 500), (640, 222, 1280, 960), ECRAN))


if __name__ == "__main__":
    unittest.main()
