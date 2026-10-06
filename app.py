"""VACINEI — digitalização e organização de carteiras de vacinação com OCR."""
from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
import shutil
import time
import uuid
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import (Flask, Response, abort, flash, g, redirect, render_template, request,
                   send_file, session, url_for)
from PIL import Image
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import db as dbm
import fhir_export as fhir
from ocr import IMAGE_EXTS, MAX_PAGES, process_file
from vaccines import CATALOG, DOSE_LABELS, DOSE_ORDER, dose_label

BASE = Path(__file__).resolve().parent
ALLOWED_EXTS = IMAGE_EXTS | {".pdf"}
MAX_FILES_PER_UPLOAD = 5
MAX_REVIEW_ROWS = 300
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
Image.MAX_IMAGE_PIXELS = 80_000_000  # proteção contra "decompression bomb"


class UploadError(Exception):
    pass


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    data_dir = Path((test_config or {}).get("DATA_DIR") or os.environ.get("VACINEI_DATA") or BASE / "instance")
    app.config.update(
        DATA_DIR=str(data_dir),
        MAX_CONTENT_LENGTH=15 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("VACINEI_HTTPS") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),
    )
    if test_config:
        app.config.update(test_config)
    data_dir.mkdir(parents=True, exist_ok=True)
    app.config.setdefault("DATABASE", str(data_dir / "vacinei.db"))
    app.config.setdefault("UPLOAD_DIR", str(data_dir / "uploads"))
    Path(app.config["UPLOAD_DIR"]).mkdir(parents=True, exist_ok=True)

    # chave de sessão: variável de ambiente ou arquivo persistente (nunca fixa no código)
    if not app.config.get("SECRET_KEY"):
        key_file = data_dir / "secret.key"
        if os.environ.get("VACINEI_SECRET"):
            app.config["SECRET_KEY"] = os.environ["VACINEI_SECRET"]
        else:
            if not key_file.exists():
                key_file.write_text(secrets.token_hex(32))
                key_file.chmod(0o600)
            app.config["SECRET_KEY"] = key_file.read_text().strip()

    dbm.init_db(app.config["DATABASE"])
    app.teardown_appcontext(dbm.close_db)

    login_attempts: dict[tuple, list[float]] = {}

    # ------------------------------------------------------------ infraestrutura
    @app.template_filter("br_date")
    def br_date(value: str | None) -> str:
        try:
            return date.fromisoformat(value).strftime("%d/%m/%Y")
        except (TypeError, ValueError):
            return value or "—"

    @app.template_filter("br_datetime")
    def br_datetime(value: str | None) -> str:
        try:
            return datetime.fromisoformat(value).strftime("%d/%m/%Y %H:%M")
        except (TypeError, ValueError):
            return value or "—"

    def csrf_token() -> str:
        if "_csrf" not in session:
            session["_csrf"] = secrets.token_urlsafe(32)
        return session["_csrf"]

    @app.context_processor
    def inject():
        return {
            "csrf_token": csrf_token,
            "catalog": CATALOG,
            "dose_options": sorted(DOSE_LABELS.items(), key=lambda kv: DOSE_ORDER.get(kv[0], 99)),
            "dose_label": dose_label,
            "today_iso": date.today().isoformat(),
        }

    @app.before_request
    def load_user_and_check_csrf():
        g.user = None
        uid = session.get("uid")
        if uid:
            g.user = dbm.get_db().execute("SELECT id, name, email FROM users WHERE id=?", (uid,)).fetchone()
            if g.user is None:
                session.clear()
        if request.method == "POST":
            sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
            if not sent or not secrets.compare_digest(sent, session.get("_csrf", "")):
                abort(400, "Sessão expirada ou formulário inválido. Recarregue a página e tente de novo.")

    @app.after_request
    def security_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Cache-Control"] = "no-store" if g.get("user") else resp.headers.get("Cache-Control", "no-cache")
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
        )
        return resp

    def login_required(view):
        @wraps(view)
        def wrapped(*a, **kw):
            if g.user is None:
                return redirect(url_for("login", next=request.path))
            return view(*a, **kw)
        return wrapped

    def safe_next(target: str | None) -> str:
        if target and target.startswith("/") and not target.startswith("//"):
            return target
        return url_for("history")

    def user_dir(uid: int) -> Path:
        d = Path(app.config["UPLOAD_DIR"]) / str(uid)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def get_document(doc_id: int):
        doc = dbm.get_db().execute("SELECT * FROM documents WHERE id=? AND user_id=?", (doc_id, g.user["id"])).fetchone()
        if doc is None:
            abort(404)
        return doc

    # ------------------------------------------------------------ páginas públicas / conta
    @app.get("/")
    def index():
        if g.user:
            return redirect(url_for("history"))
        return render_template("index.html")

    @app.get("/privacidade")
    def privacy():
        return render_template("privacy.html")

    @app.route("/cadastro", methods=["GET", "POST"])
    def register():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            pw, pw2 = request.form.get("password", ""), request.form.get("password2", "")
            consent = request.form.get("consent") == "on"
            error = None
            if len(name) < 2 or len(name) > 80:
                error = "Informe seu nome (2 a 80 caracteres)."
            elif not EMAIL_RE.match(email) or len(email) > 120:
                error = "Informe um e-mail válido."
            elif len(pw) < 8:
                error = "A senha precisa ter pelo menos 8 caracteres."
            elif pw != pw2:
                error = "As senhas não conferem."
            elif not consent:
                error = "É necessário concordar com o tratamento dos dados para criar a conta."
            else:
                db = dbm.get_db()
                if db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                    error = "Este e-mail já está cadastrado. Tente entrar."
                else:
                    cur = db.execute("INSERT INTO users (name, email, password_hash) VALUES (?,?,?)",
                                     (name, email, generate_password_hash(pw)))
                    db.commit()
                    session.clear()
                    session["uid"] = cur.lastrowid
                    session.permanent = True
                    flash("Conta criada! Comece enviando uma foto ou PDF da sua carteira de vacinação.", "ok")
                    return redirect(url_for("upload_form"))
            flash(error, "erro")
            return render_template("register.html", form=request.form), 400
        return render_template("register.html", form={})

    @app.route("/entrar", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            key = (request.remote_addr, email)
            now = time.time()
            recent = [t for t in login_attempts.get(key, []) if now - t < 600]
            login_attempts[key] = recent
            if len(recent) >= 8:
                flash("Muitas tentativas. Aguarde alguns minutos e tente novamente.", "erro")
                return render_template("login.html", form=request.form), 429
            user = dbm.get_db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
                login_attempts.pop(key, None)
                session.clear()
                session["uid"] = user["id"]
                session.permanent = True
                return redirect(safe_next(request.args.get("next")))
            recent.append(now)
            flash("E-mail ou senha incorretos.", "erro")
            return render_template("login.html", form=request.form), 401
        return render_template("login.html", form={})

    @app.post("/sair")
    def logout():
        session.clear()
        return redirect(url_for("index"))

    @app.route("/conta", methods=["GET"])
    @login_required
    def account():
        db = dbm.get_db()
        counts = {
            "documents": db.execute("SELECT COUNT(*) FROM documents WHERE user_id=?", (g.user["id"],)).fetchone()[0],
            "vaccinations": db.execute("SELECT COUNT(*) FROM vaccinations WHERE user_id=?", (g.user["id"],)).fetchone()[0],
        }
        return render_template("account.html", counts=counts)

    @app.post("/conta/excluir")
    @login_required
    def account_delete():
        db = dbm.get_db()
        user = db.execute("SELECT * FROM users WHERE id=?", (g.user["id"],)).fetchone()
        if not check_password_hash(user["password_hash"], request.form.get("password", "")):
            flash("Senha incorreta. A conta não foi excluída.", "erro")
            return redirect(url_for("account"))
        shutil.rmtree(Path(app.config["UPLOAD_DIR"]) / str(user["id"]), ignore_errors=True)
        db.execute("DELETE FROM users WHERE id=?", (user["id"],))  # cascata: documentos e vacinas
        db.commit()
        session.clear()
        flash("Sua conta e todos os seus dados foram excluídos.", "ok")
        return redirect(url_for("index"))

    # ------------------------------------------------------------ upload + OCR
    @app.get("/enviar")
    @login_required
    def upload_form():
        return render_template("upload.html", max_pages=MAX_PAGES, max_files=MAX_FILES_PER_UPLOAD)

    def process_upload(storage, uid: int) -> int:
        original = secure_filename(storage.filename or "") or "documento"
        ext = Path(original).suffix.lower()
        if ext not in ALLOWED_EXTS:
            raise UploadError(f"“{original}”: formato não suportado. Envie PDF, PNG, JPG, WEBP, BMP ou TIFF.")
        stem = uuid.uuid4().hex
        udir = user_dir(uid)
        path = udir / f"{stem}{ext}"
        storage.save(path)
        try:
            if ext == ".pdf":
                with open(path, "rb") as fh:
                    if fh.read(5) != b"%PDF-":
                        raise ValueError("cabeçalho PDF ausente")
            else:
                with Image.open(path) as im:
                    im.verify()
        except Exception:
            path.unlink(missing_ok=True)
            raise UploadError(f"“{original}” não parece ser um arquivo {ext[1:].upper()} válido.")

        try:
            result = process_file(str(path), ext)
        except Exception:
            app.logger.exception("falha no OCR de %s", original)
            path.unlink(missing_ok=True)
            raise UploadError(f"Não foi possível ler “{original}”. Tente outra foto, com mais luz e o documento reto.")

        for p in result.pages:
            p.preview.save(udir / f"{stem}_p{p.index}.jpg", quality=85)
        draft = [{**r.as_dict(), "page": p.index} for p in result.pages for r in p.records]
        db = dbm.get_db()
        cur = db.execute(
            "INSERT INTO documents (user_id, original_name, stored_name, ext, pages, ocr_text, draft_json, warnings_json) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (uid, original[:120], path.name, ext, len(result.pages), result.text,
             json.dumps(draft, ensure_ascii=False), json.dumps(result.warnings, ensure_ascii=False)),
        )
        db.commit()
        return cur.lastrowid

    @app.post("/enviar")
    @login_required
    def upload():
        files = [f for f in request.files.getlist("arquivo") if f and f.filename]
        if not files:
            flash("Selecione ao menos um arquivo (foto ou PDF).", "erro")
            return redirect(url_for("upload_form"))
        if len(files) > MAX_FILES_PER_UPLOAD:
            flash(f"Envie no máximo {MAX_FILES_PER_UPLOAD} arquivos por vez.", "erro")
            return redirect(url_for("upload_form"))
        created: list[int] = []
        for f in files:
            try:
                created.append(process_upload(f, g.user["id"]))
            except UploadError as e:
                flash(str(e), "erro")
        if len(created) == 1:
            return redirect(url_for("review", doc_id=created[0]))
        if created:
            flash(f"{len(created)} documentos lidos. Revise cada um para incluir no seu histórico.", "ok")
            return redirect(url_for("documents"))
        return redirect(url_for("upload_form"))

    @app.get("/documentos")
    @login_required
    def documents():
        docs = dbm.get_db().execute(
            "SELECT d.*, (SELECT COUNT(*) FROM vaccinations v WHERE v.document_id = d.id) AS n_saved "
            "FROM documents d WHERE d.user_id=? ORDER BY d.id DESC", (g.user["id"],)).fetchall()
        items = []
        for d in docs:
            draft = json.loads(d["draft_json"] or "[]")
            items.append({**dict(d), "n_draft": len(draft)})
        return render_template("documents.html", docs=items)

    @app.get("/documentos/<int:doc_id>/previa/<int:page>")
    @login_required
    def preview(doc_id: int, page: int):
        doc = get_document(doc_id)
        if not 1 <= page <= doc["pages"]:
            abort(404)
        path = user_dir(g.user["id"]) / f"{Path(doc['stored_name']).stem}_p{page}.jpg"
        if not path.exists():
            abort(404)
        return send_file(path, mimetype="image/jpeg")

    @app.get("/documentos/<int:doc_id>/original")
    @login_required
    def original(doc_id: int):
        doc = get_document(doc_id)
        path = user_dir(g.user["id"]) / doc["stored_name"]
        if not path.exists():
            abort(404)
        return send_file(path, as_attachment=True, download_name=doc["original_name"])

    @app.post("/documentos/<int:doc_id>/excluir")
    @login_required
    def document_delete(doc_id: int):
        doc = get_document(doc_id)
        db = dbm.get_db()
        removed = 0
        if request.form.get("remove_records") == "on":
            removed = db.execute("DELETE FROM vaccinations WHERE document_id=? AND user_id=?",
                                 (doc_id, g.user["id"])).rowcount
        udir = user_dir(g.user["id"])
        stem = Path(doc["stored_name"]).stem
        (udir / doc["stored_name"]).unlink(missing_ok=True)
        for p in udir.glob(f"{stem}_p*.jpg"):
            p.unlink(missing_ok=True)
        db.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        db.commit()
        flash("Documento excluído" + (f" junto com {removed} registro(s) dele." if removed else
                                      ". Os registros já salvos no histórico foram mantidos."), "ok")
        return redirect(url_for("documents"))

    # ------------------------------------------------------------ revisão dos dados extraídos
    def draft_to_rows(draft: list[dict]) -> list[dict]:
        rows = []
        for i, r in enumerate(draft):
            rows.append({
                "rid": str(i), "vaccine": r.get("vaccine_key") or "", "custom": "",
                "dose": r.get("dose_key") or "", "date": r.get("date") or "",
                "include": bool(r.get("date") and r.get("vaccine_key")),
                "raw": r.get("raw", ""), "confidence": r.get("confidence"),
                "flags": r.get("flags", []), "page": r.get("page"), "errors": [],
            })
        return rows

    @app.route("/documentos/<int:doc_id>/revisar", methods=["GET", "POST"])
    @login_required
    def review(doc_id: int):
        doc = get_document(doc_id)
        warnings = json.loads(doc["warnings_json"] or "[]")
        db = dbm.get_db()

        if request.method == "GET":
            rows = draft_to_rows(json.loads(doc["draft_json"] or "[]"))
            return render_template("review.html", doc=doc, rows=rows, warnings=warnings, next_rid=len(rows))

        rids = request.form.getlist("rid")[:MAX_REVIEW_ROWS]
        rows, to_save, has_error = [], [], False
        for rid in rids:
            if not re.fullmatch(r"\d{1,4}", rid):
                continue
            row = {
                "rid": rid, "vaccine": request.form.get(f"vaccine_{rid}", ""),
                "custom": request.form.get(f"custom_{rid}", ""),
                "dose": request.form.get(f"dose_{rid}", ""), "date": request.form.get(f"date_{rid}", ""),
                "include": request.form.get(f"include_{rid}") == "on",
                "raw": request.form.get(f"raw_{rid}", "")[:300],
                "confidence": request.form.get(f"conf_{rid}", type=float),
                "flags": [], "page": request.form.get(f"page_{rid}", type=int), "errors": [],
            }
            if row["include"]:
                vac = dbm.resolve_vaccine(row["vaccine"], row["custom"])
                iso = dbm.valid_iso_date(row["date"])
                if vac is None:
                    row["errors"].append("Escolha a vacina.")
                if row["dose"] not in DOSE_LABELS:
                    row["errors"].append("Dose inválida.")
                if iso is None:
                    row["errors"].append("Informe uma data de aplicação válida (não pode estar no futuro).")
                if row["errors"]:
                    has_error = True
                else:
                    to_save.append((vac, row["dose"], iso, row["confidence"], row["raw"]))
            rows.append(row)

        if has_error or not to_save:
            flash("Corrija os campos destacados antes de salvar." if has_error else
                  "Marque ao menos um registro para salvar.", "erro")
            return render_template("review.html", doc=doc, rows=rows, warnings=warnings,
                                   next_rid=max([int(r["rid"]) for r in rows] + [0]) + 1), 400

        stats = {"inserido": 0, "duplicado": 0, "mesclado": 0}
        for (key, name), dose, iso, conf, raw in to_save:
            # linhas vindas do OCR carregam confiança; linhas adicionadas à mão na revisão, não
            source = "manual" if conf is None else "ocr"
            stats[dbm.save_vaccination(db, g.user["id"], doc_id, key, name, dose, iso, conf, raw, source)] += 1
        db.execute("UPDATE documents SET status='revisado' WHERE id=?", (doc_id,))
        db.commit()
        parts = [f"{stats['inserido']} novo(s) registro(s) adicionado(s)"]
        if stats["duplicado"]:
            parts.append(f"{stats['duplicado']} já existia(m) no histórico e foi(ram) consolidado(s)")
        if stats["mesclado"]:
            parts.append(f"{stats['mesclado']} completou(aram) a dose de registros existentes")
        flash("; ".join(parts) + ".", "ok")
        return redirect(url_for("history"))

    # ------------------------------------------------------------ histórico consolidado
    @app.get("/historico")
    @login_required
    def history():
        data = dbm.build_history(dbm.get_db(), g.user["id"])
        n_docs = dbm.get_db().execute("SELECT COUNT(*) FROM documents WHERE user_id=?", (g.user["id"],)).fetchone()[0]
        view = "timeline" if request.args.get("v") == "timeline" else "groups"
        return render_template("history.html", h=data, view=view, n_docs=n_docs)

    @app.post("/vacinas/nova")
    @login_required
    def vaccination_add():
        vac = dbm.resolve_vaccine(request.form.get("vaccine", ""), request.form.get("custom", ""))
        iso = dbm.valid_iso_date(request.form.get("date", ""))
        dose = request.form.get("dose", "")
        if vac is None or iso is None or dose not in DOSE_LABELS:
            flash("Para adicionar, informe vacina, dose e uma data válida (não pode estar no futuro).", "erro")
            return redirect(url_for("history"))
        db = dbm.get_db()
        res = dbm.save_vaccination(db, g.user["id"], None, vac[0], vac[1], dose, iso, None, None, "manual")
        db.commit()
        flash({"inserido": "Registro adicionado.", "duplicado": "Esse registro já existia no seu histórico.",
               "mesclado": "Registro existente atualizado com a dose informada."}[res], "ok")
        return redirect(url_for("history"))

    @app.post("/vacinas/<int:vid>/excluir")
    @login_required
    def vaccination_delete(vid: int):
        db = dbm.get_db()
        n = db.execute("DELETE FROM vaccinations WHERE id=? AND user_id=?", (vid, g.user["id"])).rowcount
        db.commit()
        if not n:
            abort(404)
        flash("Registro removido do histórico.", "ok")
        return redirect(url_for("history", v=request.args.get("v")))

    def csv_safe(value: str) -> str:
        return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value

    @app.get("/historico.csv")
    @login_required
    def history_csv():
        data = dbm.build_history(dbm.get_db(), g.user["id"])
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["Vacina", "Dose", "Data de aplicação", "Origem"])
        for d in sorted(data["timeline"], key=lambda x: (x["date"], x["vaccine_name"])):
            w.writerow([csv_safe(d["vaccine_name"]), d["dose_label"], br_date(d["date"]),
                        "Manual" if d["source"] == "manual" else "OCR"])
        return Response("﻿" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=historico-vacinal.csv"})

    @app.get("/historico.fhir.json")
    @login_required
    def history_fhir():
        data = dbm.build_history(dbm.get_db(), g.user["id"])
        bundle = fhir.build_bundle(g.user["name"], data["timeline"], g.user["id"])
        return Response(json.dumps(bundle, ensure_ascii=False, indent=2),
                        mimetype="application/fhir+json; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=historico-vacinal.fhir.json"})

    # ------------------------------------------------------------ erros
    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(413)
    @app.errorhandler(500)
    def error(e):
        code = getattr(e, "code", 500)
        msg = {404: "Página não encontrada.", 413: "O arquivo é maior que 15 MB. Reduza o tamanho e tente de novo.",
               500: "Algo deu errado do nosso lado."}.get(code, getattr(e, "description", "Requisição inválida."))
        return render_template("error.html", code=code, message=msg), code

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=os.environ.get("FLASK_DEBUG") == "1")
