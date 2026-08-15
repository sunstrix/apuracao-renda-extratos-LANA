import pytest
from datetime import date

from src.transaction_parser import Transaction
from src.rules_engine import evaluate_transaction, normalize_text


def test_exclude_same_ownership():
    """Testa exclusão de transferências de mesma titularidade."""
    tx = Transaction(
        date=date(2023, 10, 1),
        description="Transferência mesma titularidade",
        amount=1500.00
    )
    is_excluded, reason = evaluate_transaction(tx)
    assert is_excluded is True
    assert reason == "Transferência de mesma titularidade"


def test_exclude_investment():
    """Testa exclusão de rendimentos de investimentos."""
    tx = Transaction(
        date=date(2023, 10, 5),
        description="Rendimento CDB",
        amount=250.00
    )
    is_excluded, reason = evaluate_transaction(tx)
    assert is_excluded is True
    assert reason == "Resgate/Rendimento de aplicação financeira"


def test_exclude_gambling():
    """Testa exclusão de créditos de apostas."""
    tx = Transaction(
        date=date(2023, 10, 10),
        description="PIX recebido Bet",
        amount=500.00
    )
    is_excluded, reason = evaluate_transaction(tx)
    assert is_excluded is True
    assert reason == "Crédito de aposta/jogo de azar"


def test_include_valid_income():
    """Testa inclusão de renda válida (ex: salário)."""
    tx = Transaction(
        date=date(2023, 10, 15),
        description="Salário Empresa XYZ",
        amount=3500.00
    )
    is_excluded, reason = evaluate_transaction(tx)
    assert is_excluded is False
    assert reason == "Entrada válida de renda"


def test_debit_is_excluded():
    """Testa que débitos (valores negativos) são excluídos da apuração de renda."""
    tx = Transaction(
        date=date(2023, 10, 20),
        description="Pagamento boleto",
        amount=-120.00
    )
    is_excluded, reason = evaluate_transaction(tx)
    assert is_excluded is True
    assert "débito" in reason.lower()


def test_normalize_text():
    """Testa normalização de texto para busca de palavras-chave."""
    assert normalize_text("Crédito Bet") == "credito bet"
    assert normalize_text("Rendimento CDB") == "rendimento cdb"
    assert normalize_text("Transferência Própria") == "transferencia propria"


def test_exclude_with_accents_and_uppercase():
    """Testa exclusão mesmo com acentos e caixa alta."""
    tx = Transaction(
        date=date(2023, 10, 25),
        description="RESGATE FUNDO INVESTIMENTO",
        amount=1000.00
    )
    is_excluded, reason = evaluate_transaction(tx)
    assert is_excluded is True
    assert reason == "Resgate/Rendimento de aplicação financeira"