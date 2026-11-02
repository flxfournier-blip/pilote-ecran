"""cdp.py sans Chrome : WebSocket client contre un faux serveur (poignée de main, trames, ping), session DevTools
(appels/réponses/événements), conversion arbre AX + instantané DOM → éléments de la carte."""
import base64
import hashlib
import json
import socket
import struct
import threading
import unittest
from unittest.mock import patch

import cdp


def faux_serveur_ws(reponses):
    """Un serveur WebSocket minimal : accepte UNE connexion, répond à chaque message JSON par `reponses(message)`
    (liste de messages à renvoyer), envoie un ping au passage. Rend (port, fil)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def trame(texte):
        d = texte.encode()
        n = len(d)
        return bytes([0x81]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n) if n < 65536
                                else bytes([127]) + struct.pack(">Q", n)) + d

    def servir():
        c, _ = srv.accept()
        req = b""
        while b"\r\n\r\n" not in req:
            req += c.recv(4096)
        cle = [l.split(b": ", 1)[1] for l in req.split(b"\r\n") if l.lower().startswith(b"sec-websocket-key")][0]
        accept = base64.b64encode(hashlib.sha1(cle + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
        c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
        c.sendall(bytes([0x89, 0x02]) + b"hi")                       # ping : le client doit répondre pong
        tampon = b""
        while True:
            while len(tampon) < 2:
                m = c.recv(4096)
                if not m:
                    return
                tampon += m
            b1, b2 = tampon[0], tampon[1]
            n, i = b2 & 0x7F, 2
            if n == 126:
                n, i = struct.unpack(">H", tampon[2:4])[0], 4
            while len(tampon) < i + 4 + n:
                tampon += c.recv(65536)
            masque, data = tampon[i:i + 4], tampon[i + 4:i + 4 + n]
            tampon = tampon[i + 4 + n:]
            if (b1 & 0x0F) == 0xA:                                   # pong
                continue
            texte = bytes(b ^ masque[k & 3] for k, b in enumerate(data)).decode()
            for r in reponses(json.loads(texte)):
                c.sendall(trame(json.dumps(r)))

    fil = threading.Thread(target=servir, daemon=True)
    fil.start()
    return srv.getsockname()[1], fil


class SessionTest(unittest.TestCase):
    def test_appel_reponse_evenement_et_gros_message(self):
        def reponses(m):
            if m["method"] == "Page.enable":
                return [{"method": "Page.loadEventFired", "params": {"timestamp": 1}}, {"id": m["id"], "result": {}}]
            if m["method"] == "Gros":
                return [{"id": m["id"], "result": {"x": "a" * 70000}}]
            return [{"id": m["id"], "error": {"message": "inconnue"}}]

        port, _ = faux_serveur_ws(reponses)
        s = cdp.Session(f"ws://127.0.0.1:{port}/devtools/page/1")
        self.assertEqual(s.appeler("Page.enable"), {})
        self.assertEqual(s.evenements.get(timeout=2)["method"], "Page.loadEventFired")
        self.assertEqual(len(s.appeler("Gros")["x"]), 70000)
        with self.assertRaises(RuntimeError):
            s.appeler("Autre")
        s.fermer()


class ElementsTest(unittest.TestCase):
    def test_conversion_ax_et_instantane(self):
        ax = {"nodes": [
            {"nodeId": "1", "role": {"value": "RootWebArea"}, "name": {"value": "Page"}, "backendDOMNodeId": 1},
            {"nodeId": "2", "role": {"value": "link"}, "name": {"value": "Accueil"}, "backendDOMNodeId": 2,
             "properties": [{"name": "focused", "value": {"value": True}}]},
            {"nodeId": "3", "role": {"value": "textbox"}, "name": {"value": "Rechercher"}, "value": {"value": "claude"}, "backendDOMNodeId": 3},
            {"nodeId": "4", "role": {"value": "StaticText"}, "name": {"value": "Bonjour"}, "backendDOMNodeId": 4},
            {"nodeId": "5", "role": {"value": "StaticText"}, "name": {"value": "sans boîte"}, "backendDOMNodeId": 9},
            {"nodeId": "6", "role": {"value": "button"}, "name": {"value": "Caché"}, "backendDOMNodeId": 6, "ignored": True},
        ]}
        inst = {"strings": [], "documents": [{"scrollOffsetX": 0, "scrollOffsetY": 100,
                                             "nodes": {"backendNodeId": [1, 2, 3, 4, 6]},
                                             "layout": {"nodeIndex": [0, 1, 2, 3], "bounds": [[0, 0, 1000, 2000], [10, 150, 80, 20], [10, 200, 300, 30], [10, 300, 60, 16]]}}]}
        els = cdp.elements_depuis(ax, inst, origine=(100, 200), echelle=1.0)
        self.assertEqual([(e["role"], e["nom"], e["x"], e["y"], e["w"], e["h"]) for e in els],
                         [("champ", "Rechercher", 110, 300, 300, 30), ("lien", "Accueil", 110, 250, 80, 20), ("texte", "Bonjour", 110, 400, 60, 16)])
        self.assertEqual(els[1]["etats"], ["focus"])
        self.assertEqual(els[0]["texte"], "claude")

    def test_ports_par_defaut(self):
        self.assertEqual(cdp.PORTS, [9222, 9223, 9224])


def donnees_page(nombre=1, nom="Ouvrir"):
    """Petit document avec des identifiants natifs, indépendant de l'ordre visuel."""
    ids = list(range(1, nombre + 2))
    ax = {"nodes": [{"role": {"value": "RootWebArea"}, "backendDOMNodeId": 1}] + [
        {"role": {"value": "button"}, "name": {"value": nom}, "backendDOMNodeId": bid}
        for bid in ids[1:]]}
    inst = {"strings": ["frame-principal"], "documents": [{
        "frameId": 0, "nodes": {"backendNodeId": ids, "parentIndex": [-1] + [0] * nombre},
        "layout": {"nodeIndex": list(range(nombre + 1)), "bounds": [[0, i * 20, 100, 20] for i in ids]}}]}
    return ax, inst


