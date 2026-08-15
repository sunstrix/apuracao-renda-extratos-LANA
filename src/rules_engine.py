import json
import os
import unicodedata
import logging
from typing import Tuple, Dict, List, Optional

from src.transaction_parser import Transaction

logger = logging.getLogger(__name__)

# Caminho para o arquivo de configuração
CONFIG_PATH = os.path.join("config", "exclusion_keywords.json")

# Fallback caso o arquivo JSON não seja encontrado no ambiente de deploy
DEFAULT_KEYWORDS: Dict[str, List[str]] = {
    "same_ownership": ["mesma titularidade", "conta propria", "transferencia propria"],
    "investments": ["rendimento", "cdb", "resgate", "aplicacao", "fundo"],
    "gambling": ["bet", "aposta", "jogo", "loteria", "cassino", "blaze", "kto"]
}


def load_exclusion_keywords() -> Dict[str, List[str]]:
    """
    Carrega as palavras-chave de exclusão do arquivo JSON.
    Retorna o dicionário padrão se o arquivo não existir ou for inválido.
    """
    if not os.path.exists(CONFIG_PATH):
        logger.warning(f"Arquivo de configuração não encontrado em {CONFIG_PATH}. Usando fallback padrão.")
        return DEFAULT_KEYWORDS

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Garante que as chaves necessárias existam
            return {
                "same_ownership": data.get("same_ownership", DEFAULT_KEYWORDS["same_ownership"]),
                "investments": data.get("investments", DEFAULT_KEYWORDS["investments"]),
                "gambling": data.get("gambling", DEFAULT_KEYWORDS["gambling"])
            }
    except (json.JSONDecodeError, Exception) as e:
        logger.error(f"Erro ao ler arquivo de palavras-chave: {e}. Usando fallback padrão.")
        return DEFAULT_KEYWORDS


def normalize_text(text: str) -> str:
    """
    Normaliza texto: remove acentos e converte para minúsculas.
    Ex: "Crédito Bet" -> "credito bet"
    """
    if not text:
        return ""
    
    # Remove acentos
    nfkd = unicodedata.normalize("NFD", text)
    without_accents = "".join(c for c in nfkd if unicodedata.category(c) != "Mn")
    
    return without_accents.lower().strip()


def evaluate_transaction(transaction: Transaction) -> Tuple[bool, str]:
    """
    Avalia uma transação com base nas regras de negócio.
    
    Retorna:
        (is_excluded: bool, reason: str)
        - is_excluded = True: A transação deve ser EXCLUÍDA da apuração de renda.
        - is_excluded = False: A transação deve ser INCLUÍDA na apuração de renda.
        - reason: Motivo da exclusão ou confirmação de inclusão.
    """
    # Apenas transações com valor positivo (entradas) são avaliadas para renda.
    # Se for um débito (valor negativo), ela é ignorada no cálculo de renda,
    # mas aqui retornamos como "excluída" para fins de auditoria de fluxo,
    # embora o ideal seja o calculator filtrar antes.
    # Vamos focar nas regras de exclusão de CRÉDITOS conforme solicitado.
    
    if transaction.amount < 0:
        # Débitos não são renda. Não precisamos auditá-los como exclusão de renda,
        # mas se o sistema chamar, marcamos como excluído com motivo claro.
        return True, "Lançamento de débito (não é entrada de renda)"

    # Carrega as palavras-chave (cache simples pode ser implementado no futuro)
    keywords = load_exclusion_keywords()
    normalized_description = normalize_text(transaction.description)

    # Regra 1: Transferências de mesma titularidade
    for word in keywords.get("same_ownership", []):
        if normalize_text(word) in normalized_description:
            return True, "Transferência de mesma titularidade"

    # Regra 2: Resgates e rendimentos de aplicações financeiras
    for word in keywords.get("investments", []):
        if normalize_text(word) in normalized_description:
            return True, "Resgate/Rendimento de aplicação financeira"

    # Regra 3: Créditos de plataformas de apostas/jogos
    for word in keywords.get("gambling", []):
        if normalize_text(word) in normalized_description:
            return True, "Crédito de aposta/jogo de azar"

    # Se passou por todas as exclusões, é considerada renda válida
    # (PIX de terceiros, transferências recebidas, demais créditos)
    return False, "Entrada válida de renda"