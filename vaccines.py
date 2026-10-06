"""Catálogo de vacinas e utilitários de normalização de texto.

Cada vacina tem uma chave estável (usada para consolidar registros), um nome
de exibição e uma lista de apelidos/abreviações que aparecem em cadernetas
brasileiras (PNI, clínicas particulares, campanhas).
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher


def norm(text: str) -> str:
    """Minúsculas e sem acentos (mantém o comprimento na imensa maioria dos casos)."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower()


# (chave, nome exibido, apelidos exatos, palavras para correspondência aproximada)
_RAW = [
    ("bcg", "BCG", ["bcg", "bcg-id", "bcg id"], []),
    ("hepatite_b", "Hepatite B", ["hepatite b", "hep b", "hep. b", "hepatite-b", "hbv"], []),
    ("hepatite_a", "Hepatite A", ["hepatite a", "hep a", "hep. a", "hepatite-a"], []),
    ("pentavalente", "Pentavalente (DTP/Hib/HB)",
     ["pentavalente", "penta", "dtp/hib/hb", "dtp+hib+hb", "dtp-hib-hb", "dtp hib hb"], ["pentavalente"]),
    ("hexavalente", "Hexavalente", ["hexavalente", "hexa"], ["hexavalente"]),
    ("polio_vip", "Poliomielite inativada (VIP)",
     ["poliomielite inativada", "polio inativada", "vacina inativada", "vip"], []),
    ("polio_vop", "Poliomielite oral (VOP)",
     ["poliomielite oral", "polio oral", "vop", "sabin", "gotinha"], []),
    ("polio", "Poliomielite", ["poliomielite", "polio", "paralisia infantil"], ["poliomielite"]),
    ("rotavirus", "Rotavírus humano", ["rotavirus", "rota virus", "rotarix", "rotateq", "vorh"], ["rotavirus"]),
    ("pneumococica", "Pneumocócica conjugada",
     ["pneumococica", "pneumococo", "pneumo", "pcv", "prevenar", "synflorix"], ["pneumococica"]),
    ("meningo_c", "Meningocócica C",
     ["meningococica c", "meningococica conjugada c", "meningo c", "men c", "menc"], []),
    ("meningo_acwy", "Meningocócica ACWY",
     ["meningococica acwy", "meningococica conjugada acwy", "meningo acwy", "men acwy", "menacwy"], []),
    ("meningo_b", "Meningocócica B",
     ["meningococica b", "meningo b", "men b", "menb", "bexsero"], []),
    ("febre_amarela", "Febre amarela",
     ["febre amarela", "febre-amarela", "f. amarela", "febre am"], ["amarela"]),
    ("triplice_viral", "Tríplice viral (SCR)",
     ["triplice viral", "scr", "sarampo caxumba rubeola", "sarampo, caxumba e rubeola", "mmr",
      "sar/cax/rub", "sar cax rub", "sar-cax-rub", "sarcaxrub", "sar/cax/rub."], []),
    ("tetra_viral", "Tetra viral (SCRV)", ["tetra viral", "tetraviral", "scrv"], ["tetraviral"]),
    ("varicela", "Varicela", ["varicela", "catapora", "varivax"], ["varicela"]),
    ("dtp", "DTP (tríplice bacteriana)", ["dtp", "triplice bacteriana"], []),
    ("dtpa", "dTpa (tríplice acelular adulto)",
     ["dtpa", "dtpa adulto", "triplice acelular", "boostrix", "adacel"], []),
    ("dt", "dT (dupla adulto)",
     ["dt", "dupla adulto", "dupla adulta", "difteria e tetano", "tetano", "antitetanica"], []),
    ("hib", "Haemophilus influenzae b (Hib)", ["hib", "haemophilus"], []),
    ("hpv", "HPV", ["hpv", "papilomavirus", "papiloma virus", "gardasil"], []),
    ("influenza", "Influenza (gripe)",
     ["influenza", "gripe", "febre da gripe", "vacina da gripe", "flu"], ["influenza"]),
    ("covid19", "Covid-19",
     ["covid-19", "covid 19", "covid19", "covid", "coronavac", "comirnaty", "astrazeneca",
      "pfizer", "janssen", "sars-cov-2", "sars cov 2", "bivalente"], ["coronavac", "astrazeneca"]),
    ("dengue", "Dengue", ["dengue", "qdenga"], ["dengue"]),
    ("raiva", "Raiva", ["raiva", "antirrabica", "anti-rabica"], []),
    ("febre_tifoide", "Febre tifoide", ["febre tifoide", "tifoide"], []),
    ("herpes_zoster", "Herpes zóster", ["herpes zoster", "zoster", "shingrix"], ["zoster"]),
    ("vsr", "VSR (vírus sincicial respiratório)",
     ["vsr", "abrysvo", "virus sincicial", "sincicial respiratorio"], []),
]

