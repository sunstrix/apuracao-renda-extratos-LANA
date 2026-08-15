import re
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Optional
from dateutil import parser as date_parser

logger = logging.getLogger(__name__)


@dataclass
class Transaction:
    """
    Representa uma transação bancária extraída do extrato.
    """
    date: date
    description: str
    amount: float
    is_credit: Optional[bool] = None  # None = não identificado explicitamente


def parse_money_value(text: str) -> float:
    """
    Converte string monetária no padrão BR para float.
    Ex: "R$ 1.234,56" -> 1234.56, "-1.234,56" -> -1234.56
    """
    if not text:
        return 0.0
    
    # Remove "R$", espaços e símbolos comuns
    cleaned = text.replace("R$", "").replace(" ", "").strip()
    
    # Identifica sinal negativo se houver
    is_negative = cleaned.startswith("-")
    cleaned = cleaned.replace("-", "").replace("+", "")
    
    # Converte formato BR (1.234,56) para float
    # Remove pontos de milhar e substitui vírgula por ponto decimal
    cleaned = cleaned.replace(".", "").replace(",", ".")
    
    try:
        value = float(cleaned)
        return -value if is_negative else value
    except ValueError:
        return 0.0


def infer_credit_debit(line_text: str, amount_str: str) -> Optional[bool]:
    """
    Tenta inferir se a transação é crédito (True) ou débito (False)
    com base em palavras-chave ou sinais.
    Retorna None se não for possível determinar.
    """
    lower_text = line_text.lower()
    
    # Sinais explícitos
    if amount_str.strip().startswith("+"):
        return True
    if amount_str.strip().startswith("-"):
        return False
    
    # Palavras-chave comuns
    credit_keywords = ["crédito", "credito", "entrada", "recebimento", "depósito", "deposito", "salário", "salario", "pagamento recebido"]
    debit_keywords = ["débito", "debito", "saída", "saida", "pagamento efetuado", "transferência enviada"]
    
    if any(word in lower_text for word in credit_keywords):
        return True
    if any(word in lower_text for word in debit_keywords):
        return False
        
    return None


def parse_text_to_transactions(text: str) -> List[Transaction]:
    """
    Extrai transações de um bloco de texto (geralmente uma página de extrato).
    Procura por padrões de data seguidos de descrição e valor monetário.
    """
    transactions: List[Transaction] = []
    
    # Regex para data: dd/mm/aaaa ou dd/mm/aa
    # Regex para valor: R$ 1.234,56 ou 1234,56 ou 1.234,56 (com ou sem R$)
    date_regex = r'\b(\d{1,2}/\d{1,2}/\d{2,4})\b'
    money_regex = r'(?:R\$\s*)?([-+]?\d{1,3}(?:\.\d{3})*,\d{2})'
    
    lines = text.split('\n')
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Encontra todas as datas na linha
        date_matches = re.findall(date_regex, line)
        # Encontra todos os valores na linha
        money_matches = re.findall(money_regex, line)
        
        if date_matches and money_matches:
            # Estratégia simples: assume a primeira data e o último valor da linha
            # Isso cobre a maioria dos extratos tabulares convertidos para texto
            date_str = date_matches[0]
            amount_str = money_matches[-1]
            
            # Tenta normalizar a data
            try:
                parsed_date = date_parser.parse(date_str, dayfirst=True).date()
            except Exception:
                logger.warning(f"Não foi possível interpretar a data: {date_str} na linha: {line}")
                continue
            
            # A descrição é o restante da linha removendo a data e o valor
            description = line
            description = description.replace(date_str, "").replace(amount_str, "").replace("R$", "").strip(" -")
            
            amount = parse_money_value(amount_str)
            is_credit = infer_credit_debit(line, amount_str)
            
            transactions.append(
                Transaction(
                    date=parsed_date,
                    description=description,
                    amount=amount,
                    is_credit=is_credit
                )
            )
            
    return transactions


def parse_pdf_pages(pages_text: List[str]) -> List[Transaction]:
    """
    Recebe a lista de textos por página e consolida todas as transações encontradas.
    """
    all_transactions: List[Transaction] = []
    
    for page_text in pages_text:
        if page_text.strip():
            page_transactions = parse_text_to_transactions(page_text)
            all_transactions.extend(page_transactions)
            
    # Ordena por data para facilitar a análise temporal
    all_transactions.sort(key=lambda t: t.date)
    
    return all_transactions