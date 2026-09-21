"""
Conversão de texto bruto (OCR/camada textual) em transações.
Arquitetura:
detect_bank() (bank_detector) escolhe o parser específico;
parse_nubank(): layout "dd MMM yyyy" com ruído de OCR, seções
Total de entradas/saídas, classificação crédito/débito em camadas,
look-ahead de valor inline (FIX H/J) e ALINHAMENTO POSICIONAL POR
PÁGINA com o bloco "VALORES EM R$" (FIX N, rodada 5);
parse_itau/bradesco/santander/caixa/bb/picpay: variações do layout dd/mm;
parse_generic(): fallback universal.
RODADA 7 - BLINDAGEM SANTANDER:
- Descolapsamento universal letra/número no OCR
- Gatilho alternativo de tabela (Data+Descrição) para forçar in_movimentacao
- Titular Regex agressiva (suporta nome colado na Agência)
- Bloqueio reforçado contra Renda Fixa e Índices Econômicos no Fallback
"""
import re
import logging
import unicodedata
import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, getcontext, InvalidOperation
from typing import Any, Dict, List, Optional, Set, Tuple
from dateutil import parser as date_parser

# Configuração de precisão para valores monetários (2 casas decimais)
getcontext().prec = 28
getcontext().rounding = 'ROUND_HALF_UP'

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# RESTAURAÇÃO LIMPA DE _normalize_text
# ---------------------------------------------------------------------------
def _normalize_text(text: str) -> str:
    """Remove acentos e baixa caixa para análise estatística/semântica."""
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in nfkd if unicodedata.category(c) != "Mn").lower()

@dataclass
class Transaction:
    date: date
    description: str
    amount: Decimal
    is_credit: Optional[bool] = None
    bank: str = ""
    source_file: str = ""
    needs_review: bool = False
    manually_confirmed: bool = False

# ---------------------------------------------------------------------------
# Constantes compartilhadas
# ---------------------------------------------------------------------------
MESES_PT = {
    "JAN": 1, "FEV": 2, "MAR": 3, "ABR": 4, "MAI": 5, "JUN": 6,
    "JUL": 7, "AGO": 8, "SET": 9, "OUT": 10, "NOV": 11, "DEZ": 12,
}
DATE_FULL_REGEX = r'\b(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})\b'
DATE_SHORT_REGEX = r'^(\d{1,2}[/.-]\d{1,2})\b'
MONTH_HEADER_REGEX = r'^(0[1-9]|1[0-2])/\d{2,4}$'
MONEY_REGEX = r'(?:R\$\s*)?([-+]?(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2})\b'
MONEY_END_REGEX = r'([-+]?(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2})\s*$'
MONEY_ONLY_REGEX = r'[-+]?\d{1,3}(?:\.\d{3})*,\d{2}'
SKIP_LINE_PREFIXES = (
    "SALDO ", "EXTRATO ", "PERIODO ", "PERÍODO ", "PAGINA ", "PÁGINA ",
    "BANCO ", "AGENCIA ", "AGÊNCIA ", "CONTA ", "CPF ", "CNPJ ",
    "DATA ", "HISTORICO ", "HISTÓRICO ", "LANCAMENTO ", "LANÇAMENTO ",
    "MOVIMENTACAO ", "MOVIMENTAÇÃO ", "CLIENTE ", "ENDERECO ", "ENDEREÇO ",
    "VALORES EM R$ ",
)

# --- Nubank -----------------------------------------------------------------
NU_TX_STARTERS = (
    "Transferência ", "Transferencia ", "Compra ", "Pagamento ", "Depósito ",
    "Deposito ", "Resgate ", "Estorno ", "Reembolso ", "Débito ", "Debito ", "Pix ",
)
NU_CONT_HINTS = (
    "agência ", "agencia ", "conta: ", "cnpj ", "cpf ", "pagamentos -", "- nu ",
    "unibanco ", "santander ", "bradesco ", "pagseguro ", "mercado ", "stone ",
    "adyen ", "ebanx ", "asaas ", "cloudwalk ", "neon ", "caixa ", "bco ",
    "itaú ", "itau ", "cora ", "btg ", "amazonia ", "efí ", "efi ",
)
NU_DATE_HDR_RE = re.compile(r'(\d{1,3})\s*([A-Za-z]{3,9}).?\sZ?\s(\d{4})')
NU_SUMMARY_PREFIXES = ("saldo inicial ", "rendimento ", "saldo final ")
NU_CREDIT_HINTS = (
    "transferencia recebida ", "reembolso recebido ", "deposito de emprestimo ",
    "estorno ",
)
NU_DEBIT_HINTS = (
    "compra no debito ", "transferencia enviada ", "pagamento de fatura ",
    "debito em conta ", "resgate de emprestimo ",
)

def _month_from_token(token: str) -> Optional[int]:
    return MESES_PT.get(_normalize_text(token)[:3].upper())

def parse_money_value(text: str) -> Decimal:
    if not text:
        return Decimal('0.00')
    cleaned = text.replace("R$", "").replace("  ", " ").strip()
    is_negative = cleaned.startswith("-") or cleaned.endswith("-")
    cleaned = cleaned.replace("-", "").replace("+", "")
    cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        value = Decimal(cleaned)
        return -value if is_negative else value
    except (InvalidOperation, ValueError):
        return Decimal('0.00')

