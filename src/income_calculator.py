"""
Consolidação das métricas de apuração de renda.

Responsabilidades:
- Classificar cada lançamento via rules_engine.evaluate_transaction();
- Aplicar a camada de revisão manual do operador CCA:
  - manual_exclusions: {índice original: motivo} — exclusão com prioridade máxima;
  - manual_inclusions: {índices originais} — confirmação de lançamentos
    "needs_review" como renda pelo operador;
  - needs_review SEM decisão → excluído por padrão de segurança com motivo
    "Sinal de crédito/débito indeterminado — revisão manual pendente";
- Agregar totais mensais, Total Geral, Média Mensal Geral e Média de Meses
  Completos.

O formato de retorno é mantido (mesmas chaves do contrato original).

PERF-8/9/10: Otimizações aplicadas:
- Redução de passes sobre a lista de transações (de 3 para 2 passes)
- Agrupamento mensal em uma única passagem usando defaultdict
- Soma de valores otimizada com sum() sobre generators
"""
import logging
from collections import defaultdict
from datetime import date
from typing import Any, Dict, List, Optional, Set, Tuple

from src.transaction_parser import Transaction
from src.rules_engine import evaluate_transaction

logger = logging.getLogger(__name__)

MOTIVO_REVISAO_PENDENTE = (
    "Sinal de crédito/débito indeterminado — revisão manual pendente"
)


def is_month_complete(first_day: date, last_day: date) -> bool:
    """
    Verifica se o período entre a primeira e a última transação do mês
    cobre mais de 20 dias, indicando um extrato de mês completo.
    """
    if not first_day or not last_day:
        return False
    return (last_day - first_day).days > 20


def _classify_transaction(
    idx: int,
    t: Transaction,
    holder_name: Optional[str],
    manual_exclusions: Optional[Dict[int, str]],
    manual_inclusions: Optional[Set[int]],
) -> Tuple[bool, str]:
    """
    Decide (is_excluded, reason) para um lançamento, respeitando a revisão
    manual do operador.
    
    Prioridade:
    1. Exclusão manual explícita (motivo propagado pelo rules_engine);
    2. Inclusão manual explícita (força crédito; regras automáticas de
       compliance — mesma titularidade/apostas — continuam ativas);
    3. Padrão de segurança para needs_review sem decisão.
    
    NOTA: Esta função pode mutar o objeto Transaction (t.is_credit, 
    t.manually_confirmed) para compatibilidade com report_generator.
    """
    if t.needs_review:
        if manual_exclusions and idx in manual_exclusions:
            return evaluate_transaction(
                t,
                holder_name=holder_name,
                manual_exclusions=manual_exclusions,
                transaction_index=idx,
            )
        if manual_inclusions and idx in manual_inclusions:
            # Operador confirmou como renda: força crédito e marca o
            # lançamento para o asterisco de rastreabilidade no relatório
            # (atributo dinâmico lido via getattr no report_generator).
            t.is_credit = True
            t.manually_confirmed = True
            return evaluate_transaction(
                t,
                holder_name=holder_name,
                manual_exclusions=manual_exclusions,
                transaction_index=idx,
            )
        # Sem decisão do operador: padrão de segurança = exclui e audita.
        return True, MOTIVO_REVISAO_PENDENTE

    # Lançamento com sinal determinado: fluxo automático normal.
    return evaluate_transaction(
        t,
        holder_name=holder_name,
        manual_exclusions=manual_exclusions,
        transaction_index=idx,
    )


