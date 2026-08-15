import re
import logging
from dataclasses import dataclass
from datetime import date
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


# ---------------------------------------------------------------------------
# Padrões de busca (Regex)
# ---------------------------------------------------------------------------

# Data completa: dd/mm/aaaa, dd/mm/aa, dd-mm-aaaa, dd.mm.aa
DATE_FULL_REGEX = r'\b(\d{1,2}[\/\.\-]\d{1,2}[\/\.\-]\d{2,4})\b'

# Data curta (sem ano), ancorada no início da linha: dd/mm ou dd-mm
DATE_SHORT_REGEX = r'^(\d{1,2}[\/\.\-]\d{1,2})\b'

# Cabeçalho de mês/ano isolado (define o ano de contexto): 08/2025
MONTH_HEADER_REGEX = r'^(0[1-9]|1[0-2])[\/\.\-](20\d{2})$'

# Valor monetário BR: com ou sem R$, com ou sem separador de milhar,
# com ou sem sinal: "R$ 1.500,00", "1.500,00", "1500,00", "-250,00"
MONEY_REGEX = r'(?:R\$\s*)?([-+]?(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2})\b'

# Linhas institucionais que nunca são lançamentos financeiros
SKIP_LINE_PREFIXES = (
    "SALDO", "EXTRATO", "PERIODO", "PERÍODO", "PAGINA", "PÁGINA",
    "BANCO", "AGENCIA", "AGÊNCIA", "CONTA", "CPF", "CNPJ",
    "DATA", "HISTORICO", "HISTÓRICO", "LANCAMENTO", "LANÇAMENTO",
    "MOVIMENTACAO", "MOVIMENTAÇÃO", "CLIENTE", "ENDERECO", "ENDEREÇO",
)


def parse_money_value(text: str) -> float:
    """
    Converte string monetária no padrão BR para float.
    Ex: "R$ 1.234,56" -> 1234.56 | "-1.234,56" -> -1234.56
    """
    if not text:
        return 0.0

    cleaned = text.replace("R$", "").replace(" ", "").strip()

    is_negative = cleaned.startswith("-") or cleaned.endswith("-")
    cleaned = cleaned.replace("-", "").replace("+", "")

    # Remove pontos de milhar e converte vírgula em ponto decimal
    cleaned = cleaned.replace(".", "").replace(",", ".")

    try:
        value = float(cleaned)
        return -value if is_negative else value
    except ValueError:
        return 0.0


def infer_credit_debit(line_text: str, amount_str: str) -> Optional[bool]:
    """
    Tenta inferir se a transação é crédito (True) ou débito (False).
    Analisa o caractere logo após o valor (comum em extratos: "1.500,00 C"
    ou "1.500,00 D") e palavras-chave na descrição.
    Retorna None se não for possível determinar.
    """
    idx = line_text.find(amount_str)
    if idx >= 0:
        after = line_text[idx + len(amount_str):].strip()[:1].upper()
        if after in ("-", "D"):
            return False
        if after in ("+", "C"):
            return True

    lower_text = line_text.lower()

    credit_keywords = [
        "crédito", "credito", "entrada", "recebimento", "recebido",
        "depósito", "deposito", "salário", "salario", "pagamento recebido",
        "pix recebido", "transferência recebida",
    ]
    debit_keywords = [
        "débito", "debito", "saída", "saida", "pagamento efetuado",
        "transferência enviada", "pix enviado", "estorno de débito",
    ]

    if any(word in lower_text for word in credit_keywords):
        return True
    if any(word in lower_text for word in debit_keywords):
        return False

    return None


def _build_date(date_str: str, is_short: bool, context_year: Optional[int]) -> Optional[date]:
    """
    Normaliza a string de data para um objeto date.
    Para datas curtas (dd/mm), aplica o ano de contexto do cabeçalho do mês.
    """
    try:
        if is_short:
            year = context_year or date.today().year
            return date_parser.parse(f"{date_str}/{year}", dayfirst=True).date()
        return date_parser.parse(date_str, dayfirst=True).date()
    except (ValueError, OverflowError) as e:
        logger.warning(f"Não foi possível interpretar a data: {date_str} ({e})")
        return None


