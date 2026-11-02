#!/usr/bin/python3
"""antigraphique.py — expérience demandée par Félix (09/10/2026, 19 h) : « prends une image et regarde si tu peux
la retranscrire en code, avec la carte graphique ».

1. une image : l'écran de JARVIS (capture pleine résolution, lecture seule) ;
2. l'anti-graphique : Qwen vision sur la 5090 (vLLM local) rend le CODE qui produit l'image — un document SVG
   (rectangles, textes, formes, coordonnées en pixels) ;
3. la carte refait le chemin normal : le code est rendu en image (rsvg) ;
4. mesure : ressemblance entre l'original et l'image rendue depuis le code (corrélation des niveaux de gris et des
   contours, à basse résolution) ; une planche côte à côte pour l'œil de Félix.
Sortie : dossier donné en argument (original.png, code.svg, rendu.png, planche.png, mesure.json).
"""
import base64
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

import numpy as np
from PIL import Image, ImageFilter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))
from perception import capturer  # noqa: E402

LLM_URL = os.environ.get("PILOTE_LLM_URL", "http://127.0.0.1:8016/v1")
CONSIGNE = (
    "Tu es un ANTI-GRAPHIQUE : une carte graphique transforme du code en image ; toi, tu reçois une image et tu rends "
    "le CODE qui la produit. Réponds UNIQUEMENT par un document SVG complet et valide, sans texte autour et sans "
    "balises ``` : <svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 L H\" width=\"L\" height=\"H\"> … </svg>. "
    "Fond et panneaux en <rect> avec fill (couleurs approchées), textes lisibles en <text> (x, y, font-size, fill, "
    "font-family=\"sans-serif\"), icônes et boutons en formes simples (<rect rx>, <circle>, <line>). Coordonnées en "
    "pixels de l'image reçue. Entre 40 et 150 éléments, les plus structurants d'abord. Pas de commentaire."
)


def image_b64(img, cote=1280):
    petite = img.copy()
    petite.thumbnail((cote, cote), Image.LANCZOS)
    buf = io.BytesIO()
    petite.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode(), petite.width, petite.height


def demander_code(b64, w, h):
    corps = {"model": "jarvis-llm", "temperature": 0, "max_tokens": 6000,
             "chat_template_kwargs": {"enable_thinking": False},
             "messages": [{"role": "system", "content": CONSIGNE},
                          {"role": "user", "content": [
                              {"type": "text", "text": f"Image de {w}×{h} pixels. Rends le SVG seul, viewBox 0 0 {w} {h}."},
                              {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}}]}]}
    req = urllib.request.Request(f"{LLM_URL}/chat/completions", data=json.dumps(corps).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=300) as r:
        rep = json.loads(r.read())
    texte = rep["choices"][0]["message"]["content"]
    usage = rep.get("usage", {})
    return texte, time.monotonic() - t0, usage


def extraire_svg(texte):
    m = re.search(r"<svg\b.*?</svg>", texte, flags=re.S | re.I)
    if m:
        return m.group(0)
    i = texte.find("<svg")
    if i < 0:
        return None
    # réponse coupée (plafond de jetons) : on garde les éléments complets et on referme le document
    corps = texte[i:]
    j = corps.rfind("/>")
    if j < 0:
        return None
    corps = corps[:j + 2]
    corps += "</g>" * (corps.count("<g") - corps.count("</g>"))
    return corps + "\n</svg>"


def rendre(svg_chemin, png_chemin, w, h):
    for cmd in (["rsvg-convert", "-w", str(w), "-h", str(h), "-o", png_chemin, svg_chemin],
                ["convert", "-background", "none", "-resize", f"{w}x{h}!", svg_chemin, png_chemin]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if r.returncode == 0 and os.path.exists(png_chemin):
                return cmd[0]
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


def mesurer(original, rendu):
    def gris(img, taille=(128, 72)):
        return np.asarray(img.convert("L").resize(taille, Image.LANCZOS), dtype=np.float64)

    def contours(img, taille=(128, 72)):
        return np.asarray(img.convert("L").resize((512, 288), Image.LANCZOS).filter(ImageFilter.FIND_EDGES).resize(taille, Image.LANCZOS), dtype=np.float64)

    def corr(a, b):
        a, b = a - a.mean(), b - b.mean()
        d = np.sqrt((a * a).sum() * (b * b).sum())
        return float((a * b).sum() / d) if d else 0.0

    go, gr = gris(original), gris(rendu)
    return {"correlation_gris": round(corr(go, gr), 3), "ecart_moyen_gris_sur_255": round(float(np.abs(go - gr).mean()), 1),
            "correlation_contours": round(corr(contours(original), contours(rendu)), 3)}


def main():
    dossier = sys.argv[1] if len(sys.argv) > 1 else "."
    os.makedirs(dossier, exist_ok=True)
    original = capturer()
    original.save(os.path.join(dossier, "original.png"))
    b64, w, h = image_b64(original)
    print(f"image : {original.width}×{original.height}, envoyée en {w}×{h}")
    texte, duree, usage = demander_code(b64, w, h)
    svg = extraire_svg(texte)
    with open(os.path.join(dossier, "reponse_brute.txt"), "w", encoding="utf-8") as f:
        f.write(texte)
    if not svg:
        print("✗ pas de SVG dans la réponse (voir reponse_brute.txt)")
        sys.exit(1)
    svg_chemin = os.path.join(dossier, "code.svg")
    with open(svg_chemin, "w", encoding="utf-8") as f:
        f.write(svg)
    n_elems = len(re.findall(r"<(rect|text|circle|line|path|ellipse|polygon|polyline)\b", svg))
    print(f"code : {len(svg)} caractères, {n_elems} éléments SVG, {usage.get('completion_tokens', '?')} jetons, {duree:.1f} s sur la carte")
    rendu_chemin = os.path.join(dossier, "rendu.png")
    outil = rendre(svg_chemin, rendu_chemin, w, h)
    if not outil:
        print("✗ rendu impossible (ni rsvg-convert ni convert)")
        sys.exit(2)
    rendu = Image.open(rendu_chemin).convert("RGB")
    petit_original = original.resize((w, h), Image.LANCZOS)
    mesure = mesurer(petit_original, rendu)
    mesure.update(elements=n_elems, caracteres=len(svg), duree_s=round(duree, 1), rendu_par=outil)
    with open(os.path.join(dossier, "mesure.json"), "w", encoding="utf-8") as f:
        json.dump(mesure, f, ensure_ascii=False, indent=1)
    planche = Image.new("RGB", (w * 2 + 20, h), (30, 30, 34))
    planche.paste(petit_original, (0, 0))
    planche.paste(rendu, (w + 20, 0))
    planche.save(os.path.join(dossier, "planche.png"))
    print("mesure :", json.dumps(mesure, ensure_ascii=False))
    print("--- début du code produit ---")
    for ligne in svg.splitlines()[:25]:
        print(ligne[:160])


if __name__ == "__main__":
    main()
