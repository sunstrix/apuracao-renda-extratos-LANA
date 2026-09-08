"""
Detector automático de instituição financeira a partir do texto extraído do PDF.

A ordem de verificação é CRÍTICA:
1. Bancos com marcadores MUITO específicos e únicos (Nubank, C6, Inter)
2. Bancos com marcadores específicos (Caixa, BB)
3. Bancos com marcadores genéricos que podem aparecer como contraparte (PicPay)
4. Bancos com marcadores MUITO genéricos (Itaú, Bradesco, Santander) - SEMPRE POR ÚLTIMO

Exemplo de problema: Um extrato PicPay pode conter "ITAU" ou "BRADESCO" como
contraparte de um Pix/Transferência. Se verificarmos "itau" antes de "picpay",
o extrato PicPay será classificado erroneamente como Itaú.
"""
import unicodedata
from typing import List, Tuple


# Marcadores fortes e específicos de cabeçalho/rodapé do extrato
# NUNCA use marcadores que possam aparecer em linhas de transação/contraparte
_BANK_MARKERS: List[Tuple[str, Tuple[str, ...]]] = [
    # ============================================
    # GRUPO 1: Marcadores MUITO específicos (prioridade máxima)
    # ============================================
    ("nubank", (
        "nu pagamentos s.a.",
        "nu financeira s.a.",
        "nubank.com.br",
        "nu pagamentos - ip",
        "nu pagamentos - |p",
        "agência 0001 conta",  # Padrão específico do Nubank
        "nu sociedade de crédito direto",
    )),
    ("c6", (
        "c6 bank",
        "c6bank",
        "c6 bank s.a.",
        "c6 consórcio",
    )),
    ("inter", (
        "banco inter",
        "inter cn",
        "inter s.a.",
        "banco intermedium",
    )),
    
    # ============================================
    # GRUPO 2: Marcadores específicos (prioridade média)
    # ============================================
    ("caixa", (
        "caixa economica federal",
        "cef",
        "caixa econômica federal",
        "caixa s.a.",
    )),
    ("bb", (
        "banco do brasil",
        "bb s.a.",
        "banco brasil",
    )),
    
    # ============================================
    # GRUPO 3: Marcadores moderados (prioridade baixa-média)
    # ============================================
    ("picpay", (
        "picpay",
        "pic pay",
        "picpay s.a.",
        "picpay serviços",
    )),
    
    # ============================================
    # GRUPO 4: Marcadores genéricos (SEMPRE por último!)
    # Estes podem aparecer como contraparte em outros extratos
    # ============================================
    ("santander", (
        "santander",
        "banco santander",
        "santander s.a.",
    )),
    ("bradesco", (
        "bradesco",
        "banco bradesco",
        "bradesco s.a.",
    )),
    ("itau", (
        "itau unibanco",
        "itau",
        "banco itau",
        "itau s.a.",
    )),
]

_DISPLAY_NAMES = {
    "nubank": "Nubank (Nu Pagamentos S.A.)",
    "c6": "C6 Bank",
    "inter": "Banco Inter",
    "caixa": "Caixa Econômica Federal",
    "bb": "Banco do Brasil",
    "picpay": "PicPay",
    "santander": "Santander",
    "bradesco": "Bradesco",
    "itau": "Itaú Unibanco",
    "generic": "Instituição não identificada",
}


def _normalize(text: str) -> str:
    """
    Normaliza texto: remove acentos e converte para minúsculas.
    Essencial para comparação case-insensitive e sem acentos.
    """
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in nfkd if unicodedata.category(c) != "Mn").lower()


def detect_bank(text: str) -> str:
    """
    Detecta a instituição financeira a partir do texto extraído do PDF.
    
    Args:
        text: Texto completo do extrato bancário
        
    Returns:
        Chave do banco detectado (ex: "nubank", "itau", "caixa") 
        ou "generic" se nenhum marcador for encontrado.
        
    Exemplo:
        >>> detect_bank("Nu Pagamentos S.A. - CNPJ: 18.236.120")
        'nubank'
        >>> detect_bank("Extrato PicPay - Transação ITAU")
        'picpay'  # PicPay vem antes de Itaú na ordem de verificação
    """
    norm = _normalize(text)
    for bank, markers in _BANK_MARKERS:
        if any(m in norm for m in markers):
            return bank
    return "generic"


def bank_display_name(bank: str) -> str:
    """
    Retorna o nome legível do banco para exibição no relatório.
    
    Args:
        bank: Chave do banco (ex: "nubank", "itau")
        
    Returns:
        Nome completo e formatado do banco
    """
    return _DISPLAY_NAMES.get(bank, bank)