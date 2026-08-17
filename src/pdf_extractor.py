"""
Extração de texto de PDFs com fallback em camadas e otimizações de performance.

Arquitetura:
- Detecção inteligente de texto vs imagem (evita OCR desnecessário)
- Prioridade: fitz (PyMuPDF) > pdfplumber > OCR
- Gate de legibilidade para detectar texto corrompido
- Cache com @st.cache_data para evitar reprocessamento
- DPI adaptativo para OCR (150 → 200 se qualidade baixa)
"""
import io
import os
import re
import shutil
import logging
import unicodedata
from typing import List, Dict, Any, Optional, Tuple
import hashlib

import pdfplumber
import fitz  # PyMuPDF
from PIL import Image

# Importação condicional do Streamlit para cache
try:
    import streamlit as st
    STREAMLIT_AVAILABLE = True
except ImportError:
    STREAMLIT_AVAILABLE = False

# Configuração de logging para diagnóstico sem poluir a interface do Streamlit
logger = logging.getLogger(__name__)

DEBUG_DIR = "logs"

# ---------------------------------------------------------------------------
# GATE 1 — marcadores "(cid:" (específico do pdfplumber)
# ---------------------------------------------------------------------------
# Quando o pdfplumber não consegue mapear um glifo para Unicode, ele emite
# "(cid:N)" no texto. Densidade alta desses marcadores = fonte sem CMap =
# camada textual inútil.
CID_RATIO_THRESHOLD = 0.005

# ---------------------------------------------------------------------------
# GATE 2 — proporção de vogais
# ---------------------------------------------------------------------------
# Português real tem ~40-46% de vogais entre as letras. Texto fruto de cifra
# de substituição (fonte sem ToUnicode) ficou, nas amostras reais observadas,
# em 20-24%. Faixa conservadora para aceitar texto como natural:
VOWEL_RATIO_MIN = 0.30
VOWEL_RATIO_MAX = 0.55

# ---------------------------------------------------------------------------
# GATE 3 — palavras reais do português
# ---------------------------------------------------------------------------
# POR QUE ESTA CHECAGEM SUBSTITUIU A ANTIGA REGEX DE DATA/R$/SALDO:
# A heurística anterior tratava a presença de datas (dd/mm/aaaa), "R$" e
# "saldo" como sinal POSITIVO de legibilidade. Isso causava falso positivo
# porque, na corrupção de fonte sem ToUnicode, apenas as LETRAS são
# embaralhadas — dígitos, "/" e "R$" sobrevivem intactos à cifra de
# substituição. Um PDF 100% ilegível ainda contém dezenas de datas e valores
# "corretos", enganando a checagem antiga.
# Palavras reais do português (match exato, sem acento) são estatisticamente
# impossíveis de surgir por acaso em texto embaralhado: este é o sinal
# confiável de legibilidade.
COMMON_PT_WORDS = {
    "de ",  "da ",  "do ",  "das ",  "dos ",  "para ",  "por ",  "com ",  "sem ",  "nos ",  "nas ",
    "conta ",  "valor ",  "valores ",  "data ",  "datas ",  "saldo ",  "banco ",
    "pagamento ",  "pagamentos ",  "transferencia ",  "transferido ",  "recebido ",
    "recebidos ",  "enviado ",  "enviados ",  "pix ",  "boleto ",  "boletos ",  "cartao ",
    "compra ",  "compras ",  "debito ",  "credito ",  "extrato ",  "movimentacao ",
    "movimentacoes ",  "titular ",  "agencia ",  "documento ",  "referente ",
    "descricao ",  "lancamento ",  "lancamentos ",  "periodo ",  "historico ",
    "disponivel ",  "total ",  "entrada ",  "entradas ",  "saida ",
}

# Calibração conservadora (documentação do raciocínio):
# - Em texto bancário REAL, as palavras acima representam tipicamente 10-20%
#   dos tokens alfabéticos; em texto embaralhado, ~0%.
# - REAL_WORD_RATIO_MIN = 5%: metade do piso típico de texto real, para
#   tolerar OCR ruim ou extratos com vocabulário atípico.
# - MIN_REAL_WORD_HITS = 20: exige "algumas dezenas" de ocorrências absolutas,
#   impedindo que uma página curta com 2 ou 3 acertos casuais passe no gate.
# - MIN_ALPHA_TOKENS = 100: piso amostral; abaixo disso a razão não é
#   estatisticamente confiável e o texto é tratado como corrompido (força o
#   fallback para a próxima camada, que é o comportamento seguro).
REAL_WORD_RATIO_MIN = 0.05
MIN_REAL_WORD_HITS = 20
MIN_ALPHA_TOKENS = 100

