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
RODADA 5 (evidência: debug_extracao_*.txt + logs de execução):
FIX N: o bloco "VALORES EM R$" de cada página espelha, EM ORDEM, as
linhas "portadoras de valor" da coluna esquerda (totais de seção
intercalados com lançamentos). O alinhamento agora é feito POR PÁGINA
(flush quando o bloco termina), com skip = valores excedentes à
esquerda (resumo/preview) e casamento 1:1 na ordem. A guarda global
antiga de contagens é mantida APENAS como fallback para documentos
sem bloco por página.
FIX O: após o casamento, cada seção é conferida contra o próprio
total informado pelo banco; residual (OCR que perdeu descrição) vira
linha ⚠️ needs_review explícita — o somatório do banco é a fonte de
verdade e nada some em silêncio.
CORREÇÃO DE SINTAXE E VALIDAÇÃO (Rodada Atual):
Restauração completa da formatação Python (strings corrompidas por
espaços extras, docstrings quebradas, __name__ incorretos).
Validação de integridade: toda transação criada garante descrição
não vazia e valor numérico coerente, compatível com a extração
ordenada por coordenadas Y/X do pdf_extractor.py.
RODADA DECIMAL (Correção Crítica de Precisão):
Migração de float para decimal.Decimal em Transaction.amount para
eliminar erros de arredondamento IEEE 754 em somas sucessivas.
Configuração de precisão monetária (2 casas decimais).
Todas as operações aritméticas atualizadas para usar Decimal.
RODADA DEDUPLICAÇÃO (Correção Crítica de Duplicidade): 
Implementação de deduplicação por hash SHA-256 da combinação
(data + valor + descrição normalizada) para evitar contagem dupla
de transações em extratos com períodos sobrepostos.
Função deduplicate_transactions() exportada para uso pelo
income_calculator.py.
RODADA SANTANDER (Correção Crítica de Seções):
Reescrita completa do parse_santander() com máquina de estados
para delimitar seções. Agora processa APENAS a seção "Movimentação"
da Conta Corrente, ignorando CDB/RDB, Índices Econômicos, Saldos
por Período, e outras tabelas que NÃO são transações bancárias.
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

@dataclass
class Transaction:
    date: date
    description: str
    amount: Decimal
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
DATE_FULL_REGEX = r'\b(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})\b'
DATE_SHORT_REGEX = r'^(\d{1,2}[/.-]\d{1,2})\b'
MONTH_HEADER_REGEX = r'^(0[1-9]|1[0-2])/\d{2,4}$'
MONEY_REGEX = r'(?:R$\s*)?([-+]?(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2})\b'
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

# ---------------------------------------------------------------------------
# SANTANDER: Constantes e Filtros de Seção (RODADA SANTANDER)
# ---------------------------------------------------------------------------
SANTANDER_IGNORE_SECTIONS = (
    "renda fixa", "cdb", "rdb", "minhas reservas", "aplicacao n", "aplicação n",
    "indices economicos", "índices econômicos", "indices financeiros",
    "saldos por periodo", "saldos por período", "compras com cartao",
    "compras com cartão", "comprovantes de pagamento", "pacote de servicos",
    "pacote de serviços", "fale conosco", "ouvidoria", "valor inicial",
    "saldo anterior", "saldo atual", "valor liquido", "valor líquido",
    "rendimento bruto", "valor ir/iof", "pagamento de juros",
)

SANTANDER_ANTI_LIXO_KEYWORDS = (
    "% indexador", "data de vencimento", "dolar", "euro", "salario minimo",
    "cdi", "ipca", "inpc", "igpm", "incc", "tr", "poupanca", "ibovespa",
    "dólar", "euro", "salário mínimo", "selic", "referencia", "fechamento",
    "valores referencia", "valores de referência",
)

def _normalize_for_matching(text: str) -> str:
    """Normaliza texto para matching tolerante a ruído de OCR."""
    text = _normalize_text(text)
    text = re.sub(r'(?<=\d)O(?=\d)', '0', text)
    text = re.sub(r'(?<=\d)l(?=\d)', '1', text)
    text = re.sub(r'[^\w\s]', '', text)
    return text

