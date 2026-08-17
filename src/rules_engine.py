"""
Motor de regras de negócio da apuração de renda.

Prioridade de avaliação (da mais alta para a mais baixa):
1. Exclusão manual pelo operador (tela de revisão) — motivo propagado;
2. Mesma titularidade detectada pelo NOME do titular na contraparte;
3. Lançamento de débito (valor negativo);
4. Regras automáticas por palavras-chave (config/exclusion_keywords.json).

Identificador de exclusão manual: índice da transação na lista bruta
(mesmo índice exibido/editado no st.data_editor da tela de revisão),
com o motivo digitado pelo operador como valor (Dict[int, str]).

Compatibilidade retroativa: todos os parâmetros novos são opcionais;
chamadas antigas evaluate_transaction(tx) continuam idênticas.

RODADA 2:
- FIX D: CONFIG_PATH agora resolve relativo ao projeto (fallback para o CWD
  inexistente), eliminando o fallback SILENCIOSO para DEFAULT_KEYWORDS quando
  o app é executado fora da raiz do repositório;
- FIX E: canário de inconsistência — crédito com valor negativo gera
  logger.error (regressão de parser) sem alterar o fluxo de decisão.
"""
import json
import os
import re
import unicodedata
import logging
from typing import Dict, List, Optional, Tuple

from src.transaction_parser import Transaction

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FIX D (rodada 2): resolução robusta do caminho de configuração.
# ---------------------------------------------------------------------------
# Prefere o caminho relativo ao CWD (comportamento histórico, preserva testes
# e execuções que dependam dele); se não existir, resolve relativo ao
# diretório do projeto (pai de src/), garantindo que o JSON seja encontrado
# mesmo quando o app roda fora da raiz (IDEs, serviços, Streamlit Cloud com
# CWD diferente, pytest a partir de outro diretório).
_CONFIG_PATH_CWD = os.path.join("config", "exclusion_keywords.json")
CONFIG_PATH = (
    _CONFIG_PATH_CWD
    if os.path.exists(_CONFIG_PATH_CWD)
    else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config",
        "exclusion_keywords.json",
    )
)

DEFAULT_KEYWORDS: Dict[str, List[str]] = {
    "same_ownership": [
        "mesma titularidade",
        "transferencia entre contas",
        "conta propria",
        "transferencia propria",
        "auto transferencia",
        "transferencia interna",
        "movimentacao interna",
    ],
    "investments": [
        "cdb", "rendimento", "aplicacao", "resgate", "fundo", "corretora",
        "investimento", "tesouro", "lci", "lca", "dividendos",
        "juros capital proprio", "rendimento conta corrente",
        "rendimento poupanca", "aplicacao financeira", "resgate financeiro",
        "aporte investimento", "renda variavel", "acoes", "fii",
        "criptomoeda", "binance", "mercado pago rendimento",
        "picpay rendimento",
    ],
    "gambling": [
        "bet", "aposta", "jogo", "loteria", "premio", "pix bet", "cassino",
        "sportsbook", "blaze", "estrela bet", "kto", "superbet", "novabet",
        "aposta esportiva", "jogo do tigrinho", "fortune tiger",
        "cassino online", "plataforma de jogo", "pix premio", "pix sorte",
    ],
}

# Cache simples do JSON de palavras-chave, invalidado por mtime do arquivo.
_KEYWORDS_CACHE: Dict[str, object] = {"mtime": None, "data": None}

def load_exclusion_keywords() -> Dict[str, List[str]]:
    """
    Carrega as palavras-chave de exclusão do arquivo JSON.
    Retorna o dicionário padrão se o arquivo não existir ou for inválido.
    """
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        return DEFAULT_KEYWORDS

    if _KEYWORDS_CACHE["mtime"] == mtime and _KEYWORDS_CACHE["data"] is not None:
        return _KEYWORDS_CACHE["data"]  # type: ignore[return-value]

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = {
            "same_ownership": data.get("same_ownership", DEFAULT_KEYWORDS["same_ownership"]),
            "investments": data.get("investments", DEFAULT_KEYWORDS["investments"]),
            "gambling": data.get("gambling", DEFAULT_KEYWORDS["gambling"]),
        }
        _KEYWORDS_CACHE["mtime"] = mtime
        _KEYWORDS_CACHE["data"] = merged
        return merged
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Erro ao ler arquivo de palavras-chave: %s. Usando fallback.", e)
        return DEFAULT_KEYWORDS