# Tamanho mínimo de token analisado.
# NOTA TÉCNICA (desvio consciente da sugestão de 3+): mantivemos 2+ porque os
# artigos/preposições de 2 letras ("de", "da", "do") são os tokens MAIS
# frequentes do português real e jamais aparecem em cifra de substituição —
# descartá-los jogaria fora o sinal mais forte de legibilidade.
MIN_TOKEN_LEN = 2

# Caminho padrão do Tesseract no Windows (instalador UB-Mannheim via winget)
TESSERACT_WINDOWS_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


def _dump_debug_text(source_name: str, pages_text: List[str], origin: str) -> None:
    """
    Salva o texto extraído em logs/debug_extracao_<nome>.txt para diagnóstico.
    Nunca quebra o fluxo principal: qualquer erro aqui é apenas logado.
    """
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        safe_name = "".join(c for c in source_name if c.isalnum() or c in ".-_")
        path = os.path.join(DEBUG_DIR, f"debug_extracao_{safe_name}.txt")
        
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"ORIGEM DA EXTRACAO: {origin}\n")
            f.write(f"ARQUIVO: {source_name}\n")
            f.write("=" * 60 + "\n")
            for idx, page in enumerate(pages_text, 1):
                f.write(f"\n----- PÁGINA {idx} -----\n")
                f.write(page if page and page.strip() else "<SEM TEXTO>")
                f.write("\n")
        logger.info(f"Debug de extração salvo em {path}")
    except Exception as e:
        logger.warning(f"Falha ao salvar debug de extração: {e}")