def _detect_santander_section(line: str, previous_section: str) -> str:
    """
    Detecta em qual seção do extrato Santander a linha está.
    
    Retorna:
        "movimentacao" - Conta Corrente (transações reais)
        "renda_fixa" - CDB/RDB/Aplicações (IGNORAR)
        "indices_economicos" - Tabelas de índices (IGNORAR)
        "saldos_periodo" - Resumo de saldos (IGNORAR)
        "cartao_debito" - Compras cartão (IGNORAR por enquanto)
        "comprovantes" - Comprovantes pagamento (IGNORAR)
        "outros" - Outras seções (IGNORAR)
    """
    line_norm = _normalize_for_matching(line)
    line_lower = line.lower()
    
    # Gatilhos para seção de MOVIMENTAÇÃO (Conta Corrente)
    if "conta corrente" in line_lower:
        return "movimentacao"
    if "movimentacao" in line_norm or "movimentação" in line_lower:
        if previous_section in ("", "outros"):
            return "movimentacao"
    if re.search(r'^data\s+descricao', line_norm) or re.search(r'^data\s+lancamento', line_norm):
        if previous_section in ("", "outros"):
            return "movimentacao"
    
    # Gatilhos para RENDA FIXA / CDB / RDB
    if any(kw in line_lower for kw in ["renda fixa", "cdb", "rdb", "minhas reservas"]):
        return "renda_fixa"
    if "aplicacao n" in line_norm or "aplicação n" in line_lower:
        return "renda_fixa"
    if "% indexador" in line_lower or "data de vencimento" in line_lower:
        return "renda_fixa"
    
    # Gatilhos para ÍNDICES ECONÔMICOS
    if any(kw in line_lower for kw in ["indices economicos", "índices econômicos", "indices financeiros"]):
        return "indices_economicos"
    if any(kw in line_lower for kw in ["dolar", "euro", "salario minimo", "cdi", "ipca"]):
        if previous_section == "indices_economicos":
            return "indices_economicos"
    
    # Gatilhos para SALDOS POR PERÍODO
    if "saldos por periodo" in line_norm or "saldos por período" in line_lower:
        return "saldos_periodo"
    
    # Gatilhos para CARTÃO DE DÉBITO
    if "compras com cartao de debito" in line_norm or "compras com cartão de débito" in line_lower:
        return "cartao_debito"
    
    # Gatilhos para COMPROVANTES
    if "comprovantes de pagamento" in line_lower:
        return "comprovantes"
    
    # Gatilhos para PACOTE DE SERVIÇOS / FALE CONOSCO
    if "pacote de servicos" in line_norm or "pacote de serviços" in line_lower:
        return "outros"
    if "fale conosco" in line_lower or "ouvidoria" in line_lower:
        return "outros"
    
    # Se estiver em uma seção de ignorar e não houver gatilho de mudança, mantém
    if previous_section in ("renda_fixa", "indices_economicos", "saldos_periodo", "cartao_debito", "comprovantes", "outros"):
        # Verifica se não é fim da seção (nova seção começando)
        if "conta corrente" not in line_lower and "movimentacao" not in line_norm:
            return previous_section
    
    return previous_section

def _is_valid_santander_transaction(line: str, date_obj: Optional[date]) -> bool:
    """
    Valida se uma linha é uma transação real da Conta Corrente.
    
    Rejeita:
    - Linhas com keywords de outras seções (CDB, índices, etc.)
    - Datas futuras (ano >= 2027)
    - Linhas que são cabeçalhos ou resumos
    """
    line_lower = line.lower()
    line_norm = _normalize_for_matching(line)
    
    # Rejeita keywords de seções não-transacionais
    if any(kw in line_lower for kw in SANTANDER_ANTI_LIXO_KEYWORDS):
        return False
    if any(kw in line_lower for kw in SANTANDER_IGNORE_SECTIONS):
        return False
    
    # Rejeita datas futuras (extratos são de 2025, no máximo 2026)
    if date_obj and date_obj.year >= 2027:
        return False
    
    # Rejeita linhas que são claramente cabeçalhos ou resumos
    if re.search(r'^\s*data\s+descricao', line_norm):
        return False
    if "saldo em" in line_lower and re.search(r'\d{2}/\d{2}/\d{4}', line):
        return False
    
    # Rejeita linhas sem descrição significativa (só data e valor)
    date_match = re.search(r'(\d{2}/\d{2}/\d{4})', line)
    if date_match:
        after_date = line[date_match.end():].strip()
        # Se depois da data só tem número (valor), não é transação válida
        if re.match(r'^[\d\.\,\-\s]+$', after_date):
            return False
    
    return True