def _semantic_credit_debit(description: str) -> Optional[bool]:
    low = _normalize_text(description)
    if any(h in low for h in NU_CREDIT_HINTS):
        return True
    if any(h in low for h in NU_DEBIT_HINTS):
        return False
    return None

def _decide_credit(amount_str: str, section: Optional[str], description: str):
    amount = parse_money_value(amount_str)
    if amount_str.startswith("-") or amount_str.startswith("+"):
        return (not amount_str.startswith("-")), amount, False
    if section == "E":
        return True, abs(amount), False
    if section == "S":
        return False, -abs(amount), False
    sem = _semantic_credit_debit(description)
    if sem is True:
        return True, abs(amount), False
    if sem is False:
        return False, -abs(amount), False
    return None, amount, True

def _infer_credit(line: str, amount_str: str) -> Optional[bool]:
    idx = line.find(amount_str)
    if idx >= 0:
        after = line[idx + len(amount_str):].strip()[:1].upper()
        if after in ("-", "D"):
            return False
        if after in ("+", "C"):
            return True
    low = _normalize_text(line)
    if any(w in low for w in ("credito", "recebido", "recebida", "entrada", "deposito", "salário", "salario")):
        return True
    if any(w in low for w in ("debito", "enviada", "enviado", "saida", "pagamento efetuado")):
        return False
    return None

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
        desc = desc.replace(date_str, "", 1)
    if amount_str:
        desc = desc.replace(amount_str, "", 1)
    desc = desc.replace("R$", "")
    return re.sub(r'\s+', ' ', desc).strip(" -–|*")

def _nu_date_from_line(line: str) -> Optional[date]:
    fixed = re.sub(r'(?<=\d)O(?=\d)', '0', line)
    fixed = re.sub(r'O(?=\d)', '0', fixed)
    m = NU_DATE_HDR_RE.search(fixed)
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

def _is_nu_header_line(line: str, low_ns: str) -> bool:
    return (
        line.startswith(NU_TX_STARTERS)
        or _nu_date_from_line(line) is not None
        or "totaldeentradas" in low_ns
        or "totaldesaidas" in low_ns
        or any(low_ns.startswith(p) for p in NU_SUMMARY_PREFIXES)
    )

# ---------------------------------------------------------------------------
# Parser genérico (fallback universal)
# ---------------------------------------------------------------------------
def _parse_generic_lines(text: str, bank: str, source_file: str, use_suffix: bool = False) -> List[Transaction]:
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
            if len(full_dates) > 1:
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
            
        amount_str = moneys[0] 
        
        parsed_date = _build_date(date_str, is_short, context_year)
        if parsed_date is None:
            i += 1
            continue
            
        description = _clean_description(line, date_str, amount_str if consumed_until == i else "")
        if consumed_until > i:
            extra = [ln for ln in lines[i + 1:consumed_until] if ln]
            if extra:
                description = (description + " " + " ".join(extra)).strip()
                
        if not description:
            description = "Lançamento não identificado"
            
        is_credit = _infer_credit(line, amount_str)
        if use_suffix:
            idx = line.find(amount_str)
            if idx >= 0:
                suffix = line[idx + len(amount_str):].strip()[:1].upper()
                if suffix in ("C", "D"):
                    is_credit = suffix == "C"
                    
        needs_review = is_credit is None
        amount = parse_money_value(amount_str)
        if is_credit is True and amount < 0:
            amount = -amount
            
        transactions.append(Transaction(
            date=parsed_date,
            description=description,
            amount=amount,
            is_credit=is_credit,
            bank=bank,
            source_file=source_file,
            needs_review=needs_review,
        ))
        i = consumed_until + 1
        
    return transactions

def parse_generic(text: str, bank: str = "generic", source_file: str = "") -> List[Transaction]:
    return _parse_generic_lines(text, bank, source_file)

