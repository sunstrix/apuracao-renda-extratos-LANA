"""
Consolidação das métricas de apuração de renda.

Responsabilidades:
- Classificar cada lançamento via rules_engine.evaluate_transaction();
- Aplicar a camada de revisão manual do operador CCA:
  * manual_exclusions: {índice original: motivo} — exclusão com prioridade máxima;
  * manual_inclusions: {índices originais} — confirmação de lançamentos
    "needs_review" como renda pelo operador;
  * needs_review SEM decisão → excluído por padrão de segurança com motivo
    "Sinal de crédito/débito indeterminado — revisão manual pendente";
- Agregar totais mensais, Total Geral, Média Mensal Geral e Média de Meses
  Completos.

O formato de retorno é mantido (mesmas chaves do contrato original).
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
    """
    valid_transactions: List[Transaction] = []
    excluded_transactions: List[Dict[str, Any]] = []

    # 1. Classificação (regras automáticas + revisão manual)
    for idx, t in enumerate(transactions):
        is_excluded, reason = _classify_transaction(
            idx, t, holder_name, manual_exclusions, manual_inclusions
        )

        if is_excluded:
            excluded_transactions.append({
                "date": t.date,
                "description": t.description,
                "reason": reason,
                "amount": t.amount,
            })
        else:
            valid_transactions.append(t)

    # 2. Agrupamento por Mês/Ano
    monthly_valid_data: Dict[Tuple[int, int], List[Transaction]] = defaultdict(list)
    monthly_all_dates: Dict[Tuple[int, int], List[date]] = defaultdict(list)

    for t in transactions:
        monthly_all_dates[(t.date.year, t.date.month)].append(t.date)

    for t in valid_transactions:
        monthly_valid_data[(t.date.year, t.date.month)].append(t)

    # 3. Cálculos agregados
    total_geral = sum(t.amount for t in valid_transactions)

    all_months = set(monthly_valid_data.keys()).union(set(monthly_all_dates.keys()))
    num_months_total = len(all_months)

    media_mensal_geral = (
        total_geral / num_months_total if num_months_total > 0 else 0.0
    )

    # Média de Meses Completos (cobertura > 20 dias no extrato)
    total_complete_months = 0.0
    count_complete_months = 0

    for key, dates in monthly_all_dates.items():
        if not dates:
            continue

        if is_month_complete(min(dates), max(dates)):
            month_valid_sum = sum(t.amount for t in monthly_valid_data.get(key, []))
            total_complete_months += month_valid_sum
            count_complete_months += 1

    media_meses_completos = (
        total_complete_months / count_complete_months
        if count_complete_months > 0
        else 0.0
    )

    # 4. Resumo mensal ordenado para interface/relatório
    monthly_summary: List[Dict[str, Any]] = []
    sorted_keys = sorted(monthly_valid_data.keys(), key=lambda x: (x[0], x[1]))

    for key in sorted_keys:
        year, month = key
        valid_txs = monthly_valid_data[key]

        monthly_summary.append({
            "month_label": f"{month:02d}/{year}",
            "qtd_entradas_validas": len(valid_txs),
            "total_valido": sum(t.amount for t in valid_txs),
        })

    return {
        "total_geral": total_geral,
        "media_mensal_geral": media_mensal_geral,
        "media_meses_completos": media_meses_completos,
        "resumo_mensal": monthly_summary,
        "entradas_validas": valid_transactions,
        "entradas_excluidas": excluded_transactions,
    }