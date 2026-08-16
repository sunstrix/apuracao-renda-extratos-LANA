"""
Detector automático de instituição financeira a partir do texto extraído do PDF.

A ordem de verificação importa: extratos do Nubank citam Itaú/Santander/Bradesco
como contrapartes de Pix, então os marcadores do Nubank (header/rodapé próprios)
devem ser verificados primeiro.
"""
import unicodedata
from typing import List, Tuple

# Marcadores fortes (cabeçalho/rodapé do extrato, não linhas de contraparte)
_BANK_MARKERS: List[Tuple[str, Tuple[str, ...]]] = [
    ("nubank", (
        "nu pagamentos s.a.", "nu financeira s.a.", "nubank.com.br",
        "nu pagamentos - ip", "nu pagamentos - |p", "agência 0001 conta",
    )),
    ("caixa", ("caixa economica federal",)),
    ("bb", ("banco do brasil",)),
    ("bradesco", ("bradesco",)),
    ("santander", ("santander",)),
    ("itau", ("itau unibanco", "itau",)),
]

_DISPLAY_NAMES = {
    "nubank": "Nubank (Nu Pagamentos S.A.)",
    "itau": "Itaú Unibanco",
    "bradesco": "Bradesco",
    "santander": "Santander",
    "caixa": "Caixa Econômica Federal",
    "bb": "Banco do Brasil",
    "generic": "Instituição não identificada",
}


def _normalize(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in nfkd if unicodedata.category(c) != "Mn").lower()


def detect_bank(text: str) -> str:
    """Retorna a chave do banco detectado ou 'generic'."""
    norm = _normalize(text)
    for bank, markers in _BANK_MARKERS:
        if any(m in norm for m in markers):
            return bank
    return "generic"


def bank_display_name(bank: str) -> str:
    """Nome legível para uso no relatório e na interface."""
    return _DISPLAY_NAMES.get(bank, bank)