# ---------------------------------------------------------------------------
# Parser Nubank
# ---------------------------------------------------------------------------
def parse_nubank(text: str, bank: str = "nubank", source_file: str = "") -> List[Transaction]:
    lines = [ln.strip() for ln in (text or "").splitlines()]
    n = len(lines)
    txs: List[Transaction] = []
    current_date: Optional[date] = None
    section: Optional[str] = None
    last_tx: Optional[Transaction] = None
    summary_labels = 0
    total_lines = 0
    in_values_block = False
    pending: List[dict] = []
    values_pool: List[str] = []
    page_slots: List[Dict[str, Any]] = []
    page_values: List[str] = []
    page_had_block = False
    section_records: List[Dict[str, Any]] = []

    def _make_tx(val_str: str, sec: Optional[str], desc: str, dte: Optional[date]) -> Transaction:
        is_credit, amount, needs_review = _decide_credit(val_str, sec, desc)
        if not desc:
            desc = "Lançamento não identificado"
        tx = Transaction(
            date=dte or date.today(), description=desc, amount=amount,
            is_credit=is_credit, bank=bank, source_file=source_file,
            needs_review=needs_review,
        )
        txs.append(tx)
        return tx

    def _credit_rec(rec: Optional[Dict[str, Any]], amount: Decimal) -> None:
        if rec is not None:
            rec["sum"] += abs(amount)
            rec["count"] += 1

    def _fix_k(item: Dict[str, Any]) -> None:
        desc = item.get("desc") or "Lançamento não identificado"
        txs.append(Transaction(
            date=item["date"] or date.today(), description=desc,
            amount=Decimal('0.00'), is_credit=None, bank=bank,
            source_file=source_file, needs_review=True,
        ))

    def _close_section_residual(rec: Optional[Dict[str, Any]], sec_label: Optional[str], sec_date: Optional[date]) -> None:
        if rec is None or rec.get("expected") is None:
            return
        resid = rec["expected"] - rec["sum"]
        if abs(resid) > Decimal('0.01'):
            logger.warning(
                "Nubank: seção '%s' de %s com residual de %.2f (OCR perdeu descrição); criando linha de revisão.",
                "entradas" if sec_label == "E" else "saídas",
                sec_date.strftime("%d/%m/%Y") if sec_date else "??/??/????",
                resid,
            )
            sign = 1 if sec_label == "E" else -1
            txs.append(Transaction(
                date=sec_date or date.today(),
                description="Residual de seção não recuperado pelo OCR",
                amount=sign * abs(resid),
                is_credit=(sec_label == "E"),
                bank=bank, source_file=source_file, needs_review=True,
            ))
            rec["sum"] += abs(resid)

    def _flush_page() -> None:
        nonlocal page_slots, page_values, page_had_block
        if not page_slots and not page_values:
            page_had_block = False
            return
        if not page_had_block:
            run: List[Dict[str, Any]] = []
            last_hdr: Optional[Dict[str, Any]] = None
            def close_run() -> None:
                nonlocal run
                if len(run) == 1 and last_hdr is not None and last_hdr.get("total_str"):
                    it = run[0]
                    tx = _make_tx(last_hdr["total_str"], it["section"], it["desc"], it["date"])
                    _credit_rec(last_hdr["rec"], tx.amount)
                else:
                    pending.extend(run)
                run = []
            for sl in page_slots:
                if sl["kind"] == "header":
                    close_run()
                    last_hdr = sl
                else:
                    run.append(sl)
            close_run()
        else:
            slots = page_slots
            vals = page_values
            skip = max(0, len(vals) - len(slots))
            paired = vals[skip:]
            if len(paired) >= len(slots) and slots:
                cur_rec: Optional[Dict[str, Any]] = None
                cur_label: Optional[str] = None
                cur_date: Optional[date] = None
                vi = 0
                for sl in slots:
                    if vi >= len(paired):
                        break
                    val = paired[vi]
                    vi += 1
                    if sl["kind"] == "header":
                        _close_section_residual(cur_rec, cur_label, cur_date)
                        cur_rec = sl["rec"]
                        cur_rec["expected"] = abs(parse_money_value(val))
                        cur_label = sl["section"]
                        cur_date = sl["date"]
                    else:
                        tx = _make_tx(val, sl["section"], sl["desc"], sl["date"])
                        _credit_rec(sl["rec"], tx.amount)
                _close_section_residual(cur_rec, cur_label, cur_date)
            else:
                for sl in slots:
                    if sl["kind"] == "tx":
                        _fix_k(sl)
        page_slots = []
        page_values = []
        page_had_block = False

    i = 0
    while i < n:
        line = lines[i]
        i += 1
        if not line:
            continue
        low = _normalize_text(line)
        low_ns = low.replace(" ", "").replace(";", "")
        if "valoresemr$" in low_ns:
            in_values_block = True
            page_had_block = True
            continue
        if in_values_block:
            candidate = line.replace(" ", "")
            if re.fullmatch(MONEY_ONLY_REGEX, candidate):
                page_values.append(candidate)
                values_pool.append(candidate)
                continue
            in_values_block = False
            _flush_page()
        if any(low.startswith(p) for p in NU_SUMMARY_PREFIXES):
            summary_labels += 1
            continue
        is_total_e = "totaldeentradas" in low_ns
        is_total_s = "totaldesaidas" in low_ns
        d = _nu_date_from_line(line)
        if is_total_e or is_total_s:
            total_lines += 1
            if d is not None:
                current_date = d
            section = "E" if is_total_e else "S"
            mt = re.search(MONEY_END_REGEX, line)
            total_str = mt.group(1) if mt else None
            section_rec = {
                "label": section,
                "date": current_date,
                "expected": (abs(parse_money_value(total_str)) if total_str else None),
                "sum": Decimal('0.00'),
                "count": 0,
            }
            section_records.append(section_rec)
            page_slots.append({
                "kind": "header", "rec": section_rec, "section": section,
                "date": current_date, "total_str": total_str,
            })
            continue
        if d is not None:
            current_date = d
        if line.startswith(NU_TX_STARTERS):
            m = re.search(MONEY_END_REGEX, line)
            amount_str: Optional[str] = m.group(1) if m else None
            consumed_idx = -1
            if amount_str is None:
                j = i
                while j < min(i + 4, n):
                    nxt = lines[j].strip()
                    if not nxt:
                        j += 1
                        continue
                    cand = nxt.replace(" ", "")
                    if re.fullmatch(MONEY_ONLY_REGEX, cand):
                        amount_str = cand
                        consumed_idx = j
                        break
                    low_n = _normalize_text(nxt)
                    low_n_ns = low_n.replace(" ", "").replace(";", "")
                    if _is_nu_header_line(nxt, low_n_ns):
                        break
                    if any(h in low_n for h in NU_CONT_HINTS):
                        j += 1
                        continue
                    j += 1
                if consumed_idx >= 0:
                    lines[consumed_idx] = ""
            if amount_str is not None:
                desc = _clean_description(line, "", amount_str)
                tx = _make_tx(amount_str, section, desc, current_date)
                _credit_rec(section_rec, tx.amount)
                last_tx = tx
            else:
                page_slots.append({
                    "kind": "tx", "rec": section_rec, "section": section,
                    "date": current_date, "desc": line,
                })
                last_tx = None
            continue
        if last_tx is not None and d is None and any(h in low for h in NU_CONT_HINTS):
            if len(last_tx.description) < 250:
                last_tx.description = f"{last_tx.description} {line}"
            continue
            
    _flush_page()
    if pending:
        skip = summary_labels + total_lines
        available = values_pool[skip:]
        if len(available) == len(pending):
            for item, val in zip(pending, available):
                tx = _make_tx(val, item["section"], item["desc"], item["date"])
                _credit_rec(item.get("rec"), tx.amount)
        else:
            for item in pending:
                _fix_k(item)
                
    txs.sort(key=lambda t: t.date)
    return txs