CATALOG = [{"key": k, "name": n, "aliases": a, "fuzzy": f} for k, n, a, f in _RAW]
BY_KEY = {c["key"]: c for c in CATALOG}


def vaccine_name(key: str) -> str | None:
    return BY_KEY[key]["name"] if key in BY_KEY else None


# ---------------------------------------------------------------- doses
DOSE_LABELS = {
    "d1": "1ª dose", "d2": "2ª dose", "d3": "3ª dose", "d4": "4ª dose", "d5": "5ª dose",
    "du": "Dose única",
    "r": "Reforço", "r1": "1º reforço", "r2": "2º reforço", "r3": "3º reforço",
    "anual": "Dose anual",
    "": "Dose não identificada",
}
DOSE_ORDER = {k: i for i, k in enumerate(["d1", "d2", "d3", "d4", "d5", "du", "r", "r1", "r2", "r3", "anual", ""])}


def dose_label(key: str) -> str:
    return DOSE_LABELS.get(key or "", DOSE_LABELS[""])


# ---------------------------------------------------------------- matching
def _alias_regex(alias: str) -> re.Pattern:
    parts = [re.escape(p) for p in re.split(r"[\s\-]+", alias) if p]
    body = r"[\s\-\.]*".join(parts)
    return re.compile(r"(?<![a-z0-9])" + body + r"(?![a-z0-9])")


_ALIAS_INDEX: list[tuple[str, str, re.Pattern, int]] = []
for _c in CATALOG:
    for _a in _c["aliases"]:
        _na = norm(_a)
        _ALIAS_INDEX.append((_c["key"], _na, _alias_regex(_na), len(_na)))
# aliases mais longos primeiro: "hepatite b" ganha de "hep", "triplice viral" de "triplice"
_ALIAS_INDEX.sort(key=lambda t: -t[3])

_FUZZY_WORDS: list[tuple[str, str]] = []
for _c in CATALOG:
    for _w in _c["fuzzy"]:
        _FUZZY_WORDS.append((_c["key"], norm(_w)))

# erros típicos de OCR: "Hepatite 8" -> "Hepatite B"
_FIXUPS = [
    (re.compile(r"(hepatite[\s\-]*)8(?![0-9a-z])"), r"\1b"),
    (re.compile(r"(hepatite[\s\-]*)4(?![0-9a-z])"), r"\1a"),
    (re.compile(r"(?<![a-z0-9])8cg(?![a-z0-9])"), "bcg"),
    (re.compile(r"(?<![a-z0-9])bcq(?![a-z0-9])"), "bcg"),
    (re.compile(r"(?<![a-z0-9])hp[vy](?![a-z0-9])"), "hpv"),
]


def apply_fixups(text: str) -> str:
    for pat, rep in _FIXUPS:
        text = pat.sub(rep, text)
    return text


def find_vaccines(text_norm: str) -> list[dict]:
    """Encontra vacinas numa linha já normalizada (norm + fixups).

    Retorna lista ordenada por posição, sem sobreposição:
    {key, name, start, end, score}
    """
    taken: list[tuple[int, int]] = []
    found: list[dict] = []

    def free(s: int, e: int) -> bool:
        return all(e <= ts or s >= te for ts, te in taken)

    for key, _alias, rx, _ in _ALIAS_INDEX:
        for m in rx.finditer(text_norm):
            if free(m.start(), m.end()):
                taken.append((m.start(), m.end()))
                found.append({"key": key, "name": BY_KEY[key]["name"],
                              "start": m.start(), "end": m.end(), "score": 1.0})

    # correspondência aproximada para palavras longas (tolera erros de OCR)
    for m in re.finditer(r"[a-z]{6,}", text_norm):
        if not free(m.start(), m.end()):
            continue
        word = m.group(0)
        best = (0.0, None)
        for key, target in _FUZZY_WORDS:
            if abs(len(word) - len(target)) > 3:
                continue
            r = SequenceMatcher(None, word, target).ratio()
            if r > best[0]:
                best = (r, key)
        if best[1] and best[0] >= 0.84:
            taken.append((m.start(), m.end()))
            found.append({"key": best[1], "name": BY_KEY[best[1]]["name"],
                          "start": m.start(), "end": m.end(), "score": round(best[0], 3)})

    found.sort(key=lambda d: d["start"])
    # "poliomielite" genérico não deve coexistir com VIP/VOP na mesma linha
    keys = {f["key"] for f in found}
    if "polio" in keys and ({"polio_vip", "polio_vop"} & keys):
        found = [f for f in found if f["key"] != "polio"]
    return found
