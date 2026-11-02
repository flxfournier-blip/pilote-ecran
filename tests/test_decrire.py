"""Lot 7 — `decrire` (Qwen local) : fonctions pures, sans modèle. Le modèle décrit ; l'OCR vérifie ce qu'il cite."""
import os
import sys
import unittest

from PIL import Image

from tests.outils import FIXTURES, ICI

sys.path.insert(0, ICI)
import ecran  # noqa: E402
import perception as P  # noqa: E402


class TestDecrire(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mots = P.ocr(Image.open(os.path.join(FIXTURES, "comptoir-2560.png")).convert("RGB"))

    def test_verdict(self):
        self.assertEqual(ecran.verdict_question("OUI. Je vois « Espèces »."), "OUI")
        self.assertEqual(ecran.verdict_question("**non**, rien"), "NON")
        self.assertEqual(ecran.verdict_question("Je pense que oui"), "INCERTAIN")

    def test_citations_confirmees_ou_non(self):
        texte = "On voit « Nouveau passage » et « Bouton Imaginaire Absent »."
        res = dict(ecran.confirmer_citations(texte, self.mots))
        self.assertIsNotNone(res["Nouveau passage"])
        self.assertIsNone(res["Bouton Imaginaire Absent"])

    def test_contradiction_non_alors_que_lu(self):
        a = ecran.contradiction("Y a-t-il un bouton Biper ?", "NON", "NON, pas de « Biper ».", self.mots)
        self.assertTrue(a and "Qwen dit NON" in a[0])

    def test_contradiction_oui_alors_qu_absent(self):
        a = ecran.contradiction("Y a-t-il un bouton Valider la facture ?", "OUI", "OUI : « Valider la facture ».", self.mots)
        self.assertTrue(a and "Qwen dit OUI" in a[0])

    def test_pas_de_contradiction(self):
        self.assertEqual(ecran.contradiction("Y a-t-il un bouton Biper ?", "OUI", "OUI : « Biper ».", self.mots), [])


if __name__ == "__main__":
    unittest.main()