# ---------------------------------------------------------------------------
# Parser C6 Bank
# ---------------------------------------------------------------------------
def parse_c6(text: str, bank: str = "c6", source_file: str = "") -> List[Transaction]:
    transactions: List[Transaction] = []
    lines = [ln.strip() for ln in (text or "").splitlines()]
    context_year: Optional[int] = None
    i, n = 0, len(lines)
    month_header_re = re.compile(r'([A-Za-zçãáéíóú]+)\s+(\d{4})')
    balance_line_re = re.compile(r'Saldo do dia\s+\d{1,2}/\d{1,2}/\d{2,4}')
    date_re = re.compile(r'^(\d{1,2}/\d{1,2})')
    money_re = re.compile(r'(-?R\$\s*\d{1,3}(?:\.\d{3})*,\d{2})')
    
    while i < n:
        line = lines[i]
        i += 1
        if not line:
            continue
        month_match = month_header_re.search(line)
        if month_match and '(' in line and ')' in line:
            year_str = month_match.group(2)
            context_year = int(year_str)
            continue
        if balance_line_re.search(line):
            continue
        if line.upper().startswith(('DATA', 'TIPO', 'DESCRIÇÃO', 'VALOR')):
            continue
        date_match = date_re.match(line)
        if not date_match:
            continue
        date_str = date_match.group(1)
        money_matches = money_re.findall(line)
        if not money_matches:
            continue
        amount_str = money_matches[-1]
        is_credit = None
        if 'Entrada' in line:
            is_credit = True
        elif 'Saida' in line or 'Saída' in line:
            is_credit = False
        description = line
        description = re.sub(date_re, '', description)
        description = re.sub(r'\d{1,2}/\d{1,2}', '', description, count=1)
        description = re.sub(r'(Entrada|Saida|Saída)\s*\w*', '', description)
        description = re.sub(money_re, '', description)
        description = description.strip(' -|•')
        if context_year is None:
            context_year = date.today().year
        try:
            day, month = map(int, date_str.split('/'))
            parsed_date = date(context_year, month, day)
        except (ValueError, IndexError):
            continue
        amount = parse_money_value(amount_str)
        if not description:
            description = "Lançamento não identificado"
        needs_review = is_credit is None
        transactions.append(Transaction(
            date=parsed_date,
            description=description,
            amount=amount,
            is_credit=is_credit,
            bank=bank,
            source_file=source_file,
            needs_review=needs_review,
        ))
    return transactions

# ---------------------------------------------------------------------------
# Parser Banco Inter
# ---------------------------------------------------------------------------
def parse_inter(text: str, bank: str = "inter", source_file: str = "") -> List[Transaction]:
    transactions: List[Transaction] = []
    lines = [ln.strip() for ln in (text or "").splitlines()]
    context_year: Optional[int] = None
    i, n = 0, len(lines)
    date_extenso_re = re.compile(r'(\d{1,2})\s+de\s+([A-Za-zçãáéíóú]+)\s+de\s+(\d{4})')
    balance_line_re = re.compile(r'Saldo do dia:\sR$')
    money_re = re.compile(r'(-?R\$\s\d{1,3}(?:\.\d{3})*,\d{2})')
    meses_pt = {
        'janeiro': 1, 'fevereiro': 2, 'março': 3, 'marco': 3,
        'abril': 4, 'maio': 5, 'junho': 6, 'julho': 7,
        'agosto': 8, 'setembro': 9, 'outubro': 10,
        'novembro': 11, 'dezembro': 12
    }
    last_date: Optional[date] = None
    
    while i < n:
        line = lines[i]
        i += 1
        if not line:
            continue
        date_match = date_extenso_re.search(line)
        if date_match:
            day = int(date_match.group(1))
            month_name = date_match.group(2).lower()
            year = int(date_match.group(3))
            month = meses_pt.get(month_name)
            if month is None:
                continue
            context_year = year
            try:
                last_date = date(year, month, day)
            except ValueError:
                pass
            continue
        if balance_line_re.search(line):
            continue
        if line.upper().startswith(('VALOR', 'SALDO POR TRANSAÇÃO', 'CPF/CNPJ', 'PERÍODO')):
            continue
        money_matches = money_re.findall(line)
        if not money_matches:
            continue
        amount_str = money_matches[0]
        description = line.split(amount_str)[0].strip()
        description = description.strip(' -|•')
        is_credit = None
        if amount_str.startswith('-'):
            is_credit = False
            amount = -parse_money_value(amount_str)
        else:
            is_credit = True
            amount = parse_money_value(amount_str)
        if not description:
            description = "Lançamento não identificado"
        parsed_date = last_date
        if parsed_date is None:
            if context_year is None:
                context_year = date.today().year
            parsed_date = date(context_year, 1, 1)
        transactions.append(Transaction(
            date=parsed_date,
            description=description,
            amount=amount,
            is_credit=is_credit,
            bank=bank,
            source_file=source_file,
            needs_review=False,
        ))
    return transactions

