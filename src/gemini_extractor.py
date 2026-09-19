"""
Extração de transações bancárias via Gemini API (Google).
Papel na arquitetura (rodada Gemini):
Recebe os BYTES do PDF e envia ao Gemini 3.6 Flash com input nativo de
PDF (sem OCR local): o modelo lê o layout como um humano;
Exige saída JSON estrita (response_schema): titular, totais de seção
("Total de entradas/saídas") e lista de transações;
Valida a extração contra o GABARITO do próprio banco: a soma das
transações de cada seção deve bater no total impresso da seção
(tolerância R$ 0,01) — divergência vira warning estruturado;
Converte o JSON para List[Transaction] (mesmo dataclass do
transaction_parser), de modo que o fluxo atual (revisão manual,
rules_engine, income_calculator, report_generator) consuma a tabela
SEM nenhuma mudança estrutural;
Rastreabilidade: cada Transaction recebe o atributo dinâmico
extraction_source="gemini" (lido via getattr no report_generator).
Segurança/privacidade:
A chave vem SOMENTE de GEMINI_API_KEY no .env (python-dotenv) ou de
variável de ambiente (Streamlit Cloud: secrets);
O PDF sai da máquina do usuário → o app.py exibirá checkbox de
consentimento explícito antes de chamar este módulo (próxima rodada);
Falhas (quota, rede, schema) lançam GeminiExtractionError; o app.py
faz fallback para o pipeline local determinístico.
Resiliência de plataforma (rodada atual):
F1: default do modelo atualizado para gemini-3.6-flash;
F5: cadeia de fallback MODEL_FALLBACK_CHAIN — o modelo configurado é
tentado primeiro e, SOMENTE em 404 NOT_FOUND de modelo, o próximo da
cadeia é usado automaticamente (protege da próxima depreciação);
F4: AFC (automatic function calling) desabilitado no generate_content
(não usamos tools; elimina o warning do google_genai.models).
Limites do free tier (Flash): um batch de 3 PDFs = 3 chamadas. PDFs contam
~258 tokens/página (34 páginas ≈ 9k tokens), muito abaixo do teto.

RODADA ATUAL (CORREÇÕES CRÍTICAS):
- Prompt tornado banco-agnóstico: removida lista fixa de 8 bancos,
  permitindo extração robusta de QUALQUER banco brasileiro (incluindo
  cooperativas como Sicoob/Sicredi, digitais como Will Bank/Neon/Mercado
  Pago, BTG+, PagBank, Original, etc.).
- Retry com backoff exponencial para erros 429 (quota/rate-limit):
  até 3 tentativas com delays de 2s, 4s, 8s antes de falhar e acionar
  o fallback local. Erros 404 continuam avançando na cadeia de modelos.
- Migração de float para Decimal em gemini_data_to_transactions() e
  validate_gemini_totals(), garantindo consistência com o dataclass
  Transaction (que agora usa Decimal) e eliminando erros de arredondamento
  IEEE 754 na validação de somatórios.
"""
import json
import logging
import os
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
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

# F1: gemini-3.6-flash é o modelo default atual e estável.
# O .env/secrets ainda pode sobrescrever via GEMINI_MODEL.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

TOLERANCIA_SOMATORIO = Decimal('0.01')

# F5 — cadeia de resiliência a depreciação de modelo (ordem = prioridade).
# Se o modelo configurado for removido pela Google, o próximo da cadeia é
# tentado automaticamente (apenas em 404 NOT_FOUND de modelo).
MODEL_FALLBACK_CHAIN = ("gemini-3.6-flash", "gemini-3.5-flash-lite")

# Configuração de retry para erros 429 (quota/rate-limit)
MAX_RETRIES_429 = 3
INITIAL_BACKOFF_SECONDS = 2.0

class GeminiExtractionError(Exception):
    """Falha controlada da extração via Gemini (o app faz fallback local)."""

def get_api_key() -> Optional[str]:
    """Chave vinda exclusivamente do ambiente (.env / secrets)."""
    return (os.getenv("GEMINI_API_KEY") or "").strip() or None