def _clean_description(line: str, date_str: str, amount_str: str) -> str:
    """Remove a data e o valor da linha, deixando apenas a descrição."""
    desc = line
    if date_str:
        desc = desc.replace(date_str, " ", 1)
    if amount_str:
        desc = desc.replace(amount_str, " ", 1)
    desc = desc.replace("R$", " ")
    desc = re.sub(r"\s+", " ", desc).strip(" -–|*")
    return desc


def parse_text_to_transactions(text: str) -> List[Transaction]:
    """
    Extrai transações de um bloco de texto (uma página de extrato).

    Estratégia robusta:
    1. Detecta cabeçalhos de mês/ano para definir o ano de contexto.
    2. Identifica a data do lançamento (completa ou curta) no início da linha.
    3. Busca o valor na mesma linha ou nas até 3 linhas seguintes
       (cobre layouts em que o pdfplumber quebra as colunas).
    """
    transactions: List[Transaction] = []
    lines = [ln.strip() for ln in (text or "").splitlines()]
    context_year: Optional[int] = None
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]

        if not line:
            i += 1
            continue

        # Cabeçalho isolado de mês/ano define o ano de contexto
        header = re.match(MONTH_HEADER_REGEX, line)
        if header:
            context_year = int(header.group(2))
            i += 1
            continue

        # Ignora linhas institucionais (saldo, agência, cabeçalhos de tabela)
        if line.upper().startswith(SKIP_LINE_PREFIXES):
            i += 1
            continue

        # --- Detecção da data do lançamento ---
        date_str: Optional[str] = None
        is_short = False

        full_dates = re.findall(DATE_FULL_REGEX, line)
        if full_dates:
            if len(full_dates) > 1:
                # Linha de período (ex: "01/08/2025 a 31/08/2025") não é lançamento
                i += 1
                continue
            date_str = full_dates[0]
        else:
            short = re.match(DATE_SHORT_REGEX, line)
            if short:
                date_str = short.group(1)
                is_short = True

        if not date_str:
            i += 1
            continue

        # --- Detecção do valor (mesma linha ou linhas seguintes) ---
        moneys = re.findall(MONEY_REGEX, line)
        consumed_until = i

        if not moneys:
            j = i + 1
            while j < min(i + 4, n):
                nxt = lines[j]
                if not nxt:
                    j += 1
                    continue
                if re.findall(MONEY_REGEX, nxt):
                    moneys = re.findall(MONEY_REGEX, nxt)
                    consumed_until = j
                    break
                # Interrompe se a próxima linha já é outro lançamento
                if re.findall(DATE_FULL_REGEX, nxt) or re.match(DATE_SHORT_REGEX, nxt):
                    break
                j += 1

        if not moneys:
            i += 1
            continue

        amount_str = moneys[-1]

        parsed_date = _build_date(date_str, is_short, context_year)
        if parsed_date is None:
            i += 1
            continue

        # --- Montagem da descrição ---
        description = _clean_description(line, date_str, amount_str if consumed_until == i else "")
        if consumed_until > i:
            extra = [ln for ln in lines[i + 1:consumed_until] if ln]
            if extra:
                description = (description + " " + " ".join(extra)).strip()

        transactions.append(
            Transaction(
                date=parsed_date,
                description=description or "Lançamento",
                amount=parse_money_value(amount_str),
                is_credit=infer_credit_debit(line, amount_str),
            )
        )

        i = consumed_until + 1

    return transactions


def parse_pdf_pages(pages_text: List[str]) -> List[Transaction]:
    """
    Recebe a lista de textos por página e consolida todas as transações
    encontradas, ordenadas cronologicamente.
    """
    all_transactions: List[Transaction] = []

    for page_text in pages_text:
        if page_text and page_text.strip():
            all_transactions.extend(parse_text_to_transactions(page_text))

    all_transactions.sort(key=lambda t: t.date)

    return all_transactions