# ---------------------------------------------------------------------------
# Parser Itaú (específico)
# ---------------------------------------------------------------------------
def parse_itau(text: str, bank: str = "itau", source_file: str = "") -> List[Transaction]:
    txs = _parse_generic_lines(text, bank, source_file, use_suffix=True)
    if not txs:
        lines = [ln.strip() for ln in (text or "").splitlines()]
        context_year: Optional[int] = None
        for line in lines[:20]:
            year_match = re.search(r'(\d{4})', line)
            if year_match:
                year = int(year_match.group(1))
                if 2020 <= year <= 2030:
                    context_year = year
                    break
        for line in lines:
            if not line or line.upper().startswith(('DATA', 'HISTÓRICO', 'VALOR', 'SALDO')):
                continue
            date_match = re.match(r'(\d{2}/\d{2}/\d{4})', line)
            if not date_match:
                continue
            date_str = date_match.group(1)
            try:
                parsed_date = date_parser.parse(date_str, dayfirst=True).date()
            except ValueError:
                continue
            money_matches = re.findall(r'([-+]?\d{1,3}(?:\.\d{3})*,\d{2})', line)
            if not money_matches:
                continue
            amount_str = money_matches[0] 
            amount = parse_money_value(amount_str)
            is_credit = None
            idx = line.find(amount_str)
            if idx >= 0:
                suffix = line[idx + len(amount_str):].strip()[:1].upper()
                if suffix == 'C':
                    is_credit = True
                elif suffix == 'D':
                    is_credit = False
            description = line[len(date_str):idx].strip() if idx > 0 else "Lançamento não identificado"
            if not description:
                description = "Lançamento não identificado"
            needs_review = is_credit is None
            if is_credit is True and amount < 0:
                amount = -amount
            txs.append(Transaction(
                date=parsed_date,
                description=description,
                amount=amount,
                is_credit=is_credit,
                bank=bank,
                source_file=source_file,
                needs_review=needs_review,
            ))
    return txs

# ---------------------------------------------------------------------------
# Parser Bradesco (específico)
# ---------------------------------------------------------------------------
def parse_bradesco(text: str, bank: str = "bradesco", source_file: str = "") -> List[Transaction]:
    return _parse_generic_lines(text, bank, source_file, use_suffix=True)

# ---------------------------------------------------------------------------
# Parser Santander (específico) - RODADA 7: BLINDAGEM
# ---------------------------------------------------------------------------
def _uncollapse_santander_line(line: str) -> List[str]:
    """Desfaz a colapsação do OCR injetando quebras de linha."""
    if not line:
        return []
        
    if "<table>" not in line.lower() and len(line) < 150:
        return [line]

    text = re.sub(r'</?table>', '', line, flags=re.IGNORECASE)
    text = text.replace('<br/>', ' ')
    text = re.sub(r'Data\s+Descrição\s+Nº Documento\s+Movimento \(R\$\)\s+Saldo \(R\$\)', ' ', text, flags=re.IGNORECASE)

    # 1. Desgrudar dinheiro de data seguinte: "10,8103/11" -> "10,81 \n 03/11"
    text = re.sub(r'(,\d{2}-?)(\d{2}/\d{2})', r'\1\n\2 ', text)

    # 2. Desgrudar dinheiro de letra genérica: "300,78PIX" -> "300,78 \n PIX"
    text = re.sub(r'(,\d{2}-?)([A-Za-z])', r'\1\n\2', text)

    # 3. Desgrudar Letra de dinheiro: "SILVEIRA300,78" -> "SILVEIRA 300,78"
    text = re.sub(r'([a-zA-Z])([-+]?\d{1,3}(?:\.\d{3})*,\d{2})', r'\1 \2', text)

    # 4. Adicionar espaço se a Data estiver colada num texto: "03/11PIX" -> "03/11 PIX"
    text = re.sub(r'(\d{2}/\d{2})(?=[^\s\d])', r'\1 ', text)

    # 5. Adicionar espaço se Nome estiver colado em dinheiro negativo: "costa-200,00" -> "costa -200,00"
    text = re.sub(r'([a-zA-Z])([-+]\d{1,3})', r'\1 \2', text)

    # 6. Desgrudar dois dinheiros colados: "-200,00210,81" -> "-200,00 210,81"
    text = re.sub(r'(,\d{2}-?)(?=[-+]?\d)', r'\1 ', text)

    return [ln.strip() for ln in text.split('\n') if ln.strip()]

