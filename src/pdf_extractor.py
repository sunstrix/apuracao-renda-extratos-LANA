"""
Extração de texto de PDFs com fallback em camadas e otimizações de performance.

Arquitetura:
- Detecção inteligente de texto vs imagem (evita OCR desnecessário)
- Prioridade: fitz (PyMuPDF) > pdfplumber > OCR
- Gate de legibilidade para detectar texto corrompido
- Cache com @st.cache_data para evitar reprocessamento
- DPI adaptativo para OCR (150 → 200 se qualidade baixa)
- A2: resolução de tessdata cross-platform (Windows E Linux/Streamlit Cloud)
  e seleção de idioma via pytesseract.get_languages() quando o diretório
  não é resolvido por filesystem.
- A3: auto-download do por.traineddata (uma única vez) com cache em
  ~/.cache/tessdata — elimina a dependência de instalação manual no Windows
  e iguala o OCR local ao do Cloud (lang='por').
- DEPRECATION PyMuPDF>=1.24: import via "pymupdf" (alias "fitz" mantido por
  compatibilidade com o pin >=1.23.0 do requirements.txt).
"""
import io
import os
import re
import shutil
import logging
import platform
import threading
import unicodedata
from typing import List, Dict, Any, Optional, Tuple
import hashlib

import pdfplumber
# DEPRECATION PyMuPDF>=1.24: o módulo de topo "fitz" foi renomeado para
# "pymupdf". O alias mantém o restante do código intacto; o except cobre
# PyMuPDF 1.23 (pin mínimo do requirements.txt).
try:
    import pymupdf as fitz  # PyMuPDF (nome canônico novo)
except ImportError:  # PyMuPDF legado (< 1.24)
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

# ---------------------------------------------------------------------------
# A2 — candidatos de tessdata no Linux (apt, compilação própria, contêineres)
# ---------------------------------------------------------------------------
# No Windows o layout é resolvido relativo ao executável (ver
# _resolve_tessdata_dir). No Linux, os pacotes apt (tesseract-ocr-por/eng)
# instalam os .traineddata em caminhos versionados que variam por distro;
# por isso a lista de candidatos + fallback via pytesseract.get_languages().
TESSDATA_LINUX_CANDIDATES = (
    "/usr/share/tesseract-ocr/5/tessdata",
    "/usr/share/tesseract-ocr/4.13/tessdata",
    "/usr/share/tesseract-ocr/4.11/tessdata",
    "/usr/share/tesseract-ocr/4/tessdata",
    "/usr/share/tessdata",
    "/usr/local/share/tessdata",
    "/app/tessdata",
)

# ---------------------------------------------------------------------------
# A3 — auto-download de traineddata (cache em ~/.cache/tessdata)
# ---------------------------------------------------------------------------
# No Windows, a instalação padrão do Tesseract traz apenas eng.traineddata,
# forçando OCR lang='eng' com qualidade inferior em português (483 lançamentos
# locais vs 742 no Cloud com 'por'). Para eliminar a dependência de instalação
# manual, o modelo ausente é baixado UMA única vez do repositório oficial
# tessdata e cacheado em ~/.cache/tessdata, que então vira TESSDATA_PREFIX
# (diretório autocontido: por + eng). No Streamlit Cloud o apt (packages.txt)
# já provê 'por' — o download nunca é acionado lá.
TESSDATA_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "tessdata")

TESSDATA_DOWNLOAD_URLS = {
    "por": "https://github.com/tesseract-ocr/tessdata/raw/main/por.traineddata",
    "eng": "https://github.com/tesseract-ocr/tessdata/raw/main/eng.traineddata",
}

# Serializa o download entre chamadas concorrentes (ThreadPoolExecutor do app.py).
_TESSDATA_LOCK = threading.Lock()

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

