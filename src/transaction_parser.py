"""
Conversão de texto bruto (OCR/camada textual) em transações.

Arquitetura:
- detect_bank() (bank_detector) escolhe o parser específico;
- parse_nubank(): layout "dd MMM yyyy" com ruído de OCR, seções
  Total de entradas/saídas, classificação crédito/débito em camadas,
  look-ahead de valor inline (FIX H/J) e ALINHAMENTO POSICIONAL POR
  PÁGINA com o bloco "VALORES EM R$" (FIX N, rodada 5);
- parse_itau/bradesco/santander/caixa/bb(): variações do layout dd/mm;
- parse_generic(): fallback universal.

RODADA 5 (evidência: debug_extracao_*.txt + logs de execução):
- FIX N: o bloco "VALORES EM R$" de cada página espelha, EM ORDEM, as
  linhas "portadoras de valor" da coluna esquerda (totais de seção
  intercalados com lançamentos). O alinhamento agora é feito POR PÁGINA
  (flush quando o bloco termina), com skip = valores excedentes à
  esquerda (resumo/preview) e casamento 1:1 na ordem. A guarda global
  antiga de contagens é mantida APENAS como fallback para documentos
  sem bloco por página.
- FIX O: após o casamento, cada seção é conferida contra o próprio
  total informado pelo banco; residual (OCR que perdeu descrição) vira
  linha ⚠️ needs_review explícita — o somatório do banco é a fonte de
  verdade e nada some em silêncio.

CORREÇÃO DE SINTAXE E VALIDAÇÃO (Rodada Atual):
- Restauração completa da formatação Python (strings corrompidas por
  espaços extras, docstrings quebradas, __name__/__file__ incorretos).
- Validação de integridade: toda transação criada garante descrição
  não vazia e valor numérico coerente, compatível com a extração
  ordenada por coordenadas Y/X do pdf_extractor.py.
"""
import re
import logging
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional

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
    needs_review: bool = False
    manually_confirmed: bool = False  # BUG-1 FIX: Evitar AttributeError em report_generator


# ---------------------------------------------------------------------------
# Constantes compartilhadas
# ---------------------------------------------------------------------------
MESES_PT = {
    "JAN": 1, "FEV": 2, "MAR": 3, "ABR": 4, "MAI": 5, "JUN": 6,
    "JUL": 7, "AGO": 8, "SET": 9, "OUT": 10, "NOV": 11, "DEZ": 12,
}

DATE_FULL_REGEX = r'\b(\d{1,2}[\/\.\-]\d{1,2}[\/\.\-]\d{2,4})\b'
DATE_SHORT_REGEX = r'^(\d{1,2}[\/\.\-]\d{1,2})\b'
MONTH_HEADER_REGEX = r'^(0[1-9]|1[0-2])\/\.\-$'
MONEY_REGEX = r'(?:R\$\s*)?([-+]?(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2})\b'
MONEY_END_REGEX = r'([-+]?(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2})\s*$'
MONEY_ONLY_REGEX = r'[-+]?\d{1,3}(?:\.\d{3})*,\d{2}'

SKIP_LINE_PREFIXES = (
    "SALDO", "EXTRATO", "PERIODO", "PERÍODO", "PAGINA", "PÁGINA",
    "BANCO", "AGENCIA", "AGÊNCIA", "CONTA", "CPF", "CNPJ",
    "DATA", "HISTORICO", "HISTÓRICO", "LANCAMENTO", "LANÇAMENTO",
    "MOVIMENTACAO", "MOVIMENTAÇÃO", "CLIENTE", "ENDERECO", "ENDEREÇO",
    "VALORES EM R$",
)

# --- Nubank -----------------------------------------------------------------
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
NU_SUMMARY_PREFIXES = ("saldo inicial", "rendimento", "saldo final")

NU_CREDIT_HINTS = (
    "transferencia recebida",
    "reembolso recebido",
    "deposito de emprestimo",
    "estorno",
)
NU_DEBIT_HINTS = (
    "compra no debito",
    "transferencia enviada",
    "pagamento de fatura",
    "debito em conta",
    "resgate de emprestimo",
)


def _normalize_text(text: str) -> str:
    """Remove acentos e baixa caixa para análise estatística/semântica."""
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in nfkd if unicodedata.category(c) != "Mn").lower()