def extract_santander_holder(text: str) -> Optional[str]:
    """Extrai o titular imune ao caos de espaços e quebras do OCR."""
    # Padrão 1: "Nome", "Cliente" ou "Titular" seguido pelo nome e âncoras finais
    m = re.search(r'(?:Nome|Cliente|Titular)[\s:-]+([A-ZÀ-Ü][A-ZÀ-Ü\s]{5,80}?)(?:\s+(?:Agência|Agencia|Conta|Movimentação|CPF|CNPJ|Endereço|-))', text, re.IGNORECASE)
    if m:
        name = re.sub(r'\s+', ' ', m.group(1)).strip()
        name = re.sub(r'\d+', '', name).strip()
        if name: return name
        
    # Padrão 2: O OCR simplesmente joga o nome em maiúsculas antes de "Agência"
    m2 = re.search(r'([A-ZÀ-Ü][A-ZÀ-Ü\s]{8,60}?)\s+Ag[êe]ncia', text)
    if m2:
        name = re.sub(r'\s+', ' ', m2.group(1)).strip()
        name = re.sub(r'\d+', '', name).strip()
        if name: return name
        
    m3 = re.search(r'Prezad[ao]\s+([A-ZÀ-Ü][a-zà-ü]+(?:\s+[A-ZÀ-Ü][a-zà-ü]+){1,4})', text, re.IGNORECASE)
    if m3: return m3.group(1).strip()
        
    return None

def _parse_santander(text: str, bank: str = "santander", source_file: str = "") -> List[Transaction]:
    transactions: List[Transaction] = []
    lines = [ln.strip() for ln in (text or "").splitlines()]
    
    context_year: Optional[int] = None
    for line in lines[:40]:
        # Busca contexto priorizando Data explícita ou Período para evitar pegar ano do CDB (ex: 2029)
        m = re.search(r'\b(?:data|período|periodo|emissão|emissao|extrato).{0,20}\b(202[0-9])\b', line, re.IGNORECASE)
        if m:
            context_year = int(m.group(1))
            break
            
    if context_year is None:
        for line in lines[:30]:
            # Regex segura que pega ano atrelado a mês/ano para não capturar números aleatórios
            m2 = re.search(r'\b\d{2}/(202[0-9])\b', line)
            if m2:
                context_year = int(m2.group(1))
                break
                
    if context_year is None:
        context_year = date.today().year
        
    # Keywords expandidas
    ignore_keywords_norm = tuple(_normalize_text(k) for k in (
        "renda fixa", "cdb", "rdb", "minhas reservas", "aplicacao n", "aplicação n",
        "compras com cartao", "valor inicial", "saldo anterior", "saldo atual", "valor liquido",
        "rendimento bruto", "valor ir/iof", "pagamento de juros", "% indexador", "data de vencimento",
        "dolar", "euro", "salario minimo", "cdi", "ipca", "inpc", "igpm", "incc", "tr", "poupanca",
        "ibovespa", "selic", "referencia", "fechamento", "valores referencia", "valor aplicado",
        "% sobre", "iof", "irrf", "taxa", "iof retido"
    ))
    
    exit_keywords_norm = tuple(_normalize_text(k) for k in [
        "saldos por periodo", "compras com cartao de debito", "comprovantes de pagamento",
        "renda fixa", "pacote de servicos", "fale conosco", "ouvidoria", "indices economicos",
        "minhas reservas", "resumo de fundos", "poupanca", "salario minimo"
    ])
    
    macro_section = None
    in_movimentacao = False
    current_date = None
    
    i = 0
    while i < len(lines):
        raw_line = lines[i]
        i += 1
        if not raw_line:
            continue
            
        low = _normalize_text(raw_line)
        low_ns = low.replace(" ", "")
        
        # 1. RASTREAMENTO DE MACRO-SEÇÕES
        if "contacorrente" in low_ns or "conta-corrente" in low_ns or "conta corrente" in low:
            macro_section = "CC"
        elif any(kw in low_ns for kw in ("rendafixa", "cdb/rdb", "minhasreservas", "poupanca", "investimento")):
            macro_section = "RF"
        elif any(kw in low_ns for kw in ("indiceseconomicos", "resumodefundos")):
            macro_section = "IE"
            
        # ENTRADA
        if (macro_section == "CC" or macro_section is None) and not in_movimentacao:
            # Gatilho primário "movimentação" ou Gatilho secundário (Tabela) se o OCR corromper o título
            if "movimentacao" in low_ns or ("data" in low and "descri" in low and ("saldo" in low or "valor" in low or "movimento" in low)):
                in_movimentacao = True
                macro_section = "CC"
                logger.info("Santander: ENTRADA na seção Movimentação (linha: %s)", raw_line[:80])
                continue
                
        # SAÍDA
        if in_movimentacao:
            if macro_section in ("RF", "IE") or any(kw in low for kw in exit_keywords_norm):
                in_movimentacao = False
                logger.info("Santander: SAÍDA da seção Movimentação (Macro=%s / linha: %s)", macro_section, raw_line[:80])
                continue
                
        if not in_movimentacao:
            continue
            
        # 2. DESCOLAPSAMENTO DO TEXTO DO OCR
        processed_lines = _uncollapse_santander_line(raw_line)
        
        for line in processed_lines:
            low_p = _normalize_text(line)
            
            if any(kw in low_p for kw in ignore_keywords_norm):
                continue
                
            if low_p.startswith(('data', 'lancamento', 'valor', 'saldo', 'periodo')):
                continue
            if 'saldo' in low_p and ('inicial' in low_p or 'final' in low_p or 'em ' in low_p):
                continue
                
            norm_line = re.sub(r'(?<=\d)O(?=\d)', '0', line)
            norm_line = re.sub(r'(?<=\d)l(?=\d)', '1', norm_line)
            
            # 3. EXTRAÇÃO DE DATA (com memória persistente)
            parsed_date = None
            date_str = ""
            
            date_match = re.search(r'(\d{2}/\d{2}/\d{4})', norm_line)
            if date_match:
                date_str = date_match.group(1)
                try:
                    parsed_date = date_parser.parse(date_str, dayfirst=True).date()
                except ValueError:
                    pass
            else:
                short_match = re.search(r'(\d{2}/\d{2})\b', norm_line)
                if short_match and context_year:
                    date_str = short_match.group(1)
                    try:
                        parsed_date = date_parser.parse(f"{date_str}/{context_year}", dayfirst=True).date()
                    except ValueError:
                        pass

            if parsed_date:
                current_date = parsed_date
            elif current_date:
                parsed_date = current_date
            else:
                continue
                
            # 4. EXTRAÇÃO FINANCEIRA E SINAL
            money_matches = re.findall(r'([-+]?\d{1,3}(?:\.\d{3})*,\d{2}-?)', norm_line)
            if not money_matches:
                continue
                
            amount_str = money_matches[0]
            amount = parse_money_value(amount_str)
            
            is_credit = None
            if amount_str.startswith('+'):
                is_credit = True
                amount = abs(amount)
            elif amount_str.startswith('-') or amount_str.endswith('-'):
                is_credit = False
                amount = -abs(amount)
            else:
                if any(w in low_p for w in ("credito", "recebido", "entrada", "deposito", "salario")):
                    is_credit = True
                    amount = abs(amount)
                elif any(w in low_p for w in ("debito", "enviado", "saida", "pagamento", "resgate")):
                    is_credit = False
                    amount = -abs(amount)
                else:
                    is_credit = amount >= 0
                    
            # 5. EXTRAÇÃO DE DESCRIÇÃO
            desc = norm_line
            if date_str:
                desc = desc.replace(date_str, "", 1)
            for m in money_matches:
                desc = desc.replace(m, "", 1)
                
            description = re.sub(r'\b\d{3,}\b', '', desc)
            description = re.sub(r'\s+', ' ', description).strip(" -–|*")
            
            if not description or len(description) < 3:
                description = "Lançamento não identificado"
                
            transactions.append(Transaction(
                date=parsed_date,
                description=description,
                amount=amount,
                is_credit=is_credit,
                bank=bank,
                source_file=source_file,
                needs_review=(is_credit is None),
            ))
            
    logger.info("Santander: %d transações extraídas de %s", len(transactions), source_file or "PDF")
    return transactions

