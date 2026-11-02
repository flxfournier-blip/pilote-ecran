"""cdp.py — la prise DevTools (Chrome DevTools Protocol) du pilote d'écran (09/10/2026, PLAN-VOIR prise 1).

Chrome, Electron (VS Code, Codex, Mailspring, WhatsApp) et Steam (CEF, `-cef-enable-debugging`) vident leur arbre
d'accessibilité quand leur fenêtre est en arrière-plan. Lancés avec `--remote-debugging-port=PORT`, ils exposent
au contraire TOUT, devant ou derrière : le contenu de chaque page (arbre d'accessibilité + boîtes de mise en page),
les navigations, les erreurs de console. Ce module parle ce protocole avec la bibliothèque standard seule
(WebSocket minimal, RFC 6455, côté client) : rien à installer, python système.

Lecture seule : aucune commande n'agit sur la page (pas de clic, pas de frappe, pas de script modifiant le DOM).
"""
import base64
import json
import os
import queue
import socket
import struct
import threading
import time
import urllib.parse
import urllib.request

PORTS = [int(p) for p in os.environ.get("PILOTE_CDP_PORTS", "9222,9223,9224").split(",") if p.strip().isdigit()]

ROLES_AX = {"link": "lien", "button": "bouton", "textbox": "champ", "searchbox": "champ", "combobox": "liste", "listbox": "liste",
            "checkbox": "case", "radio": "radio", "switch": "interrupteur", "tab": "onglet", "menuitem": "menu",
            "menuitemcheckbox": "menu", "menuitemradio": "menu", "option": "élément", "listitem": "élément", "treeitem": "élément",
            "heading": "titre", "StaticText": "texte", "image": "image", "img": "image", "dialog": "dialogue",
            "alertdialog": "dialogue", "alert": "alerte", "slider": "curseur", "spinbutton": "compteur", "cell": "cellule",
            "gridcell": "cellule", "columnheader": "cellule", "rowheader": "cellule", "progressbar": "indicateur"}
PRIORITE = {"dialogue": 0, "alerte": 0, "champ": 1, "bouton": 2, "lien": 3, "liste": 3, "case": 3, "radio": 3, "onglet": 3,
            "menu": 3, "élément": 4, "titre": 4, "cellule": 5, "texte": 6, "image": 7}


