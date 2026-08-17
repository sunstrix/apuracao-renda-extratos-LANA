"""
Extração de transações bancárias via Gemini API (Google).

Papel na arquitetura (rodada Gemini):
- Recebe os BYTES do PDF e envia ao Gemini 2.5 Flash com input nativo de
  PDF (sem OCR local): o modelo lê o layout de duas colunas como um humano;
- Exige saída JSON estrita (response_schema): titular, totais de seção
  ("Total de entradas/saídas") e lista de transações;
- Valida a extração contra o GABARITO do próprio banco: a soma das
  transações de cada seção deve bater no total impresso da seção
  (tolerância R$ 0,01) — divergência vira warning estruturado;
- Converte o JSON para List[Transaction] (mesmo dataclass do
  transaction_parser), de modo que o fluxo atual (revisão manual,
  rules_engine, income_calculator, report_generator) consuma a tabela
  SEM nenhuma mudança estrutural;
- Rastreabilidade: cada Transaction recebe o atributo dinâmico
  extraction_source="gemini" (lido via getattr no report_generator).

Segurança/privacidade:
- A chave vem SOMENTE de GEMINI_API_KEY no .env (python-dotenv) ou de
  variável de ambiente (Streamlit Cloud: secrets);
- O PDF sai da máquina do usuário → o app.py exibirá checkbox de
  consentimento explícito antes de chamar este módulo (próxima rodada);
- Falhas (quota, rede, schema) lançam GeminiExtractionError; o app.py
  faz fallback para o pipeline local determinístico.

Limites do free tier (2.5 Flash): 15 RPM / 1.500 req/dia — um batch de
3 PDFs = 3 chamadas. PDFs contam ~258 tokens/página (34 páginas ≈ 9k
tokens), muito abaixo do teto de tokens.
"""
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from dateutil import parser as date_parser

from src.transaction_parser import Transaction

logger = logging.getLogger(__name__)

# Carrega .env local se python-dotenv estiver instalado (no Streamlit Cloud
# a chave vem via st.secrets/variável de ambiente — load_dotenv é no-op).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
TOLERANCIA_SOMATORIO = 0.01


class GeminiExtractionError(Exception):
    """Falha controlada da extração via Gemini (o app faz fallback local)."""


def get_api_key() -> Optional[str]:
    """Chave vinda exclusivamente do ambiente (.env / secrets)."""
    return (os.getenv("GEMINI_API_KEY") or "").strip() or None


def gemini_available() -> bool:
    """True se há chave configurada (não testa quota/rede)."""
    return get_api_key() is not None


# ---------------------------------------------------------------------------
# Schema JSON estrito (controlled generation)
# ---------------------------------------------------------------------------
EXTRACTION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "titular": {"type": "string"},
        "totais_secao": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "data": {"type": "string",
                             "description": "Data do cabeçalho da seção, ISO yyyy-mm-dd"},
                    "tipo": {"type": "string", "enum": ["entradas", "saidas"]},
                    "total": {"type": "number",
                              "description": "Valor absoluto impresso no cabeçalho"},
                },
                "required": ["data", "tipo", "total"],
            },
        },
        "transacoes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "data": {"type": "string",
                             "description": "ISO yyyy-mm-dd (herdada do cabeçalho de data)"},
                    "descricao": {"type": "string",
                                  "description": "Linha do lançamento + contraparte "
                                                 "(nome - CPF/CNPJ - banco), se visível"},
                    "valor": {"type": "number",
                              "description": "SEMPRE positivo, na coluna da direita"},
                    "direcao": {"type": "string", "enum": ["credito", "debito"],
                                "description": "credito se está sob 'Total de entradas' "
                                               "da seção; debito se sob 'Total de saídas'"},
                },
                "required": ["data", "descricao", "valor", "direcao"],
            },
        },
    },
    "required": ["transacoes"],
}

# ---------------------------------------------------------------------------
# Prompt de extração (o modelo recebe o PDF + este texto)
# ---------------------------------------------------------------------------
EXTRACTION_PROMPT = """
Você é um motor de extração de dados de extratos bancários brasileiros
(Nubank). O PDF anexado é um extrato com layout de DUAS COLUNAS:
esquerda = descrições e cabeçalhos; direita = valores alinhados visualmente
à linha correspondente.

REGRAS OBRIGATÓRIAS:
1. Cabeçalhos de data ("10 ABR 2026", "01 DE ABRIL DE 2026 a 30 DE ABRIL...")
   definem a data de todos os lançamentos abaixo, até o próximo cabeçalho.
2. Linhas "Total de entradas" e "Total de saídas" (com ou sem data) são
   CABEÇALHOS DE SEÇÃO: registre-os em "totais_secao" e NUNCA como transação.
3. A direção de cada transação é dada pela seção em que ela está:
   sob "Total de entradas" => "credito"; sob "Total de saídas" => "debito".
4. "valor" é o número da coluna direita alinhado à linha da descrição,
   SEMPRE positivo. O "+"/"-" impresso no total da seção NÃO vai no valor.
5. "descricao" = texto do lançamento concatenado com a contraparte
   (nome - CPF/CNPJ mascarado - banco/agência/conta), quando visíveis.
6. IGNORE totalmente: "Saldo inicial", "Saldo final do periodo",
   "Rendimento liquido", o rótulo "VALORES EM R$", rodapés de atendimento,
   números de página e o bloco jurídico final (CNPJ das instituições).
7. NÃO invente lançamentos. Se uma linha não tiver valor visível, ainda
   assim extraia-a com o valor que estiver alinhado a ela na coluna direita;
   se realmente não existir valor, descarte a linha (não chute).
8. Datas de saída em formato ISO "yyyy-mm-dd".
9. Responda APENAS o JSON do schema, sem texto extra.
"""


