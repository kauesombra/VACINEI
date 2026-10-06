"""Exportação FHIR e rotas relacionadas."""
import io
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import fhir_export as fhir  # noqa: E402
from app import create_app  # noqa: E402

SAMPLES = ROOT / "tests" / "samples"

class FhirTests(unittest.TestCase):
    ROWS = [
        {"vaccine_key": "bcg", "vaccine_name": "BCG", "dose_key": "du", "date": "2019-03-12", "source": "ocr"},
        {"vaccine_key": "pentavalente", "vaccine_name": "Pentavalente", "dose_key": "d2", "date": "2019-07-14", "source": "ocr"},
        {"vaccine_key": "custom_x", "vaccine_name": "Outra vacina", "dose_key": "r1", "date": "2020-01-02", "source": "manual"},
    ]

    def test_bundle(self):
        b = fhir.build_bundle("Maria Teste", self.ROWS, 7)
        self.assertEqual(b["resourceType"], "Bundle")
        self.assertEqual(b["type"], "collection")
        patient, *imms = [e["resource"] for e in b["entry"]]
        self.assertEqual(patient["resourceType"], "Patient")
        self.assertEqual(patient["name"][0]["text"], "Maria Teste")
        self.assertEqual(len(imms), 3)
        for i in imms:
            self.assertEqual(i["resourceType"], "Immunization")
            self.assertEqual(i["status"], "completed")
            self.assertIn("primarySource", i)
            self.assertEqual(i["patient"]["reference"], b["entry"][0]["fullUrl"])
            self.assertRegex(i["occurrenceDateTime"], r"^\d{4}-\d{2}-\d{2}$")
        bcg, penta, custom = imms
        self.assertEqual(bcg["vaccineCode"]["coding"][0]["system"], fhir.CVX_SYSTEM)
        self.assertEqual(bcg["vaccineCode"]["coding"][0]["code"], "19")
        self.assertEqual(bcg["protocolApplied"][0]["doseNumberString"], "Dose única")
        self.assertEqual(penta["protocolApplied"][0]["doseNumberPositiveInt"], 2)
        self.assertNotIn("coding", custom["vaccineCode"])      # sem código seguro: só texto
        self.assertEqual(custom["reportOrigin"]["text"], "Informado pelo usuário")
        # ids estáveis: exportar duas vezes dá os mesmos recursos
        again = fhir.build_bundle("Maria Teste", self.ROWS, 7)
        self.assertEqual([e["fullUrl"] for e in b["entry"]], [e["fullUrl"] for e in again["entry"]])
        json.dumps(b)  # serializável

    def test_catalogo_tem_cvx_valido(self):
        from vaccines import BY_KEY
        for key in fhir.CVX:
            self.assertIn(key, BY_KEY)
            self.assertRegex(fhir.CVX[key][0], r"^\d{2,3}$")


class AppIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.app = create_app({"TESTING": True, "DATA_DIR": cls.tmp.name})

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def csrf(self, c, path):
        return re.search(r'name="csrf_token" value="([^"]+)"', c.get(path).get_data(as_text=True)).group(1)

    def test_rota_fhir_e_checkbox(self):
        c = self.app.test_client()
        tok = self.csrf(c, "/cadastro")
        c.post("/cadastro", data={"csrf_token": tok, "name": "Ana", "email": "ana@example.com",
                                  "password": "senha-forte-1", "password2": "senha-forte-1", "consent": "on"})
        tok = self.csrf(c, "/historico")
        c.post("/vacinas/nova", data={"csrf_token": tok, "vaccine": "bcg", "dose": "du", "date": "2019-03-12"})
        self.assertEqual(c.get("/historico.fhir.json").status_code, 200)
        b = json.loads(c.get("/historico.fhir.json").get_data(as_text=True))
        self.assertEqual(b["entry"][0]["resource"]["name"][0]["text"], "Ana")
        self.assertEqual(b["entry"][1]["resource"]["vaccineCode"]["coding"][0]["code"], "19")
        self.assertIn("Exportar FHIR", c.get("/historico").get_data(as_text=True))

        # deslogado não exporta
        self.assertEqual(self.app.test_client().get("/historico.fhir.json").status_code, 302)


if __name__ == "__main__":
    unittest.main()