def parse_santander(text: str, bank: str = "santander", source_file: str = "") -> List[Transaction]:
    return _parse_santander(text, bank, source_file)

# ---------------------------------------------------------------------------
# Parser Caixa Econômica Federal (específico)
# ---------------------------------------------------------------------------
def parse_caixa(text: str, bank: str = "caixa", source_file: str = "") -> List[Transaction]:
    transactions: List[Transaction] = []
    lines = [ln.strip() for ln in (text or "").splitlines()]
    context_year: Optional[int] = None
    current_section: Optional[str] = None
    
    for line in lines[:20]:
        year_match = re.search(r'(\d{4})', line)
        if year_match:
            year = int(year_match.group(1))
            if 2020 <= year <= 2030:
                context_year = year
                break
                
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line:
            continue
        low = line.lower()
        if 'créditos' in low or ('creditos' in low and 'total' in low):
            current_section = "E"
            continue
        if 'débitos' in low or ('debitos' in low and 'total' in low):
            current_section = "S"
            continue
        if line.upper().startswith(('DATA', 'LANÇAMENTO', 'VALOR', 'SALDO', 'PERÍODO')):
            continue
        if 'saldo' in low and 'inicial' in low:
            continue
        if 'saldo' in low and 'final' in low:
            continue
        date_match = re.match(r'(\d{2}/\d{2}/\d{4})', line)
        if not date_match:
            continue
        date_str = date_match.group(1)
        try:
            parsed_date = date_parser.parse(date_str, dayfirst=True).date()
        except ValueError:
            continue
        money_matches = re.findall(r'([-+]?\d{1,3}(?:\.\d{3})*,\d{2})', line)
        if not money_matches:
            continue
        amount_str = money_matches[0]
        amount = parse_money_value(amount_str)
        is_credit = None
        if amount_str.startswith('+'):
            is_credit = True
        elif amount_str.startswith('-'):
            is_credit = False
        elif current_section == "E":
            is_credit = True
        elif current_section == "S":
            is_credit = False
        idx = line.find(amount_str)
        description = line[len(date_str):idx].strip() if idx > 0 else "Lançamento não identificado"
        if not description:
            description = "Lançamento não identificado"
        needs_review = is_credit is None
        if is_credit is True and amount < 0:
            amount = -amount
        transactions.append(Transaction(
            date=parsed_date,
            description=description,
            amount=amount,
            is_credit=is_credit,
            bank=bank,
            source_file=source_file,
            needs_review=needs_review,
        ))
    return transactions