def _parse_santander_movimentacao_line(line: str, context_year: int, 
                                        previous_balance: Optional[Decimal]) -> Optional[Tuple[Transaction, Decimal]]:
    """
    Parse de uma linha da seção Movimentação do Santander.
    
    Formato esperado:
    <Data> <Descrição> <Nº Documento> <Movimento (R$)> <Saldo (R$)>
    
    Retorna:
        (Transaction, novo_saldo) ou None se não for transação válida
    """
    line = line.strip()
    if not line:
        return None
    
    # Normalização OCR
    line = re.sub(r'(?<=\d)O(?=\d)', '0', line)
    line = re.sub(r'(?<=\d)l(?=\d)', '1', line)
    
    # Extrai data (deve estar no início)
    date_match = re.match(r'(\d{2}/\d{2}/\d{4})', line)
    if not date_match:
        return None
    
    date_str = date_match.group(1)
    try:
        tx_date = date_parser.parse(date_str, dayfirst=True).date()
    except ValueError:
        return None
    
    # Validação semântica
    if not _is_valid_santander_transaction(line, tx_date):
        return None
    
    # Extrai todos os valores monetários da linha
    money_matches = re.findall(r'([-+]?\d{1,3}(?:\.\d{3})*,\d{2})', line)
    
    if len(money_matches) < 1:
        return None
    
    # Estratégia de parsing:
    # - Último valor = saldo
    # - Penúltimo valor = movimento (transação)
    # - Se só tem 1 valor, ele é o movimento
    
    if len(money_matches) >= 2:
        movement_str = money_matches[-2]
        # saldo_str = money_matches[-1]  # Não usamos o saldo para nada agora
    else:
        movement_str = money_matches[0]
    
    # Parse do valor
    movement_value = parse_money_value(movement_str)
    
    # Determina se é crédito ou débito
    is_credit = None
    if movement_str.startswith('-'):
        is_credit = False
        movement_value = -abs(movement_value)
    elif movement_str.startswith('+'):
        is_credit = True
        movement_value = abs(movement_value)
    else:
        # Sem sinal explícito: usa contexto do saldo anterior (se disponível)
        # ou heurística semântica
        is_credit = _infer_credit(line, movement_str)
        if is_credit is None:
            is_credit = movement_value >= 0
        if is_credit and movement_value < 0:
            movement_value = -movement_value
    
    # Extrai descrição (texto entre a data e o movimento)
    date_end_idx = date_match.end()
    movement_idx = line.find(movement_str)
    
    if movement_idx > date_end_idx:
        description = line[date_end_idx:movement_idx].strip()
        # Remove possíveis números de documento no meio
        description = re.sub(r'\b\d{3,}\b', '', description)
        description = re.sub(r'\s+', ' ', description).strip()
    else:
        description = "Lançamento não identificado"
    
    # Limpeza final
    description = description.strip(' -–|*')
    if not description or len(description) < 3:
        description = "Lançamento não identificado"
    
    tx = Transaction(
        date=tx_date,
        description=description,
        amount=movement_value,
        is_credit=is_credit,
        bank="santander",
        source_file="",
        needs_review=(is_credit is None),
    )
    
    # Calcula novo saldo (aproximado, para uso em heurísticas futuras)
    new_balance = None
    if len(money_matches) >= 2:
        try:
            new_balance = parse_money_value(money_matches[-1])
        except:
            new_balance = None
    
    return (tx, new_balance)

