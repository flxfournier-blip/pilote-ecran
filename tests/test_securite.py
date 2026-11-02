"""Lot 5 — garde-fous et multi-agents (décisions de Félix du 26/09/2026). Sans écran : faux xdotool, faux cadre."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests.outils import FIXTURES, ICI, PYTHON_CADRE, Bac, lire

sys.path.insert(0, ICI)
COMPTOIR = os.path.join(FIXTURES, "comptoir-2560.png")


class Base(unittest.TestCase):
    def setUp(self):
        self.bac = Bac()
        self.bac.ecran_fixe(COMPTOIR)

    def tearDown(self):
        self.bac.fermer()

    def pilote(self, agent, age=0):
        with open(self.bac.fichier("pilote"), "w") as f:
            f.write(json.dumps({"agent": agent}))
        with open(self.bac.fichier("actif"), "w") as f:
            f.write("x")
        t = time.time() - age
        os.utime(self.bac.fichier("actif"), (t, t))

    def journal(self):
        try:
            return [json.loads(l) for l in lire(self.bac.fichier("journal.ndjson")).splitlines()]
        except OSError:
            return []


class TestIdentite(Base):
    def test_claude_deduit_du_client(self):
        r = self.bac.lancer("souris", "10", "10", PILOTE_AGENT="", CLAUDECODE="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(lire(self.bac.fichier("pilote")))["agent"], "claude")

    def test_codex_deduit_du_client(self):
        env = {k: "" for k in os.environ if k.startswith("CLAUDE")}
        r = self.bac.lancer("souris", "10", "10", PILOTE_AGENT="", CODEX_HOME="/tmp/x", **env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(lire(self.bac.fichier("pilote")))["agent"], "codex")

    def test_jamais_anonyme_ppid(self):
        import ecran
        with mock.patch.dict(os.environ, {"PILOTE_AGENT": ""}, clear=False):
            for k in [k for k in os.environ if k.startswith(("CLAUDE", "CODEX_", "HERMES_"))]:
                os.environ.pop(k)
            self.assertEqual(ecran.agent_courant(), "inconnu")


class TestReservation(Base):
    def test_reservation_implicite_puis_autre_agent_refuse(self):
        self.assertEqual(self.bac.lancer("souris", "10", "10", PILOTE_AGENT="claude").returncode, 0)
        r = self.bac.lancer("souris", "20", "20", PILOTE_AGENT="codex")
        self.assertEqual(r.returncode, 4, r.stderr)

    def test_stop_d_un_autre_refuse_puis_force_journalise(self):
        self.pilote("codex")
        r = self.bac.lancer("stop", PILOTE_AGENT="claude")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertTrue(os.path.exists(self.bac.fichier("pilote")))
        r = self.bac.lancer("stop", "--forcer", PILOTE_AGENT="claude")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(self.bac.fichier("pilote")))
        self.assertTrue(any(j.get("commande") == "stop --forcer" and j.get("pilote_evince") == "codex"
                            for j in self.journal()))

    def test_reprendre_d_un_autre_refuse_et_stop_conserve(self):
        self.pilote("codex")
        with open(self.bac.fichier("stop"), "w") as f:
            f.write("[cadre] geste de Félix")
        r = self.bac.lancer("reprendre", PILOTE_AGENT="claude")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertTrue(os.path.exists(self.bac.fichier("stop")))

    def test_reprendre_journalise_la_levee(self):
        with open(self.bac.fichier("stop"), "w") as f:
            f.write("[cadre] geste de Félix")
        r = self.bac.lancer("reprendre", "--raison", "GO de Félix par écrit", PILOTE_AGENT="claude")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(self.bac.fichier("stop")))
        j = [x for x in self.journal() if x.get("commande") == "reprendre"]
        self.assertTrue(j and j[-1]["stop_leve"].startswith("[cadre]") and "GO" in j[-1]["raison"])

    def test_observateur_ne_prolonge_pas(self):
        self.pilote("codex", age=500)
        avant = os.path.getmtime(self.bac.fichier("actif"))
        self.bac.lancer("texte", "--zone", "2300", "0", "260", "70", PILOTE_AGENT="claude")
        self.assertEqual(os.path.getmtime(self.bac.fichier("actif")), avant)

    def test_reservation_gelee_sous_stop(self):
        self.pilote("codex", age=5000)
        with open(self.bac.fichier("stop"), "w") as f:
            f.write("[cadre] geste")
        r = self.bac.lancer("etat", PILOTE_AGENT="claude")
        self.assertIn("codex", r.stdout)


class TestGardeFous(Base):
    def test_pilote_sans_cadre_sans_effet(self):
        self.bac.extra.pop("FAUX_CADRE")
        os.remove(self.bac.fichier("pret"))
        r = self.bac.lancer("souris", "10", "10", PILOTE_SANS_CADRE="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("cadre non affiché", r.stderr)
        self.assertEqual(self.bac.injections(), [])

    def test_journal_sans_le_texte_tape(self):
        self.bac.lancer("taper", "motdepasse-secret", PILOTE_AGENT="claude")
        brut = lire(self.bac.fichier("journal.ndjson"))
        self.assertNotIn("secret", brut)
        self.assertIn("<17 caractères>", brut)

    def test_journal_consultable(self):
        self.bac.lancer("souris", "10", "10", PILOTE_AGENT="claude")
        r = self.bac.lancer("journal", "5")
        self.assertIn("souris", r.stdout)

    def test_stop_reel_respecte_malgre_pilote_etat(self):
        import ecran
        reel, faux = tempfile.mkdtemp(), tempfile.mkdtemp()
        with open(os.path.join(reel, "stop"), "w") as f:
            f.write("[cadre] geste réel")
        with mock.patch.object(ecran, "ETAT_REEL", reel), mock.patch.object(ecran, "ETAT", faux), \
                mock.patch("commun.ETAT", faux):
            with self.assertRaises(SystemExit) as ctx:
                ecran.verifier_stop(False)
        self.assertEqual(ctx.exception.code, 3)


@unittest.skipUnless(os.path.exists(PYTHON_CADRE), "venv PySide6 absent")
class TestCadreReservation(unittest.TestCase):
    def test_cadre_reste_pendant_la_reservation(self):
        bac = Bac()
        try:
            with open(bac.fichier("pilote"), "w") as f:
                f.write('{"agent": "claude"}')
            with open(bac.fichier("actif"), "w") as f:
                f.write("x")
            t = time.time() - 900                  # 15 min sans commande : réservation toujours valable
            os.utime(bac.fichier("actif"), (t, t))
            proc = subprocess.Popen([PYTHON_CADRE, os.path.join(ICI, "cadre.py")], env=bac.env(QT_QPA_PLATFORM="offscreen"),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2.5)
            self.assertIsNone(proc.poll(), "le cadre s'est effacé pendant une réservation")
            proc.kill(); proc.wait()
        finally:
            bac.fermer()


if __name__ == "__main__":
    unittest.main()
