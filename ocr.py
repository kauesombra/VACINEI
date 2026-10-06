"""Leitura de arquivos (imagem/PDF), pré-processamento e OCR com Tesseract."""
from __future__ import annotations

import glob
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
import pytesseract
from PIL import Image, ImageOps
from pdf2image import convert_from_path, pdfinfo_from_path

from parser import Line, Record, parse_lines, score_records

BASE = Path(__file__).resolve().parent
TESSDATA = BASE / "tessdata"
MAX_PAGES = 8
PDF_DPI = 300

# usa o pacote de português do projeto, se existir; senão, cai para o do sistema
if (TESSDATA / "por.traineddata").exists():
    os.environ["TESSDATA_PREFIX"] = str(TESSDATA)
    LANG = "por"
else:  # pragma: no cover - depende do ambiente
    LANG = "eng"

# ------------------------------------------------- localização dos programas
# No Windows, o instalador do Tesseract e o zip do Poppler nem sempre entram no
# PATH. Procuramos nos lugares comuns; também dá para forçar com as variáveis
# de ambiente TESSERACT_CMD (caminho do tesseract.exe) e POPPLER_PATH (pasta bin).
def _find_tesseract() -> str | None:
    env = os.environ.get("TESSERACT_CMD")
    if env and Path(env).is_file():
        return env
    if shutil.which("tesseract"):
        return None  # já está no PATH
    candidates = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        str(Path.home() / "AppData/Local/Programs/Tesseract-OCR/tesseract.exe"),
        "/opt/homebrew/bin/tesseract",
        "/usr/local/bin/tesseract",
    ]
    return next((c for c in candidates if Path(c).is_file()), None)


def _find_poppler() -> str | None:
    env = os.environ.get("POPPLER_PATH")
    if env and Path(env).is_dir():
        return env
    if shutil.which("pdftoppm"):
        return None  # já está no PATH
    patterns = [
        r"C:\poppler*\Library\bin",
        r"C:\poppler*\bin",
        r"C:\Release*\poppler*\Library\bin",
        r"C:\Program Files\poppler*\Library\bin",
        str(Path.home() / "Downloads" / "**" / "poppler*" / "Library" / "bin"),
        "/opt/homebrew/bin",
    ]
    for pat in patterns:
        for hit in sorted(glob.glob(pat, recursive=True), reverse=True):
            if (Path(hit) / "pdftoppm.exe").exists() or (Path(hit) / "pdftoppm").exists():
                return hit
    return None


_tess = _find_tesseract()
if _tess:
    pytesseract.pytesseract.tesseract_cmd = _tess
POPPLER_PATH = _find_poppler()


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


@dataclass
class PageResult:
    index: int
    variant: str
    text: str
    records: list[Record]
    preview: Image.Image


@dataclass
class PipelineResult:
    pages: list[PageResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def records(self) -> list[Record]:
        out: list[Record] = []
        for p in self.pages:
            out.extend(p.records)
        return out

    @property
    def text(self) -> str:
        return "\n\n".join(f"--- página {p.index} ({p.variant}) ---\n{p.text}" for p in self.pages)


# ---------------------------------------------------------------- carga
def load_pages(path: str, ext: str) -> tuple[list[Image.Image], list[str]]:
    warnings: list[str] = []
    if ext == ".pdf":
        info = pdfinfo_from_path(path, poppler_path=POPPLER_PATH)
        total = int(info.get("Pages", 1))
        last = min(total, MAX_PAGES)
        if total > MAX_PAGES:
            warnings.append(f"O PDF tem {total} páginas; apenas as {MAX_PAGES} primeiras foram lidas.")
        images = convert_from_path(path, dpi=PDF_DPI, first_page=1, last_page=last, poppler_path=POPPLER_PATH)
        return [im.convert("RGB") for im in images], warnings
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)  # fotos de celular vêm "deitadas" via EXIF
    return [img.convert("RGB")], warnings


# ---------------------------------------------------------------- pré-processamento
def _resize(gray: np.ndarray) -> np.ndarray:
    """Imagens de baixa resolução (fotos pequenas, prints) precisam ser ampliadas:
    o Tesseract rende bem quando o texto tem ~25-35 px de altura. Nos testes, ampliar
    uma foto de 935 px para ~3600 px foi o que mais melhorou o reconhecimento."""
    h, w = gray.shape
    if w < 1600:
        f = min(3600 / w, 4.0)
        return cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
    if w > 3200:
        f = 3000 / w
        return cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
    return gray


