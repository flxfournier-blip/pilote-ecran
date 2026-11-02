"""Lot 3 — actions qui se vérifient : verdict avant/après, --verifier,
--avec, taper --dans/--entree, attendre à condition, diff. Faux écran qui
change au premier clic/frappe (voir outils.Bac.apres_action)."""
import json
import io
import os
import sys
import time
import unittest
from contextlib import ExitStack, redirect_stdout
from types import SimpleNamespace
from unittest import mock

from PIL import Image, ImageDraw, ImageFont

from tests.outils import FIXTURES, ICI, Bac

sys.path.insert(0, ICI)

COMPTOIR = os.path.join(FIXTURES, "comptoir-2560.png")
POLICE = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def ecrire_sur(bac, texte, nom="apres.png", boite=(1100, 600, 1900, 680)):
    """Copie du comptoir avec un bandeau clair portant `texte` (écran « après »)."""
    img = Image.open(COMPTOIR).convert("RGB")
    d = ImageDraw.Draw(img)
    d.rectangle(boite, fill=(245, 245, 245))
    d.text((boite[0] + 20, boite[1] + 20), texte, fill=(10, 10, 10),
           font=ImageFont.truetype(POLICE, 22))
    chemin = os.path.join(bac.racine, nom)
    img.save(chemin)
    return chemin


class Base(unittest.TestCase):
    def setUp(self):
        self.bac = Bac()
        self.bac.ecran_fixe(COMPTOIR)

    def tearDown(self):
        self.bac.fermer()

    def ordre(self):
        """Suite des actions xdotool (sous-commande + 1er argument utile)."""
        res = []
        for a in self.bac.injections():
            p = a.split()
            res.append(" ".join(p[1:3]) if p[1] in ("keydown", "keyup", "click") else p[1]
                       if p[1] != "key" else f"key {p[-1]}")
        return res


class TestVerdict(Base):
    def test_voir_sur_les_quatre_actions_et_profil_sans_ocr(self):
        for commande in (("cliquer", "500", "500"), ("taper", "secret-test"),
                         ("touche", "Tab"), ("defiler", "bas", "1")):
            with self.subTest(commande=commande[0]):
                for nom in ("elements.json", "texte.json"):
                    with open(self.bac.fichier(nom), "w") as f:
                        f.write("{}")
                r = self.bac.lancer(*commande, "--voir", "--profil")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("Image après action", r.stdout)
                self.assertNotIn("secret-test", r.stderr)
                profil = json.loads(next(l[7:] for l in r.stderr.splitlines() if l.startswith("PROFIL ")))
                phases = profil["phases_inclusives"]
                self.assertNotIn("ocr", phases)
                self.assertNotIn("atspi", phases)
                self.assertEqual(phases["capture"]["appels"], 3)
                self.assertGreater(phases["action_x11"]["appels"], 0)
                for nom in ("elements.json", "texte.json"):
                    self.assertFalse(os.path.exists(self.bac.fichier(nom)))
                with open(self.bac.fichier("ecran.json")) as f:
                    meta = json.load(f)
                self.assertEqual(meta["mode"], "apres-action")
                self.assertEqual(meta["echelle"], 0.75)
                with Image.open(self.bac.fichier("ecran.png")) as img:
                    self.assertEqual(img.size, (1920, 1080))

    def test_voir_verifier_echec_conserve_image_et_code(self):
        r = self.bac.lancer("cliquer", "500", "500", "--voir", "--verifier", "INEXISTANT")
        self.assertEqual(r.returncode, 2)
        self.assertIn("Image après action", r.stdout)
        self.assertTrue(os.path.exists(self.bac.fichier("ecran.json")))
        self.assertEqual(len(self.bac.clics()), 1)

    def test_voir_sous_stop_ne_clique_pas(self):
        with open(self.bac.fichier("stop"), "w") as f:
            f.write("[test] geste réel")
        r = self.bac.lancer("cliquer", "500", "500", "--voir", "--profil")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.bac.injections(), [])
        self.assertFalse(os.path.exists(self.bac.fichier("ecran.json")))

    def test_voir_rapide_zone_et_conversion_image(self):
        r = self.bac.lancer("voir", "--rapide", "--zone", "400", "200", "600", "300", "--profil")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.bac.fichier("ecran.json")) as f:
            meta = json.load(f)
        self.assertEqual(meta["taille"], [600, 300])
        self.assertEqual(meta["origine"], [400, 200])
        r = self.bac.lancer("souris", "10", "20", "--img")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("xdotool mousemove 410 220", self.bac.appels())

    def test_changement_localise(self):
        self.bac.apres_action(ecrire_sur(self.bac, "Facture validée"))
        r = self.bac.lancer("cliquer", "@Nouveau passage")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("→ changement", r.stdout)
        self.assertIn("1100,600", r.stdout)
        self.assertTrue(os.path.exists(self.bac.fichier("avant.png")))

    def test_aucun_changement(self):
        r = self.bac.lancer("cliquer", "500", "500")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("aucun changement visible", r.stdout)

    def test_verifier_vu(self):
        self.bac.apres_action(ecrire_sur(self.bac, "Facture validée"))
        r = self.bac.lancer("cliquer", "500", "500", "--verifier", "Facture validée")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("✓ « Facture validée » vu", r.stdout)

    def test_verifier_absent_exit_2_mais_clic_parti(self):
        r = self.bac.lancer("cliquer", "500", "500", "--verifier", "Facture validée")
        self.assertEqual(r.returncode, 2)
        self.assertIn("PAS vu", r.stdout)
        self.assertEqual(len(self.bac.clics()), 1)

    def test_diff_texte_apres_action(self):
        self.bac.apres_action(ecrire_sur(self.bac, "Facture validée"))
        self.bac.lancer("cliquer", "500", "500")
        r = self.bac.lancer("diff", "--texte")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("+ « Facture validée »", r.stdout)