def _dir_has_traineddata(path: Optional[str]) -> bool:
    """True se o diretório existe e contém ao menos um .traineddata."""
    if not path or not os.path.isdir(path):
        return False
    try:
        return any(name.endswith(".traineddata") for name in os.listdir(path))
    except Exception as e:
        logger.warning("Não foi possível listar %s: %s", path, e)
        return False

def _resolve_tessdata_dir(exe: str) -> Optional[str]:
    """
    A2: resolve o diretório tessdata de forma cross-platform.

    Ordem de candidatos:
    1. TESSDATA_PREFIX já definido e válido;
    2. pasta 'tessdata' ao lado do executável (layout padrão Windows);
    3. candidatos Linux (apt/compilado/contêiner);
    4. None — o chamador então NÃO seta TESSDATA_PREFIX e confia no default
       compilado do Tesseract (caso típico de instalações apt no Linux),
       delegando a detecção de idioma a _pick_ocr_lang().
    """
    candidates: List[str] = []

    env_prefix = os.environ.get("TESSDATA_PREFIX")
    if env_prefix:
        candidates.append(env_prefix)

    candidates.append(
        os.path.join(os.path.dirname(os.path.abspath(exe)), "tessdata")
    )

    if platform.system() == "Windows":
        candidates.append(
            os.path.join(os.path.dirname(TESSERACT_WINDOWS_PATH), "tessdata")
        )
    else:
        candidates.extend(TESSDATA_LINUX_CANDIDATES)

    for cand in candidates:
        if _dir_has_traineddata(cand):
            return cand
    return None

def _traineddata_exists(directory: Optional[str], lang: str) -> bool:
    """A3: True se <directory>/<lang>.traineddata existe no filesystem."""
    if not directory:
        return False
    return os.path.exists(os.path.join(directory, f"{lang}.traineddata"))

def _tessdata_system_candidates(exe: Optional[str]) -> List[str]:
    """A3: diretórios de sistema que podem conter .traineddata (exclui cache)."""
    candidates: List[str] = []
    env_prefix = os.environ.get("TESSDATA_PREFIX")
    if env_prefix:
        candidates.append(env_prefix)
    if exe:
        candidates.append(
            os.path.join(os.path.dirname(os.path.abspath(exe)), "tessdata")
        )
    if platform.system() == "Windows":
        candidates.append(
            os.path.join(os.path.dirname(TESSERACT_WINDOWS_PATH), "tessdata")
        )
    else:
        candidates.extend(TESSDATA_LINUX_CANDIDATES)
    return candidates

def _download_traineddata(lang: str, dest_dir: str) -> bool:
    """
    A3: baixa <lang>.traineddata do repositório oficial tessdata para
    dest_dir, com escrita atômica (.part -> final) para nunca corromper
    um cache existente. Nunca lança exceção: retorna False em qualquer
    falha (rede, HTTP, disco).
    """
    url = TESSDATA_DOWNLOAD_URLS.get(lang)
    if not url:
        return False
    final_path = os.path.join(dest_dir, f"{lang}.traineddata")
    part_path = final_path + ".part"
    try:
        import urllib.request
        os.makedirs(dest_dir, exist_ok=True)
        logger.info("A3: baixando %s.traineddata (uma única vez, ~15 MB)...", lang)
        request = urllib.request.Request(
            url, headers={"User-Agent": "apuracao-renda-extratos-LANA"}
        )
        with urllib.request.urlopen(request, timeout=300) as response:
            with open(part_path, "wb") as out:
                shutil.copyfileobj(response, out)
        os.replace(part_path, final_path)
        logger.info("A3: %s.traineddata cacheado em %s", lang, dest_dir)
        return True
    except Exception as e:
        logger.error("A3: falha ao baixar %s.traineddata de %s: %s", lang, url, e)
        try:
            if os.path.exists(part_path):
                os.remove(part_path)
        except OSError:
            pass
        return False

