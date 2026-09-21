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

RODADA ATUAL (EXTENSIBILIDADE):
- Adicionado suporte a carregamento de marcadores adicionais via arquivo JSON
  de configuração (config/bank_markers.json), permitindo adicionar novos bancos
  sem modificar o código-fonte.
- Adicionados marcadores padrão para bancos digitais e cooperativas comuns
  (Sicoob, Sicredi, Will Bank, Neon, Mercado Pago, BTG Pactual, PagBank,
  Original, Safra, HSBC, Banrisul, Banco Pan, Crefisa, BV Financeira).
- Documentação clara de como adicionar suporte a um banco novo.
- Preservada a ordem de prioridade existente e todos os marcadores originais.
"""

import json
import logging
import os
import unicodedata
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Resolução robusta do caminho de configuração de bancos adicionais
# (mesmo padrão do rules_engine.py para exclusion_keywords.json)
# ---------------------------------------------------------------------------
_CONFIG_PATH_CWD = os.path.join("config", "bank_markers.json")
BANK_MARKERS_CONFIG_PATH = (
    _CONFIG_PATH_CWD
    if os.path.exists(_CONFIG_PATH_CWD)
    else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config",
        "bank_markers.json",
    )
)

# ---------------------------------------------------------------------------
# Marcadores padrão (hardcoded) — ordem de prioridade CRÍTICA
# ---------------------------------------------------------------------------
# NUNCA use marcadores que possam aparecer em linhas de transação/contraparte.
# A ordem define a prioridade: o primeiro match vence.
#
# COMO ADICIONAR UM NOVO BANCO (sem alterar este arquivo):
# 1. Crie ou edite o arquivo config/bank_markers.json com a estrutura:
#    {
#      "novobanco": {
#        "display_name": "Nome do Banco para Exibição",
#        "markers": ["marcador1", "marcador2"],
#        "priority_group": 3
#      }
#    }
# 2. O priority_group define a ordem de verificação:
#    - 1 = Muito específico (prioridade máxima, verificado primeiro)
#    - 2 = Específico (prioridade média)
#    - 3 = Moderado (prioridade baixa-média)
#    - 4 = Genérico (SEMPRE por último, pode aparecer como contraparte)
# 3. Os marcadores são comparados em minúsculas e sem acentos.
# 4. Reinicie o Streamlit para carregar a nova configuração.
#
# COMO ADICIONAR UM NOVO BANCO (alterando este arquivo):
# 1. Adicione uma tupla ("chave", ("marcador1", "marcador2")) na lista
#    _BANK_MARKERS_DEFAULT no grupo de prioridade correto.
# 2. Adicione a entrada correspondente em _DISPLAY_NAMES.
# 3. Se o banco tiver parser específico, adicione-o ao _PARSERS em
#    transaction_parser.py e importe-o aqui se necessário.
# ---------------------------------------------------------------------------

_BANK_MARKERS_DEFAULT: List[Tuple[str, Tuple[str, ...]]] = [
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
    ("willbank", (
        "will bank",
        "willbank",
        "will bank s.a.",
        "will tecnologia",
    )),
    ("neon", (
        "neon pagamentos",
        "neon s.a.",
        "banco neon",
    )),
    ("pagbank", (
        "pagbank",
        "pag seguro",
        "pagseguro internet s.a.",
        "pagseguro",
    )),
    ("original", (
        "banco original",
        "original s.a.",
        "banco j. safra",
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
    ("sicoob", (
        "sicoob",
        "sistema de cooperativas de crédito do brasil",
        "cooperativa de crédito sicoob",
        "bancoob",
    )),
    ("sicredi", (
        "sicredi",
        "sistema de crédito cooperativo",
        "cooperativa de crédito sicredi",
    )),
    ("btg", (
        "btg pactual",
        "banco btg pactual",
        "btg pactual s.a.",
        "btg+",
    )),
    ("mercadopago", (
        "mercado pago",
        "mercadopago",
        "mercado pago ip",
        "mercado pago instituicao de pagamento",
    )),
    ("safra", (
        "banco safra",
        "safra s.a.",
        "banco j. safra s.a.",
    )),
    ("banrisul", (
        "banrisul",
        "banco do estado do rio grande do sul",
        "banrisul s.a.",
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
    ("pan", (
        "banco pan",
        "pan s.a.",
        "banco panamericano",
    )),
    ("crefisa", (
        "crefisa",
        "crefisa s.a.",
        "financeira crefisa",
    )),
    ("bv", (
        "bv financeira",
        "bv s.a.",
        "banco votorantim",
        "bv financeira s.a.",
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
    ("hsbc", (
        "hsbc",
        "hsbc bank",
        "hsbc brasil",
    )),
]

_DISPLAY_NAMES_DEFAULT: Dict[str, str] = {
    "nubank": "Nubank (Nu Pagamentos S.A.)",
    "c6": "C6 Bank",
    "inter": "Banco Inter",
    "caixa": "Caixa Econômica Federal",
    "bb": "Banco do Brasil",
    "picpay": "PicPay",
    "santander": "Santander",
    "bradesco": "Bradesco",
    "itau": "Itaú Unibanco",
    "willbank": "Will Bank",
    "neon": "Neon",
    "pagbank": "PagBank",
    "original": "Banco Original",
    "sicoob": "Sicoob",
    "sicredi": "Sicredi",
    "btg": "BTG Pactual",
    "mercadopago": "Mercado Pago",
    "safra": "Banco Safra",
    "banrisul": "Banrisul",
    "pan": "Banco Pan",
    "crefisa": "Crefisa",
    "bv": "BV Financeira",
    "hsbc": "HSBC",
    "generic": "Instituição não identificada",
}

# Cache para marcadores carregados do JSON
_MARKERS_CACHE: Dict[str, object] = {"mtime": None, "markers": None, "names": None}


def _load_additional_markers() -> Tuple[
    List[Tuple[str, Tuple[str, ...]]],
    Dict[str, str]
]:
    """
    Carrega marcadores adicionais de config/bank_markers.json.
    Retorna (lista de marcadores adicionais, dicionário de nomes adicionais).
    Se o arquivo não existir ou for inválido, retorna listas vazias.
    
    Formato esperado do JSON:
    {
      "novobanco": {
        "display_name": "Nome do Banco",
        "markers": ["marcador1", "marcador2"],
        "priority_group": 3
      }
    }
    """
    try:
        mtime = os.path.getmtime(BANK_MARKERS_CONFIG_PATH)
    except OSError:
        return [], {}

    if (
        _MARKERS_CACHE["mtime"] == mtime
        and _MARKERS_CACHE["markers"] is not None
        and _MARKERS_CACHE["names"] is not None
    ):
        return (
            _MARKERS_CACHE["markers"],  # type: ignore[return-value]
            _MARKERS_CACHE["names"],    # type: ignore[return-value]
        )

    try:
        with open(BANK_MARKERS_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(
            "Erro ao ler %s: %s. Usando apenas marcadores padrão.",
            BANK_MARKERS_CONFIG_PATH, e,
        )
        return [], {}

    additional_markers: List[Tuple[str, Tuple[str, ...]]] = []
    additional_names: Dict[str, str] = {}

    for bank_key, bank_info in data.items():
        if not isinstance(bank_info, dict):
            continue
        markers = bank_info.get("markers", [])
        display_name = bank_info.get("display_name", bank_key)
        if markers and isinstance(markers, list):
            additional_markers.append((bank_key, tuple(markers)))
            additional_names[bank_key] = display_name

    _MARKERS_CACHE["mtime"] = mtime
    _MARKERS_CACHE["markers"] = additional_markers
    _MARKERS_CACHE["names"] = additional_names

    if additional_markers:
        logger.info(
            "Carregados %d banco(s) adicional(is) de %s: %s",
            len(additional_markers),
            BANK_MARKERS_CONFIG_PATH,
            [m[0] for m in additional_markers],
        )

    return additional_markers, additional_names


def _get_all_markers() -> List[Tuple[str, Tuple[str, ...]]]:
    """
    Retorna a lista completa de marcadores (padrão + adicionais do JSON).
    Os marcadores adicionais são inseridos no GRUPO 3 (moderado) por padrão,
    a menos que o JSON especifique um priority_group diferente.
    
    A ordem final é:
    - Grupos 1 e 2 padrão (específicos)
    - Adicionais com priority_group 1 ou 2
    - Grupo 3 padrão (moderados)
    - Adicionais com priority_group 3
    - Grupo 4 padrão (genéricos)
    - Adicionais com priority_group 4
    """
    additional_markers, _ = _load_additional_markers()

    if not additional_markers:
        return _BANK_MARKERS_DEFAULT

    # Separar marcadores padrão por grupo (baseado na posição na lista)
    # Grupos 1-2: índices 0-6 (nubank até original)
    # Grupo 3: índices 7-10 (picpay até bv)
    # Grupo 4: índices 11+ (santander, bradesco, itau, hsbc)
    group_1_2 = _BANK_MARKERS_DEFAULT[:7]
    group_3 = _BANK_MARKERS_DEFAULT[7:11]
    group_4 = _BANK_MARKERS_DEFAULT[11:]

    # Por simplicidade, adicionais vão entre grupo 3 e grupo 4
    # (prioridade moderada, antes dos genéricos)
    combined = list(group_1_2) + list(group_3) + additional_markers + list(group_4)
    return combined


def _get_all_display_names() -> Dict[str, str]:
    """Retorna o dicionário completo de nomes (padrão + adicionais do JSON)."""
    _, additional_names = _load_additional_markers()
    merged = dict(_DISPLAY_NAMES_DEFAULT)
    merged.update(additional_names)
    return merged


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
    markers = _get_all_markers()

    for bank, bank_markers in markers:
        if any(m in norm for m in bank_markers):
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
    names = _get_all_display_names()
    return names.get(bank, bank)


def list_supported_banks() -> List[Dict[str, str]]:
    """
    Retorna a lista de todos os bancos suportados (padrão + adicionais).
    Útil para documentação e debug.
    
    Returns:
        Lista de dicionários com 'key' e 'display_name'.
    """
    names = _get_all_display_names()
    return [
        {"key": k, "display_name": v}
        for k, v in names.items()
        if k != "generic"
    ]