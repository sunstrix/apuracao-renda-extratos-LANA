import io
import os
import re
import shutil
import logging
import unicodedata
from typing import List, Dict, Any

import pdfplumber
import fitz  # PyMuPDF
from PIL import Image

# Configuração de logging para diagnóstico sem poluir a interface do Streamlit
logger = logging.getLogger(__name__)

DEBUG_DIR = "logs"

# Densidade máxima tolerada de marcadores "(cid:" na camada textual.
# Acima disso, a fonte do PDF não tem mapa Unicode confiável e o texto
# extraído é considerado lixo — a extração deve cair para OCR.
CID_RATIO_THRESHOLD = 0.005

# Caminho padrão do Tesseract no Windows (instalador UB-Mannheim via winget)
TESSERACT_WINDOWS_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# Padrões regex para validar se o texto pertence ao domínio de extratos bancários BR.
# Usados para detectar se o texto extraído é legível ou apenas lixo (mojibake).
RE_DATE = re.compile(r'\d{1,2}/\d{1,2}/\d{2,4}')
RE_CURRENCY = re.compile(r'R\$\s?\d', re.IGNORECASE)
RE_BANKING_KEYWORDS = re.compile(
    r'\b(saldo|agencia|ag[eê]ncia|conta|banco|historico|hist[oó]rico|lan[cç]amentos|extrato)\b', 
    re.IGNORECASE
)


def _dump_debug_text(source_name: str, pages_text: List[str], origin: str) -> None:
    """
    Salva o texto extraído em logs/debug_extracao_<nome>.txt para diagnóstico.
    Nunca quebra o fluxo principal: qualquer erro aqui é apenas logado.
    """
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        safe_name = "".join(c for c in source_name if c.isalnum() or c in "._-")
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


def _looks_like_garbled_text(text: str) -> bool:
    """
    Heurística agnóstica de biblioteca para detectar texto embaralhado (mojibake).
    
    POR QUE ISSO É NECESSÁRIO:
    O PyMuPDF (fitz) NÃO emite '(cid:)' quando o PDF não tem mapa ToUnicode; 
    ele mapeia glifos para codepoints Unicode incorretos, gerando uma cifra de 
    substituição (ex: "8OcWSBQbYO" em vez de "Transferência"). A checagem de 
    '(cid:' sozinha falha em detectar esse lixo silencioso.
    
    Esta função analisa a proporção de vogais (PT-BR tem ~40%) e a presença de 
    padrões bancários para identificar esse lixo textual com precisão.
    """
    if not text or not text.strip():
        return True
        
    # Normaliza para remover acentos e facilitar a contagem estatística de vogais
    normalized = unicodedata.normalize('NFKD', text).encode('ASCII', 'ignore').decode('ASCII')
    letters = [c for c in normalized.lower() if c.isalpha()]
    
    # Verificação de padrões de domínio (Extrato Bancário BR)
    has_patterns = bool(
        RE_DATE.search(text) or 
        RE_CURRENCY.search(text) or 
        RE_BANKING_KEYWORDS.search(text)
    )
    
    if len(letters) < 20:
        # Texto muito curto para análise estatística confiável de vogais.
        # Se for curto e não tiver padrões de banco, consideramos suspeito.
        if not has_patterns:
            logger.warning(f"Heurística de garbled: texto muito curto ({len(letters)} letras) e sem padrões bancários.")
            return True
        return False

    vowels = set('aeiou')
    vowel_count = sum(1 for c in letters if c in vowels)
    vowel_ratio = vowel_count / len(letters)
    
    # PT-BR tipicamente tem entre 35% e 45% de vogais em textos corridos. 
    # Textos corrompidos por mapeamento de fonte costumam ter < 20% ou > 60%.
    is_vowel_ratio_ok = 0.25 <= vowel_ratio <= 0.55
    
    if not is_vowel_ratio_ok:
        if not has_patterns:
            logger.warning(
                f"Texto considerado corrompido (mojibake/PyMuPDF): "
                f"proporção de vogais {vowel_ratio:.2%} fora da faixa esperada "
                f"e ausência de padrões bancários (datas/R$/saldo)."
            )
            return True
        else:
            # Tem padrão de banco, mas vogais estranhas. Pode ser uma tabela 
            # muito densa em números/símbolos. Aceitamos, mas logamos para auditoria.
            logger.info(f"Proporção de vogais atípica ({vowel_ratio:.2%}), mas padrões bancários encontrados.")
            
    return False