# ───────────── WebSocket minimal (client) ─────────────
class WebSocket:
    def __init__(self, url, timeout=5.0):
        u = urllib.parse.urlparse(url)
        host, port = u.hostname, u.port or 80
        self.sock = socket.create_connection((host, port), timeout=timeout)
        cle = base64.b64encode(os.urandom(16)).decode()
        chemin = (u.path or "/") + (f"?{u.query}" if u.query else "")
        self.sock.sendall((f"GET {chemin} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {cle}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        reponse = b""
        while b"\r\n\r\n" not in reponse:
            morceau = self.sock.recv(4096)
            if not morceau:
                raise ConnectionError("connexion fermée pendant la poignée de main")
            reponse += morceau
        entete, self.tampon = reponse.split(b"\r\n\r\n", 1)
        if b" 101 " not in entete.split(b"\r\n", 1)[0]:
            raise ConnectionError(entete[:120].decode("latin-1"))
        self.sock.settimeout(None)
        self.verrou_envoi = threading.Lock()

    def envoyer(self, texte):
        donnees = texte.encode("utf-8")
        masque = os.urandom(4)
        n = len(donnees)
        entete = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536
                                  else bytes([0x80 | 127]) + struct.pack(">Q", n))
        masquees = bytes(b ^ masque[i & 3] for i, b in enumerate(donnees))
        with self.verrou_envoi:
            self.sock.sendall(entete + masque + masquees)

    def _lire(self, n):
        while len(self.tampon) < n:
            morceau = self.sock.recv(1 << 18)
            if not morceau:
                raise ConnectionError("connexion fermée")
            self.tampon += morceau
        donnees, self.tampon = self.tampon[:n], self.tampon[n:]
        return donnees

    def recevoir(self):
        """Un message texte complet (fragments réassemblés) ; répond aux pings ; lève ConnectionError à la fermeture."""
        message = b""
        while True:
            b1, b2 = self._lire(2)
            fin, opcode, masque_present, n = b1 & 0x80, b1 & 0x0F, b2 & 0x80, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._lire(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._lire(8))[0]
            masque = self._lire(4) if masque_present else None
            donnees = self._lire(n)
            if masque:
                donnees = bytes(b ^ masque[i & 3] for i, b in enumerate(donnees))
            if opcode == 0x9:
                with self.verrou_envoi:
                    self.sock.sendall(bytes([0x8A, 0x80 | len(donnees)]) + b"\0\0\0\0" + donnees)
                continue
            if opcode == 0x8:
                raise ConnectionError("fermé par le serveur")
            if opcode in (0x0, 0x1, 0x2):
                message += donnees
                if fin:
                    return message.decode("utf-8", "replace")

    def fermer(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ───────────── une session DevTools (une page) ─────────────
class Session:
    def __init__(self, ws_url):
        self.ws = WebSocket(ws_url)
        self.suivant, self.attentes, self.verrou = 0, {}, threading.Lock()
        self.evenements = queue.Queue(maxsize=2000)
        self.vivante = True
        threading.Thread(target=self._lecteur, daemon=True, name="cdp-lecteur").start()

    def _lecteur(self):
        while self.vivante:
            try:
                m = json.loads(self.ws.recevoir())
            except Exception:
                self.vivante = False
                for ev, _ in self.attentes.values():
                    ev.set()
                return
            if "id" in m:
                with self.verrou:
                    entree = self.attentes.get(m["id"])
                if entree:
                    entree[1] = m
                    entree[0].set()
            else:
                try:
                    self.evenements.put_nowait(m)
                except queue.Full:
                    pass

    def appeler(self, methode, params=None, delai=10.0):
        with self.verrou:
            self.suivant += 1
            i = self.suivant
            entree = [threading.Event(), None]
            self.attentes[i] = entree
        self.ws.envoyer(json.dumps({"id": i, "method": methode, "params": params or {}}))
        if not entree[0].wait(delai):
            with self.verrou:
                self.attentes.pop(i, None)
            raise TimeoutError(f"{methode} : pas de réponse en {delai} s")
        with self.verrou:
            self.attentes.pop(i, None)
        if entree[1] is None:
            raise ConnectionError("session fermée")
        if "error" in entree[1]:
            raise RuntimeError(entree[1]["error"].get("message", "erreur DevTools"))
        return entree[1].get("result", {})

    def fermer(self):
        self.vivante = False
        self.ws.fermer()


# ───────────── de l'arbre AX + des boîtes de mise en page aux éléments de la carte ─────────────
def elements_depuis(ax, instantane, origine=(0, 0), echelle=1.0, maxi=400, *, prefixe_source="", couverture=None):
    """`ax` = résultat d'Accessibility.getFullAXTree, `instantane` = DOMSnapshot.captureSnapshot (includeDOMRects).
    `origine` = position écran du coin haut gauche de la zone de page (viewport) ; `echelle` = devicePixelRatio.
    `prefixe_source` identifie la connexion, la cible et le document ; la clé native ne dépend ni du texte ni du tri.
    `couverture`, si fourni, reçoit les limites de cette projection du document principal.
    → [{role, nom, texte, x, y, w, h, etats, role_ax, source, cle_source}] triés par importance, en pixels écran."""
    doc = (instantane.get("documents") or [{}])[0]
    noeuds, mise = doc.get("nodes", {}), doc.get("layout", {})
    bilan = {"partielle": True, "perimetre": "projection_accessible_document_principal",
             "documents_lus": int(bool(instantane.get("documents"))),
             "documents_ignores": max(0, len(instantane.get("documents", [])) - 1),
             "noeuds_ax": len(ax.get("nodes", [])), "roles_ignores": 0, "sans_boite": 0}
    def cle_source(bid):
        return f"devtools:{prefixe_source}:dom:{bid}"

    dx, dy = doc.get("scrollOffsetX", 0) or 0, doc.get("scrollOffsetY", 0) or 0
    boites = {}
    for i, idx in enumerate(mise.get("nodeIndex", [])):
        b = mise.get("bounds", [[]])[i] if i < len(mise.get("bounds", [])) else None
        if b and len(b) == 4 and b[2] > 0 and b[3] > 0:
            boites[idx] = b
    par_backend, parents = {}, {}
    backend_ids = noeuds.get("backendNodeId", [])
    for i, bid in enumerate(backend_ids):
        if i in boites:
            par_backend[bid] = boites[i]
        indices_parents = noeuds.get("parentIndex", [])
        parent = indices_parents[i] if i < len(indices_parents) else -1
        if 0 <= parent < len(backend_ids):
            parents[bid] = backend_ids[parent]
    sortie = []
    for n in ax.get("nodes", []):
        if n.get("ignored"):
            continue
        role_ax = (n.get("role") or {}).get("value", "")
        role = ROLES_AX.get(role_ax)
        if not role:
            bilan["roles_ignores"] += 1
            continue
        nom = ((n.get("name") or {}).get("value") or "").strip()
        valeur = ((n.get("value") or {}).get("value") or "")
        valeur = str(valeur).strip() if valeur is not None else ""
        if role == "texte" and not nom:
            continue
        bid = n.get("backendDOMNodeId")
        b = par_backend.get(bid)
        if not b:
            bilan["sans_boite"] += 1
            continue
        x, y, w, h = b
        x, y = (x - dx) * echelle + origine[0], (y - dy) * echelle + origine[1]
        etats = []
        for p in n.get("properties", []) or []:
            nomp, valp = p.get("name"), (p.get("value") or {}).get("value")
            if nomp == "focused" and valp:
                etats.append("focus")
            elif nomp == "checked" and valp in (True, "true"):
                etats.append("coché")
            elif nomp == "selected" and valp:
                etats.append("sélectionné")
            elif nomp == "disabled" and valp:
                etats.append("inactif")
            elif nomp == "expanded" and valp:
                etats.append("déplié")
        element = {"role": role, "role_ax": role_ax, "nom": nom[:120], "texte": valeur[:120],
                   "x": int(round(x)), "y": int(round(y)), "w": int(round(w * echelle)), "h": int(round(h * echelle)),
                   "etats": etats, "source": "devtools", "cle_source": cle_source(bid),
                   "texte_tronque": len(nom) > 120 or len(valeur) > 120}
        if bid in parents:
            element["parent_source"] = cle_source(parents[bid])
        sortie.append(element)
    sortie.sort(key=lambda e: (PRIORITE.get(e["role"], 9), e["y"], e["x"]))
    retenus = sortie[:maxi]
    bilan.update(elements_eligibles=len(sortie), elements_retenus=len(retenus),
                 elements_tronques=len(sortie) - len(retenus), textes_tronques=sum(e["texte_tronque"] for e in retenus))
    if couverture is not None:
        couverture.update(bilan)
    return retenus


# ───────────── la prise : toutes les pages de tous les ports ─────────────
class Devtools(threading.Thread):
    """Surveille les ports DevTools (PILOTE_CDP_PORTS) ; pour chaque page : session, événements de navigation et de
    console vers le bus, et lecture du contenu à la demande (`lire(titre)`) ou après chaque chargement."""

    def __init__(self, bus, ports=None):
        super().__init__(daemon=True, name="devtools")
        self.bus = bus
        self.ports = ports or PORTS
        self.pages = {}            # id -> {"port", "titre", "url", "session", "ts", "charge"}
        self.verrou = threading.Lock()
        self.vus = set()

    def cibles(self, port):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1.5) as r:
                return [c for c in json.load(r) if c.get("type") == "page" and c.get("webSocketDebuggerUrl")
                        and not (c.get("url") or "").startswith(("devtools://", "chrome://", "chrome-extension://"))]
        except Exception:
            return None

    def run(self):
        while True:
            for port in self.ports:
                cibles = self.cibles(port)
                if cibles is None:
                    if port in self.vus:
                        self.vus.discard(port)
                        self._retirer_port(port)
                    continue
                if port not in self.vus:
                    self.vus.add(port)
                    self.bus.emettre("devtools", "port", f"DevTools : port {port} ouvert, {len(cibles)} page(s)", port=port)
                presents = set()
                for c in cibles:
                    presents.add(c["id"])
                    with self.verrou:
                        p = self.pages.get(c["id"])
                    if p is None:
                        self._attacher(port, c)
                    elif p["titre"] != (c.get("title") or "") or p["url"] != (c.get("url") or ""):
                        p["titre"], p["url"] = c.get("title") or "", c.get("url") or ""
                for pid in [i for i, p in self.pages.items() if p["port"] == port and i not in presents]:
                    self._detacher(pid, "page fermée")
            self._depiler_evenements()
            time.sleep(2)

    def _attacher(self, port, c):
        try:
            s = Session(c["webSocketDebuggerUrl"])
            s.appeler("Page.enable", delai=5)
            s.appeler("Runtime.enable", delai=5)
        except Exception as e:
            self.bus.emettre("devtools", "erreur", f"DevTools : page « {(c.get('title') or '')[:60]} » injoignable ({e})"[:160])
            return
        with self.verrou:
            self.pages[c["id"]] = {"id": c["id"], "connexion": os.urandom(8).hex(),
                                   "port": port, "titre": c.get("title") or "", "url": c.get("url") or "", "session": s,
                                   "ts": time.monotonic(), "charge": 0.0}
        self.bus.emettre("devtools", "page", f"DevTools : page suivie « {(c.get('title') or '')[:80]} » ({(c.get('url') or '')[:80]})",
                         titre=c.get("title"), url=c.get("url"), port=port)

    def _detacher(self, pid, raison):
        with self.verrou:
            p = self.pages.pop(pid, None)
        if p:
            p["session"].fermer()
            self.bus.emettre("devtools", "fin", f"DevTools : {raison} « {p['titre'][:80]} »", titre=p["titre"])

    def _retirer_port(self, port):
        for pid in [i for i, p in self.pages.items() if p["port"] == port]:
            self._detacher(pid, "port fermé")

    def _depiler_evenements(self):
        with self.verrou:
            pages = list(self.pages.items())
        for pid, p in pages:
            s = p["session"]
            if not s.vivante:
                self._detacher(pid, "session perdue")
                continue
            for _ in range(200):
                try:
                    m = s.evenements.get_nowait()
                except queue.Empty:
                    break
                meth, par = m.get("method"), m.get("params", {})
                if meth == "Page.frameNavigated" and not par.get("frame", {}).get("parentId"):
                    url = par["frame"].get("url", "")
                    p["url"] = url
                    self.bus.emettre("devtools", "navigation", f"page : navigation vers {url[:120]}", url=url, titre=p["titre"])
                elif meth == "Page.loadEventFired":
                    p["charge"] = time.monotonic()
                    self.bus.emettre("devtools", "chargee", f"page : « {p['titre'][:80]} » chargée", titre=p["titre"], url=p["url"])
                elif meth == "Runtime.consoleAPICalled" and par.get("type") in ("error", "warning"):
                    texte = " ".join(str((a.get("value") if a.get("value") is not None else a.get("description")) or "") for a in par.get("args", []))[:160]
                    self.bus.emettre("devtools", "console", f"console {par['type']} dans « {p['titre'][:50]} » : {texte}", titre=p["titre"], niveau=par["type"], texte=texte)
                elif meth == "Runtime.exceptionThrown":
                    d = par.get("exceptionDetails", {})
                    texte = (d.get("exception", {}).get("description") or d.get("text") or "")[:160]
                    self.bus.emettre("devtools", "exception", f"exception JS dans « {p['titre'][:50]} » : {texte}", titre=p["titre"], texte=texte)

    def page_pour(self, titre):
        """La page dont le titre correspond au titre de fenêtre X11 (« Titre - Google Chrome »)."""
        t = (titre or "").strip()
        with self.verrou:
            pages = list(self.pages.values())
        for p in pages:
            if p["titre"] and (t == p["titre"] or t.startswith(p["titre"] + " - ") or t.startswith(p["titre"] + " · ")):
                return p
        for p in pages:
            if p["titre"] and p["titre"] in t:
                return p
        return None

    def lire(self, titre, geometrie=None, delai=8.0):
        """Éléments de la page, ou None si aucune cible ; `erreur` signifie lecture échouée, jamais page vide.
        Le délai borne toute la lecture. Deux lectures du frame vérifient qu'une navigation ne mélange pas les documents.
        La projection reste partielle : rôles accessibles reconnus, boîtes DOM et document principal uniquement."""
        p = self.page_pour(titre)
        if not p:
            return None
        s = p["session"]
        t0 = time.monotonic()
        def appeler(methode, params=None):
            restant = delai - (time.monotonic() - t0)
            if restant <= 0:
                raise TimeoutError("délai de lecture DevTools dépassé")
            return s.appeler(methode, params, delai=restant)

        try:
            avant = appeler("Page.getFrameTree")["frameTree"]["frame"]
            if not avant.get("id") or not avant.get("loaderId"):
                raise RuntimeError("document DevTools sans identifiant de chargement")
            rep = appeler("Runtime.evaluate", {"expression": "JSON.stringify([window.screenX, window.screenY, window.outerWidth - window.innerWidth, "
                                                               "window.outerHeight - window.innerHeight, window.devicePixelRatio, document.title])",
                                                 "returnByValue": True})
            sx, sy, dl, dh, dpr, titre_doc = json.loads(rep["result"]["value"])
            ax = appeler("Accessibility.getFullAXTree")
            inst = appeler("DOMSnapshot.captureSnapshot", {"computedStyles": [], "includeDOMRects": True})
            apres = appeler("Page.getFrameTree")["frameTree"]["frame"]
            if (avant["id"], avant["loaderId"]) != (apres.get("id"), apres.get("loaderId")):
                raise RuntimeError("document changé pendant la lecture DevTools ; réessayer")
            doc = inst["documents"][0]
            frame_id = inst["strings"][doc["frameId"]]
            if frame_id != avant["id"]:
                raise RuntimeError("instantané DevTools d'un autre document ; réessayer")
            racine_dom = doc["nodes"]["backendNodeId"][0]
        except Exception as e:
            return {"erreur": f"{type(e).__name__}: {e}"[:120], "source": "devtools"}
        if geometrie and geometrie[0] is not None:            # X11 fait foi pour la position de la fenêtre
            ox, oy = geometrie[0] + dl // 2, geometrie[1] + dh
        else:
            ox, oy = sx + dl // 2, sy + dh
        prefixe = ":".join(urllib.parse.quote(str(v), safe="") for v in
                           (p["port"], p["id"], p["connexion"], avant["id"], avant["loaderId"], racine_dom))
        couverture = {}
        els = elements_depuis(ax, inst, (ox, oy), dpr or 1.0, prefixe_source=prefixe, couverture=couverture)
        return {"elements": els, "titre": titre_doc, "url": p["url"], "lecture_ms": int((time.monotonic() - t0) * 1000),
                "port": p["port"], "source": "devtools", "couverture": couverture,
                "tronquee": bool(couverture["elements_tronques"] or couverture["textes_tronques"])}

    def verifier_cible(self, titre, geometrie, element):
        """Hit-test DOM en lecture seule, borné à 1 s : True confirmé, False réfuté, None non vérifiable.

        Le point est celui que le pilote cliquera, pas un nouveau centre recalculé. Il doit encore appartenir
        au même objet natif, dans le même document, et être atteint par elementFromPoint (surcouches comprises).
        Une correspondance ambiguë entre titre X11 et cible CDP ne permet jamais de confirmer un clic.
        """
        morceaux = (element.get("cle_source") or "").split(":")
        if len(morceaux) != 9 or morceaux[0] != "devtools" or morceaux[7] != "dom":
            return None
        port, cible, connexion, frame, loader, racine, bid = [urllib.parse.unquote(v) for v in morceaux[1:7]] + [morceaux[8]]
        t = (titre or "").strip()
        with self.verrou:
            pages = list(self.pages.values())
        candidats = [p for p in pages if p["titre"] and
                     (t == p["titre"] or t.startswith(p["titre"] + " - ") or t.startswith(p["titre"] + " · "))]
        if not candidats:
            candidats = [p for p in pages if p["titre"] and p["titre"] in t]
        if len(candidats) != 1:
            return None
        p = candidats[0]
        if (str(p["port"]), p["id"], p["connexion"]) != (port, cible, connexion):
            return False
        if element.get("visible_source") is False or "inactif" in (element.get("etats") or []):
            return False

        fin = time.monotonic() + 1.0
        objet = None

        def appeler(methode, params=None):
            restant = fin - time.monotonic()
            if restant <= 0:
                raise TimeoutError("délai de vérification DevTools dépassé")
            return p["session"].appeler(methode, params, delai=restant)

        def meme_document():
            courant = appeler("Page.getFrameTree")["frameTree"]["frame"]
            if (courant.get("id"), courant.get("loaderId")) != (frame, loader):
                return False
            return str(appeler("DOM.getDocument", {"depth": 0})["root"]["backendNodeId"]) == racine

        try:
            if not meme_document():
                return False
            objet = appeler("DOM.resolveNode", {"backendNodeId": int(bid)})["object"]["objectId"]
            # Le calcul reprend exactement le passage viewport -> écran de lire(). Le DOM peut avoir changé
            # depuis la carte : rectangles, visibilité et cible du point sont donc tous relus maintenant.
            fonction = """function(point, geometrie) {
                const n = this.nodeType === 1 ? this : this.parentElement;
                if (!n || !this.isConnected || n.ownerDocument !== document) return false;
                if (n.matches(':disabled') || n.closest('[inert], [aria-disabled="true"]')) return false;
                const dpr = window.devicePixelRatio || 1;
                const dl = window.outerWidth - window.innerWidth, dh = window.outerHeight - window.innerHeight;
                const ox = (geometrie ? geometrie[0] : window.screenX) + Math.floor(dl / 2);
                const oy = (geometrie ? geometrie[1] : window.screenY) + dh;
                const x = (point[0] - ox) / dpr, y = (point[1] - oy) / dpr;
                if (![x, y].every(Number.isFinite) || x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) return false;
                for (let a = n; a; a = a.parentElement) {
                    const s = getComputedStyle(a);
                    if (s.display === 'none' || s.visibility === 'hidden' || s.visibility === 'collapse' || Number(s.opacity) === 0) return false;
                }
                let rects;
                if (this.nodeType === 1) rects = this.getClientRects();
                else {
                    const r = document.createRange();
                    r.selectNodeContents(this);
                    rects = r.getClientRects();
                }
                if (!Array.from(rects).some(r => r.left <= x && x < r.right && r.top <= y && y < r.bottom)) return false;
                let hit = document.elementFromPoint(x, y);
                while (hit && hit.shadowRoot) {
                    const deeper = hit.shadowRoot.elementFromPoint(x, y);
                    if (!deeper || deeper === hit) break;
                    hit = deeper;
                }
                return !!hit && (hit === n || n.contains(hit));
            }"""
            point = [element["x"] + element["w"] / 2, element["y"] + element["h"] / 2]
            geo = list(geometrie[:2]) if geometrie and all(v is not None for v in geometrie[:2]) else None
            rep = appeler("Runtime.callFunctionOn", {"objectId": objet, "functionDeclaration": fonction,
                          "arguments": [{"value": point}, {"value": geo}], "returnByValue": True,
                          "silent": True, "userGesture": False})
            if "exceptionDetails" in rep or not isinstance(rep.get("result", {}).get("value"), bool):
                return None
            if not rep["result"]["value"]:
                return False
            return meme_document()
        except Exception:
            return None
        finally:
            if objet:
                try:
                    appeler("Runtime.releaseObject", {"objectId": objet})
                except Exception:
                    pass