def _populate_eng_in_cache(exe: Optional[str]) -> None:
    """
    A3: garante eng.traineddata no cache — copia de um diretório de sistema
    quando disponível (evita download desnecessário), senão baixa.
    Falha aqui NÃO é fatal: com 'por' no cache o OCR já funciona; o 'eng'
    só é usado no fallback de último recurso.
    """
    if _traineddata_exists(TESSDATA_CACHE_DIR, "eng"):
        return
    for cand in _tessdata_system_candidates(exe):
        src = os.path.join(cand, "eng.traineddata")
        if os.path.exists(src):
            try:
                os.makedirs(TESSDATA_CACHE_DIR, exist_ok=True)
                shutil.copyfile(
                    src, os.path.join(TESSDATA_CACHE_DIR, "eng.traineddata")
                )
                logger.info("A3: eng.traineddata copiado de %s", cand)
                return
            except Exception as e:
                logger.warning("A3: falha ao copiar eng.traineddata de %s: %s", cand, e)
    _download_traineddata("eng", TESSDATA_CACHE_DIR)

def _ensure_por_traineddata(exe: Optional[str],
                            tessdata_dir: Optional[str]) -> Optional[str]:
    """
    A3: garante disponibilidade de 'por' para o OCR, baixando o modelo para
    o cache quando ausente.

    Estratégia (nunca quebra o fluxo — pior caso retorna o diretório original):
    1. 'por' já presente no diretório resolvido      -> nada a fazer;
    2. sem diretório resolvido, mas Tesseract reporta 'por' (Linux/apt)
                                                      -> nada a fazer;
    3. senão, monta cache autocontido (~/.cache/tessdata):
       - por: baixa se ausente;
       - eng: copia de diretório de sistema se disponível, senão baixa
         (preserva o fallback lang='eng' mesmo com TESSDATA_PREFIX=cache);
       - retorna o cache se 'por' OK; falha de rede retorna o original
         (comportamento anterior: eng com log de erro).

    Thread-safe: ThreadPoolExecutor do app.py pode acionar esta função
    concorrentemente; o lock + double-check evitam download duplicado.
    """
    if _traineddata_exists(tessdata_dir, "por"):
        return tessdata_dir

    if not tessdata_dir:
        try:
            import pytesseract
            if "por" in set(pytesseract.get_languages()):
                return tessdata_dir  # apt/default compilado já provê 'por'
        except Exception:
            pass

    with _TESSDATA_LOCK:
        # Double-check dentro do lock (threads concorrentes).
        if _traineddata_exists(TESSDATA_CACHE_DIR, "por"):
            _populate_eng_in_cache(exe)
            return TESSDATA_CACHE_DIR

        if not _download_traineddata("por", TESSDATA_CACHE_DIR):
            return tessdata_dir  # offline/falha: mantém comportamento original

        _populate_eng_in_cache(exe)
        return TESSDATA_CACHE_DIR

def _ensure_tesseract_cmd() -> Tuple[Optional[str], Optional[str]]:
    """
    Localiza o executável do Tesseract e aponta o pytesseract para ele.

    A2 (correção online): TESSDATA_PREFIX agora é definido SOMENTE se um
    diretório válido for resolvido por _resolve_tessdata_dir(). Antes, o
    código setava o prefixo incondicionalmente para <pasta do exe>/tessdata,
    que no Linux vira /usr/bin/tessdata (inexistente) — fazendo a checagem de
    por.traineddata falhar e abortar o OCR mesmo com Tesseract instalado.

    A3: após resolver o diretório, _ensure_por_traineddata() pode substituí-lo
    pelo cache autocontido (~/.cache/tessdata) quando 'por' estiver ausente
    (caso típico do Windows), baixando o modelo automaticamente.

    Retorna (exe, tessdata_dir); exe=None significa Tesseract ausente.
    """
    exe = shutil.which("tesseract")
    if exe is None and os.path.exists(TESSERACT_WINDOWS_PATH):
        exe = TESSERACT_WINDOWS_PATH

    if exe is None:
        logger.error(
            "Executável do Tesseract não encontrado no PATH nem em %s.",
            TESSERACT_WINDOWS_PATH,
        )
        return None, None

    try:
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = exe
    except Exception as e:
        logger.warning("Não foi possível configurar pytesseract.tesseract_cmd: %s", e)

    tessdata_dir = _resolve_tessdata_dir(exe)

    # A3: auto-download do 'por' ausente; pode trocar o diretório pelo cache.
    tessdata_dir = _ensure_por_traineddata(exe, tessdata_dir)

    if tessdata_dir:
        os.environ["TESSDATA_PREFIX"] = tessdata_dir
        logger.info("TESSDATA_PREFIX definido para %s", tessdata_dir)
    else:
        logger.info(
            "Nenhum diretório tessdata explícito encontrado; confiando no "
            "default compilado do Tesseract (comum em instalações apt/Linux). "
            "Idiomas serão consultados via pytesseract.get_languages()."
        )
    return exe, tessdata_dir