class TestDiffChiffre(unittest.TestCase):
    """Un seul chiffre change (« Compté 3 fois » → « 4 ») : diff --texte le dit."""

    def test_chiffre(self):
        bac = Bac()
        try:
            police = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
            images = {}
            for n in (3, 4):
                im = Image.new("RGB", (2560, 1440), "white")
                ImageDraw.Draw(im).text((600, 420), f"Compté {n} fois", fill="black", font=police)
                images[n] = os.path.join(bac.racine, f"{n}.png")
                im.save(images[n])
            bac.ecran_fixe(images[4])
            import shutil
            shutil.copy(images[3], bac.fichier("avant.png"))
            r = bac.lancer("diff", "--texte")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("+ « Compté 4 fois »", r.stdout)
            self.assertIn("− « Compté 3 fois »", r.stdout)
        finally:
            bac.fermer()


class TestModificateurs(Base):
    def test_clic_lent_en_cli_arrive_avant_de_cliquer(self):
        r = self.bac.lancer("cliquer", "500", "500", "--lent", "--attendre", "0")
        self.assertEqual(r.returncode, 0, r.stderr)
        injections = self.bac.injections()
        mouvements = [a for a in injections if a.startswith("xdotool mousemove ")]
        self.assertGreater(len(mouvements), 20)
        self.assertEqual(mouvements[-1], "xdotool mousemove 500 500")
        self.assertEqual(injections[-1], "xdotool click 1")
        self.assertFalse(os.path.exists(self.bac.fichier("occupe")))
        self.assertFalse(os.path.exists(self.bac.fichier("stop")))

    def test_ctrl_clic_relache(self):
        r = self.bac.lancer("cliquer", "500", "500", "--avec", "ctrl+shift")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ordre(), ["mousemove", "keydown ctrl", "keydown shift", "click 1",
                                        "keyup shift", "keyup ctrl"])

    def test_modificateur_inconnu(self):
        r = self.bac.lancer("cliquer", "500", "500", "--avec", "meta")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.bac.injections(), [])