def _parse_santander(text: str, bank: str = "santander", source_file: str = "") -> List[Transaction]:
    """
    Parser robusto para extratos Santander.
    
    Estratégia:
    1. Máquina de estados para identificar seções
    2. Processa APENAS a seção "Movimentação" da Conta Corrente
    3. Ignora CDB/RDB, Índices Econômicos, Saldos por Período, etc.
    4. Validação semântica rigorosa (rejeita datas futuras, keywords de lixo)
    """
    transactions: List[Transaction] = []
    lines = [ln.strip() for ln in (text or "").splitlines()]
    
    current_section = ""
    context_year: Optional[int] = None
    previous_balance: Optional[Decimal] = None
    
    # Detecta ano de contexto nas primeiras linhas
    for line in lines[:20]:
        year_match = re.search(r'\b(202[0-9]|203[0-5])\b', line)
        if year_match:
            context_year = int(year_match.group(1))
            break
    
    if context_year is None:
        context_year = date.today().year
    
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        
        if not line:
            continue
        
        # Detecta seção atual
        current_section = _detect_santander_section(line, current_section)
        
        # Só processa se estiver na seção de MOVIMENTAÇÃO
        if current_section != "movimentacao":
            continue
        
        # Tenta parsear como transação
        result = _parse_santander_movimentacao_line(line, context_year, previous_balance)
        
        if result:
            tx, new_balance = result
            tx.source_file = source_file
            transactions.append(tx)
            if new_balance is not None:
                previous_balance = new_balance
    
    return transactions

# Alias para manter compatibilidade
def parse_santander(text: str, bank: str = "santander", source_file: str = "") -> List[Transaction]:
    """Wrapper para _parse_santander (mantém compatibilidade)."""
    return _parse_santander(text, bank, source_file)

