"""Extração de (vacina, dose, data) a partir do texto reconhecido pelo OCR.

O OCR devolve linhas de texto; este módulo identifica, em cada linha,
a vacina (via catálogo + correspondência aproximada), as datas (com
correção de confusões comuns do OCR, como O/0 e l/1) e as doses
("1ª dose", "dose única", "reforço", "R1"...).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from datetime import date

from vaccines import norm, apply_fixups, find_vaccines

MONTHS = {"jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
          "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12}

# palavras que, logo antes de uma data, indicam que NÃO é data de aplicação
_NOT_APPLICATION = ("val", "fabric", "nasc", "emiss", "expedi", "impress", "vencim")
# linhas de cabeçalho/identificação cujas datas devem ser ignoradas se não houver vacina
_ID_LINE = ("nasc", "cpf", "cartao sus", "cns", "emissao", "expedic", "validade")


@dataclass
class Line:
    text: str
    conf: float | None = None  # confiança média do OCR na linha (0-1)


@dataclass
class Record:
    vaccine_key: str       # chave do catálogo ou "" quando não identificada
    vaccine_name: str
    dose_key: str          # d1..d5, du, r, r1..r3, anual ou ""
    date: str | None       # ISO (AAAA-MM-DD) ou None se ilegível
    confidence: float
    raw: str
    flags: list[str]

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- datas
_OCR_DIGITS = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "|": "1", "S": "5", "B": "8", "Z": "2"})
_G = r"[0-9OoIl|SBZ]"
_NUM_DATE = re.compile(
    rf"(?<![0-9A-Za-z])({_G}{{1,2}})\s*[/\-.,]\s*({_G}{{1,2}})\s*[/\-.,]\s*({_G}{{4}}|{_G}{{2}})(?![0-9A-Za-z])"
)
_TEXT_DATE = re.compile(
    r"(?<![0-9a-z])(\d{1,2})\s*(?:de\s+)?(jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)[a-z]*\.?\s*(?:de\s+)?(\d{4}|\d{2})(?![0-9a-z])"
)


def _fix_digits(token: str) -> tuple[str, bool]:
    fixed = token.translate(_OCR_DIGITS)
    return fixed, fixed != token


def _year(y: str, today: date) -> tuple[int, bool]:
    if len(y) == 4:
        return int(y), False
    yy = int(y)
    return (2000 + yy if yy <= today.year % 100 else 1900 + yy), True


def find_dates(raw_line: str, today: date) -> list[dict]:
    """Datas de uma linha, na ordem em que aparecem.

    Retorna {start, end, iso (ou None), flags}. Opera sobre a linha original
    (precisa distinguir 'O' de '0') e usa posições comparáveis às de norm().
    """
    out: list[dict] = []
    for m in _NUM_DATE.finditer(raw_line):
        d_s, f1 = _fix_digits(m.group(1))
        m_s, f2 = _fix_digits(m.group(2))
        y_s, f3 = _fix_digits(m.group(3))
        if not (d_s.isdigit() and m_s.isdigit() and y_s.isdigit()):
            continue
        # evita "Sa/Bb/.." : exige pelo menos metade de dígitos reais
        real = sum(c.isdigit() for c in m.group(0))
        if real < 4:
            continue
        out.append(_build_date(m.start(), m.end(), int(d_s), int(m_s), y_s, f1 or f2 or f3, today))

    lowered = norm(raw_line)
    for m in _TEXT_DATE.finditer(lowered):
        if any(o["start"] <= m.start() < o["end"] for o in out):
            continue
        out.append(_build_date(m.start(), m.end(), int(m.group(1)), MONTHS[m.group(2)], m.group(3), False, today))

    out.sort(key=lambda d: d["start"])
    # descarta datas de validade/fabricação/nascimento
    low = norm(raw_line)
    kept = []
    for d in out:
        before = low[max(0, d["start"] - 14):d["start"]]
        if any(w in before for w in _NOT_APPLICATION):
            continue
        kept.append(d)
    return kept


def _build_date(start: int, end: int, day: int, month: int, year_s: str, ocr_fixed: bool, today: date) -> dict:
    flags: list[str] = []
    year, two_digit = _year(year_s, today)
    if two_digit:
        flags.append("ano com 2 dígitos")
    if ocr_fixed:
        flags.append("dígitos corrigidos")
    iso = None
    try:
        dt = date(year, month, day)
        if dt > today:
            flags.append("data no futuro")
        elif year < 1900:
            flags.append("data inválida")
        else:
            iso = dt.isoformat()
    except ValueError:
        flags.append("data inválida")
    return {"start": start, "end": end, "iso": iso, "flags": flags}


# ---------------------------------------------------------------- doses
_DOSE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?<![a-z])(?:dose\s*unica|d\.?\s?u|unica)(?![a-z])"), "du"),
    # "1ª dose"; o OCR costuma ler o "ª" como 2/9/*/° ("12 dose" == "1ª dose")
    (re.compile(r"(?<![0-9a-z])([1-5])\s*[2-9]?\s*[ao*°]?\s*\.?\s*(?:dose|dos)(?![a-z])"), "d{1}"),
    (re.compile(r"(?<![0-9a-z])([6-9])\s*[ao]?\.?\s*(?:dose|dos)(?![a-z])"), "d{1}"),
    (re.compile(r"(?<![a-z])dose\s*([1-9])(?![0-9a-z])"), "d{1}"),
    (re.compile(r"(?<![a-z0-9])d\s?([1-9])(?![0-9a-z/])"), "d{1}"),
    (re.compile(r"(?<![0-9a-z])([1-9])\s*[ao]?\.?\s*refor[cç]o"), "r{1}"),
    (re.compile(r"refor[cç]o\s*([1-9])(?![0-9a-z/])"), "r{1}"),
    (re.compile(r"(?<![a-z0-9])r\s?([1-9])(?![0-9a-z/])"), "r{1}"),
    (re.compile(r"refor[cç]o"), "r"),
    (re.compile(r"(?<![a-z])ref\.?\s*([1-9])?(?![a-z0-9/])"), "r{1}"),
    (re.compile(r"(?<![a-z])(?:anual|campanha)(?![a-z])"), "anual"),
]


def find_doses(text_norm: str) -> list[dict]:
    cands = []
    for rx, tmpl in _DOSE_PATTERNS:
        for m in rx.finditer(text_norm):
            key = tmpl
            if "{1}" in tmpl:
                key = tmpl.replace("{1}", m.group(1) or "")
            cands.append({"key": key, "start": m.start(), "end": m.end()})
    # remove sobreposições (prefere o trecho mais longo, depois o mais à esquerda)
    cands.sort(key=lambda c: (-(c["end"] - c["start"]), c["start"]))
    chosen: list[dict] = []
    for c in cands:
        if all(c["end"] <= o["start"] or c["start"] >= o["end"] for o in chosen):
            chosen.append(c)
    chosen.sort(key=lambda c: c["start"])
    return chosen


# ---------------------------------------------------------------- parser
def parse_lines(lines: list[Line], today: date | None = None) -> list[Record]:
    today = today or date.today()
    records: list[Record] = []
    ctx: dict | None = None   # vacina "herdada" de linhas anteriores
    ctx_age = 0

    for line in lines:
        raw = line.text.strip()
        if not raw:
            continue
        tn = apply_fixups(norm(raw))
        ocr_factor = 0.6 + 0.4 * (line.conf if line.conf is not None else 0.8)

        vaccines = find_vaccines(tn)
        dates = find_dates(raw, today)
        doses = find_doses(tn)

        if vaccines:
            ctx, ctx_age = vaccines[-1], 0
        else:
            ctx_age += 1

        if not dates:
            continue

        # --- decide a quais vacinas as datas pertencem
        segments: list[tuple[dict | None, list, list, bool]] = []
        if len(vaccines) <= 1:
            v = vaccines[0] if vaccines else None
            inherited = False
            if v is None and ctx is not None and ctx_age <= 3:
                v, inherited = ctx, True
            if v is None and any(w in tn for w in _ID_LINE):
                continue
            segments.append((v, dates, doses, inherited))
        else:
            # várias vacinas na mesma linha (cartões em duas colunas)
            bounds = [v["start"] for v in vaccines] + [10**9]
            bounds[0] = 0
            for i, v in enumerate(vaccines):
                s, e = bounds[i], bounds[i + 1]
                segments.append((v,
                                 [d for d in dates if s <= d["start"] < e],
                                 [x for x in doses if s <= x["start"] < e], False))

        for v, seg_dates, seg_doses, inherited in segments:
            for i, d in enumerate(seg_dates):
                flags = list(d["flags"])
                dose_key = seg_doses[i]["key"] if i < len(seg_doses) else ""
                score = 1.0
                if v is None:
                    flags.append("vacina não identificada")
                    score = 0.35
                elif inherited:
                    flags.append("vacina herdada da linha anterior")
                    score = 0.7
                else:
                    score = v["score"]
                    if score < 1.0:
                        flags.append("nome da vacina aproximado")
                if not dose_key:
                    flags.append("dose não identificada")
                    score *= 0.9
                if d["iso"] is None:
                    score *= 0.4
                elif d["flags"]:
                    score *= 0.85
                score *= ocr_factor
                records.append(Record(
                    vaccine_key=v["key"] if v else "",
                    vaccine_name=v["name"] if v else "",
                    dose_key=dose_key,
                    date=d["iso"],
                    confidence=round(min(score, 1.0), 3),
                    raw=raw,
                    flags=flags,
                ))
    return _dedupe(records)


def parse_text(text: str, today: date | None = None) -> list[Record]:
    return parse_lines([Line(t) for t in text.splitlines()], today)


def _dedupe(records: list[Record]) -> list[Record]:
    best: dict[tuple, Record] = {}
    order: list[tuple] = []
    for r in records:
        k = (r.vaccine_key, r.dose_key, r.date, r.raw if r.date is None else "")
        if k not in best:
            best[k] = r
            order.append(k)
        elif r.confidence > best[k].confidence:
            best[k] = r
    return [best[k] for k in order]


def score_records(records: list[Record]) -> float:
    """Pontuação usada para escolher a melhor variante de pré-processamento."""
    return sum(r.confidence for r in records if r.date and r.vaccine_key)