def _estimate_skew(gray: np.ndarray) -> float:
    """Ângulo (graus) que maximiza a variância do perfil de projeção horizontal."""
    small = cv2.resize(gray, None, fx=800 / gray.shape[1], fy=800 / gray.shape[1], interpolation=cv2.INTER_AREA)
    _, bw = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    h, w = bw.shape
    best_angle, best_score = 0.0, -1.0
    for angle in np.arange(-8, 8.01, 0.5):
        m = cv2.getRotationMatrix2D((w / 2, h / 2), float(angle), 1.0)
        rot = cv2.warpAffine(bw, m, (w, h), flags=cv2.INTER_NEAREST)
        score = float(np.var(rot.sum(axis=1)))
        if score > best_score:
            best_angle, best_score = float(angle), score
    return best_angle


def _rotate(gray: np.ndarray, angle: float) -> np.ndarray:
    if abs(angle) < 0.4:
        return gray
    h, w = gray.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=255)


def preprocess(pil: Image.Image, variant: str) -> np.ndarray:
    gray = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2GRAY)
    gray = _resize(gray)
    gray = _rotate(gray, _estimate_skew(gray))
    # corrige iluminação irregular/sombras dividindo pela estimativa do fundo
    k = max(31, (gray.shape[1] // 40) | 1)
    bg = cv2.medianBlur(cv2.dilate(gray, np.ones((7, 7), np.uint8)), k)
    flat = cv2.divide(gray, bg, scale=255)
    if variant == "sharp":
        # realce de nitidez (unsharp mask) antes de binarizar: recupera texto borrado
        blur = cv2.GaussianBlur(flat, (0, 0), 3)
        flat = cv2.addWeighted(flat, 1.8, blur, -0.8, 0)
    _, out = cv2.threshold(flat, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return cv2.copyMakeBorder(out, 30, 30, 30, 30, cv2.BORDER_CONSTANT, value=255)


# ---------------------------------------------------------------- OCR
def ocr_lines(arr: np.ndarray, psm: int = 6) -> list[Line]:
    data = pytesseract.image_to_data(
        Image.fromarray(arr), lang=LANG, config=f"--oem 1 --psm {psm}",
        output_type=pytesseract.Output.DICT,
    )
    groups: dict[tuple, list[tuple[int, str, float]]] = {}
    for i, txt in enumerate(data["text"]):
        txt = (txt or "").strip()
        if not txt:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        groups.setdefault(key, []).append((data["left"][i], txt, float(data["conf"][i])))
    lines = []
    for words in groups.values():
        words.sort()
        confs = [c for _, _, c in words if c >= 0]
        lines.append(Line(" ".join(w for _, w, _ in words), (sum(confs) / len(confs) / 100) if confs else None))
    return lines


def process_file(path: str, ext: str) -> PipelineResult:
    """Lê o arquivo, roda OCR em cada página e extrai os registros de vacinação."""
    result = PipelineResult()
    images, warnings = load_pages(path, ext)
    result.warnings.extend(warnings)

    for i, pil in enumerate(images, start=1):
        best: tuple[float, str, list[Line], list[Record]] | None = None
        # psm 4 ("coluna única de texto") lê bem tabelas com linhas de grade, onde
        # o psm 6 trata a tabela inteira como um bloco e falha.
        prepared: dict[str, np.ndarray] = {}

        def prep(variant: str) -> np.ndarray:
            if variant not in prepared:
                prepared[variant] = preprocess(pil, variant)
            return prepared[variant]

        for variant, psm in [("sharp", 4), ("binary", 4)]:
            lines = ocr_lines(prep(variant), psm)
            recs = parse_lines(lines)
            score = score_records(recs)
            if best is None or score > best[0]:
                best = (score, f"{variant}/psm{psm}", lines, recs)
            good = [r for r in recs if r.date and r.vaccine_key]
            if len(good) >= 3 and sum(r.confidence for r in good) / len(good) >= 0.8:
                break  # primeira tentativa já foi boa: não gasta tempo na segunda
        if best[0] == 0:  # nada encontrado: tenta layouts alternativos
            for variant, psm in [("binary", 6), ("sharp", 3), ("binary", 11)]:
                lines = ocr_lines(prep(variant), psm)
                recs = parse_lines(lines)
                score = score_records(recs)
                if score > best[0]:
                    best = (score, f"{variant}/psm{psm}", lines, recs)
        _, variant, lines, recs = best
        preview = pil.copy()
        preview.thumbnail((1600, 1600))
        result.pages.append(PageResult(i, variant, "\n".join(l.text for l in lines), recs, preview))

    if not result.records:
        result.warnings.append(
            "Nenhuma vacina com data foi reconhecida automaticamente. "
            "Você ainda pode adicionar os registros manualmente na revisão."
        )
    return result