def _pages_readable(pages_text: List[str]) -> bool:
    """
    Verifica se a camada textual extraída é confiável.
    Combina a checagem de '(cid:)' (pdfplumber) com heurísticas de 
    linguagem natural e domínio (PyMuPDF) para garantir que o pipeline
    caia para OCR quando a camada textual for inútil.
    """
    total_text = "".join(pages_text or [])
    if not total_text.strip():
        return False

    # 1. Checagem clássica de '(cid:)' (Específica do pdfplumber)
    cid_count = total_text.count("(cid:")
    if cid_count > 0:
        ratio = cid_count / max(1, len(total_text))
        if ratio >= CID_RATIO_THRESHOLD:
            logger.warning(
                f"Camada textual não confiável (pdfplumber): {cid_count} marcadores (cid:) "
                f"(densidade {ratio:.4f} >= limite {CID_RATIO_THRESHOLD})."
            )
            return False

    # 2. Checagem de mojibake/cifra de substituição (Agnóstica, pega o fallback do PyMuPDF)
    if _looks_like_garbled_text(total_text):
        return False

    return True


def _ensure_tesseract_cmd() -> None:
    """
    Garante que o pytesseract encontra o binário no Windows,
    mesmo que o instalador não tenha registrado o Tesseract no PATH
    da sessão atual.
    """
    try:
        import pytesseract

        if shutil.which("tesseract") is None and os.path.exists(TESSERACT_WINDOWS_PATH):
            pytesseract.pytesseract.tesseract_cmd = TESSERACT_WINDOWS_PATH
            logger.info(f"Apontando pytesseract para {TESSERACT_WINDOWS_PATH}")
    except Exception as e:
        logger.warning(f"Não foi possível ajustar o caminho do Tesseract: {e}")


def extract_text_from_pdf(file_obj: io.BytesIO) -> List[str]:
    """
    Extrai o texto de um PDF com fallback em camadas e gate de legibilidade:
    1. pdfplumber  (PDFs nativos com camada textual íntegra)
    2. PyMuPDF     (fallback rápido, mesmo gate de legibilidade)
    3. OCR         (PDFs escaneados OU com fonte sem mapa Unicode — cids/mojibake)

    Args:
        file_obj: Objeto de arquivo em memória (BytesIO/UploadedFile) do Streamlit.

    Returns:
        Lista de strings, uma por página. Lista vazia se nada funcionar.
    """
    source_name = getattr(file_obj, "name", "desconhecido.pdf")
    file_obj.seek(0)
    pages_text: List[str] = []

    # Camada 1: pdfplumber
    try:
        with pdfplumber.open(file_obj) as pdf:
            if getattr(pdf, "is_encrypted", False):
                logger.warning("PDF protegido por senha detectado.")
                return []

            for page in pdf.pages:
                pages_text.append(page.extract_text() or "")

        if _pages_readable(pages_text):
            _dump_debug_text(source_name, pages_text, "pdfplumber")
            return pages_text

        logger.warning("pdfplumber retornou texto corrompido; tentando PyMuPDF.")

    except Exception as e:
        logger.warning(f"Falha na extração via pdfplumber: {e}")

    # Camada 2: PyMuPDF (fitz)
    try:
        file_obj.seek(0)
        with fitz.open(stream=file_obj.read(), filetype="pdf") as doc:
            if doc.needs_pass:
                logger.warning("PDF protegido por senha detectado no PyMuPDF.")
                return []

            pages_text = [page.get_text("text") for page in doc]

        if _pages_readable(pages_text):
            _dump_debug_text(source_name, pages_text, "pymupdf")
            return pages_text

        logger.warning("PyMuPDF retornou texto corrompido (mojibake); partindo para OCR.")

    except Exception as e:
        logger.warning(f"Falha na extração via PyMuPDF: {e}")

    # Camada 3: OCR (pytesseract) — lê os glifos renderizados,
    # contornando fontes sem mapa Unicode e PDFs escaneados.
    try:
        import pytesseract

        _ensure_tesseract_cmd()

        file_obj.seek(0)
        pages_text = []
        with fitz.open(stream=file_obj.read(), filetype="pdf") as doc:
            for page_num in range(len(doc)):
                page = doc.load_page(page_num)
                # DPI 200: equilíbrio entre qualidade de OCR e uso de memória
                pix = page.get_pixmap(dpi=200)

                image = Image.open(io.BytesIO(pix.tobytes("png")))
                text = pytesseract.image_to_string(image, lang="por")
                pages_text.append(text)

                # Liberação explícita de memória (crítico para evitar OOM)
                image.close()
                del pix
                del image

        _dump_debug_text(source_name, pages_text, "ocr")
        return pages_text

    except ImportError:
        logger.error("pytesseract não instalado. OCR indisponível.")
        return []
    except Exception as e:
        logger.error(f"Falha crítica na extração via OCR: {e}")
        return []


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