# ---------------------------------------------------------------------------
# Parser genérico (fallback universal)
# ---------------------------------------------------------------------------
def _parse_generic_lines(text: str, bank: str, source_file: str, use_suffix: bool = False) -> List[Transaction]:
    """Layout clássico: data dd/mm + descrição + valor na mesma linha
    (ou valor nas até 3 linhas seguintes). FIX B: indeterminado => needs_review=True."""
    transactions: List[Transaction] = []
    lines = [ln.strip() for ln in (text or "").splitlines()]
    context_year: Optional[int] = None
    i, n = 0, len(lines)
    
    while i < n:
        line = lines[i]
        if not line:
            i += 1
            continue
            
        # CORREÇÃO: Normalização leve para OCR (O -> 0, l -> 1 em contextos numéricos)
        norm_line = re.sub(r'(?<=\d)O(?=\d)', '0', line)
        norm_line = re.sub(r'(?<=\d)l(?=\d)', '1', norm_line)
        
        header = re.match(MONTH_HEADER_REGEX, norm_line)
        if header:
            year_match = re.search(r'(\d{4})', norm_line)
            if year_match:
                context_year = int(year_match.group(1))
            i += 1
            continue
            
        if norm_line.upper().startswith(SKIP_LINE_PREFIXES):
            i += 1
            continue
        
        # FILTRO ANTI-LIXO (aplicado também ao genérico)
        line_lower = norm_line.lower()
        if any(kw in line_lower for kw in SANTANDER_ANTI_LIXO_KEYWORDS):
            i += 1
            continue
        if any(kw in line_lower for kw in SANTANDER_IGNORE_SECTIONS):
            i += 1
            continue
            
        date_str: Optional[str] = None
        is_short = False
        full_dates = re.findall(DATE_FULL_REGEX, norm_line)
        if full_dates:
            if len(full_dates) > 1:
                date_str = full_dates[0]
            else:
                date_str = full_dates[0]
        else:
            # CORREÇÃO: Usar re.search em vez de re.match para tolerar ruído de OCR no início da linha
            short = re.search(r'(\d{1,2}[/.-]\d{1,2})\b', norm_line)
            if short:
                date_str, is_short = short.group(1), True
                
        if not date_str:
            i += 1
            continue
            
        moneys = re.findall(MONEY_REGEX, norm_line)
        consumed_until = i
        if not moneys:
            j = i + 1
            while j < min(i + 4, n):
                nxt = lines[j]
                if nxt and re.findall(MONEY_REGEX, nxt):
                    moneys = re.findall(MONEY_REGEX, nxt)
                    consumed_until = j
                    break
                if nxt and (re.findall(DATE_FULL_REGEX, nxt) or re.search(r'(\d{1,2}[/.-]\d{1,2})\b', nxt)):
                    break
                j += 1
                
        if not moneys:
            i += 1
            continue
            
        # BUG 2 FIX: pegar o 1º valor (transação), evitando capturar o saldo final
        amount_str = moneys[0] 
        
        parsed_date = _build_date(date_str, is_short, context_year)
        if parsed_date is None:
            i += 1
            continue
        
        # FILTRO DE DATAS FUTURAS (também no genérico)
        if parsed_date.year >= 2027:
            i += 1
            continue
            
        # CORREÇÃO: Extração robusta da descrição tolerando deslocamento de OCR
        date_idx = norm_line.find(date_str)
        amount_idx = norm_line.find(amount_str)
        
        if date_idx >= 0 and amount_idx >= 0 and amount_idx > date_idx:
            description = norm_line[date_idx + len(date_str):amount_idx].strip()
        else:
            description = norm_line.replace(date_str, "", 1).replace(amount_str, "", 1).strip()
            
        description = re.sub(r'\s+', ' ', description).strip(" -–|*")
        if not description:
            description = "Lançamento não identificado"
            
        is_credit = _infer_credit(norm_line, amount_str)
        if use_suffix:
            idx = norm_line.find(amount_str)
            if idx >= 0:
                suffix = norm_line[idx + len(amount_str):].strip()[:1].upper()
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
    """Parser do extrato Nubank (OCR). Ver docstring do módulo para detalhes do FIX N e O."""
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
                "Nubank: seção '%s' de %s com residual de %.2f (OCR perdeu descrição); criando linha de revisão. (%s)",
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
                logger.warning(
                    "Nubank: alinhamento por página impossível (%d valores vs %d slots) em %s — aplicando FIX K.",
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
            logger.warning(
                "Nubank: bloco 'VALORES EM R$' não casado (%d valores vs %d descrições) em %s — aplicando rede de segurança FIX K.",
                len(available), len(pending), source_file or "PDF",
            )
            for item in pending:
                _fix_k(item)
                
    for rec in section_records:
        if rec["expected"] is not None and rec["count"] > 0:
            if abs(rec["sum"] - rec["expected"]) > Decimal('0.01'):
                logger.warning(
                    "Nubank: seção '%s' de %s inconsistente com o somatório do banco: esperado=%.2f apurado=%.2f (%d lançamento(s)) em %s.",
                    "entradas" if rec["label"] == "E" else "saídas",
                    rec["date"].strftime("%d/%m/%Y") if rec["date"] else "??/??/????",
                    rec["expected"], rec["sum"], rec["count"],
                    source_file or "PDF",
                )
                
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
    money_re = re.compile(r'(-?R$\s*\d{1,3}(?:\.\d{3})*,\d{2})')
    
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
        amount_str = money_matches[-1] # C6 mantém [-1] pois o layout é estritamente colunar e o último é o valor da transação
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
    balance_line_re = re.compile(r'Saldo do dia:\s*R\$')
    money_re = re.compile(r'(-?R$\s*\d{1,3}(?:\.\d{3})*,\d{2})')
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
            
            # BUG 2 FIX: pegar o 1º valor (transação), evitando capturar o saldo final
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
            
        # BUG 2 FIX: pegar o 1º valor (transação), evitando capturar o saldo final
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
            
        # BUG 2 FIX: pegar o 1º valor (transação), evitando capturar o saldo final
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
    """Escolhe o parser do banco; se ele não produzir nada, usa o genérico."""
    parser_fn = _PARSERS.get(bank, parse_generic)
    txs = parser_fn(text, bank=bank, source_file=source_file)
    if not txs and parser_fn is not parse_generic:
        logger.info("Parser '%s' vazio para %s — aplicando fallback genérico.", bank, source_file or "PDF")
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

# ---------------------------------------------------------------------------
# Deduplicação de transações (Rodada Deduplicação)
# ---------------------------------------------------------------------------
def _transaction_hash(tx: Transaction) -> str:
    """Gera hash SHA-256 único para a transação baseado em data, valor e descrição normalizada."""
    norm_desc = _normalize_text(tx.description)
    norm_desc = re.sub(r'\s+', ' ', norm_desc).strip()
    amount_str = str(tx.amount.quantize(Decimal('0.01')))
    date_str = tx.date.isoformat()
    hash_input = f"{date_str}|{amount_str}|{norm_desc}"
    return hashlib.sha256(hash_input.encode('utf-8')).hexdigest()

def deduplicate_transactions(transactions: List[Transaction]) -> Tuple[List[Transaction], int]:
    """Remove transações duplicadas baseado em hash SHA-256."""
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