def _normalize_text(text: str) -> str:
    """Remove acentos e baixa caixa para análise estatística uniforme."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if unicodedata.category(c) != "Mn").lower()


def _vowel_ratio(text: str) -> float:
    """Proporção de vogais entre as letras do texto (0.0 a 1.0)."""
    letters = [c for c in _normalize_text(text) if c.isalpha()]
    if not letters:
        return 0.0
    vowels = sum(1 for c in letters if c in "aeiou")
    return vowels / len(letters)


def _real_word_stats(text: str) -> Tuple[int, int]:
    """
    Retorna (acertos, total): quantos tokens batem exatamente com
    COMMON_PT_WORDS versus quantos tokens alfabéticos foram analisados.
    """
    tokens = [
        t for t in re.findall(r"[a-z]+", _normalize_text(text))
        if len(t) >= MIN_TOKEN_LEN
    ]
    hits = sum(1 for t in tokens if t in COMMON_PT_WORDS)
    return hits, len(tokens)


def _looks_like_garbled_text(text: str) -> Tuple[bool, str]:
    """
    Heurística agnóstica de biblioteca para detectar texto embaralhado
    (mojibake de fonte sem ToUnicode).
    
    Regra de decisão: o texto SÓ é aceito como legível se AMBOS os sinais
    indicarem texto natural:
      (a) proporção de vogais dentro da faixa esperada, E
      (b) razão de palavras reais do português acima dos limites calibrados.
    
    Se QUALQUER um dos dois falhar, o texto é considerado corrompido.
    
    Retorna (eh_garbled, detalhe) com o detalhe indicando exatamente qual
    sinal aprovou/reprovou, para facilitar diagnóstico futuro.
    """
    vowel_ratio = _vowel_ratio(text)
    vowel_ok = VOWEL_RATIO_MIN <= vowel_ratio <= VOWEL_RATIO_MAX
    
    hits, total = _real_word_stats(text)
    if total < MIN_ALPHA_TOKENS:
        words_ok = False
        words_detail = (
            f"palavras reais: amostra insuficiente ({total} tokens < {MIN_ALPHA_TOKENS})"
        )
    else:
        ratio = hits / total
        words_ok = (hits >= MIN_REAL_WORD_HITS) and (ratio >= REAL_WORD_RATIO_MIN)
        words_detail = f"palavras reais {hits}/{total} ({ratio:.2%})"
    
    if vowel_ok and words_ok:
        return False, (
            f"texto aceito: vogais {vowel_ratio:.2%} na faixa esperada E "
            f"{words_detail} acima do limite"
        )
    
    reasons = []
    if not vowel_ok:
        reasons.append(
            f"proporção de vogais {vowel_ratio:.2%} fora da faixa "
            f"[{VOWEL_RATIO_MIN:.2f}, {VOWEL_RATIO_MAX:.2f}]"
        )
    if not words_ok:
        reasons.append(f"{words_detail} abaixo do limite exigido")
    
    return True, "texto corrompido: " + " E ".join(reasons)


def _pages_readable(pages_text: List[str]) -> bool:
    """
    Verifica se a camada textual extraída é confiável, combinando:
    1. Checagem de '(cid:)' (pdfplumber);
    2. Gate duplo vogais + palavras reais (pega o mojibake silencioso do
       PyMuPDF, que NÃO emite '(cid:)').
    """
    total_text = "\n".join(pages_text or [])
    if not total_text.strip():
        return False
    
    # Gate 1: marcadores (cid:) do pdfplumber
    cid_count = total_text.count("(cid:")
    if cid_count > 0:
        ratio = cid_count / max(1, len(total_text))
        if ratio >= CID_RATIO_THRESHOLD:
            logger.warning(
                "Camada textual não confiável (pdfplumber): %d marcadores (cid:) "
                "(densidade %.4f >= limite %.3f).",
                cid_count, ratio, CID_RATIO_THRESHOLD,
            )
            return False
    
    # Gate 2+3: vogais E palavras reais
    garbled, detail = _looks_like_garbled_text(total_text)
    if garbled:
        logger.warning("Gate de legibilidade REPROVOU: %s", detail)
        return False
    
    logger.info("Gate de legibilidade APROVOU: %s", detail)
    return True


def _ensure_tesseract_cmd() -> Optional[str]:
    """
    Localiza o executável do Tesseract, aponta o pytesseract para ele e
    define TESSDATA_PREFIX explicitamente (sempre, mesmo que já exista, para
    garantir consistência entre sessões).
    
    Retorna o diretório tessdata resolvido, ou None se o executável não for
    encontrado.
    """
    exe = shutil.which("tesseract")
    if exe is None and os.path.exists(TESSERACT_WINDOWS_PATH):
        exe = TESSERACT_WINDOWS_PATH
    
    if exe is None:
        logger.error(
            "Executável do Tesseract não encontrado no PATH nem em %s.",
            TESSERACT_WINDOWS_PATH,
        )
        return None
    
    try:
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = exe
    except Exception as e:
        logger.warning("Não foi possível configurar pytesseract.tesseract_cmd: %s", e)
    
    # tessdata normalmente é a subpasta "tessdata" ao lado do executável.
    # Se o executável veio de um shim (ex: chocolatey), tenta o caminho padrão
    # do instalador UB-Mannheim.
    tessdata_dir = os.path.join(os.path.dirname(os.path.abspath(exe)), "tessdata")
    if not os.path.isdir(tessdata_dir):
        win_candidate = os.path.join(
            os.path.dirname(TESSERACT_WINDOWS_PATH), "tessdata"
        )
        if os.path.isdir(win_candidate):
            tessdata_dir = win_candidate
    
    os.environ["TESSDATA_PREFIX"] = tessdata_dir
    logger.info("TESSDATA_PREFIX definido para %s", tessdata_dir)
    return tessdata_dir


def pdf_has_text(file_bytes: bytes) -> bool:
    """
    PERF-3: Detecção rápida de texto vs imagem usando PyMuPDF.
    
    Verifica se o PDF possui camada de texto extraível em pelo menos uma página.
    Esta verificação é feita ANTES de qualquer tentativa de OCR, evitando
    processamento desnecessário em PDFs nativos.
    
    Args:
        file_bytes: Conteúdo do PDF em bytes
        
    Returns:
        True se o PDF tem texto extraível, False caso contrário
    """
    try:
        with fitz.open(stream=file_bytes, filetype="pdf") as doc:
            # Verifica apenas as primeiras 2 páginas para performance
            pages_to_check = min(2, len(doc))
            for page_num in range(pages_to_check):
                page = doc.load_page(page_num)
                text = page.get_text("text").strip()
                if text and len(text) > 50:  # Threshold mínimo para considerar "tem texto"
                    return True
            return False
    except Exception as e:
        logger.warning(f"Erro ao verificar se PDF tem texto: {e}")
        # Em caso de erro, assume que pode ter texto (conservador)
        return True


def _ocr_with_adaptive_dpi(page, lang: str = "por") -> str:
    """
    PERF-4: OCR com DPI adaptativo.
    
    Tenta OCR com DPI 150 primeiro (mais rápido); se qualidade for baixa,
    reprocessa com DPI 200 (mais lento, mas confiável).
    
    Args:
        page: Página do PyMuPDF
        lang: Idioma do OCR ('por' ou 'eng')
        
    Returns:
        Texto extraído via OCR
    """
    import pytesseract
    
    # Tentativa 1: DPI baixo (mais rápido)
    pix = page.get_pixmap(dpi=150)
    image = Image.open(io.BytesIO(pix.tobytes("png")))
    text = pytesseract.image_to_string(image, lang=lang)
    image.close()
    
    # Valida qualidade usando gate de legibilidade
    hits, total = _real_word_stats(text)
    if total >= MIN_ALPHA_TOKENS and (hits / total) >= (REAL_WORD_RATIO_MIN * 0.8):
        logger.info(f"OCR com DPI 150 aceitável ({hits}/{total} palavras)")
        del pix
        return text
    
    # Tentativa 2: DPI alto (mais lento, mas confiável)
    logger.info(f"OCR com DPI 150 insuficiente; retentando com DPI 200")
    pix = page.get_pixmap(dpi=200)
    image = Image.open(io.BytesIO(pix.tobytes("png")))
    text = pytesseract.image_to_string(image, lang=lang)
    image.close()
    del pix
    del image
    
    return text


def _compute_file_hash(file_bytes: bytes) -> str:
    """Computa hash SHA256 do conteúdo do arquivo para cache."""
    return hashlib.sha256(file_bytes).hexdigest()


def _extract_text_from_pdf_impl(file_bytes: bytes, source_name: str) -> List[str]:
    """
    Implementação interna da extração de texto (sem cache).
    
    PERF-1: Inverteu a prioridade para fitz > pdfplumber > OCR.
    
    Args:
        file_bytes: Conteúdo do PDF em bytes
        source_name: Nome do arquivo para logging
        
    Returns:
        Lista de strings (uma por página)
    """
    pages_text: List[str] = []
    
    # PERF-3: Verificação rápida de texto antes de qualquer extração
    has_text = pdf_has_text(file_bytes)
    if not has_text:
        logger.info(f"PDF sem camada de texto detectada. Partindo direto para OCR.")
    
    # Camada 1: PyMuPDF (fitz) - MAIS RÁPIDO para texto puro
    if has_text:
        try:
            with fitz.open(stream=file_bytes, filetype="pdf") as doc:
                if doc.needs_pass:
                    logger.warning("PDF protegido por senha detectado no PyMuPDF.")
                    return []
                pages_text = [page.get_text("text") for page in doc]
            
            if _pages_readable(pages_text):
                _dump_debug_text(source_name, pages_text, "pymupdf")
                logger.info(f"Extração bem-sucedida via PyMuPDF (fitz)")
                return pages_text
            
            logger.warning("PyMuPDF retornou texto corrompido; tentando pdfplumber.")
        except Exception as e:
            logger.warning(f"Falha na extração via PyMuPDF: {e}")
    
    # Camada 2: pdfplumber (fallback para tabelas complexas)
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            if getattr(pdf, "is_encrypted", False):
                logger.warning("PDF protegido por senha detectado.")
                return []
            pages_text = [page.extract_text() or "" for page in pdf.pages]
        
        if _pages_readable(pages_text):
            _dump_debug_text(source_name, pages_text, "pdfplumber")
            logger.info(f"Extração bem-sucedida via pdfplumber")
            return pages_text
        
        logger.warning("pdfplumber retornou texto corrompido; partindo para OCR.")
    except Exception as e:
        logger.warning(f"Falha na extração via pdfplumber: {e}")
    
    # Camada 3: OCR (pytesseract) — lê os glifos renderizados,
    # contornando fontes sem mapa Unicode e PDFs escaneados.
    try:
        import pytesseract
    except ImportError:
        logger.error("pytesseract não instalado. OCR indisponível.")
        return []
    
    tessdata_dir = _ensure_tesseract_cmd()
    if tessdata_dir is None:
        return []
    
    # Verificação explícita do pacote de idioma ANTES de chamar o Tesseract.
    por_traineddata = os.path.join(tessdata_dir, "por.traineddata")
    if os.path.exists(por_traineddata):
        ocr_lang = "por"
    else:
        # Erro legível e específico (não a exceção crua): cita o caminho
        # exato que falta e como resolver.
        logger.error(
            "Pacote de idioma 'Português' do Tesseract NÃO está instalado. "
            "Arquivo esperado: %s. "
            "Solução: baixe por.traineddata em "
            "https://github.com/tesseract-ocr/tessdata/raw/main/por.traineddata "
            "e copie para a pasta indicada. "
            "ÚLTIMO RECURSO: executando OCR com lang='eng' (qualidade REDUZIDA "
            "para texto em português — dígitos, datas e valores seguem confiáveis).",
            por_traineddata,
        )
        eng_traineddata = os.path.join(tessdata_dir, "eng.traineddata")
        if not os.path.exists(eng_traineddata):
            logger.error(
                "Nenhum pacote de idioma disponível em %s (nem 'por', nem 'eng'). "
                "OCR abortado para este arquivo.",
                tessdata_dir,
            )
            return []
        ocr_lang = "eng"
    
    try:
        pages_text = []
        with fitz.open(stream=file_bytes, filetype="pdf") as doc:
            for page_num in range(len(doc)):
                page = doc.load_page(page_num)
                # PERF-4: DPI adaptativo (150 primeiro, 200 se qualidade baixa)
                text = _ocr_with_adaptive_dpi(page, lang=ocr_lang)
                pages_text.append(text)
        
        _dump_debug_text(source_name, pages_text, f"ocr_{ocr_lang}")
        logger.info(f"Extração bem-sucedida via OCR ({ocr_lang})")
        return pages_text
    except Exception as e:
        logger.error(f"Falha crítica na extração via OCR: {e}")
        return []


# PERF-2: Cache com @st.cache_data para evitar reprocessamento
if STREAMLIT_AVAILABLE:
    @st.cache_data
    def extract_text_from_pdf(file_obj: io.BytesIO) -> List[str]:
        """
        Extrai o texto de um PDF com fallback em camadas e gate de legibilidade.
        
        PERF-1: Prioridade invertida para fitz (PyMuPDF) > pdfplumber > OCR.
        PERF-2: Cache com @st.cache_data para evitar reprocessamento.
        PERF-3: Detecção inteligente de texto vs imagem antes do OCR.
        PERF-4: DPI adaptativo para OCR (150 → 200 se qualidade baixa).
        
        Contrato de retorno mantido: List[str] (uma string por página),
        lista vazia se nada funcionar.
        """
        source_name = getattr(file_obj, "name", "desconhecido.pdf")
        file_obj.seek(0)
        file_bytes = file_obj.read()
        
        logger.info(f"Iniciando extração de texto de {source_name}")
        return _extract_text_from_pdf_impl(file_bytes, source_name)
else:
    # Fallback para quando Streamlit não está disponível (ex: testes)
    def extract_text_from_pdf(file_obj: io.BytesIO) -> List[str]:
        """
        Extrai o texto de um PDF com fallback em camadas e gate de legibilidade.
        
        Versão sem cache (para ambientes sem Streamlit).
        """
        source_name = getattr(file_obj, "name", "desconhecido.pdf")
        file_obj.seek(0)
        file_bytes = file_obj.read()
        
        logger.info(f"Iniciando extração de texto de {source_name}")
        return _extract_text_from_pdf_impl(file_bytes, source_name)


def extract_tables_from_pdf(file_obj: io.BytesIO) -> List[List[List[str]]]:
    """
    Extrai tabelas de um PDF usando pdfplumber.
    
    Args:
        file_obj: Objeto de arquivo em memória (BytesIO).
        
    Returns:
        Lista de tabelas. Cada tabela é uma lista de linhas,
        e cada linha é uma lista de strings (células).
    """
    file_obj.seek(0)
    all_tables: List[List[List[str]]] = []
    
    try:
        with pdfplumber.open(file_obj) as pdf:
            for page in pdf.pages:
                try:
                    tables = page.extract_tables()
                    all_tables.extend(tables)
                except Exception as e:
                    logger.warning(f"Erro ao extrair tabela de página específica: {e}")
                    continue
    except Exception as e:
        logger.warning(f"Falha na extração de tabelas: {e}")
    
    return all_tables


def get_pdf_metadata(file_obj: io.BytesIO) -> Dict[str, Any]:
    """
    Extrai metadados básicos do PDF para auto-detecção de titular/instituição.
    
    Args:
        file_obj: Objeto de arquivo em memória (BytesIO).
        
    Returns:
        Dicionário com metadados disponíveis.
    """
    file_obj.seek(0)
    metadata: Dict[str, Any] = {
        "author": None,
        "title": None,
        "subject": None,
        "pages": 0
    }
    
    try:
        with fitz.open(stream=file_obj.read(), filetype="pdf") as doc:
            metadata["pages"] = len(doc)
            meta = doc.metadata
            if meta:
                metadata["author"] = meta.get("author")
                metadata["title"] = meta.get("title")
                metadata["subject"] = meta.get("subject")
    except Exception as e:
        logger.warning(f"Falha ao extrair metadados: {e}")
    
    return metadata