def gemini_available() -> bool:
    """True se há chave configurada (não testa quota/rede)."""
    return get_api_key() is not None

def _model_candidates() -> List[str]:
    """F5: modelo configurado primeiro, depois a cadeia (sem duplicados)."""
    candidates = [GEMINI_MODEL]
    for model in MODEL_FALLBACK_CHAIN:
        if model not in candidates:
            candidates.append(model)
    return candidates

def _is_model_not_found(error: Exception) -> bool:
    """F5: detecta 404 NOT_FOUND 'modelo removido' na exceção do SDK."""
    text = str(error)
    return "404" in text and ("NOT_FOUND" in text or "no longer available" in text)

def _is_rate_limit_error(error: Exception) -> bool:
    """Detecta erro 429 (quota/rate-limit) na exceção do SDK."""
    text = str(error)
    return "429" in text or "RESOURCE_EXHAUSTED" in text or "quota" in text.lower()

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
                    "data": {
                        "type": "string",
                        "description": "Data do cabeçalho da seção, ISO yyyy-mm-dd"
                    },
                    "tipo": {
                        "type": "string",
                        "enum": ["entradas", "saidas"]
                    },
                    "total": {
                        "type": "number",
                        "description": "Valor absoluto impresso no cabeçalho"
                    },
                },
                "required": ["data", "tipo", "total"],
            },
        },
        "transacoes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "data": {
                        "type": "string",
                        "description": "ISO yyyy-mm-dd (herdada do cabeçalho de data)"
                    },
                    "descricao": {
                        "type": "string",
                        "description": "Linha do lançamento + contraparte (nome - CPF/CNPJ - banco), se visível"
                    },
                    "valor": {
                        "type": "number",
                        "description": "SEMPRE positivo, na coluna da direita"
                    },
                    "direcao": {
                        "type": "string",
                        "enum": ["credito", "debito"],
                        "description": "credito se está sob 'Total de entradas' da seção; debito se sob 'Total de saídas'"
                    },
                },
                "required": ["data", "descricao", "valor", "direcao"],
            },
        },
    },
    "required": ["transacoes"],
}