class LectureTest(unittest.TestCase):
    class Session:
        def __init__(self):
            self.ax, self.inst = donnees_page()
            self.loader = "chargement-A"
            self.apres = None
            self.appels = []
            self.nb_frames = 0
            self.hit = True

        def appeler(self, methode, params=None, delai=8.0):
            self.appels.append((methode, delai))
            if methode == "Page.getFrameTree":
                self.nb_frames += 1
                return {"frameTree": {"frame": {"id": "frame-principal", "loaderId":
                                               self.apres if self.apres and self.nb_frames % 2 == 0 else self.loader}}}
            if methode == "Runtime.evaluate":
                return {"result": {"value": json.dumps([0, 0, 0, 80, 1, "Page"])}}
            if methode == "Accessibility.getFullAXTree":
                return self.ax
            if methode == "DOMSnapshot.captureSnapshot":
                return self.inst
            if methode == "DOM.getDocument":
                return {"root": {"backendNodeId": self.inst["documents"][0]["nodes"]["backendNodeId"][0]}}
            if methode == "DOM.resolveNode":
                return {"object": {"objectId": "objet-test"}}
            if methode == "Runtime.callFunctionOn":
                return {"result": {"value": self.hit}}
            if methode == "Runtime.releaseObject":
                return {}
            raise AssertionError(f"Appel inattendu : {methode}")

    def setUp(self):
        self.session = self.Session()
        self.dev = cdp.Devtools(None)
        self.page = {"id": "cible-A", "connexion": "connexion-A", "port": 9222,
                     "titre": "Page", "url": "https://exemple.test/", "session": self.session}
        self.dev.pages["cible-A"] = self.page

    def test_identite_resiste_aux_changements_de_texte_et_geometrie(self):
        premiere = self.dev.lire("Page")
        element = premiere["elements"][0]
        self.session.ax["nodes"][1]["name"]["value"] = "Fermer"
        self.session.inst["documents"][0]["layout"]["bounds"][1] = [250, 300, 120, 40]
        seconde = self.dev.lire("Page")
        self.assertEqual(element["cle_source"], seconde["elements"][0]["cle_source"])
        self.assertNotEqual(element["nom"], seconde["elements"][0]["nom"])
        self.assertNotEqual(element["x"], seconde["elements"][0]["x"])
        self.assertEqual(element["source"], "devtools")
        self.assertEqual(element["parent_source"], element["cle_source"].rsplit(":", 1)[0] + ":1")
        self.assertFalse(premiere["tronquee"])
        self.assertEqual(premiere["couverture"]["elements_retenus"], 1)
        self.assertEqual({m for m, _ in self.session.appels},
                         {"Page.getFrameTree", "Runtime.evaluate", "Accessibility.getFullAXTree", "DOMSnapshot.captureSnapshot"})

    def test_identite_change_quand_les_identifiants_dom_sont_recycles(self):
        def cle():
            return self.dev.lire("Page")["elements"][0]["cle_source"]

        cles = [cle()]
        self.session.loader = "chargement-B"     # même backendDOMNodeId, nouveau document
        cles.append(cle())
        self.page["id"] = "cible-B"             # même loader et DOM dans une autre cible
        cles.append(cle())
        self.page["connexion"] = "connexion-B"  # nouveau processus ou nouvelle connexion
        cles.append(cle())
        self.session.inst["documents"][0]["nodes"]["backendNodeId"][0] = 99
        cles.append(cle())                        # remplacement du document sans nouveau loader
        self.assertEqual(len(set(cles)), len(cles))

    def test_navigation_pendant_lecture_est_une_erreur_pas_une_page_vide(self):
        self.session.apres = "chargement-B"
        resultat = self.dev.lire("Page")
        self.assertIn("document changé", resultat["erreur"])
        self.assertNotIn("elements", resultat)

    def test_vide_reel_distinct_d_un_echec_et_d_une_cible_absente(self):
        self.session.ax, self.session.inst = donnees_page(0)
        resultat = self.dev.lire("Page")
        self.assertEqual(resultat["elements"], [])
        self.assertNotIn("erreur", resultat)
        self.assertIsNone(self.dev.lire("Autre page"))
        self.session.inst = {"strings": [], "documents": []}
        resultat = self.dev.lire("Page")
        self.assertIn("erreur", resultat)
        self.assertNotIn("elements", resultat)

    def test_limites_de_couverture_et_troncature_sont_explicites(self):
        self.session.ax, self.session.inst = donnees_page(401, "a" * 121)
        self.session.inst["documents"].append({})  # sous-document non lu
        self.session.ax["nodes"].append({"role": {"value": "button"}, "backendDOMNodeId": 999})
        resultat = self.dev.lire("Page")
        self.assertTrue(resultat["tronquee"])
        self.assertEqual(len(resultat["elements"]), 400)
        self.assertEqual(len({e["cle_source"] for e in resultat["elements"]}), 400)
        couverture = resultat["couverture"]
        self.assertEqual(couverture["documents_ignores"], 1)
        self.assertEqual(couverture["elements_eligibles"], 401)
        self.assertEqual(couverture["elements_tronques"], 1)
        self.assertEqual(couverture["textes_tronques"], 400)
        self.assertEqual(couverture["sans_boite"], 1)

    def test_delai_borne_toute_la_lecture(self):
        with patch.object(cdp.time, "monotonic", side_effect=[0, 1, 2, 3]):
            resultat = self.dev.lire("Page", delai=3)
        self.assertIn("TimeoutError", resultat["erreur"])
        self.assertEqual([d for _, d in self.session.appels], [2, 1])

    def test_verifier_cible_exige_hit_test_et_libere_l_objet(self):
        element = self.dev.lire("Page")["elements"][0]
        self.assertIs(self.dev.verifier_cible("Page", (0, 0, 800, 600), element), True)
        self.assertEqual(self.session.appels[-1][0], "Runtime.releaseObject")
        self.session.hit = False
        self.assertIs(self.dev.verifier_cible("Page", (0, 0, 800, 600), element), False)
        self.session.hit = "résultat invalide"
        self.assertIsNone(self.dev.verifier_cible("Page", (0, 0, 800, 600), element))

    def test_verifier_cible_refuse_document_change_ou_titre_ambigu(self):
        element = self.dev.lire("Page")["elements"][0]
        self.session.apres = "autre-document"   # changement pendant le hit-test
        self.assertIs(self.dev.verifier_cible("Page", None, element), False)
        self.session.apres = None
        self.dev.pages["cible-B"] = dict(self.page, id="cible-B")
        self.assertIsNone(self.dev.verifier_cible("Page", None, element))
        del self.dev.pages["cible-B"]
        self.page["connexion"] = "autre-connexion"
        self.assertIs(self.dev.verifier_cible("Page", None, element), False)

    def test_verifier_cible_ne_valide_pas_un_element_non_visible(self):
        element = self.dev.lire("Page")["elements"][0]
        self.assertIs(self.dev.verifier_cible("Page", None, dict(element, visible_source=False)), False)
        self.assertIs(self.dev.verifier_cible("Page", None, dict(element, etats=["inactif"])), False)

    def test_verifier_cible_borne_les_appels_a_une_seconde(self):
        element = self.dev.lire("Page")["elements"][0]
        self.session.appels.clear()
        with patch.object(cdp.time, "monotonic", side_effect=[0, 0.4, 0.8, 1.2]):
            self.assertIsNone(self.dev.verifier_cible("Page", None, element))
        self.assertEqual([m for m, _ in self.session.appels], ["Page.getFrameTree", "DOM.getDocument"])
        self.assertTrue(all(0 < d <= 1 for _, d in self.session.appels))


if __name__ == "__main__":
    unittest.main()
