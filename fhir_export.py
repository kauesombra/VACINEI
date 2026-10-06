"""Exportação do histórico vacinal no padrão HL7 FHIR (R4).

Gera um Bundle do tipo "collection" com um recurso Patient e um recurso
Immunization por dose. O JSON pode ser validado no servidor público do HAPI
FHIR (https://hapi.fhir.org) — use apenas dados fictícios nesse servidor.

Observações importantes:
* Os códigos de vacina abaixo são CVX (CDC), um código internacional usado
  para dar significado padronizado a cada vacina. Sistemas brasileiros (RNDS)
  têm terminologia própria para imunobiológicos; para integrar de verdade,
  troque o mapeamento em CVX por ele, seguindo o guia de implementação adotado.
* O Patient leva só o nome informado no cadastro. Nenhum identificador
  (CPF, CNS, e-mail) é exportado.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from vaccines import DOSE_LABELS

CVX_SYSTEM = "http://hl7.org/fhir/sid/cvx"

# chave do catálogo -> código CVX (vacinas sem equivalente seguro ficam só com texto)
CVX = {
    "bcg": ("19", "BCG"),
    "hepatite_b": ("45", "Hepatitis B, unspecified formulation"),
    "hepatite_a": ("85", "Hepatitis A, unspecified formulation"),
    "pentavalente": ("102", "DTP-Haemophilus influenzae type b conjugate and Hepatitis b vaccine"),
    "hexavalente": ("146", "DTaP, IPV, Hib, HepB"),
    "polio_vip": ("10", "poliovirus vaccine, inactivated"),
    "polio_vop": ("02", "trivalent poliovirus vaccine, live, oral"),
    "polio": ("89", "poliovirus vaccine, unspecified formulation"),
    "rotavirus": ("122", "rotavirus, unspecified formulation"),
    "pneumococica": ("109", "pneumococcal vaccine, unspecified formulation"),
    "meningo_c": ("103", "meningococcal C conjugate"),
    "meningo_acwy": ("108", "meningococcal ACWY vaccine, unspecified formulation"),
    "meningo_b": ("163", "meningococcal B vaccine, unspecified formulation"),
    "febre_amarela": ("37", "yellow fever vaccine"),
    "triplice_viral": ("03", "MMR"),
    "tetra_viral": ("94", "MMRV"),
    "varicela": ("21", "varicella"),
    "dtp": ("01", "DTP"),
    "dtpa": ("115", "Tdap"),
    "dt": ("09", "Td (adult), adsorbed"),
    "hib": ("17", "Hib, unspecified formulation"),
    "hpv": ("137", "HPV, unspecified formulation"),
    "influenza": ("88", "influenza virus vaccine, unspecified formulation"),
    "covid19": ("213", "SARS-COV-2 (COVID-19) vaccine, unspecified"),
}

_NAMESPACE = uuid.UUID("6f1c2a52-7b64-4d0a-9d0e-5a7a0c1f3c11")


def _dose_number(dose_key: str) -> dict | None:
    """protocolApplied para uma chave de dose do sistema (d1..d5, du, r, r1.., anual)."""
    if not dose_key:
        return None
    if dose_key[0] == "d" and dose_key[1:].isdigit():
        return {"doseNumberPositiveInt": int(dose_key[1:])}
    return {"doseNumberString": DOSE_LABELS.get(dose_key, dose_key)}


def immunization(patient_url: str, row: dict) -> dict:
    """Recurso Immunization (R4) de uma dose do histórico."""
    code = CVX.get(row["vaccine_key"])
    vaccine_code: dict = {"text": row["vaccine_name"]}
    if code:
        vaccine_code["coding"] = [{"system": CVX_SYSTEM, "code": code[0], "display": code[1]}]
    manual = row.get("source") == "manual"
    res: dict = {
        "resourceType": "Immunization",
        "id": str(uuid.uuid5(_NAMESPACE, f"{patient_url}|{row['vaccine_key']}|{row['dose_key']}|{row['date']}")),
        "status": "completed",
        "vaccineCode": vaccine_code,
        "patient": {"reference": patient_url},
        "occurrenceDateTime": row["date"],
        # dado vindo da carteira/relato do próprio usuário, não do aplicador
        "primarySource": False,
        "reportOrigin": {"text": "Informado pelo usuário" if manual else "Carteira de vacinação digitalizada (OCR)"},
    }
    proto = _dose_number(row["dose_key"])
    if proto:
        res["protocolApplied"] = [proto]
    return res


def build_bundle(user_name: str, timeline: list[dict], user_id: int | str) -> dict:
    """Bundle FHIR com o paciente e todas as doses consolidadas."""
    patient_id = str(uuid.uuid5(_NAMESPACE, f"patient|{user_id}"))
    patient_url = f"urn:uuid:{patient_id}"
    entries = [{
        "fullUrl": patient_url,
        "resource": {"resourceType": "Patient", "id": patient_id, "name": [{"text": user_name}]},
    }]
    for row in sorted(timeline, key=lambda r: (r["date"], r["vaccine_name"])):
        imm = immunization(patient_url, row)
        entries.append({"fullUrl": f"urn:uuid:{imm['id']}", "resource": imm})
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "entry": entries,
    }