def normalize_text(text: str) -> str:
    """Remove acentos e baixa caixa para comparação semântica."""
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in nfkd if unicodedata.category(c) != "Mn").lower().strip()

def _holder_tokens(name: str) -> List[str]:
    """
    Extrai tokens significativos (3+ caracteres) do nome do titular.
    Ex.: "Davi Herculano e Silva" -> ["davi", "herculano", "silva"].
    """
    return [t for t in re.split(r"[^a-z0-9]+", normalize_text(name)) if len(t) >= 3]

def _holder_matches(holder_name: str, description: str) -> bool:
    """
    Verifica se o nome do titular aparece na descrição da contraparte.
    Critério anti-falso-positivo (exigência da TAREFA 2):
    - nome completo normalizado presente na descrição, OU
    - pelo menos 2 tokens de 3+ letras do nome presentes como tokens
      da descrição (casamento por token, não por substring livre).
    Isso exclui "DAVI HERCULANO E SILVA" (3 acertos) mas NÃO exclui
    "ELIANE HERCULANO DE ANDRADE" (apenas 1 acerto: "herculano").
    """
    norm_holder = normalize_text(holder_name)
    norm_desc = normalize_text(description)
    if not norm_holder or not norm_desc:
        return False
    if norm_holder in norm_desc:
        return True
    holder_words = _holder_tokens(holder_name)
    if len(holder_words) < 2:
        return False
    desc_tokens = set(re.split(r"[^a-z0-9]+", norm_desc))
    hits = sum(1 for w in holder_words if w in desc_tokens)
    return hits >= 2

def evaluate_transaction(
    transaction: Transaction,
    holder_name: Optional[str] = None,
    manual_exclusions: Optional[Dict[int, str]] = None,
    transaction_index: Optional[int] = None,
) -> Tuple[bool, str]:
    """
    Avalia uma transação contra as regras de negócio.

    Args:
        transaction: Transação parseada.
        holder_name: Nome do titular (habilita a regra de mesma titularidade
            por contraparte). Opcional.
        manual_exclusions: Dict {índice da transação na lista bruta: motivo}
            preenchido pela tela de revisão. Tem PRIORIDADE máxima. Opcional.
        transaction_index: Posição desta transação na lista bruta, para
            consulta em manual_exclusions. Opcional.

    Returns:
        (is_excluded: bool, reason: str)
    """
    # 1) Exclusão manual do operador — prioridade máxima, motivo propagado.
    if (
        manual_exclusions
        and transaction_index is not None
        and transaction_index in manual_exclusions
    ):
        motivo = (manual_exclusions[transaction_index] or "").strip()
        if motivo:
            return True, f"Excluída manualmente pelo usuário ({motivo})"
        return True, "Excluída manualmente pelo usuário"

    # 2) Mesma titularidade pelo nome do titular na contraparte.
    if holder_name and _holder_matches(holder_name, transaction.description):
        return True, (
            "Transferência de mesma titularidade "
            "(nome do titular identificado na contraparte)"
        )

    # FIX E (rodada 2): CANÁRIO de inconsistência de sinal.
    # Contrato do sistema (após rodadas B/B2): is_credit=True => amount > 0.
    # Se esta condição disparar, algum parser REGREDIU e está entregando
    # crédito negativo — registra erro para diagnóstico imediato, SEM alterar
    # o fluxo de decisão (a regra 3 abaixo continua tratando o sinal).
    if transaction.is_credit is True and transaction.amount < 0:
        logger.error(
            "INCONSISTÊNCIA DE SINAL: transação marcada como CRÉDITO com valor "
            "negativo (%s | %s | %.2f | %s). Regressão de parser suspeita — "
            "verifique o parser do banco correspondente.",
            transaction.date,
            transaction.description,
            transaction.amount,
            transaction.source_file or "PDF",
        )

    # 3) Débitos nunca são renda.
    if transaction.amount < 0:
        return True, "Lançamento de débito (não é entrada de renda)"

    # 4) Regras automáticas por palavras-chave.
    keywords = load_exclusion_keywords()
    norm = normalize_text(transaction.description)
    for word in keywords.get("same_ownership", []):
        if normalize_text(word) in norm:
            return True, "Transferência de mesma titularidade"
    for word in keywords.get("investments", []):
        if normalize_text(word) in norm:
            return True, "Resgate/Rendimento de aplicação financeira"
    for word in keywords.get("gambling", []):
        if normalize_text(word) in norm:
            return True, "Crédito de aposta/jogo de azar"

    return False, "Entrada válida de renda"