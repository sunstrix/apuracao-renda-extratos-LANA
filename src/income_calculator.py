import logging
from datetime import date
from typing import List, Dict, Any, Tuple
from collections import defaultdict

from src.transaction_parser import Transaction
from src.rules_engine import evaluate_transaction

logger = logging.getLogger(__name__)


def is_month_complete(first_day: date, last_day: date) -> bool:
    """
    Verifica se o período entre a primeira e a última transação do mês
    cobre mais de 20 dias, indicando um extrato de mês completo.
    """
    if not first_day or not last_day:
        return False
    
    delta = (last_day - first_day).days
    return delta > 20


def calculate_income_metrics(transactions: List[Transaction]) -> Dict[str, Any]:
    """
    Processa todas as transações, aplica as regras de negócio e calcula as métricas
    de apuração de renda.

    Returns:
        Dicionário com:
        - total_geral: float
        - media_mensal_geral: float
        - media_meses_completos: float
        - resumo_mensal: List[Dict] (Mês/Ano, Qtd Entradas Válidas, Total Válido)
        - entradas_validas: List[Transaction]
        - entradas_excluidas: List[Dict] (Auditoria)
    """
    # 1. Classificação das transações
    valid_transactions: List[Transaction] = []
    excluded_transactions: List[Dict[str, Any]] = []

    for transaction in transactions:
        is_excluded, reason = evaluate_transaction(transaction)
        
        if is_excluded:
            excluded_transactions.append({
                "date": transaction.date,
                "description": transaction.description,
                "reason": reason,
                "amount": transaction.amount
            })
        else:
            valid_transactions.append(transaction)

    # 2. Agrupamento por Mês/Ano
    # Para o resumo financeiro (renda válida)
    monthly_valid_data: Dict[Tuple[int, int], List[Transaction]] = defaultdict(list)
    
    # Para a análise de cobertura do extrato (todas as transações)
    monthly_all_dates: Dict[Tuple[int, int], List[date]] = defaultdict(list)

    for t in transactions:
        key = (t.date.year, t.date.month)
        monthly_all_dates[key].append(t.date)

    for t in valid_transactions:
        key = (t.date.year, t.date.month)
        monthly_valid_data[key].append(t)

    # 3. Cálculos Agregados
    total_geral = sum(t.amount for t in valid_transactions)
    
    # Identifica todos os meses únicos presentes no extrato (completos ou não)
    all_months = set(monthly_valid_data.keys()).union(set(monthly_all_dates.keys()))
    num_months_total = len(all_months)
    
    media_mensal_geral = total_geral / num_months_total if num_months_total > 0 else 0.0

    # Cálculo da Média de Meses Completos
    total_complete_months = 0.0
    count_complete_months = 0
    
    for key, dates in monthly_all_dates.items():
        if not dates:
            continue
        
        first_day = min(dates)
        last_day = max(dates)
        
        if is_month_complete(first_day, last_day):
            # Soma apenas as entradas válidas deste mês completo
            month_valid_sum = sum(t.amount for t in monthly_valid_data.get(key, []))
            total_complete_months += month_valid_sum
            count_complete_months += 1

    media_meses_completos = total_complete_months / count_complete_months if count_complete_months > 0 else 0.0

    # 4. Preparação do Resumo Mensal para a interface e PDF
    monthly_summary = []
    # Ordena os meses para exibição cronológica
    sorted_keys = sorted(monthly_valid_data.keys(), key=lambda x: (x[0], x[1]))
    
    for key in sorted_keys:
        year, month = key
        valid_txs = monthly_valid_data[key]
        
        # Formata o nome do mês (ex: "01/2023")
        month_label = f"{month:02d}/{year}"
        
        monthly_summary.append({
            "month_label": month_label,
            "qtd_entradas_validas": len(valid_txs),
            "total_valido": sum(t.amount for t in valid_txs)
        })

    return {
        "total_geral": total_geral,
        "media_mensal_geral": media_mensal_geral,
        "media_meses_completos": media_meses_completos,
        "resumo_mensal": monthly_summary,
        "entradas_validas": valid_transactions,
        "entradas_excluidas": excluded_transactions
    }