def _month_from_token(token: str) -> Optional[int]:
    return MESES_PT.get(_normalize_text(token)[:3].upper())


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


def _semantic_credit_debit(description: str) -> Optional[bool]:
    low = _normalize_text(description)
    if any(h in low for h in NU_CREDIT_HINTS):
        return True
    if any(h in low for h in NU_DEBIT_HINTS):
        return False
    return None


def _decide_credit(amount_str: str, section: Optional[str], description: str):
    """
    Decide (is_credit, amount, needs_review) em camadas:
    1) sinal explícito "+"/"-"; 2) seção (E/S); 3) semântica; 4) revisão.
    """
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
    """Heurística de crédito/débito para o parser genérico (bancos dd/mm)."""
    idx = line.find(amount_str)
    if idx >= 0:
        after = line[idx + len(amount_str):].strip()[:1].upper()
        if after in ("-", "D"):
            return False
        if after in ("+", "C"):
            return True
    low = _normalize_text(line)
    if any(w in low for w in ("credito", "recebido", "recebida", "entrada",
                              "deposito", "salário", "salario")):
        return True
    if any(w in low for w in ("debito", "enviada", "enviado", "saida",
                              "pagamento efetuado")):
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
        desc = desc.replace(date_str, " ", 1)
    if amount_str:
        desc = desc.replace(amount_str, " ", 1)
    desc = desc.replace("R$", " ")
    return re.sub(r"\s+", " ", desc).strip(" -–|*")


def _nu_date_from_line(line: str) -> Optional[date]:
    """Data de cabeçalho Nubank com ruído de OCR (O1ABR2026, 1O0MAR2026...)."""
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
    """True se a linha é cabeçalho (lançamento/data/seção/resumo)."""
    return (
        line.startswith(NU_TX_STARTERS)
        or _nu_date_from_line(line) is not None
        or "totaldeentradas" in low_ns
        or "totaldesaidas" in low_ns
        or low_ns.startswith(NU_SUMMARY_PREFIXES)
    )


