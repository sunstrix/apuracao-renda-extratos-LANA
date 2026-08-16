"""
Conversão de texto bruto (OCR/camada textual) em transações.

Arquitetura:
- detect_bank() (bank_detector) escolhe o parser específico;
- parse_nubank(): layout "dd MMM yyyy" com ruído de OCR, seções
  Total de entradas/saídas e bloco "VALORES EM R$";
- parse_itau/bradesco/santander/caixa/bb(): variações do layout
  "dd/mm descrição valor [C/D]";
- parse_generic(): fallback universal (datas dd/mm[/aaaa]).

Nenhum parser fabrica dados: o casamento do bloco de valores em coluna
só é aplicado quando o número de valores disponíveis é EXATAMENTE igual
ao número de descrições pendentes.
"""
import re
import logging
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import List, Optional

from dateutil import parser as date_parser

logger = logging.getLogger(__name__)


@dataclass
class Transaction:
    date: date
    description: str
    amount: float
    is_credit: Optional[bool] = None
    bank: str = ""
    source_file: str = ""


# ---------------------------------------------------------------------------
# Constantes compartilhadas
# ---------------------------------------------------------------------------
MESES_PT = {
    "JAN": 1, "FEV": 2, "MAR": 3, "ABR": 4, "MAI": 5, "JUN": 6,
    "JUL": 7, "AGO": 8, "SET": 9, "OUT": 10, "NOV": 11, "DEZ": 12,
}

DATE_FULL_REGEX = r'\b(\d{1,2}[\/\.\-]\d{1,2}[\/\.\-]\d{2,4})\b'
DATE_SHORT_REGEX = r'^(\d{1,2}[\/\.\-]\d{1,2})\b'
MONTH_HEADER_REGEX = r'^(0[1-9]|1[0-2])[\/\.\-](20\d{2})$'
MONEY_REGEX = r'(?:R\$\s*)?([-+]?(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2})\b'
MONEY_END_REGEX = r'([-+]?(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2})\s*$'

SKIP_LINE_PREFIXES = (
    "SALDO", "EXTRATO", "PERIODO", "PERÍODO", "PAGINA", "PÁGINA",
    "BANCO", "AGENCIA", "AGÊNCIA", "CONTA", "CPF", "CNPJ",
    "DATA", "HISTORICO", "HISTÓRICO", "LANCAMENTO", "LANÇAMENTO",
    "MOVIMENTACAO", "MOVIMENTAÇÃO", "CLIENTE", "ENDERECO", "ENDEREÇO",
    "VALORES EM R$",
)

# --- Nubank ---------------------------------------------------------------
NU_TX_STARTERS = (
    "Transferência", "Transferencia", "Compra", "Pagamento", "Depósito",
    "Deposito", "Resgate", "Estorno", "Reembolso", "Débito", "Debito", "Pix",
)
NU_CONT_HINTS = (
    "agência", "agencia", "conta:", "cnpj", "cpf", "pagamentos -", "- nu",
    "unibanco", "santander", "bradesco", "pagseguro", "mercado", "stone",
    "adyen", "ebanx", "asaas", "cloudwalk", "neon", "caixa", "bco",
    "itaú", "itau", "cora", "btg", "amazonia", "efí", "efi",
)
NU_DATE_HDR_RE = re.compile(r'(\d{1,3})\s*([A-Za-z]{3,9})\.?\s*Z?\s*(\d{4})')
NU_SUMMARY_PREFIXES = (
    "saldo inicial", "rendimento", "total de entradas", "total de saídas",
    "total de saidas", "saldo final",
)


def _normalize(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in nfkd if unicodedata.category(c) != "Mn").lower()


def _month_from_token(token: str) -> Optional[int]:
    return MESES_PT.get(_normalize(token)[:3].upper())


def parse_money_value(text: str) -> float:
    """Converte 'R$ 1.234,56' / '-1.234,56' / '1500,00' -> float."""
    if not text:
        return 0.0
    cleaned = text.replace("R$", "").replace(" ", "").strip()
    is_negative = cleaned.startswith("-") or cleaned.endswith("-")
    cleaned = cleaned.replace("-", "").replace("+", "")
    cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        value = float(cleaned)
        return -value if is_negative else value
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Parser genérico (fallback universal)
# ---------------------------------------------------------------------------
def _build_date(date_str: str, is_short: bool, context_year: Optional[int]) -> Optional[date]:
    try:
        if is_short:
            year = context_year or date.today().year
            return date_parser.parse(f"{date_str}/{year}", dayfirst=True).date()
        return date_parser.parse(date_str, dayfirst=True).date()
    except (ValueError, OverflowError):
        return None


def _clean_description(line: str, date_str: str, amount_str: str) -> str:
    desc = line
    if date_str:
        desc = desc.replace(date_str, " ", 1)
    if amount_str:
        desc = desc.replace(amount_str, " ", 1)
    desc = desc.replace("R$", " ")
    return re.sub(r"\s+", " ", desc).strip(" -–|*")


