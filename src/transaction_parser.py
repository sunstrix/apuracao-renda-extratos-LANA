"""
Conversão de texto bruto (OCR/camada textual) em transações.

Arquitetura:
- detect_bank() (bank_detector) escolhe o parser específico;
- parse_nubank(): layout "dd MMM yyyy" com ruído de OCR, seções
  Total de entradas/saídas (com ou sem data na linha), classificação
  crédito/débito em camadas (sinal explícito > seção > semântica >
  revisão manual), fallback posicional para páginas em duas colunas,
  look-ahead de valor em linha independente e recuperação por seção;
- parse_itau/bradesco/santander/caixa/bb(): variações do layout dd/mm;
- parse_generic(): fallback universal.

Fluxo de revisão humana (CCA/CAIXA): transações cujo sinal de
crédito/débito não pôde ser determinado saem com is_credit=None e
needs_review=True, para decisão do operador na tela de revisão (app.py).

FIX B (rodada 2): parser genérico marca needs_review=True quando o sinal
é indeterminado.
FIX H (rodada 4): look-ahead de valor em linha independente.
RODADA 4 (consenso de consultorias DeepSeek/ChatGPT auditado pelo Qwen):
- FIX M: detector do bloco corrigido para "valoresemr$" (o low_ns não tem
  espaços; a checagem antiga "valores em r$" NUNCA casava — values_pool
  nascia morto e todo o fallback de duas colunas era código morto);
- FIX I: seção com exatamente 1 pending + total inline no cabeçalho →
  atribuição EXATA do total ao lançamento (recupera +1.360,00 sem
  adivinhação; total de seção continua sem virar Transaction);
- FIX J: look-ahead avança em linha desconhecida (não interrompe mais);
- FIX K: pending não casado → transação needs_review=True (amount=0.0) —
  FIM do descarte silencioso;
- FIX L: canário de somatório por seção (esperado do cabeçalho vs apurado)
  via logger.warning — o total do próprio banco vira teste de integridade.
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
# Valor "sozinho" na linha (usado no look-ahead FIX H/J e no bloco de valores)
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

# Linhas de resumo (não são lançamento nem cabeçalho de seção)
NU_SUMMARY_PREFIXES = ("saldo inicial", "rendimento", "saldo final")

# Fallback semântico de crédito/débito (aplicado somente quando não há sinal
# explícito "+"/"-" e a seção (Total de entradas/saídas) é desconhecida).
# CRÉDITO verificado ANTES do débito para cobrir casos como
# "Estorno - Compra no débito via Uber" (é entrada, apesar de citar débito).
NU_CREDIT_HINTS = (
    "transferencia recebida",
    "reembolso recebido",
    "deposito de emprestimo",
    "estorno",
)
# DÉBITO. "resgate de emprestimo" confirmado como SAÍDA com o extrato real
# (debug_extracao_b.pdf): o valor compõe o "Total de saídas"
# (ex.: 22,00 + 15,00 + 30,27 + 55,00 = 122,27 em 19/MAR/2026 e
# 15,00 + 15,00 + 9,00 + 584,05 + 18,51 = 641,56 em 20-21/JUL/2026),
# ou seja, é abatimento do empréstimo, não renda.
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
    """
    Fallback semântico baseado em palavras-chave da descrição.
    Retorna True (crédito), False (débito) ou None (indeterminado).
    """
    low = _normalize_text(description)
    if any(h in low for h in NU_CREDIT_HINTS):
        return True
    if any(h in low for h in NU_DEBIT_HINTS):
        return False
    return None

def _decide_credit(amount_str: str, section: Optional[str], description: str):
    """
    Decide (is_credit, amount, needs_review) em camadas:
    1) sinal explícito "+"/"-";
    2) seção rastreada (Total de entradas = E / Total de saídas = S);
    3) fallback semântico por palavras-chave (somente se 1 e 2 ausentes);
    4) indeterminado -> is_credit=None, needs_review=True, valor como veio
       (sem forçar sinal): a decisão final é do operador na tela de revisão.

    BUG-2 FIX: Sinal explícito tem prioridade ABSOLUTA sobre seção.
    """
    amount = parse_money_value(amount_str)

    # PRIORIDADE 1: Sinal explícito tem precedência absoluta
    if amount_str.startswith("-") or amount_str.startswith("+"):
        # Se já tem sinal explícito, respeite-o SEM aplicar regra de seção
        return (not amount_str.startswith("-")), amount, False

    # PRIORIDADE 2: Seção (apenas se NÃO há sinal explícito)
    if section == "E":
        return True, abs(amount), False
    if section == "S":
        return False, -abs(amount), False

    # PRIORIDADE 3: Fallback semântico
    sem = _semantic_credit_debit(description)
    if sem is True:
        return True, abs(amount), False
    if sem is False:
        return False, -abs(amount), False

    # PRIORIDADE 4: Indeterminado - revisão manual
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
    """True se a linha é cabeçalho (lançamento/data/seção/resumo) — usado
    pelo look-ahead do FIX H/J para não casar valor de outro lançamento."""
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
    """Layout clássico: data dd/mm[/aa[aa]] + descrição + valor na mesma linha
    (ou valor nas até 3 linhas seguintes).

    FIX B (rodada 2): quando o sinal de crédito/débito é indeterminado
    (is_credit=None), a transação agora sai com needs_review=True para ser
    exibida como ⚠️ na revisão manual do app.py.
    """
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

        is_credit = _infer_credit(line, amount_str)
        if use_suffix:
            idx = line.find(amount_str)
            suffix = line[idx + len(amount_str):].strip()[:1].upper()
            if suffix in ("C", "D"):
                is_credit = suffix == "C"

        # FIX B (rodada 2): sinal indeterminado => revisão manual obrigatória.
        needs_review = is_credit is None

        amount = parse_money_value(amount_str)
        # FIX B2 (rodada 2): consistência de sinal — um crédito JAMAIS pode
        # ter valor negativo. Não mexemos no sinal de débitos.
        if is_credit is True and amount < 0:
            amount = -amount

        transactions.append(Transaction(
            date=parsed_date,
            description=description or "Lançamento",
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
    Parser do extrato Nubank (OCR): cabeçalhos de data com ruído, seções
    "Total de entradas/saídas" (COM ou SEM data na linha), lançamentos com
    valor inline, look-ahead de valor em linha independente (FIX H/J),
    recuperação EXATA por seção (FIX I), fallback posicional para o bloco
    "VALORES EM R$" (FIX M) e rede de segurança sem descarte (FIX K).

    O valor inline do cabeçalho de seção NUNCA vira Transaction (é o
    somatório da seção — evita double count); ele alimenta o FIX I
    (atribuição exata) e o FIX L (canário de integridade).
    """
    lines = [ln.strip() for ln in (text or "").splitlines()]
    n = len(lines)
    txs: List[Transaction] = []
    current_date: Optional[date] = None
    section: Optional[str] = None
    last_tx: Optional[Transaction] = None
    pending: List[dict] = []
    values_pool: List[str] = []
    summary_labels = 0
    total_lines = 0
    in_values_block = False

    # --- Estado por seção (FIX I / FIX L, rodada 4) -------------------------
    section_total_str: Optional[str] = None
    section_rec: Optional[Dict[str, Any]] = None
    section_pending: List[dict] = []
    section_records: List[Dict[str, Any]] = []

    def _flush_section() -> None:
        """Fecha a seção atual: FIX I (1 pending + total => atribuição exata),
        devolve os demais pendings ao fallback global (pool + FIX K)."""
        nonlocal section_pending, section_total_str, section_rec

        # FIX I: exatamente 1 pending E total do cabeçalho capturado =>
        # atribuição EXATA (total da seção == valor do único lançamento).
        if len(section_pending) == 1 and section_total_str is not None:
            item = section_pending.pop(0)
            is_credit, amount, needs_review = _decide_credit(
                section_total_str, item["section"], item["desc"])
            txs.append(Transaction(
                date=item["date"] or date.today(),
                description=item["desc"],
                amount=amount,
                is_credit=is_credit,
                bank=bank,
                source_file=source_file,
                needs_review=needs_review,
            ))
            if section_rec is not None:
                section_rec["sum"] += abs(amount)
                section_rec["count"] += 1

        # Demais (0 ou 2+) seguem para o fallback global; guarda a referência
        # do registro da seção para o canário FIX L pós-pool.
        for it in section_pending:
            it["rec"] = section_rec
        pending.extend(section_pending)

        section_pending = []
        section_total_str = None
        section_rec = None

    i = 0
    while i < n:
        line = lines[i]
        i += 1
        if not line:
            continue
        low = _normalize_text(line)
        low_ns = low.replace(" ", "").replace(";", "")

        # FIX M (rodada 4): low_ns NÃO tem espaços — a checagem antiga
        # "valores em r$" (com espaço) jamais casava e o values_pool nascia
        # morto. Corrigido para "valoresemr$".
        if "valoresemr$" in low_ns:
            in_values_block = True
            continue

        if in_values_block:
            candidate = line.replace(" ", "")
            if re.fullmatch(MONEY_ONLY_REGEX, candidate):
                values_pool.append(candidate)
                continue
            in_values_block = False

        # Linhas de resumo (saldo inicial/rendimento/saldo final)
        if low.startswith(NU_SUMMARY_PREFIXES):
            summary_labels += 1
            continue

        # Cabeçalho de seção "Total de entradas/saídas", COM ou SEM data.
        is_total_e = "totaldeentradas" in low_ns
        is_total_s = "totaldesaidas" in low_ns
        d = _nu_date_from_line(line)

        if is_total_e or is_total_s:
            _flush_section()  # fecha a seção anterior antes de abrir a nova
            total_lines += 1
            if d is not None:
                current_date = d
            section = "E" if is_total_e else "S"
            # FIX I/L: captura o total inline do cabeçalho SOMENTE para
            # validação/atribuição exata — NUNCA vira Transaction aqui.
            mt = re.search(MONEY_END_REGEX, line)
            section_total_str = mt.group(1) if mt else None
            section_rec = {
                "label": section,
                "date": current_date,
                "expected": (abs(parse_money_value(section_total_str))
                             if section_total_str else None),
                "sum": 0.0,
                "count": 0,
            }
            section_records.append(section_rec)
            continue

        if d is not None:
            current_date = d

        if line.startswith(NU_TX_STARTERS):
            m = re.search(MONEY_END_REGEX, line)
            amount_str: Optional[str] = m.group(1) if m else None
            consumed_idx = -1

            # FIX H/J (rodada 4): sem valor inline, procura o valor nas até 4
            # linhas seguintes, pulando linhas de contraparte/vazias/
            # desconhecidas (FIX J: desconhecida AVANÇA em vez de quebrar) e
            # interrompendo SOMENTE em novo cabeçalho (não casa valor alheio).
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
                        break  # novo cabeçalho: não casar valor alheio
                    if any(h in low_n for h in NU_CONT_HINTS):
                        j += 1  # linha de contraparte: pula e continua
                        continue
                    j += 1  # FIX J: linha desconhecida: avança no limite
                if consumed_idx >= 0:
                    lines[consumed_idx] = ""  # consome a linha de valor

            if amount_str is not None:
                desc = _clean_description(line, "", amount_str)
                is_credit, amount, needs_review = _decide_credit(amount_str, section, desc)
                tx = Transaction(
                    date=current_date or date.today(),
                    description=desc,
                    amount=amount,
                    is_credit=is_credit,
                    bank=bank,
                    source_file=source_file,
                    needs_review=needs_review,
                )
                txs.append(tx)
                last_tx = tx
                if section_rec is not None:
                    section_rec["sum"] += abs(amount)
                    section_rec["count"] += 1
            else:
                section_pending.append(
                    {"date": current_date, "desc": line, "section": section})
                last_tx = None
            continue

        # Continuação (banco/agência/conta da contraparte)
        if last_tx is not None and d is None and any(h in low for h in NU_CONT_HINTS):
            if len(last_tx.description) < 250:
                last_tx.description = f"{last_tx.description} {line}"
            continue

    # Fecha a última seção aberta.
    _flush_section()

    # Fallback em duas colunas (MANTIDO, agora funcional via FIX M): casa
    # valores do bloco "VALORES EM R$" com as descrições sem valor inline,
    # SOMENTE se as contagens baterem exatamente.
    if pending:
        skip = summary_labels + total_lines
        available = values_pool[skip:]
        if len(available) == len(pending):
            for item, val in zip(pending, available):
                is_credit, amount, needs_review = _decide_credit(val, item["section"], item["desc"])
                txs.append(Transaction(
                    date=item["date"] or date.today(),
                    description=item["desc"],
                    amount=amount,
                    is_credit=is_credit,
                    bank=bank,
                    source_file=source_file,
                    needs_review=needs_review,
                ))
                rec = item.get("rec")
                if rec is not None:
                    rec["sum"] += abs(amount)
                    rec["count"] += 1
        else:
            logger.warning(
                "Nubank: bloco 'VALORES EM R$' não casado (%d valores vs %d descrições) "
                "em %s — aplicando rede de segurança FIX K (sem descarte silencioso).",
                len(available), len(pending), source_file or "PDF",
            )
            # FIX K (rodada 4): NADA é descartado em silêncio. Pendings não
            # casados viram transações ⚠️ (amount=0.0, needs_review=True) para
            # decisão/visibilidade do operador na tela de revisão.
            for item in pending:
                txs.append(Transaction(
                    date=item["date"] or date.today(),
                    description=item["desc"],
                    amount=0.0,
                    is_credit=None,
                    bank=bank,
                    source_file=source_file,
                    needs_review=True,
                ))

    # FIX L (rodada 4): canário de somatório por seção — o total informado
    # pelo próprio banco no cabeçalho vira teste de integridade do parser.
    # Somente log, NUNCA altera o fluxo de decisão.
    for rec in section_records:
        if rec["expected"] is not None and rec["count"] > 0:
            if abs(rec["sum"] - rec["expected"]) > 0.01:
                logger.warning(
                    "Nubank: seção '%s' de %s inconsistente com o somatório do banco: "
                    "esperado=%.2f apurado=%.2f (%d lançamento(s)) em %s.",
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