def _call_gemini_raw(pdf_bytes: bytes) -> str:
    """Chama a API com o PDF nativo e retorna o texto JSON cru."""
    key = get_api_key()
    if not key:
        raise GeminiExtractionError(
            "GEMINI_API_KEY não configurada (.env ou variável de ambiente)."
        )
    try:
        from google import genai
    except ImportError as e:
        raise GeminiExtractionError(
            "Pacote google-genai não instalado. Rode: pip install google-genai"
        ) from e

    try:
        client = genai.Client(api_key=key)
        part = genai.types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")
        config = genai.types.GenerateContentConfig(
            temperature=0.0,
            response_mime_type="application/json",
            response_schema=EXTRACTION_SCHEMA,
        )
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[part, EXTRACTION_PROMPT],
            config=config,
        )
        return response.text
    except GeminiExtractionError:
        raise
    except Exception as e:
        # Quota, rede, modelo indisponível etc. — o app faz fallback local.
        raise GeminiExtractionError(f"Falha na chamada Gemini: {e}") from e


def _parse_iso_date(value: str):
    """Converte 'yyyy-mm-dd' (preferido) com fallback dayfirst p/ dd/mm/yyyy."""
    value = (value or "").strip()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return date_parser.parse(value, dayfirst=True).date()


def validate_gemini_totals(data: Dict[str, Any],
                           tolerance: float = TOLERANCIA_SOMATORIO
                           ) -> List[Dict[str, Any]]:
    """
    Gabarito do banco: soma as transações por (data, tipo) e compara com
    "totais_secao". Retorna a lista de divergências (lista vazia = íntegro).
    """
    sums: Dict[Tuple[str, str], float] = {}
    for row in data.get("transacoes", []):
        tipo = "entradas" if (row.get("direcao") or "") == "credito" else "saidas"
        key = (row.get("data"), tipo)
        try:
            sums[key] = sums.get(key, 0.0) + float(row.get("valor", 0.0))
        except (TypeError, ValueError):
            continue

    mismatches: List[Dict[str, Any]] = []
    for sec in data.get("totais_secao", []):
        key = (sec.get("data"), sec.get("tipo"))
        try:
            expected = float(sec.get("total", 0.0))
        except (TypeError, ValueError):
            continue
        got = sums.get(key, 0.0)
        if abs(expected - got) > tolerance:
            mismatches.append({
                "data": sec.get("data"),
                "tipo": sec.get("tipo"),
                "esperado": expected,
                "apurado": got,
                "diferenca": round(expected - got, 2),
            })
    return mismatches


def gemini_data_to_transactions(data: Dict[str, Any],
                                source_name: str) -> List[Transaction]:
    """
    Converte o JSON do Gemini para List[Transaction] — o mesmo dataclass do
    fluxo local — para que review/rules/calculator/relatório não mudem.
    """
    txs: List[Transaction] = []
    for row in data.get("transacoes", []):
        try:
            d = _parse_iso_date(row.get("data", ""))
            valor = abs(float(row.get("valor", 0.0)))
        except (ValueError, TypeError) as e:
            logger.warning("Gemini: linha inválida descartada (%s): %s", e, row)
            continue
        is_credit = (row.get("direcao") or "").lower() == "credito"
        tx = Transaction(
            date=d,
            description=(row.get("descricao") or "Lançamento").strip(),
            amount=valor if is_credit else -valor,
            is_credit=is_credit,
            bank="nubank",
            source_file=source_name,
            needs_review=False,
        )
        # Rastreabilidade (lido via getattr no report_generator).
        tx.extraction_source = "gemini"
        txs.append(tx)
    txs.sort(key=lambda t: t.date)
    return txs


def extract_transactions_via_gemini(
    pdf_bytes: bytes,
    source_name: str,
) -> Tuple[List[Transaction], Dict[str, Any], List[Dict[str, Any]]]:
    """
    Pipeline completo: PDF -> Gemini -> JSON validado -> List[Transaction].

    Returns:
        (transacoes, json_bruto, divergencias_de_somatorio)
    Raises:
        GeminiExtractionError: qualquer falha controlada (o app faz fallback).
    """
    raw = _call_gemini_raw(pdf_bytes)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise GeminiExtractionError(f"Gemini retornou JSON inválido: {e}") from e

    mismatches = validate_gemini_totals(data)
    if mismatches:
        logger.warning(
            "Gemini: %d seção(ões) com somatório divergente em %s: %s",
            len(mismatches), source_name, mismatches,
        )
    else:
        logger.info("Gemini: somatórios de todas as seções conferem em %s.",
                    source_name)

    txs = gemini_data_to_transactions(data, source_name)
    logger.info("Gemini: %d transações extraídas de %s.", len(txs), source_name)
    return txs, data, mismatches