class TestTaper(Base):
    def test_dans_champ_puis_entree(self):
        r = self.bac.lancer("taper", "bonjour", "--dans", "@Dépose une ordonnance", "--entree")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ordre(), ["mousemove", "click 1", "type", "key Return"])
        x, y = self.bac.clics()[0]
        self.assertLess(abs(x - 165) + abs(y - 1116), 16)
        self.assertIn("puis Entrée", r.stdout)

    def test_dans_champ_introuvable_rien_ne_part(self):
        r = self.bac.lancer("taper", "bonjour", "--dans", "@Champ inexistant", "--entree")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.bac.injections(), [])

    def test_dans_mauvaise_fenetre(self):
        r = self.bac.lancer("taper", "bonjour", "--dans", "Mailspring", "--entree")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("rien n'est envoyé", r.stderr)
        self.assertEqual(self.bac.injections(), [])

    def test_touche_dans_bonne_fenetre(self):
        r = self.bac.lancer("touche", "Return", "--dans", "Fenetre de test")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ordre(), ["key Return"])

    def test_touche_dans_mauvaise_fenetre(self):
        r = self.bac.lancer("touche", "Return", "--dans", "Mailspring")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.bac.injections(), [])

    def test_coller_ne_laisse_pas_le_texte(self):
        # Le faux xclip ne charge rien : échec, mais coller.txt doit être effacé.
        r = self.bac.lancer("taper", "secret multiligne\nligne 2", "--coller")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(self.bac.fichier("coller.txt")))
        self.assertNotIn("key ctrl+v", self.ordre())


class TestAttendre(Base):
    def test_texte_present(self):
        r = self.bac.lancer("attendre", "--texte", "Nouveau passage", "--max", "5")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("✓ « Nouveau passage » vu", r.stdout)

    def test_texte_absent_exit_2_et_proches(self):
        t = time.monotonic()
        r = self.bac.lancer("attendre", "--texte", "Facture validée", "--max", "1")
        self.assertEqual(r.returncode, 2)
        self.assertIn("pas vu", r.stdout)
        self.assertIn("proche :", r.stdout)
        self.assertLess(time.monotonic() - t, 15)

    def test_disparu(self):
        r = self.bac.lancer("attendre", "--disparu", "Charli réfléchit", "--max", "5")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_stable(self):
        r = self.bac.lancer("attendre", "--stable", "1", "--max", "10", "--pas", "300")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("immobile", r.stdout)

    def test_permis_sous_stop(self):
        with open(self.bac.fichier("stop"), "w") as f:
            f.write("[cadre] geste")
        r = self.bac.lancer("attendre", "--texte", "Nouveau passage", "--max", "5")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_prolonge_la_reservation_du_detenteur_seulement(self):
        with open(self.bac.fichier("pilote"), "w") as f:
            f.write('{"agent": "test"}')
        vieux = time.time() - 200
        with open(self.bac.fichier("actif"), "w") as f:
            f.write("x")
        os.utime(self.bac.fichier("actif"), (vieux, vieux))
        self.bac.lancer("attendre", "--texte", "Nouveau passage", "--max", "5")
        self.assertGreater(os.path.getmtime(self.bac.fichier("actif")), time.time() - 30)
        # Un autre agent qui attend ne prolonge rien.
        os.utime(self.bac.fichier("actif"), (vieux, vieux))
        self.bac.lancer("attendre", "--texte", "Nouveau passage", "--max", "5", PILOTE_AGENT="codex")
        self.assertLess(os.path.getmtime(self.bac.fichier("actif")), time.time() - 100)

    def test_pause_simple_toujours_la(self):
        r = self.bac.lancer("attendre", "0")
        self.assertEqual(r.returncode, 0)
        self.assertIn("attendu 0 s", r.stdout)