def _infer_credit(line: str, amount_str: str) -> Optional[bool]:
    idx = line.find(amount_str)
    if idx >= 0:
        after = line[idx + len(amount_str):].strip()[:1].upper()
        if after in ("-", "D"):
            return False
        if after in ("+", "C"):
            return True
    low = _normalize(line)
    if any(w in low for w in ("credito", "recebido", "recebida", "entrada", "deposito", "salário", "salario")):
        return True
    if any(w in low for w in ("debito", "enviada", "enviado", "saida", "pagamento efetuado")):
        return False
    return None


def _parse_generic_lines(text: str, bank: str, source_file: str,
                         use_suffix: bool = False) -> List[Transaction]:
    """Layout clássico: data dd/mm[/aa[aa]] + descrição + valor na mesma linha
    (ou valor nas até 3 linhas seguintes)."""
    transactions: List[Transaction] = []
    lines = [ln.strip() for ln in (text or "").splitlines()]
    context_year: Optional[int] = None
    i, n = 0, len(lines)

    while i < n:
        line = lines[i]
        if not line:
            i += 1
            continue

        header = re.match(MONTH_HEADER_REGEX, line)
        if header:
            context_year = int(header.group(2))
            i += 1
            continue

        if line.upper().startswith(SKIP_LINE_PREFIXES):
            i += 1
            continue

        date_str: Optional[str] = None
        is_short = False
        full_dates = re.findall(DATE_FULL_REGEX, line)
        if full_dates:
            if len(full_dates) > 1:  # linha de período, não lançamento
                i += 1
                continue
            date_str = full_dates[0]
        else:
            short = re.match(DATE_SHORT_REGEX, line)
            if short:
                date_str, is_short = short.group(1), True

        if not date_str:
            i += 1
            continue

        moneys = re.findall(MONEY_REGEX, line)
        consumed_until = i
        if not moneys:
            j = i + 1
            while j < min(i + 4, n):
                nxt = lines[j]
                if nxt and re.findall(MONEY_REGEX, nxt):
                    moneys = re.findall(MONEY_REGEX, nxt)
                    consumed_until = j
                    break
                if nxt and (re.findall(DATE_FULL_REGEX, nxt) or re.match(DATE_SHORT_REGEX, nxt)):
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

        description = _clean_description(line, date_str,
                                         amount_str if consumed_until == i else "")
        if consumed_until > i:
            extra = [ln for ln in lines[i + 1:consumed_until] if ln]
            if extra:
                description = (description + " " + " ".join(extra)).strip()

        is_credit = _infer_credit(line, amount_str)
        if use_suffix:
            idx = line.find(amount_str)
            suffix = line[idx + len(amount_str):].strip()[:1].upper()
            if suffix in ("C", "D"):
                is_credit = suffix == "C"

        transactions.append(Transaction(
            date=parsed_date,
            description=description or "Lançamento",
            amount=parse_money_value(amount_str),
            is_credit=is_credit,
            bank=bank,
            source_file=source_file,
        ))
        i = consumed_until + 1

    return transactions


def parse_generic(text: str, bank: str = "generic", source_file: str = "") -> List[Transaction]:
    return _parse_generic_lines(text, bank, source_file)


# ---------------------------------------------------------------------------
# Parsers específicos por banco
# ---------------------------------------------------------------------------
def _nu_fix_ocr(line: str) -> str:
    """Corrige ruído típico de OCR em tokens de data: O->0 adjacente a dígitos."""
    line = re.sub(r'(?<=\d)O(?=\d)', '0', line)
    return re.sub(r'O(?=\d)', '0', line)


def _nu_date_from_line(line: str) -> Optional[date]:
    m = NU_DATE_HDR_RE.search(_nu_fix_ocr(line))
    if not m:
        return None
    digits, mon, year = m.groups()
    day = None
    for cand in (digits, digits[:2], digits[-2:]):
        if cand.isdigit() and 1 <= int(cand) <= 31:
            day = int(cand)
            break
    if day is None:
        return None
    month = _month_from_token(mon)
    if month is None:
        return None
    try:
        return date(int(year), month, day)
    except ValueError:
        return None


