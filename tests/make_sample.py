"""Gera cadernetas de vacinação sintéticas (com gabarito) para testar o OCR.

Uso: python tests/make_sample.py [pasta_de_saída]
Cria: caderneta_limpa.png, caderneta_foto.jpg, caderneta.pdf e gabarito.json
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# (texto da vacina, chave, texto da dose, dose_key, data dd/mm/aaaa)
ROWS = [
    ("BCG", "bcg", "Dose única", "du", "12/03/2019"),
    ("Hepatite B", "hepatite_b", "1ª dose", "d1", "12/03/2019"),
    ("Pentavalente", "pentavalente", "1ª dose", "d1", "12/05/2019"),
    ("Pentavalente", "pentavalente", "2ª dose", "d2", "14/07/2019"),
    ("Poliomielite inativada", "polio_vip", "1ª dose", "d1", "12/05/2019"),
    ("Rotavírus humano", "rotavirus", "1ª dose", "d1", "12/05/2019"),
    ("Tríplice viral", "triplice_viral", "1ª dose", "d1", "20/03/2020"),
    ("Febre amarela", "febre_amarela", "Dose única", "du", "20/03/2020"),
    ("Hepatite A", "hepatite_a", "Dose única", "du", "22/09/2020"),
    ("Covid-19", "covid19", "3ª dose", "d3", "18/11/2021"),
    ("Influenza", "influenza", "Dose anual", "anual", "05/04/2023"),
    ("HPV", "hpv", "1ª dose", "d1", "03/08/2024"),
]


def gabarito() -> list[dict]:
    out = []
    for _v, key, _d, dkey, dt in ROWS:
        d, m, y = dt.split("/")
        out.append({"vaccine_key": key, "dose_key": dkey, "date": f"{y}-{m}-{d}"})
    return out


def render_clean(width: int = 1700) -> Image.Image:
    f = ImageFont.truetype(FONT, 30)
    fb = ImageFont.truetype(FONT_B, 30)
    ft = ImageFont.truetype(FONT_B, 42)
    row_h, top = 64, 260
    height = top + row_h * (len(ROWS) + 2) + 120
    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)
    d.text((60, 40), "CADERNETA DE VACINAÇÃO", font=ft, fill="black")
    d.text((60, 110), "Nome: Maria Helena Souza", font=f, fill="black")
    d.text((60, 160), "Data de nascimento: 02/03/2019", font=f, fill="black")
    cols = [60, 620, 920, 1250, 1700 - 40]
    heads = ["Vacina", "Dose", "Data", "Unidade"]
    d.rectangle([cols[0], top, cols[-1], top + row_h], outline="black", width=2)
    for i, h in enumerate(heads):
        d.text((cols[i] + 12, top + 14), h, font=fb, fill="black")
    for r, (vac, _k, dose, _dk, dt) in enumerate(ROWS, start=1):
        y = top + r * row_h
        d.rectangle([cols[0], y, cols[-1], y + row_h], outline="black", width=1)
        for i, txt in enumerate([vac, dose, dt, "UBS Centro"]):
            d.text((cols[i] + 12, y + 14), txt, font=f, fill="black")
    for x in cols[1:-1]:
        d.line([x, top, x, top + row_h * (len(ROWS) + 1)], fill="black", width=1)
    return img


def make_photo_like(img: Image.Image, seed: int = 7) -> Image.Image:
    """Simula foto de celular: rotação, sombra, desfoque, ruído e JPEG."""
    rng = random.Random(seed)
    np.random.seed(seed)
    arr = np.array(img.convert("RGB")).astype(np.float32)
    h, w = arr.shape[:2]
    # gradiente de iluminação (sombra de um lado)
    grad = np.linspace(0.62, 1.05, w, dtype=np.float32)[None, :, None]
    arr = arr * grad
    arr = np.clip(arr + np.random.normal(0, 9, arr.shape), 0, 255).astype(np.uint8)
    arr = cv2.GaussianBlur(arr, (0, 0), 1.3)
    m = cv2.getRotationMatrix2D((w / 2, h / 2), rng.uniform(2.0, 3.0), 1.0)
    arr = cv2.warpAffine(arr, m, (w, h), borderValue=(215, 210, 205))
    arr = cv2.resize(arr, None, fx=0.55, fy=0.55, interpolation=cv2.INTER_AREA)  # baixa resolução
    return Image.fromarray(arr)


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    clean = render_clean()
    clean.save(out / "caderneta_limpa.png")
    make_photo_like(clean).save(out / "caderneta_foto.jpg", quality=55)
    clean.save(out / "caderneta.pdf", resolution=200)
    (out / "gabarito.json").write_text(json.dumps(gabarito(), indent=2), encoding="utf-8")
    print("gerado em", out)


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "samples")