def _pick_ocr_lang(tessdata_dir: Optional[str]) -> Optional[str]:
    """
    A2: escolhe o idioma do OCR, preferindo 'por' com fallback 'eng'.

    Com tessdata_dir resolvido, usa filesystem (rápido). Sem diretório
    resolvido, consulta o próprio Tesseract via pytesseract.get_languages()
    — caminho confiável no Linux/apt, onde o default compilado funciona sem
    TESSDATA_PREFIX.

    Retorna None (com log de erro acionável) se nenhum idioma usável.
    """
    if tessdata_dir:
        if os.path.exists(os.path.join(tessdata_dir, "por.traineddata")):
            return "por"
        if os.path.exists(os.path.join(tessdata_dir, "eng.traineddata")):
            logger.error(
                "Pacote de idioma 'Português' do Tesseract NÃO está instalado. "
                "Arquivo esperado: %s. "
                "Solução: baixe por.traineddata em "
                "https://github.com/tesseract-ocr/tessdata/raw/main/por.traineddata "
                "e copie para a pasta indicada. "
                "ÚLTIMO RECURSO: executando OCR com lang='eng' (qualidade REDUZIDA "
                "para texto em português — dígitos, datas e valores seguem confiáveis).",
                os.path.join(tessdata_dir, "por.traineddata"),
            )
            return "eng"

    # Sem diretório resolvido (ou sem por/eng nele): pergunta ao Tesseract.
    try:
        import pytesseract
        langs = set(pytesseract.get_languages())
        if "por" in langs:
            return "por"
        if "eng" in langs:
            logger.error(
                "Pacote de idioma 'por' ausente no Tesseract; usando 'eng' "
                "(qualidade REDUZIDA para português). "
                "Linux: apt install tesseract-ocr-por."
            )
            return "eng"
    except Exception as e:
        logger.warning("Consulta de idiomas do Tesseract falhou: %s", e)

    logger.error(
        "Nenhum pacote de idioma disponível (nem 'por', nem 'eng'). "
        "OCR abortado para este arquivo. "
        "Linux: apt install tesseract-ocr-por tesseract-ocr-eng | "
        "Windows: copie os .traineddata para a pasta tessdata."
    )
    return None

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
    A2: resolução de tessdata cross-platform + seleção de idioma robusta.

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

    # A2: novo contrato — (exe, tessdata_dir); tessdata_dir pode ser None.
    exe, tessdata_dir = _ensure_tesseract_cmd()
    if exe is None:
        return []

    ocr_lang = _pick_ocr_lang(tessdata_dir)
    if ocr_lang is None:
        return []

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
        PERF-4: DPI adaptativo (150 → 200 se qualidade baixa).
        A2: tessdata cross-platform + seleção de idioma robusta.
        A3: auto-download do por.traineddata (cache ~/.cache/tessdata).

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