import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parser import parse_text  # noqa: E402

TODAY = date(2026, 10, 5)


def P(text):
    return parse_text(text, TODAY)


def triples(text):
    return {(r.vaccine_key, r.dose_key, r.date) for r in P(text)}


class ParserTests(unittest.TestCase):
    def test_linha_simples(self):
        self.assertEqual(triples("Hepatite B 1ª dose 12/03/2019 UBS Centro"),
                         {("hepatite_b", "d1", "2019-03-12")})

    def test_dose_unica_e_reforco(self):
        t = triples("BCG Dose única 12/03/2019\nDTP 1º reforço 10/01/2021\nDTP reforço 11/02/2022")
        self.assertIn(("bcg", "du", "2019-03-12"), t)
        self.assertIn(("dtp", "r1", "2021-01-10"), t)
        self.assertIn(("dtp", "r", "2022-02-11"), t)

    def test_ocr_confunde_ordinal(self):
        # "1ª" lido como "12", "3ª" como "32"
        self.assertEqual(triples("HPV 12º dose 03/08/2024"), {("hpv", "d1", "2024-08-03")})
        self.assertEqual(triples("Covid-19 32 dose 18/11/2021"), {("covid19", "d3", "2021-11-18")})

    def test_ocr_confunde_digitos_na_data(self):
        r = P("Hepatite B 2ª dose 1O/05/2O21")[0]
        self.assertEqual(r.date, "2021-05-10")
        self.assertIn("dígitos corrigidos", r.flags)
        self.assertEqual(triples("Hepatite 8 1ª dose 10/05/2021"), {("hepatite_b", "d1", "2021-05-10")})

    def test_ano_de_dois_digitos(self):
        r = P("Febre amarela dose única 20/03/20")[0]
        self.assertEqual(r.date, "2020-03-20")
        self.assertIn("ano com 2 dígitos", r.flags)
        self.assertEqual(P("BCG dose única 01/02/98")[0].date, "1998-02-01")

    def test_data_textual(self):
        self.assertEqual(triples("Influenza dose anual 5 de abril de 2023"),
                         {("influenza", "anual", "2023-04-05")})

    def test_data_invalida_ou_futura_fica_sem_data(self):
        self.assertIsNone(P("BCG dose única 31/02/2020")[0].date)
        self.assertIsNone(P("BCG dose única 10/10/2030")[0].date)

    def test_ignora_nascimento_e_validade(self):
        self.assertEqual(P("Data de nascimento: 02/03/2019"), [])
        t = triples("BCG dose única 12/03/2019 Lote AB123 Val 05/09/2025")
        self.assertEqual(t, {("bcg", "du", "2019-03-12")})

    def test_vacina_herdada_da_linha_anterior(self):
        recs = P("Pentavalente\n1ª dose 12/05/2019\n2ª dose 14/07/2019")
        self.assertEqual({(r.vaccine_key, r.dose_key, r.date) for r in recs},
                         {("pentavalente", "d1", "2019-05-12"), ("pentavalente", "d2", "2019-07-14")})
        self.assertTrue(all("vacina herdada da linha anterior" in r.flags for r in recs))

    def test_duas_vacinas_na_mesma_linha(self):
        t = triples("BCG dose única 12/03/2019 Hepatite B 1ª dose 12/03/2019")
        self.assertEqual(t, {("bcg", "du", "2019-03-12"), ("hepatite_b", "d1", "2019-03-12")})

    def test_varias_doses_na_mesma_linha(self):
        t = triples("Hepatite A 1ª dose 10/01/2020 2ª dose 10/07/2020")
        self.assertEqual(t, {("hepatite_a", "d1", "2020-01-10"), ("hepatite_a", "d2", "2020-07-10")})

    def test_hepatite_a_nao_vira_b(self):
        self.assertEqual({r.vaccine_key for r in P("Hepatite A dose única 22/09/2020")}, {"hepatite_a"})

    def test_aproximacao_de_nome(self):
        r = P("Pentavalenle 1ª dose 12/05/2019")[0]  # erro de OCR
        self.assertEqual(r.vaccine_key, "pentavalente")
        self.assertIn("nome da vacina aproximado", r.flags)

    def test_data_sem_vacina_fica_para_revisao(self):
        r = P("Unidade X 12/03/2019")[0]
        self.assertEqual(r.vaccine_key, "")
        self.assertIn("vacina não identificada", r.flags)
        self.assertLess(r.confidence, 0.4)

    def test_dedup(self):
        self.assertEqual(len(P("BCG dose única 12/03/2019\nBCG dose única 12/03/2019")), 1)

    def test_confianca_menor_quando_incerto(self):
        limpo = P("BCG dose única 12/03/2019")[0].confidence
        incerto = P("BCG 12/03/2019")[0].confidence  # sem dose
        self.assertGreater(limpo, incerto)


if __name__ == "__main__":
    unittest.main()
