import io
import os
import logging
from typing import List, Dict, Any

import pdfplumber
import fitz  # PyMuPDF
from PIL import Image

# Configuração de logging para diagnóstico sem poluir a interface do Streamlit
logger = logging.getLogger(__name__)

DEBUG_DIR = "logs"


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


def extract_text_from_pdf(file_obj: io.BytesIO) -> List[str]:
    """
    Extrai o texto de um PDF utilizando uma estratégia de fallback em camadas:
    1. pdfplumber (ideal para PDFs nativos com tabelas)
    2. PyMuPDF (fitz) como fallback rápido
    3. OCR (pytesseract) como último recurso para PDFs escaneados

    Args:
        file_obj: Objeto de arquivo em memória (BytesIO/UploadedFile) do Streamlit.

    Returns:
        Lista de strings, onde cada elemento representa o texto de uma página.
        Retorna lista vazia se nenhuma extração for bem-sucedida.
    """
    source_name = getattr(file_obj, "name", "desconhecido.pdf")
    file_obj.seek(0)
    pages_text: List[str] = []

    # Camada 1: pdfplumber
    try:
        with pdfplumber.open(file_obj) as pdf:
            # getattr seguro: versões antigas/novas do pdfplumber podem não
            # expor o atributo de criptografia da mesma forma
            if getattr(pdf, "is_encrypted", False):
                logger.warning("PDF protegido por senha detectado.")
                return []

            for page in pdf.pages:
                text = page.extract_text() or ""
                pages_text.append(text)

        if any(text.strip() for text in pages_text):
            _dump_debug_text(source_name, pages_text, "pdfplumber")
            return pages_text

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

        if any(text.strip() for text in pages_text):
            _dump_debug_text(source_name, pages_text, "pymupdf")
            return pages_text

    except Exception as e:
        logger.warning(f"Falha na extração via PyMuPDF: {e}")

    # Camada 3: OCR (pytesseract) — último recurso, página por página
    try:
        import pytesseract

        file_obj.seek(0)
        with fitz.open(stream=file_obj.read(), filetype="pdf") as doc:
            pages_text = []
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