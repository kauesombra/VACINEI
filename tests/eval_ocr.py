"""Mede acerto do pipeline (OCR + parser) contra o gabarito sintético.

Uso: python tests/eval_ocr.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ocr import process_file  # noqa: E402

SAMPLES = Path(__file__).parent / "samples"


def triples(items) -> set[tuple]:
    return {(i["vaccine_key"], i["dose_key"], i["date"]) for i in items}


def main() -> int:
    truth = triples(json.loads((SAMPLES / "gabarito.json").read_text(encoding="utf-8")))
    ok_all = True
    for name in ["caderneta_limpa.png", "caderneta_foto.jpg", "caderneta.pdf"]:
        path = SAMPLES / name
        t0 = time.time()
        res = process_file(str(path), path.suffix.lower())
        got = triples([r.as_dict() for r in res.records if r.date])
        tp = len(truth & got)
        precision = tp / len(got) if got else 0
        recall = tp / len(truth)
        print(f"{name:24s} {time.time() - t0:5.1f}s  recall={recall:.0%} precisão={precision:.0%} "
              f"({tp}/{len(truth)} corretos, {len(got) - tp} extras) variante={res.pages[0].variant}")
        for miss in sorted(truth - got):
            print("   faltou:", miss)
        for extra in sorted(got - truth):
            print("   extra :", extra)
        ok_all &= recall >= 0.8
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
