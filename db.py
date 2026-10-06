"""Banco de dados (SQLite) e lógica de consolidação do histórico vacinal."""
from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from datetime import date

from flask import current_app, g

from vaccines import BY_KEY, DOSE_ORDER, dose_label, norm

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT NOT NULL,
  email         TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS documents (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  original_name TEXT NOT NULL,
  stored_name   TEXT NOT NULL,
  ext           TEXT NOT NULL,
  pages         INTEGER NOT NULL DEFAULT 1,
  status        TEXT NOT NULL DEFAULT 'pendente',      -- pendente | revisado
  ocr_text      TEXT,
  draft_json    TEXT,                                  -- extração automática (rascunho)
  warnings_json TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_documents_user ON documents(user_id);

CREATE TABLE IF NOT EXISTS vaccinations (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  document_id  INTEGER REFERENCES documents(id) ON DELETE SET NULL,
  vaccine_key  TEXT NOT NULL,
  vaccine_name TEXT NOT NULL,
  dose_key     TEXT NOT NULL DEFAULT '',
  date         TEXT NOT NULL,                          -- ISO AAAA-MM-DD
  confidence   REAL,
  raw_line     TEXT,
  source       TEXT NOT NULL DEFAULT 'ocr',            -- ocr | manual
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (user_id, vaccine_key, dose_key, date)
);
CREATE INDEX IF NOT EXISTS idx_vacc_user ON vaccinations(user_id, date);
"""


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(path: str) -> None:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.commit()
    con.close()


# ---------------------------------------------------------------- vacinas
def custom_key(name: str) -> str:
    return "custom:" + re.sub(r"\s+", " ", norm(name)).strip()[:60]


def resolve_vaccine(selected: str, custom_name: str) -> tuple[str, str] | None:
    """(chave, nome) a partir da seleção do formulário; None se inválido."""
    if selected in BY_KEY:
        return selected, BY_KEY[selected]["name"]
    if selected == "custom":
        name = re.sub(r"\s+", " ", (custom_name or "").strip())[:80]
        if len(name) >= 2:
            return custom_key(name), name
    return None


def valid_iso_date(value: str) -> str | None:
    try:
        d = date.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if d.year < 1900 or d > date.today():
        return None
    return d.isoformat()


# ---------------------------------------------------------------- consolidação
def save_vaccination(db, user_id: int, document_id: int | None, vaccine_key: str, vaccine_name: str,
                     dose_key: str, iso_date: str, confidence: float | None, raw: str | None,
                     source: str) -> str:
    """Insere um registro no histórico do usuário, consolidando duplicatas.

    Retorna 'inserido', 'duplicado' (já existia) ou 'mesclado' (completou a dose
    de um registro existente que estava sem dose).
    """
    row = db.execute(
        "SELECT id, dose_key FROM vaccinations WHERE user_id=? AND vaccine_key=? AND date=? "
        "AND (dose_key=? OR dose_key='' OR ?='') "
        "ORDER BY CASE WHEN dose_key=? THEN 0 ELSE 1 END LIMIT 1",
        (user_id, vaccine_key, iso_date, dose_key, dose_key, dose_key),
    ).fetchone()
    if row:
        if row["dose_key"] == dose_key or dose_key == "":
            return "duplicado"
        db.execute("UPDATE vaccinations SET dose_key=? WHERE id=?", (dose_key, row["id"]))
        return "mesclado"
    db.execute(
        "INSERT INTO vaccinations (user_id, document_id, vaccine_key, vaccine_name, dose_key, date, "
        "confidence, raw_line, source) VALUES (?,?,?,?,?,?,?,?,?)",
        (user_id, document_id, vaccine_key, vaccine_name, dose_key, iso_date, confidence, raw, source),
    )
    return "inserido"


_SINGLE_SLOT = {"d1", "d2", "d3", "d4", "d5", "du"}


def build_history(db, user_id: int) -> dict:
    """Histórico consolidado: agrupado por vacina, doses em ordem cronológica."""
    rows = db.execute(
        "SELECT v.*, d.original_name AS doc_name FROM vaccinations v "
        "LEFT JOIN documents d ON d.id = v.document_id "
        "WHERE v.user_id=? ORDER BY v.date, v.id", (user_id,)
    ).fetchall()

    groups: dict[str, dict] = {}
    for r in rows:
        g_ = groups.setdefault(r["vaccine_key"], {"key": r["vaccine_key"], "name": r["vaccine_name"], "doses": []})
        g_["doses"].append({**dict(r), "dose_label": dose_label(r["dose_key"])})

    for g_ in groups.values():
        g_["doses"].sort(key=lambda d: (d["date"], DOSE_ORDER.get(d["dose_key"], 99)))
        # mesma dose (ex.: 1ª dose) registrada em datas diferentes => possível inconsistência
        by_slot: dict[str, set] = defaultdict(set)
        for d in g_["doses"]:
            if d["dose_key"] in _SINGLE_SLOT:
                by_slot[d["dose_key"]].add(d["date"])
        conflicts = {k for k, dates in by_slot.items() if len(dates) > 1}
        g_["conflicts"] = sorted(conflicts, key=lambda k: DOSE_ORDER.get(k, 99))
        for d in g_["doses"]:
            d["conflict"] = d["dose_key"] in conflicts
        g_["first_date"] = g_["doses"][0]["date"]
        g_["last_date"] = g_["doses"][-1]["date"]

    ordered = sorted(groups.values(), key=lambda g_: (g_["first_date"], g_["name"]))
    timeline = [{**d, "vaccine_name": g_["name"]} for g_ in ordered for d in g_["doses"]]
    timeline.sort(key=lambda d: (d["date"], d["vaccine_name"]), reverse=True)
    return {
        "groups": ordered,
        "timeline": timeline,
        "total_doses": len(rows),
        "total_vaccines": len(ordered),
        "last_date": max((r["date"] for r in rows), default=None),
        "n_conflicts": sum(len(g_["conflicts"]) for g_ in ordered),
    }