# ---------------------------------------------------------------------------
# Parser PicPay (específico)
# ---------------------------------------------------------------------------
def parse_picpay(text: str, bank: str = "picpay", source_file: str = "") -> List[Transaction]:
    transactions: List[Transaction] = []
    lines = [ln.strip() for ln in (text or "").splitlines()]
    context_year: Optional[int] = None
    
    for line in lines[:20]:
        year_match = re.search(r'(\d{4})', line)
        if year_match:
            year = int(year_match.group(1))
            if 2020 <= year <= 2030:
                context_year = year
                break
                
    credit_keywords = ('recebido', 'recebida', 'entrada', 'deposito', 'recarga')
    debit_keywords = ('enviado', 'enviada', 'saida', 'pagamento', 'compra', 'transferencia enviada')
    
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line:
            continue
        if line.upper().startswith(('DATA', 'DESCRIÇÃO', 'VALOR', 'PERÍODO')):
            continue
        if 'picpay' in line.lower() and 'serviços' in line.lower():
            continue
        date_match = re.match(r'(\d{2}/\d{2}/\d{4})', line)
        if not date_match:
            continue
        date_str = date_match.group(1)
        try:
            parsed_date = date_parser.parse(date_str, dayfirst=True).date()
        except ValueError:
            continue
        money_matches = re.findall(r'([-+]?\d{1,3}(?:\.\d{3})*,\d{2})', line)
        if not money_matches:
            continue
        amount_str = money_matches[0]
        amount = parse_money_value(amount_str)
        is_credit = None
        if amount_str.startswith('+'):
            is_credit = True
        elif amount_str.startswith('-'):
            is_credit = False
        else:
            low = line.lower()
            if any(kw in low for kw in credit_keywords):
                is_credit = True
            elif any(kw in low for kw in debit_keywords):
                is_credit = False
        idx = line.find(amount_str)
        description = line[len(date_str):idx].strip() if idx > 0 else "Lançamento não identificado"
        if not description:
            description = "Lançamento não identificado"
        needs_review = is_credit is None
        if is_credit is True and amount < 0:
            amount = -amount
        transactions.append(Transaction(
            date=parsed_date,
            description=description,
            amount=amount,
            is_credit=is_credit,
            bank=bank,
            source_file=source_file,
            needs_review=needs_review,
        ))
    return transactions

# ---------------------------------------------------------------------------
# Parser Banco do Brasil (específico)
# ---------------------------------------------------------------------------
def parse_bb(text: str, bank: str = "bb", source_file: str = "") -> List[Transaction]:
    return _parse_generic_lines(text, bank, source_file, use_suffix=True)

# ---------------------------------------------------------------------------
# Dispatcher + compatibilidade
# ---------------------------------------------------------------------------
_PARSERS = {
    "nubank": parse_nubank,
    "c6": parse_c6,
    "inter": parse_inter,
    "itau": parse_itau,
    "bradesco": parse_bradesco,
    "santander": parse_santander,
    "caixa": parse_caixa,
    "bb": parse_bb,
    "picpay": parse_picpay,
}

def parse_statement(text: str, bank: str = "generic", source_file: str = "") -> List[Transaction]:
    parser_fn = _PARSERS.get(bank, parse_generic)
    txs = parser_fn(text, bank=bank, source_file=source_file)
    if not txs and parser_fn is not parse_generic:
        logger.info("Parser '%s' vazio para %s — aplicando fallback genérico.", bank, source_file or "PDF")
        txs = parse_generic(text, bank=bank, source_file=source_file)
    return txs

def parse_statement_with_holder(text: str, bank: str = "generic", source_file: str = "") -> Tuple[List[Transaction], Optional[str]]:
    txs = parse_statement(text, bank=bank, source_file=source_file)
    holder = None
    if bank == "santander":
        holder = extract_santander_holder(text)
    return txs, holder

def parse_pdf_pages(pages_text: List[str]) -> List[Transaction]:
    all_txs: List[Transaction] = []
    for page_text in pages_text or []:
        if page_text and page_text.strip():
            all_txs.extend(parse_generic(page_text))
    all_txs.sort(key=lambda t: t.date)
    return all_txs

# ---------------------------------------------------------------------------
# Deduplicação de transações (Rodada Deduplicação)
# ---------------------------------------------------------------------------
def _transaction_hash(tx: Transaction) -> str:
    norm_desc = _normalize_text(tx.description)
    norm_desc = re.sub(r'\s+', ' ', norm_desc).strip()
    amount_str = str(tx.amount.quantize(Decimal('0.01')))
    date_str = tx.date.isoformat()
    hash_input = f"{date_str}|{amount_str}|{norm_desc}"
    return hashlib.sha256(hash_input.encode('utf-8')).hexdigest()

def deduplicate_transactions(transactions: List[Transaction]) -> Tuple[List[Transaction], int]:
    seen_hashes: Set[str] = set()
    unique_transactions: List[Transaction] = []
    duplicates_count = 0
    
    for tx in transactions:
        tx_hash = _transaction_hash(tx)
        if tx_hash not in seen_hashes:
            seen_hashes.add(tx_hash)
            unique_transactions.append(tx)
        else:
            duplicates_count += 1
            logger.debug("Transação duplicada removida: %s | %s | %s (%s)", tx.date, tx.description, tx.amount, tx.source_file or "PDF")
            
    if duplicates_count > 0:
        logger.info("Deduplicação: %d transação(ões) duplicada(s) removida(s) de %d total.", duplicates_count, len(transactions))
        
    unique_transactions.sort(key=lambda t: t.date)
    return unique_transactions, duplicates_count