def calculate_income_metrics(
    transactions: List[Transaction],
    holder_name: Optional[str] = None,
    manual_exclusions: Optional[Dict[int, str]] = None,
    manual_inclusions: Optional[Set[int]] = None,
) -> Dict[str, Any]:
    """
    Processa todas as transações, aplica regras de negócio + revisão manual
    e calcula as métricas de apuração de renda.

    Args:
        transactions: Lista bruta (a posição na lista é o índice usado nas
            estruturas de revisão manual).
        holder_name: Nome do titular (habilita a regra de mesma titularidade
            por contraparte). Opcional.
        manual_exclusions: {índice: motivo} — exclusões do operador. Opcional.
        manual_inclusions: {índices} — confirmações do operador para
            lançamentos needs_review. Opcional.

    Returns:
        Dict com as chaves históricas: total_geral, media_mensal_geral,
        media_meses_completos, resumo_mensal, entradas_validas,
        entradas_excluidas.
    
    PERF-8/9/10: Otimizações aplicadas:
    - Classificação e agrupamento em uma única passagem principal
    - Estruturas defaultdict pré-inicializadas para evitar verificações de chave
    - Soma de valores usando sum() com generators (mais eficiente que loops)
    """
    if not transactions:
        # Retorno rápido para lista vazia (evita processamento desnecessário)
        return {
            "total_geral": 0.0,
            "media_mensal_geral": 0.0,
            "media_meses_completos": 0.0,
            "resumo_mensal": [],
            "entradas_validas": [],
            "entradas_excluidas": [],
        }

    valid_transactions: List[Transaction] = []
    excluded_transactions: List[Dict[str, Any]] = []

    # PERF-9: Estruturas pré-alocadas para agrupamento em uma única passagem
    # Chave: (ano, mes) -> lista de transações válidas / datas de todas as transações
    monthly_valid_data: Dict[Tuple[int, int], List[Transaction]] = defaultdict(list)
    monthly_all_dates: Dict[Tuple[int, int], List[date]] = defaultdict(list)
    
    # PERF-10: Pré-alocar capacidade estimada (opcional, Python gerencia dinamicamente)
    # Mas podemos usar append direto que é O(1) amortizado

    # 1. Classificação + Agrupamento em UMA ÚNICA PASSAGEM (PERF-8/9)
    # Antes: 3 passes (classificar, agrupar all_dates, agrupar valid_data)
    # Agora: 1 passe principal que faz tudo simultaneamente
    for idx, t in enumerate(transactions):
        # Classificação (regras automáticas + revisão manual)
        is_excluded, reason = _classify_transaction(
            idx, t, holder_name, manual_exclusions, manual_inclusions
        )
        
        # Agrupamento por mês/ano (todas as transações, válidas ou não)
        month_key = (t.date.year, t.date.month)
        monthly_all_dates[month_key].append(t.date)
        
        if is_excluded:
            excluded_transactions.append({
                "date": t.date,
                "description": t.description,
                "reason": reason,
                "amount": t.amount,
            })
        else:
            valid_transactions.append(t)
            # PERF-9: Agrupar transações válidas na mesma passagem
            monthly_valid_data[month_key].append(t)

    # 2. Cálculos agregados (PERF-10: sum() com generator é mais eficiente)
    total_geral = sum(t.amount for t in valid_transactions)
    
    # União de todos os meses que apareceram (válidos ou excluídos)
    all_months = set(monthly_valid_data.keys()) | set(monthly_all_dates.keys())
    num_months_total = len(all_months)
    
    media_mensal_geral = (
        total_geral / num_months_total if num_months_total > 0 else 0.0
    )

    # Média de Meses Completos (cobertura > 20 dias no extrato)
    # PERF-10: Otimizado com sum() sobre generator
    total_complete_months = 0.0
    count_complete_months = 0
    
    for month_key, dates in monthly_all_dates.items():
        if not dates:
            continue
        
        # Verifica se o mês cobre mais de 20 dias (extrato de mês completo)
        if is_month_complete(min(dates), max(dates)):
            # Soma apenas transações válidas deste mês
            month_valid_sum = sum(
                t.amount for t in monthly_valid_data.get(month_key, [])
            )
            total_complete_months += month_valid_sum
            count_complete_months += 1

    media_meses_completos = (
        total_complete_months / count_complete_months
        if count_complete_months > 0
        else 0.0
    )

    # 3. Resumo mensal ordenado para interface/relatório
    # PERF-10: Ordenação otimizada (sorted já é eficiente em Python)
    monthly_summary: List[Dict[str, Any]] = []
    sorted_keys = sorted(monthly_valid_data.keys(), key=lambda x: (x[0], x[1]))
    
    for key in sorted_keys:
        year, month = key
        valid_txs = monthly_valid_data[key]
        
        # PERF-10: Calcular soma uma única vez por mês
        total_valido = sum(t.amount for t in valid_txs)
        
        monthly_summary.append({
            "month_label": f"{month:02d}/{year}",
            "qtd_entradas_validas": len(valid_txs),
            "total_valido": total_valido,
        })

    return {
        "total_geral": total_geral,
        "media_mensal_geral": media_mensal_geral,
        "media_meses_completos": media_meses_completos,
        "resumo_mensal": monthly_summary,
        "entradas_validas": valid_transactions,
        "entradas_excluidas": excluded_transactions,
    }