class TestActionsCarte(unittest.TestCase):
    """Vrai contexte agir, transport et X11 simulés : aucune action sur le bureau."""
    def setUp(self):
        import commun
        import ecran
        self.ecran = ecran
        self.bac = Bac()
        self.addCleanup(self.bac.fermer)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ, self.bac.env()))
        self.stack.enter_context(mock.patch.object(ecran, "ETAT", self.bac.etat))
        self.stack.enter_context(mock.patch.object(commun, "ETAT", self.bac.etat))
        self.stack.enter_context(mock.patch.object(ecran, "cadre_demarrer"))
        self.stack.enter_context(mock.patch.object(ecran, "stop_reel", return_value=None))
        self.stack.enter_context(mock.patch.object(ecran, "pointeur", return_value=(100, 100)))
        self.stack.enter_context(mock.patch.object(ecran, "fenetre_active", return_value=("4242", "Test", 1)))
        self.stack.enter_context(mock.patch.object(ecran, "ecran_taille", return_value=(1920, 1080)))
        self.capture = self.stack.enter_context(mock.patch.object(ecran, "capture_texte", side_effect=AssertionError("capture interdite")))
        self.stack.enter_context(mock.patch.object(ecran, "verdict", side_effect=AssertionError("analyse image interdite")))
        self.xdotool = self.stack.enter_context(mock.patch.object(ecran, "xdotool"))
        self.souris = self.stack.enter_context(mock.patch.object(ecran, "bouger_souris"))
        self.sh = self.stack.enter_context(mock.patch.object(ecran, "sh", return_value=("", "")))
        self.sortie = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.sortie))
        self.element = {"id": "objet-1", "wid": 4242, "x": 90, "y": 90, "w": 20, "h": 20,
                        "role": "bouton", "nom": "Tester", "etats": []}
        self.cible = {"element": self.element, "fenetre": {"wid": 4242}, "session": "s", "revision": 1}
        self.ouvrir = self.stack.enter_context(mock.patch("urllib.request.urlopen", side_effect=self.reponse))

    def reponse(self, requete, **kw):
        url = requete if isinstance(requete, str) else requete.full_url
        if "/carte/cible?" in url:
            self.assertTrue(os.path.exists(self.bac.fichier("occupe")), "résolution hors du verrou d'action")
            d = self.cible
        else:
            d = {"session": "s", "revision": 2, "fenetres": [], "contexte": {"fenetre_active": "0x1092"}}
        return io.BytesIO(json.dumps(d).encode())

    def test_clic_stable_sans_capture_et_sans_activation(self):
        self.ecran.cmd_cliquer(["id:objet-1", "--attendre", "0"])
        self.souris.assert_called_once_with(100, 100)
        self.xdotool.assert_called_once_with("click", "1")
        self.capture.assert_not_called()
        self.assertIn("révision 1 à 2", self.sortie.getvalue())
        self.assertIn("non vérifié", self.sortie.getvalue())

    def test_clic_lent_revalide_la_cible_apres_le_trajet(self):
        with mock.patch.object(self.ecran, "bouger_souris_lent") as trajet:
            self.ecran.cmd_cliquer(["id:objet-1", "--lent", "--attendre", "0"])
        self.assertEqual(trajet.call_count, 1)
        self.assertEqual(trajet.call_args.args[:2], (100, 100))
        resolutions = [c for c in self.ouvrir.call_args_list if "/carte/cible?" in c.args[0]]
        self.assertEqual(len(resolutions), 2)
        self.xdotool.assert_called_once_with("click", "1")
        self.capture.assert_not_called()

    def test_cible_changee_pendant_approche_refuse_clic(self):
        for champ, valeur in (("x", 120), ("nom", "Autre action")):
            with self.subTest(champ=champ):
                ancien = self.element[champ]
                def changement(*args):
                    self.element[champ] = valeur
                with mock.patch.object(self.ecran, "bouger_souris_lent", side_effect=changement):
                    with self.assertRaisesRegex(SystemExit, "cible changée"):
                        self.ecran.cmd_cliquer(["id:objet-1", "--lent"])
                self.element[champ] = ancien
        self.xdotool.assert_not_called()

    def test_stop_a_la_fin_de_l_approche_interdit_le_clic(self):
        def arret(*args):
            with open(self.bac.fichier("stop"), "w") as f:
                f.write("[vigie] mouvement de Félix pendant le déplacement")
        with mock.patch.object(self.ecran, "bouger_souris_lent", side_effect=arret):
            with self.assertRaises(SystemExit):
                self.ecran.cmd_cliquer(["id:objet-1", "--lent", "--avec", "ctrl"])
        self.xdotool.assert_not_called()

    def test_double_clic_lent_interrompu_entre_les_clics(self):
        def premier_clic(*args):
            with open(self.bac.fichier("stop"), "w") as f:
                f.write("[vigie] geste après le premier clic")
        self.xdotool.side_effect = premier_clic
        with mock.patch.object(self.ecran, "bouger_souris_lent"):
            with self.assertRaises(SystemExit):
                self.ecran.cmd_cliquer(["id:objet-1", "--lent", "--double"])
        self.xdotool.assert_called_once_with("click", "1")

    def test_autre_fenetre_ne_declenche_ni_activation_ni_clic(self):
        self.element["wid"] = 999
        with self.assertRaisesRegex(SystemExit, "autre fenêtre"):
            self.ecran.cmd_cliquer(["id:objet-1"])
        self.souris.assert_not_called()
        self.xdotool.assert_not_called()

    def test_stop_refuse_avant_http(self):
        with open(self.bac.fichier("stop"), "w") as f:
            f.write("[test] souris bougée")
        with self.assertRaises(SystemExit):
            self.ecran.cmd_cliquer(["id:objet-1"])
        self.ouvrir.assert_not_called()
        self.xdotool.assert_not_called()

    def test_reservation_autre_agent_refuse_avant_http(self):
        with open(self.bac.fichier("pilote"), "w") as f:
            json.dump({"agent": "claude"}, f)
        with open(self.bac.fichier("actif"), "w") as f:
            f.write(str(time.time()))
        with self.assertRaises(SystemExit):
            self.ecran.cmd_cliquer(["id:objet-1"])
        self.ouvrir.assert_not_called()
        self.xdotool.assert_not_called()

    def test_cible_perimee_refuse_clic(self):
        import urllib.error
        self.ouvrir.side_effect = urllib.error.HTTPError(
            "http://localhost", 409, "Conflict", {}, io.BytesIO(b'{"erreur":"cible perimee"}'))
        with self.assertRaisesRegex(SystemExit, "cible perimee"):
            self.ecran.cmd_cliquer(["id:objet-1"])
        self.xdotool.assert_not_called()

    def test_stop_apparait_durant_resolution(self):
        def reponse(*args, **kw):
            with open(self.bac.fichier("stop"), "w") as f:
                f.write("[test] arrêt pendant HTTP")
            return self.reponse(*args, **kw)
        self.ouvrir.side_effect = reponse
        with self.assertRaises(SystemExit):
            self.ecran.cmd_cliquer(["id:objet-1"])
        self.souris.assert_not_called()
        self.xdotool.assert_not_called()

    def test_fenetre_change_pendant_resolution(self):
        self.ecran.fenetre_active.side_effect = [("4242", "Test", 1), ("999", "Autre", 2)]
        with self.assertRaisesRegex(SystemExit, "changée avant le clic"):
            self.ecran.cmd_cliquer(["id:objet-1"])
        self.souris.assert_not_called()
        self.xdotool.assert_not_called()
        self.assertFalse(os.path.exists(self.bac.fichier("stop")), "un refus de fenêtre ne simule pas un geste de Félix")

    def test_souris_change_pendant_resolution(self):
        self.ecran.pointeur.side_effect = [(100, 100), (160, 160), (160, 160)]
        with self.assertRaisesRegex(SystemExit, "souris bougée"):
            self.ecran.cmd_cliquer(["id:objet-1"])
        self.xdotool.assert_not_called()
        self.assertTrue(os.path.exists(self.bac.fichier("stop")))

    def test_frappe_et_touches_sans_capture(self):
        self.ecran.cmd_taper(["bonjour", "--sans-capture", "--attendre", "0", "--dans", "Test"])
        self.xdotool.assert_called_once_with("type", "--clearmodifiers", "--delay", "2", "--", "bonjour")
        self.ecran.cmd_touche(["Tab", "--sans-capture", "--attendre", "0", "--dans", "Test"])
        self.sh.assert_called_once_with("xdotool", "key", "--clearmodifiers", "--delay", "60", "Tab", stderr_aussi=True)
        self.capture.assert_not_called()

    def test_frappe_refuse_fenetre_changee_depuis_http_sans_dans(self):
        self.ecran.fenetre_active.return_value = ("999", "Autre", 2)
        with self.assertRaisesRegex(SystemExit, "changée depuis la carte"):
            self.ecran.cmd_taper(["bonjour", "--sans-capture", "--attendre", "0"])
        self.xdotool.assert_not_called()

    def test_touche_refuse_fenetre_changee_depuis_http_sans_dans(self):
        self.ecran.fenetre_active.return_value = ("999", "Autre", 2)
        with self.assertRaisesRegex(SystemExit, "changée depuis la carte"):
            self.ecran.cmd_touche(["Return", "--sans-capture", "--attendre", "0"])
        self.sh.assert_not_called()

    def test_serie_touches_refuse_nouvelle_fenetre(self):
        self.ecran.fenetre_active.side_effect = [("4242", "Test", 1), ("999", "Autre", 2)]
        with self.assertRaisesRegex(SystemExit, "changée depuis la carte"):
            self.ecran.cmd_touche(["Tab", "Return", "--sans-capture", "--attendre", "0"])
        self.sh.assert_called_once_with("xdotool", "key", "--clearmodifiers", "--delay", "60", "Tab", stderr_aussi=True)

    def test_entree_apres_frappe_refuse_nouvelle_fenetre(self):
        self.ecran.fenetre_active.side_effect = [("4242", "Test", 1), ("999", "Autre", 2)]
        with self.assertRaisesRegex(SystemExit, "changée depuis la carte"):
            self.ecran.cmd_taper(["bonjour", "--entree", "--sans-capture", "--attendre", "0"])
        self.xdotool.assert_called_once_with("type", "--clearmodifiers", "--delay", "2", "--", "bonjour")

    def test_carte_sans_fenetre_active_refuse_frappe(self):
        with mock.patch.object(self.ecran, "lire_carte", return_value={"session": "s", "revision": 2}):
            with self.assertRaisesRegex(SystemExit, "non décrite par la carte"):
                self.ecran.cmd_taper(["bonjour", "--sans-capture", "--attendre", "0"])
        self.xdotool.assert_not_called()

    def test_defilement_sans_capture(self):
        self.ecran.cmd_defiler(["bas", "2", "--sans-capture", "--attendre", "0"])
        self.xdotool.assert_called_once_with("click", "--repeat", "2", "--delay", "40", "5")
        self.capture.assert_not_called()

    def test_voir_explicite_conserve_la_verification_visuelle(self):
        self.capture.side_effect = None
        self.ecran.verdict.side_effect = None
        self.ecran.verdict.return_value = ([], None)
        self.ecran.cmd_cliquer(["id:objet-1", "--voir", "--attendre", "0"])
        self.capture.assert_called_once_with()
        self.ecran.verdict.assert_called_once_with(self.capture.return_value, 0.0, None, voir=True)

    def test_ancienne_action_conserve_la_verification_visuelle(self):
        self.capture.side_effect = None
        self.ecran.verdict.side_effect = None
        self.ecran.verdict.return_value = ([], None)
        self.ecran.cmd_taper(["bonjour", "--attendre", "0"])
        self.capture.assert_called_once_with()
        self.ecran.verdict.assert_called_once_with(self.capture.return_value, 0.0, None, voir=False)
        self.ouvrir.assert_not_called()

    def test_modes_incompatibles_refuses_sans_action(self):
        for args in (["500", "500", "--sans-capture", "--voir"],
                     ["500", "500", "--sans-capture", "--verifier", "texte"],
                     ["@texte", "--sans-capture"], ["id:objet-1", "--img"]):
            with self.subTest(args=args), self.assertRaises(SystemExit):
                self.ecran.cmd_cliquer(args)
        self.ouvrir.assert_not_called()
        self.xdotool.assert_not_called()


