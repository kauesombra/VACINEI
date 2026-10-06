"""Teste de ponta a ponta: cadastro -> upload (OCR real) -> revisão -> histórico consolidado."""
import io
import json
import re
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from werkzeug.datastructures import MultiDict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from app import create_app  # noqa: E402
import make_sample  # noqa: E402

SAMPLES = ROOT / "tests" / "samples"


class FormCollector(HTMLParser):
    """Serializa o <form id=review-form> como um navegador faria (ignora <template>)."""

    def __init__(self):
        super().__init__()
        self.data: list[tuple[str, str]] = []
        self.in_form = False
        self.template_depth = 0
        self.select_name = None
        self.select_options: list[tuple[str, bool]] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and a.get("id") == "review-form":
            self.in_form = True
        if not self.in_form:
            return
        if tag == "template":
            self.template_depth += 1
        if self.template_depth:
            return
        if tag == "input" and a.get("name"):
            t = a.get("type", "text")
            if t == "checkbox":
                if "checked" in a:
                    self.data.append((a["name"], a.get("value") or "on"))
            else:
                self.data.append((a["name"], a.get("value", "")))
        elif tag == "select":
            self.select_name, self.select_options = a.get("name"), []
        elif tag == "option" and self.select_name:
            self.select_options.append((a.get("value", ""), "selected" in a))

    def handle_endtag(self, tag):
        if tag == "template" and self.template_depth:
            self.template_depth -= 1
        elif tag == "select" and self.select_name:
            chosen = next((v for v, sel in self.select_options if sel),
                          self.select_options[0][0] if self.select_options else "")
            self.data.append((self.select_name, chosen))
            self.select_name = None
        elif tag == "form" and self.in_form and not self.template_depth:
            self.in_form = False


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        make_sample.main(SAMPLES)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.app = create_app({"TESTING": True, "DATA_DIR": cls.tmp.name})
        cls.truth = {(t["vaccine_key"], t["dose_key"], t["date"])
                     for t in json.loads((SAMPLES / "gabarito.json").read_text())}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # -------- helpers
    def client(self):
        return self.app.test_client()

    def csrf(self, c, path="/entrar"):
        html = c.get(path).get_data(as_text=True)
        return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)

    def register(self, c, email, name="Maria"):
        tok = self.csrf(c, "/cadastro")
        return c.post("/cadastro", data={"csrf_token": tok, "name": name, "email": email, "password": "senha-forte-1",
                                         "password2": "senha-forte-1", "consent": "on"})

    def upload(self, c, filename):
        tok = self.csrf(c, "/enviar")
        data = {"csrf_token": tok, "arquivo": (io.BytesIO((SAMPLES / filename).read_bytes()), filename)}
        return c.post("/enviar", data=data, content_type="multipart/form-data")

    def review_form(self, c, url):
        html = c.get(url).get_data(as_text=True)
        fc = FormCollector()
        fc.feed(html)
        return html, fc.data

    def history_triples(self, c):
        from db import build_history
        with self.app.app_context():
            import sqlite3
            con = sqlite3.connect(self.app.config["DATABASE"])
            con.row_factory = sqlite3.Row
            uid = con.execute("SELECT id FROM users WHERE email=?", (self._email,)).fetchone()["id"]
            rows = con.execute("SELECT vaccine_key, dose_key, date FROM vaccinations WHERE user_id=?", (uid,)).fetchall()
            con.close()
        return {(r["vaccine_key"], r["dose_key"], r["date"]) for r in rows}

    # -------- testes
    def test_fluxo_completo(self):
        c = self.client()
        self._email = "maria@example.com"
        self.assertEqual(self.register(c, self._email).status_code, 302)

        # 1) PDF -> revisão -> salvar
        r = self.upload(c, "caderneta.pdf")
        self.assertEqual(r.status_code, 302)
        url = r.headers["Location"]
        self.assertIn("/revisar", url)
        html, form = self.review_form(c, url)
        self.assertIn("Confira o que foi lido", html)
        self.assertGreaterEqual(len([k for k, _ in form if k == "rid"]), 12)
        r = c.post(url, data=MultiDict(form))
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:500])
        self.assertEqual(self.history_triples(c), self.truth)

        # 2) mesma carteira em foto: tudo já existe -> não pode duplicar
        r = self.upload(c, "caderneta_limpa.png")
        url2 = r.headers["Location"]
        _, form2 = self.review_form(c, url2)
        r = c.post(url2, data=MultiDict(form2), follow_redirects=True)
        page = r.get_data(as_text=True)
        self.assertIn("consolidado", page)
        self.assertEqual(self.history_triples(c), self.truth)

        # 3) histórico renderiza carimbos e resumo
        page = c.get("/historico").get_data(as_text=True)
        self.assertIn("12 doses registradas", page)
        self.assertIn("12/03/2019", page)
        self.assertIn('class="stamp', page)
        self.assertIn("Cronológico", page)
        self.assertEqual(c.get("/historico?v=timeline").status_code, 200)
        csv = c.get("/historico.csv").get_data(as_text=True)
        self.assertIn("Hepatite B;1ª dose;12/03/2019", csv)

        # 4) isolamento: outro usuário não vê documentos nem registros
        other = self.client()
        self.register(other, "joao@example.com", "João")
        self.assertEqual(other.get(url).status_code, 404)
        self.assertEqual(other.get(url.replace("/revisar", "/previa/1")).status_code, 404)
        self.assertIn("ainda está vazio", other.get("/historico").get_data(as_text=True))

        # 5) uma dose com data conflitante é sinalizada
        tok = self.csrf(c, "/historico")
        c.post("/vacinas/nova", data={"csrf_token": tok, "vaccine": "hepatite_b", "custom": "", "dose": "d1",
                                      "date": "2019-04-01"})
        page = c.get("/historico").get_data(as_text=True)
        self.assertIn("com mais de uma data", page)

        # 6) exclusão da conta apaga tudo, inclusive arquivos
        import sqlite3
        udir = Path(self.app.config["UPLOAD_DIR"])
        con = sqlite3.connect(self.app.config["DATABASE"])
        uid = con.execute("SELECT id FROM users WHERE email=?", (self._email,)).fetchone()[0]
        self.assertTrue(any((udir / str(uid)).glob("*.pdf")))
        tok = self.csrf(c, "/conta")
        r = c.post("/conta/excluir", data={"csrf_token": tok, "password": "senha-forte-1"})
        self.assertEqual(r.status_code, 302)
        self.assertFalse((udir / str(uid)).exists())
        for table, col in [("users", "id"), ("documents", "user_id"), ("vaccinations", "user_id")]:
            n = con.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (uid,)).fetchone()[0]
            self.assertEqual(n, 0, table)
        con.close()

    def test_seguranca(self):
        c = self.client()
        # POST sem CSRF é recusado
        self.assertEqual(c.post("/entrar", data={"email": "a@b.co", "password": "x"}).status_code, 400)
        # rotas protegidas redirecionam para login
        self.assertEqual(c.get("/historico").status_code, 302)
        self.assertIn("/entrar", c.get("/historico").headers["Location"])
        # open redirect bloqueado
        self.register(c, "seg@example.com")
        c2 = self.client()
        tok = self.csrf(c2, "/entrar")
        r = c2.post("/entrar?next=//evil.example", data={"csrf_token": tok, "email": "seg@example.com",
                                                          "password": "senha-forte-1"})
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("evil", r.headers["Location"])
        # senha errada
        c3 = self.client()
        tok = self.csrf(c3, "/entrar")
        self.assertEqual(c3.post("/entrar", data={"csrf_token": tok, "email": "seg@example.com",
                                                   "password": "errada"}).status_code, 401)
        # cabeçalhos de segurança
        h = c3.get("/").headers
        self.assertIn("default-src 'self'", h["Content-Security-Policy"])
        self.assertEqual(h["X-Frame-Options"], "DENY")

    def test_upload_invalido(self):
        c = self.client()
        self.register(c, "up@example.com")
        tok = self.csrf(c, "/enviar")
        # extensão não suportada
        r = c.post("/enviar", data={"csrf_token": tok, "arquivo": (io.BytesIO(b"x"), "virus.exe")},
                   content_type="multipart/form-data", follow_redirects=True)
        self.assertIn("formato não suportado", r.get_data(as_text=True))
        # extensão .png mas conteúdo falso
        r = c.post("/enviar", data={"csrf_token": tok, "arquivo": (io.BytesIO(b"nao sou imagem"), "falsa.png")},
                   content_type="multipart/form-data", follow_redirects=True)
        self.assertIn("não parece ser um arquivo PNG válido", r.get_data(as_text=True))
        # nenhum arquivo
        r = c.post("/enviar", data={"csrf_token": tok}, content_type="multipart/form-data", follow_redirects=True)
        self.assertIn("Selecione ao menos um arquivo", r.get_data(as_text=True))

    def test_revisao_com_erro_preserva_dados(self):
        c = self.client()
        self.register(c, "rev@example.com")
        r = self.upload(c, "caderneta_limpa.png")
        url = r.headers["Location"]
        _, form = self.review_form(c, url)
        # estraga a data da primeira linha
        form = [(k, ("2999-01-01" if k == "date_0" else v)) for k, v in form]
        r = c.post(url, data=MultiDict(form))
        self.assertEqual(r.status_code, 400)
        page = r.get_data(as_text=True)
        self.assertIn("Informe uma data de aplicação válida", page)
        self.assertIn("Corrija os campos destacados", page)


if __name__ == "__main__":
    unittest.main()
