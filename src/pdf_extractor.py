import io
import logging
from typing import List, Dict, Any, Optional

import pdfplumber
import fitz  # PyMuPDF
from PIL import Image

# Configuração de logging para diagnóstico sem poluir a interface do Streamlit
logger = logging.getLogger(__name__)


def extract_text_from_pdf(file_obj: io.BytesIO) -> List[str]:
    """
    Extrai o texto de um PDF utilizando uma estratégia de fallback em camadas:
    1. pdfplumber (ideal para PDFs nativos com tabelas)
    2. PyMuPDF (fitz) como fallback rápido
    3. OCR (pytesseract) como último recurso para PDFs escaneados

    Args:
        file_obj: Objeto de arquivo em memória (BytesIO) vindo do upload do Streamlit.

    Returns:
        Lista de strings, onde cada elemento representa o texto de uma página.
        Retorna lista vazia se nenhuma extração for bem-sucedida.
    """
    file_obj.seek(0)
    pages_text: List[str] = []

    # Camada 1: pdfplumber
    try:
        with pdfplumber.open(file_obj) as pdf:
            if pdf.is_locked:
                logger.warning("PDF protegido por senha detectado.")
                return []
            
            for page in pdf.pages:
                text = page.extract_text() or ""
                pages_text.append(text)
        
        # Se conseguimos extrair texto significativo, retornamos
        if any(text.strip() for text in pages_text):
            return pages_text
            
    except Exception as e:
        logger.warning(f"Falha na extração via pdfplumber: {e}")
        file_obj.seek(0)

    # Camada 2: PyMuPDF (fitz)
    try:
        with fitz.open(stream=file_obj.read(), filetype="pdf") as doc:
            if doc.needs_pass:
                logger.warning("PDF protegido por senha detectado no PyMuPDF.")
                return []
            
            pages_text = []
            for page in doc:
                text = page.get_text("text")
                pages_text.append(text)
        
        if any(text.strip() for text in pages_text):
            return pages_text
            
    except Exception as e:
        logger.warning(f"Falha na extração via PyMuPDF: {e}")
        file_obj.seek(0)

    # Camada 3: OCR (pytesseract) — Último recurso
    # Processamento página por página para economizar memória (crítico para Streamlit Cloud)
    try:
        import pytesseract
        
        file_obj.seek(0)
        with fitz.open(stream=file_obj.read(), filetype="pdf") as doc:
            pages_text = []
            for page_num in range(len(doc)):
                page = doc.load_page(page_num)
                # Renderiza a página em DPI 200 (equilíbrio entre qualidade e memória)
                pix = page.get_pixmap(dpi=200)
                
                # Converte para PIL Image
                img_data = pix.tobytes("png")
                image = Image.open(io.BytesIO(img_data))
                
                # Executa OCR apenas nesta página
                text = pytesseract.image_to_string(image, lang="por")
                pages_text.append(text)
                
                # Liberação explícita de memória (crítico para evitar OOM)
                image.close()
                del pix
                del img_data
                del image
        
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
        Lista de tabelas por página. Cada tabela é uma lista de linhas,
        e cada linha é uma lista de strings (células).
    """
    file_obj.seek(0)
    all_tables: List[List[List[str]]] = []
    
    try:
        with pdfplumber.open(file_obj) as pdf:
            if pdf.is_locked:
                return []
            
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
    Extrai metadados básicos do PDF para tentar auto-detectar informações
    como nome do titular ou instituição financeira.
    
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