class TestMouvementLent(unittest.TestCase):
    """Pointeur et horloge simulés ; jamais d'injection sur le vrai bureau."""

    def setUp(self):
        import commun
        import ecran
        self.ecran = ecran
        self.bac = Bac()
        self.addCleanup(self.bac.fermer)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(commun, "ETAT", self.bac.etat))
        self.stack.enter_context(mock.patch.object(ecran, "stop_reel", return_value=None))
        self.position, self.horloge, self.mouvements = (100, 200), 0.0, []
        self.ctx = SimpleNamespace(depart=self.position, vers=(1100, 700))
        self.stack.enter_context(mock.patch.object(ecran, "pointeur", side_effect=lambda: self.position))
        self.stack.enter_context(mock.patch.object(ecran, "xdotool", side_effect=self.injecter))
        self.stack.enter_context(mock.patch.object(ecran.time, "monotonic", side_effect=lambda: self.horloge))
        self.pause = self.stack.enter_context(mock.patch.object(ecran.time, "sleep", side_effect=self.avancer))

    def avancer(self, duree):
        self.horloge += duree

    def injecter(self, cmd, x, y):
        self.assertEqual(cmd, "mousemove")
        self.position = (int(x), int(y))
        self.mouvements.append(self.position)

    def test_trajet_progressif_borne_et_arrive_a_destination(self):
        self.ecran.bouger_souris_lent(1100, 700, self.ctx)
        self.assertEqual(self.position, (1100, 700))
        self.assertEqual(self.ctx.vers, self.position)
        self.assertAlmostEqual(self.horloge, 2)
        self.assertGreater(len(self.mouvements), 20)
        self.assertTrue(all(100 <= x <= 1100 and 200 <= y <= 700 for x, y in self.mouvements))
        self.assertEqual(self.mouvements, sorted(self.mouvements))

    def test_deja_sur_la_cible_ne_bouge_pas(self):
        self.ecran.bouger_souris_lent(100, 200, self.ctx)
        self.assertEqual(self.mouvements, [])
        self.pause.assert_not_called()

    def test_stop_pendant_pause_aucun_pas_supplementaire(self):
        def pause(duree):
            self.avancer(duree)
            if self.horloge >= .6:
                with open(self.bac.fichier("stop"), "w") as f:
                    f.write("[vigie] geste physique")
                self.dernier_nombre = len(self.mouvements)
        self.pause.side_effect = pause
        with self.assertRaises(SystemExit) as arret:
            self.ecran.bouger_souris_lent(1100, 700, self.ctx)
        self.assertEqual(arret.exception.code, 3)
        self.assertEqual(len(self.mouvements), self.dernier_nombre)
        self.assertNotEqual(self.position, (1100, 700))

    def test_mouvement_humain_d_un_pixel_non_ecrase(self):
        def pause(duree):
            self.avancer(duree)
            if self.horloge >= .6:
                self.position = (self.position[0] + 1, self.position[1])
                self.position_humaine = self.position
        self.pause.side_effect = pause
        with self.assertRaises(SystemExit):
            self.ecran.bouger_souris_lent(1100, 700, self.ctx)
        self.assertEqual(self.position, self.position_humaine)
        self.assertNotEqual(self.position, self.mouvements[-1])
        self.assertTrue(os.path.exists(self.bac.fichier("stop")))

    def test_retard_excessif_annule_sans_faux_geste_humain(self):
        self.pause.side_effect = lambda duree: self.avancer(6)
        with self.assertRaisesRegex(SystemExit, "trop longue"):
            self.ecran.bouger_souris_lent(1100, 700, self.ctx)
        self.assertEqual(self.mouvements, [])
        self.assertEqual(self.ctx.vers, self.position)
        self.assertFalse(os.path.exists(self.bac.fichier("stop")))

    def test_dernier_pas_trop_lent_refuse_aussi(self):
        def injecter(cmd, x, y):
            self.injecter(cmd, x, y)
            if self.position == (1100, 700):
                self.horloge += 6
        with mock.patch.object(self.ecran, "xdotool", side_effect=injecter):
            with self.assertRaisesRegex(SystemExit, "trop longue"):
                self.ecran.bouger_souris_lent(1100, 700, self.ctx)
        self.assertEqual(self.ctx.vers, self.position)


if __name__ == "__main__":
    unittest.main()