# ---------------------------------------------------------------------------
# Parser genérico (fallback universal)
# ---------------------------------------------------------------------------
def _parse_generic_lines(text: str, bank: str, source_file: str,
                         use_suffix: bool = False) -> List[Transaction]:
    """Layout clássico: data dd/mm + descrição + valor na mesma linha
    (ou valor nas até 3 linhas seguintes). FIX B: indeterminado =>
    needs_review=True."""
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

        # Validação de integridade: descrição nunca vazia
        if not description:
            description = "Lançamento não identificado"

        is_credit = _infer_credit(line, amount_str)
        if use_suffix:
            idx = line.find(amount_str)
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
    """
    Parser do extrato Nubank (OCR).

    RODADA 5 — FIX N (alinhamento posicional por página): o bloco
    "VALORES EM R$" de cada página espelha, em ordem, as linhas portadoras
    de valor da coluna esquerda (totais de seção INTERCALADOS com valores
    de lançamentos). O flush do alinhamento ocorre quando o bloco termina
    (linha não-valor após valores) ou no fim do texto — contenção por
    página, sem vazamento entre páginas.

    FIX O: cada seção é conferida contra o total informado pelo banco;
    residual vira linha ⚠️ needs_review (nada some em silêncio).

    Mantidos: look-ahead inline (H/J), fallback global antigo (somente para
    documentos SEM bloco por página), rede FIX K e canário FIX L.
    """
    lines = [ln.strip() for ln in (text or "").splitlines()]
    n = len(lines)
    txs: List[Transaction] = []
    current_date: Optional[date] = None
    section: Optional[str] = None
    last_tx: Optional[Transaction] = None
    summary_labels = 0
    total_lines = 0
    in_values_block = False

    # Fallback global (rodadas anteriores) — só alimenta docs sem bloco/página
    pending: List[dict] = []
    values_pool: List[str] = []

    # --- estado por página (FIX N) ---
    page_slots: List[Dict[str, Any]] = []
    page_values: List[str] = []
    page_had_block = False

    section_records: List[Dict[str, Any]] = []
    section_rec: Optional[Dict[str, Any]] = None

    def _make_tx(val_str: str, sec: Optional[str], desc: str,
                 dte: Optional[date]) -> Transaction:
        is_credit, amount, needs_review = _decide_credit(val_str, sec, desc)
        # Validação de integridade: descrição nunca vazia
        if not desc:
            desc = "Lançamento não identificado"
        tx = Transaction(
            date=dte or date.today(), description=desc, amount=amount,
            is_credit=is_credit, bank=bank, source_file=source_file,
            needs_review=needs_review,
        )
        txs.append(tx)
        return tx

    def _credit_rec(rec: Optional[Dict[str, Any]], amount: float) -> None:
        if rec is not None:
            rec["sum"] += abs(amount)
            rec["count"] += 1

    def _fix_k(item: Dict[str, Any]) -> None:
        desc = item.get("desc") or "Lançamento não identificado"
        txs.append(Transaction(
            date=item["date"] or date.today(), description=desc,
            amount=0.0, is_credit=None, bank=bank,
            source_file=source_file, needs_review=True,
        ))

    def _close_section_residual(rec: Optional[Dict[str, Any]],
                                sec_label: Optional[str],
                                sec_date: Optional[date]) -> None:
        """FIX O: confere a seção contra o total do banco; residual vira ⚠️."""
        if rec is None or rec.get("expected") is None:
            return
        resid = rec["expected"] - rec["sum"]
        if abs(resid) > 0.01:
            logger.warning(
                "Nubank: seção '%s' de %s com residual de %.2f (OCR perdeu "
                "descrição); criando linha de revisão. (%s)",
                "entradas" if sec_label == "E" else "saídas",
                sec_date.strftime("%d/%m/%Y") if sec_date else "??/??/????",
                resid, source_file or "PDF",
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
            # Sem bloco na página: FIX I (seção 1:1 com total inline) e o
            # restante vai para o fallback global antigo.
            run: List[Dict[str, Any]] = []
            last_hdr: Optional[Dict[str, Any]] = None

            def close_run() -> None:
                nonlocal run
                if len(run) == 1 and last_hdr is not None and last_hdr.get("total_str"):
                    it = run[0]
                    tx = _make_tx(last_hdr["total_str"], it["section"],
                                  it["desc"], it["date"])
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
                logger.warning(
                    "Nubank: alinhamento por página impossível (%d valores vs "
                    "%d slots) em %s — aplicando FIX K.",
                    len(vals), len(slots), source_file or "PDF",
                )
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

        # FIX M: low_ns não tem espaços — checagem sem espaço.
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
            _flush_page()  # fim do bloco => fim lógico da página

        if low.startswith(NU_SUMMARY_PREFIXES):
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
                "sum": 0.0,
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

            # FIX H/J: look-ahead de valor inline (até 4 linhas).
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

    # Fallback global antigo (MANTIDO): só relevante para documentos sem
    # bloco por página, onde pending acumulou e values_pool pode casar.
    if pending:
        skip = summary_labels + total_lines
        available = values_pool[skip:]
        if len(available) == len(pending):
            for item, val in zip(pending, available):
                tx = _make_tx(val, item["section"], item["desc"], item["date"])
                _credit_rec(item.get("rec"), tx.amount)
        else:
            logger.warning(
                "Nubank: bloco 'VALORES EM R$' não casado (%d valores vs %d "
                "descrições) em %s — aplicando rede de segurança FIX K.",
                len(available), len(pending), source_file or "PDF",
            )
            for item in pending:
                _fix_k(item)

    # FIX L: canário de somatório por seção (log-only).
    for rec in section_records:
        if rec["expected"] is not None and rec["count"] > 0:
            if abs(rec["sum"] - rec["expected"]) > 0.01:
                logger.warning(
                    "Nubank: seção '%s' de %s inconsistente com o somatório do "
                    "banco: esperado=%.2f apurado=%.2f (%d lançamento(s)) em %s.",
                    "entradas" if rec["label"] == "E" else "saídas",
                    rec["date"].strftime("%d/%m/%Y") if rec["date"] else "??/??/????",
                    rec["expected"], rec["sum"], rec["count"],
                    source_file or "PDF",
                )

    txs.sort(key=lambda t: t.date)
    return txs


# ---------------------------------------------------------------------------
# Parsers específicos dos demais bancos (config sobre o genérico)
# ---------------------------------------------------------------------------
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