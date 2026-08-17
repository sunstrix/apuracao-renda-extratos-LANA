"""
Consolidação das métricas de apuração de renda.

Responsabilidades:
- Classificar cada lançamento via rules_engine.evaluate_transaction();
- Aplicar a camada de revisão manual do operador CCA:
  * exclusão manual (Dict[int, str]  -> índice original: motivo);
  * inclusão manual  (Set[int]       -> índices de lançamentos "needs_review"
    confirmados como renda pelo operador);
  * padrão de segurança: lançamento "needs_review" SEM decisão vai para a
    auditoria como "Sinal de crédito/débito indeterminado — revisão manual
    pendente";
- Agregar totais mensais, Total Geral, Média Mensal Geral e Média de Meses
  Completos.

O formato de retorno histórico é preservado (mesmas chaves); a chave nova
"revisao_manual" é aditiva e usada apenas pela interface/relatório.
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
) -> Tuple[bool, str, bool]:
    """
    Decide (is_excluded, reason, manually_confirmed) para um lançamento.

    Prioridade:
    1. Exclusão manual explícita (motivo do operador);
    2. Inclusão manual explícita (operador confirmou como renda) — ainda
       assim as regras automáticas de compliance são aplicadas;
    3. Padrão de segurança para needs_review sem decisão;
    4. Regras automáticas do rules_engine.
    """
    manually_confirmed = False

    if t.needs_review:
        if manual_exclusions and idx in manual_exclusions:
            is_excluded, reason = evaluate_transaction(
                t,
                holder_name=holder_name,
                manual_exclusions=manual_exclusions,
                transaction_index=idx,
            )
            return is_excluded, reason, False

        if manual_inclusions and idx in manual_inclusions:
            # Operador confirmou como renda: força crédito, mas mantém as
            # regras automáticas (mesma titularidade / apostas) ativas.
            t.is_credit = True
            is_excluded, reason = evaluate_transaction(
                t,
                holder_name=holder_name,
                manual_exclusions=manual_exclusions,
                transaction_index=idx,
            )
            if not is_excluded:
                manually_confirmed = True
                t.manually_confirmed = True
            return is_excluded, reason, manually_confirmed

        # Sem decisão do operador: padrão de segurança = exclui e audita.
        return True, MOTIVO_REVISAO_PENDENTE, False

    # Lançamento com sinal determinado: fluxo automático normal.
    is_excluded, reason = evaluate_transaction(
        t,
        holder_name=holder_name,
        manual_exclusions=manual_exclusions,
        transaction_index=idx,
    )
    return is_excluded, reason, False


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
        transactions: Lista bruta (ordem original preservada nos índices).
        holder_name: Nome do titular para regra de mesma titularidade.
        manual_exclusions: {índice original: motivo} — exclusões do operador.
        manual_inclusions: {índices originais} — confirmações do operador
            para lançamentos needs_review.

    Returns:
        Dict com as chaves históricas (total_geral, media_mensal_geral,
        media_meses_completos, resumo_mensal, entradas_validas,
        entradas_excluidas) + chave aditiva "revisao_manual".
    """
    valid_transactions: List[Transaction] = []
    excluded_transactions: List[Dict[str, Any]] = []
    revisao: Dict[str, List[int]] = {"incluidas": [], "excluidas": [], "pendentes": []}

    # 1. Classificação (regras + revisão manual)
    for idx, t in enumerate(transactions):
        is_excluded, reason, confirmed = _classify_transaction(
            idx, t, holder_name, manual_exclusions, manual_inclusions
        )

        if t.needs_review:
            if manual_exclusions and idx in manual_exclusions:
                revisao["excluidas"].append(idx)
            elif manual_inclusions and idx in manual_inclusions:
                revisao["incluidas"].append(idx)
            else:
                revisao["pendentes"].append(idx)

        if is_excluded:
            excluded_transactions.append({
                "date": t.date,
                "description": t.description,
                "reason": reason,
                "amount": t.amount,
            })
        else:
            valid_transactions.append(t)

    if revisao["incluidas"] or revisao["excluidas"] or revisao["pendentes"]:
        logger.info(
            "Revisão manual: %d incluída(s), %d excluída(s), %d pendente(s).",
            len(revisao["incluidas"]),
            len(revisao["excluidas"]),
            len(revisao["pendentes"]),
        )

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

        first_day = min(dates)
        last_day = max(dates)

        if is_month_complete(first_day, last_day):
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
        "revisao_manual": revisao,
    }