def parse_nubank(text: str, bank: str = "nubank", source_file: str = "") -> List[Transaction]:
    """
    Layout Nubank (OCR): cabeçalhos '01 ABR 2026' / 'O1ABR2026' seguidos de
    linhas de lançamento com valor no fim; algumas páginas trazem os valores
    em bloco separado 'VALORES EM R$' (fallback posicional com guarda).
    """
    txs: List[Transaction] = []
    current_date: Optional[date] = None
    section: Optional[str] = None  # "E" entradas | "S" saídas
    last_tx: Optional[Transaction] = None
    pending: List[tuple] = []      # (date, desc, section) sem valor inline
    values_pool: List[str] = []
    summary_labels = 0
    total_lines = 0
    in_values_block = False

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        low = _normalize(line)
        low_ns = low.replace(" ", "")

        if "valores em r$" in low:
            in_values_block = True
            continue

        if in_values_block:
            candidate = line.replace(" ", "")
            if re.fullmatch(r'[-+]?\d{1,3}(?:\.\d{3})*,\d{2}', candidate):
                values_pool.append(candidate)
                continue
            in_values_block = False  # rodapé/página nova

        if low.startswith(NU_SUMMARY_PREFIXES) or low_ns.startswith(NU_SUMMARY_PREFIXES):
            summary_labels += 1

        d = _nu_date_from_line(line)
        if d is not None and "total" in low_ns:
            current_date = d
            total_lines += 1
            if "entradas" in low_ns:
                section = "E"
            elif "saidas" in low_ns:
                section = "S"
            continue
        if d is not None:
            current_date = d

        if line.startswith(NU_TX_STARTERS):
            m = re.search(MONEY_END_REGEX, line)
            if m:
                amount_str = m.group(1)
                desc = _clean_description(line, "", amount_str)
                amount = parse_money_value(amount_str)
                if amount_str.startswith("-"):
                    is_credit, amount = False, -abs(amount)
                elif section == "S":
                    is_credit, amount = False, -abs(amount)
                elif amount_str.startswith("+"):
                    is_credit = True
                elif section == "E":
                    is_credit = True
                else:
                    is_credit = None
                last_tx = Transaction(current_date or date.today(), desc, amount,
                                      is_credit, bank, source_file)
                if current_date is not None:
                    txs.append(last_tx)
                else:
                    pending.append((None, desc, section))  # data ausente: descarta abaixo
            else:
                pending.append((current_date, line, section))
                last_tx = None
            continue

        # Linha de continuação (banco/agência/conta da contraparte)
        if last_tx is not None and d is None and any(h in low for h in NU_CONT_HINTS):
            if len(last_tx.description) < 250:
                last_tx.description = f"{last_tx.description} {line}"
            continue

    # Fallback em coluna: só casa se contagens baterem exatamente.
    if pending:
        skip = summary_labels + total_lines
        available = values_pool[skip:]
        real_pending = [p for p in pending if p[0] is not None]
        if real_pending and len(available) == len(real_pending):
            for (p_date, desc, sec), val in zip(real_pending, available):
                amount = parse_money_value(val)
                if sec == "S":
                    amount = -abs(amount)
                txs.append(Transaction(p_date, desc, amount,
                                       None if sec is None else sec == "E",
                                       bank, source_file))
        else:
            logger.warning(
                "Nubank: bloco 'VALORES EM R$' não casado (%d valores vs %d descrições) "
                "em %s — linhas sem valor inline descartadas por segurança.",
                len(available), len(real_pending), source_file or "PDF",
            )

    txs.sort(key=lambda t: t.date)
    return txs


def parse_itau(text: str, bank: str = "itau", source_file: str = "") -> List[Transaction]:
    """Itaú: 'dd/mm descrição valor C/D'."""
    return _parse_generic_lines(text, bank, source_file, use_suffix=True)


def parse_bradesco(text: str, bank: str = "bradesco", source_file: str = "") -> List[Transaction]:
    """Bradesco: 'dd/mm descrição valor' com C/D eventual."""
    return _parse_generic_lines(text, bank, source_file, use_suffix=True)


def parse_santander(text: str, bank: str = "santander", source_file: str = "") -> List[Transaction]:
    """Santander: layout dd/mm padrão."""
    return _parse_generic_lines(text, bank, source_file)


def parse_caixa(text: str, bank: str = "caixa", source_file: str = "") -> List[Transaction]:
    """Caixa: layout dd/mm/aaaa padrão."""
    return _parse_generic_lines(text, bank, source_file)


def parse_bb(text: str, bank: str = "bb", source_file: str = "") -> List[Transaction]:
    """Banco do Brasil: layout dd/mm padrão."""
    return _parse_generic_lines(text, bank, source_file)


# ---------------------------------------------------------------------------
# Dispatcher + compatibilidade
# ---------------------------------------------------------------------------
_PARSERS = {
    "nubank": parse_nubank,
    "itau": parse_itau,
    "bradesco": parse_bradesco,
    "santander": parse_santander,
    "caixa": parse_caixa,
    "bb": parse_bb,
}


def parse_statement(text: str, bank: str = "generic", source_file: str = "") -> List[Transaction]:
    """Escolhe o parser do banco; se ele não produzir nada, usa o genérico."""
    parser_fn = _PARSERS.get(bank, parse_generic)
    txs = parser_fn(text, bank=bank, source_file=source_file)
    if not txs and parser_fn is not parse_generic:
        logger.info("Parser '%s' vazio para %s — aplicando fallback genérico.",
                    bank, source_file or "PDF")
        txs = parse_generic(text, bank=bank, source_file=source_file)
    return txs


def parse_pdf_pages(pages_text: List[str]) -> List[Transaction]:
    """Compatibilidade retroativa: parse genérico de todas as páginas."""
    all_txs: List[Transaction] = []
    for page_text in pages_text or []:
        if page_text and page_text.strip():
            all_txs.extend(parse_generic(page_text))
    all_txs.sort(key=lambda t: t.date)
    return all_txs