# ---------------------------------------------------------------------------
# Prompt de extração (BANCO-AGNÓSTICO — suporta qualquer banco brasileiro)
# ---------------------------------------------------------------------------
EXTRACTION_PROMPT = """
Você é um motor de extração de dados de extratos bancários brasileiros.
O PDF anexado é um extrato bancário de QUALQUER instituição financeira brasileira
(bancos tradicionais, bancos digitais, cooperativas de crédito, fintechs, etc.).

INSTRUÇÕES GERAIS:
1. Identifique automaticamente o banco/instituição a partir do cabeçalho, rodapé
   ou marcas d'água do documento (ex.: "Nubank", "C6 Bank", "Sicoob", "Sicredi",
   "Will Bank", "Neon", "Mercado Pago", "BTG+", "PagBank", "Original", etc.).
   Se o nome da instituição estiver visível, registre-o no campo "titular" ou
   use-o como contexto para entender o layout específico do extrato.

2. Adapte-se ao layout específico do banco:
   - Alguns bancos usam seções separadas de "Créditos/Entradas" e "Débitos/Saídas"
     com totais impressos no cabeçalho de cada seção.
   - Outros bancos usam uma única tabela com coluna de sinal (+/-) ou sufixo
     (C/D) para indicar a direção da transação.
   - Outros bancos ainda separam em colunas "Valor Entrada" e "Valor Saída".
   Analise a estrutura do documento e extraia conforme o padrão encontrado.

REGRAS OBRIGATÓRIAS:
1. Cabeçalhos de data ("10 ABR 2026", "01 DE ABRIL DE 2026 a 30 DE ABRIL...",
   "Data", "Período", "01/06/2026 a 30/06/2026") definem a data de todos os
   lançamentos abaixo, até o próximo cabeçalho.

2. Linhas "Total de entradas", "Total de saídas", "Total de Créditos",
   "Total de Débitos", "Saldo do período", "Saldo inicial", "Saldo final"
   (com ou sem data) são CABEÇALHOS DE SEÇÃO ou RESUMOS: registre-os em
   "totais_secao" (apenas os totais de entradas/saídas) e NUNCA como transação.

3. A direção de cada transação é dada por:
   - Seção em que ela está: sob "Total de entradas"/"Créditos" => "credito";
     sob "Total de saídas"/"Débitos" => "debito"
   - OU sinal explícito: "+" ou sufixo "C" => "credito"; "-" ou sufixo "D" => "debito"
   - OU colunas separadas: valor na coluna "Entrada" => "credito"; na coluna "Saída" => "debito"

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
    """
    Chama a API com o PDF nativo e retorna o texto JSON cru.
    F5: tenta os modelos de _model_candidates() em ordem. SOMENTE 404
    NOT_FOUND de modelo avança para o próximo da cadeia; demais erros
    (quota, rede, auth, schema) lançam GeminiExtractionError na hora
    (o app.py faz fallback local).
    
    RODADA ATUAL: Retry com backoff exponencial para erros 429 (quota/rate-limit).
    Até MAX_RETRIES_429 tentativas com delays de INITIAL_BACKOFF_SECONDS * 2^retry.
    """
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
    except Exception as e:
        raise GeminiExtractionError(f"Falha ao criar client Gemini: {e}") from e

    part = genai.types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")
    
    # F4: desabilita AFC (não usamos tools neste fluxo; elimina o warning
    # "Direct use of automatic function calling... is not recommended").
    config_kwargs: Dict[str, Any] = dict(
        temperature=0.0,
        response_mime_type="application/json",
        response_schema=EXTRACTION_SCHEMA,
    )
    try:
        config_kwargs["automatic_function_calling"] = (
            genai.types.AutomaticFunctionCallingConfig(disable=True)
        )
    except Exception:
        pass  # SDK antigo sem o tipo: mantém comportamento padrão

    config = genai.types.GenerateContentConfig(**config_kwargs)
    candidates = _model_candidates()
    last_error: Optional[Exception] = None

    for idx, model in enumerate(candidates):
        # RODADA ATUAL: Retry com backoff exponencial para erros 429
        for retry in range(MAX_RETRIES_429):
            try:
                logger.info("Gemini: chamando modelo %s (%d/%d)...",
                            model, idx + 1, len(candidates))
                response = client.models.generate_content(
                    model=model,
                    contents=[part, EXTRACTION_PROMPT],
                    config=config,
                )
                if model != GEMINI_MODEL:
                    logger.warning(
                        "Gemini: modelo configurado (%s) indisponível; "
                        "fallback automático usado: %s.", GEMINI_MODEL, model,
                    )
                return response.text
            except Exception as e:
                last_error = e
                
                # Erro 429 (quota/rate-limit): retry com backoff exponencial
                if _is_rate_limit_error(e) and retry < MAX_RETRIES_429 - 1:
                    delay = INITIAL_BACKOFF_SECONDS * (2 ** retry)
                    logger.warning(
                        "Gemini: erro 429 (quota/rate-limit) no modelo %s; "
                        "tentando novamente em %.1f segundos (retry %d/%d).",
                        model, delay, retry + 1, MAX_RETRIES_429,
                    )
                    time.sleep(delay)
                    continue
                
                # Erro 404 (modelo removido): avança para o próximo da cadeia
                if _is_model_not_found(e) and idx + 1 < len(candidates):
                    logger.warning(
                        "Gemini: modelo %s removido/indisponível (404 NOT_FOUND); "
                        "tentando %s.", model, candidates[idx + 1],
                    )
                    break  # Sai do loop de retry e avança para o próximo modelo
                
                # Demais erros (rede, auth, schema): falha imediata
                raise GeminiExtractionError(f"Falha na chamada Gemini: {e}") from e
        
        # Se esgotou os retries de 429, lança o erro
        if retry == MAX_RETRIES_429 - 1:
            raise GeminiExtractionError(
                f"Falha na chamada Gemini após {MAX_RETRIES_429} tentativas "
                f"(erro 429 persistente): {last_error}"
            ) from last_error

    raise GeminiExtractionError(f"Falha na chamada Gemini: {last_error}")

def _parse_iso_date(value: str):
    """Converte 'yyyy-mm-dd' (preferido) com fallback dayfirst p/ dd/mm/yyyy."""
    value = (value or "").strip()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return date_parser.parse(value, dayfirst=True).date()

def validate_gemini_totals(data: Dict[str, Any],
                           tolerance: Decimal = TOLERANCIA_SOMATORIO
                           ) -> List[Dict[str, Any]]:
    """
    Gabarito do banco: soma as transações por (data, tipo) e compara com
    "totais_secao". Retorna a lista de divergências (lista vazia = íntegro).
    
    RODADA ATUAL: Usa Decimal para precisão monetária, consistente com
    Transaction.amount (que agora é Decimal).
    """
    sums: Dict[Tuple[str, str], Decimal] = {}
    for row in data.get("transacoes", []):
        tipo = "entradas" if (row.get("direcao") or "") == "credito" else "saidas"
        key = (row.get("data"), tipo)
        try:
            valor = Decimal(str(row.get("valor", 0.0)))
            sums[key] = sums.get(key, Decimal('0.00')) + valor
        except (TypeError, ValueError, InvalidOperation):
            continue

    mismatches: List[Dict[str, Any]] = []
    for sec in data.get("totais_secao", []):
        key = (sec.get("data"), sec.get("tipo"))
        try:
            expected = Decimal(str(sec.get("total", 0.0)))
        except (TypeError, ValueError, InvalidOperation):
            continue
        got = sums.get(key, Decimal('0.00'))
        if abs(expected - got) > tolerance:
            mismatches.append({
                "data": sec.get("data"),
                "tipo": sec.get("tipo"),
                "esperado": float(expected),
                "apurado": float(got),
                "diferenca": float(expected - got),
            })
    return mismatches

def gemini_data_to_transactions(data: Dict[str, Any],
                                source_name: str,
                                bank: str) -> List[Transaction]:
    """
    Converte o JSON do Gemini para List[Transaction] — o mesmo dataclass do
    fluxo local — para que review/rules/calculator/relatório não mudem.
    
    Args:
        data: JSON extraído do Gemini
        source_name: Nome do arquivo PDF
        bank: Chave do banco detectado (ex: "nubank", "itau", "caixa")
    
    RODADA ATUAL: Usa Decimal para Transaction.amount, consistente com o
    dataclass Transaction (que agora usa Decimal em vez de float).
    """
    txs: List[Transaction] = []
    for row in data.get("transacoes", []):
        try:
            d = _parse_iso_date(row.get("data", ""))
            valor = abs(Decimal(str(row.get("valor", 0.0))))
        except (ValueError, TypeError, InvalidOperation) as e:
            logger.warning("Gemini: linha inválida descartada (%s): %s", e, row)
            continue

        is_credit = (row.get("direcao") or "").lower() == "credito"
        tx = Transaction(
            date=d,
            description=(row.get("descricao") or "Lançamento").strip(),
            amount=valor if is_credit else -valor,
            is_credit=is_credit,
            bank=bank,  # AGORA USA O BANCO DETECTADO (não mais hardcoded)
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
    bank: str,
) -> Tuple[List[Transaction], Dict[str, Any], List[Dict[str, Any]]]:
    """
    Pipeline completo: PDF -> Gemini -> JSON validado -> List[Transaction].
    
    Args:
        pdf_bytes: Bytes do arquivo PDF
        source_name: Nome do arquivo fonte
        bank: Chave do banco detectado (ex: "nubank", "itau", "caixa")
        
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

    txs = gemini_data_to_transactions(data, source_name, bank)
    logger.info("Gemini: %d transações extraídas de %s.", len(txs), source_name)
    return txs, data, mismatches