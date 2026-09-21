"""
Interface principal Streamlit da Apuração de Renda via Extratos PDF.
Fluxo (com revisão humana obrigatória antes da exportação):
upload -> extração (st.status + st.progress, PARALELA via ThreadPoolExecutor)
-> parsing por banco;
expander com TODAS as transações brutas (antes das regras);
tabela editável (st.data_editor) de revisão manual:
Sinal Detectado (Crédito / Débito / Indeterminado);
checkbox "Incluir na apuração" (pré-marcado só p/ créditos automáticos);
coluna "Motivo da exclusão (manual)";
botão "Confirmar revisão e gerar relatório" -> calculate_income_metrics()
recebendo manual_exclusions / manual_inclusions;
downloads PDF / Excel / CSV habilitados somente após a confirmação.
RODADA GEMINI (integração híbrida):
Toggle "Extração via Gemini (nuvem)" no sidebar — habilitado apenas com
GEMINI_API_KEY configurada (.env / secrets do Cloud);
Checkbox de CONSENTIMENTO explícito (LGPD): sem ele, nada sai da máquina;
Roteamento por arquivo: Gemini primeiro (se autorizado); erro da API ou
zero transações => fallback AUTOMÁTICO para o pipeline local;
Divergências do gabarito (somatórios do banco vs extração IA) viram
banner de aviso após o processamento;
Rastreabilidade: transações com extraction_source="gemini" recebem selo
🤖 na prévia de resultados.
RODADA ATUAL (CORREÇÕES CRÍTICAS):
BUG 1 FIX: render_kpi_card substituído por st.metric nativo (estilizado via
CSS externo) — elimina definitivamente o problema de HTML cru renderizado.
BUG 3 FIX: try/except explícito na geração do PDF + brl() reescrito para
suportar Decimal nativamente (sem perda de precisão).
Bug crítico da deduplicação: movida de calculate_income_metrics() para
app.py, ANTES da revisão manual. Assim os índices da tabela de revisão
correspondem aos índices reais da lista deduplicada, evitando que
decisões do operador recaiam sobre transações erradas.
Regressão regex: HOLDER_EXCLUSION_KEYWORDS revertido de 'NUs' para 'NU\s'
(espaço após NU), restaurando filtro correto contra "NU PAGAMENTOS S.A.".
Warnings de depreciação: use_container_width substituído por width="stretch"
(compatível com Streamlit >= 1.62).
Autoria: crédito "Desenvolvido por Lana Gleizi Vieira Paes" adicionado
na hero section (topo da página), mantido também no rodapé.
CORREÇÃO: Emojis removidos de f-strings para evitar SyntaxError.
"""
import logging
import os
import re
import streamlit as st
import pandas as pd
from decimal import Decimal, InvalidOperation
from concurrent.futures import ThreadPoolExecutor, as_completed
from src.pdf_extractor import extract_text_from_pdf
from src.bank_detector import detect_bank, bank_display_name
from src.transaction_parser import parse_statement, deduplicate_transactions
from src.income_calculator import calculate_income_metrics
from src.report_generator import generate_report, generate_excel, generate_csv
from src.gemini_extractor import (
    GeminiExtractionError,
    extract_transactions_via_gemini,
    gemini_available,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

st.set_page_config(
    page_title="Apuração de Renda - Extratos PDF",
    page_icon="💼",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Carregamento de CSS externo (.streamlit/style.css)
# ---------------------------------------------------------------------------
def load_css(file_name: str) -> None:
    """Carrega arquivo CSS do diretório .streamlit/ e injeta via st.markdown."""
    css_path = os.path.join(".streamlit", file_name)
    if os.path.exists(css_path):
        with open(css_path, "r", encoding="utf-8") as f:
            st.markdown(f"<style>{f.read()}</style>", unsafe_allow_html=True)
    else:
        logger.warning("Arquivo CSS não encontrado: %s", css_path)

load_css("style.css")

# ---------------------------------------------------------------------------
# PERF-7: Regex compiladas FORA da função para evitar recompilação a cada chamada
# CORREÇÃO: 'NUs' revertido para 'NU\s' (espaço após NU)
# ---------------------------------------------------------------------------
HOLDER_EXCLUSION_KEYWORDS = re.compile(
    r"(CPF|CNPJ|AGÊNCIA|AGENCIA|CONTA|BANCO|MOVIMENTA|SALDO|EXTRATO|NU\s|VALORES)",
    re.IGNORECASE
)

def brl(value) -> str:
    """
    Formata valor monetário em R$ com separadores pt-BR.
    BUG 3 FIX: Suporta Decimal nativamente (sem conversão para float).
    """
    try:
        if isinstance(value, Decimal):
            v = value
        else:
            v = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        v = Decimal('0.00')
    
    # Formata com 2 casas decimais e separadores pt-BR
    formatted = f"{v:,.2f}"
    return f"R$ {formatted}".replace(",", "X").replace(".", ",").replace("X", ".")

def try_detect_holder_name(text_pages) -> str:
    """
    Detecta o titular no extrato (Nubank/OCR): a linha imediatamente acima
    da linha que contém "CPF" é o nome do titular.
    PERF-7: 2 páginas + regex compilada.
    """
    lines = []
    for page in (text_pages or [])[:2]:
        lines.extend((page or "").splitlines())
    
    for idx, line in enumerate(lines):
        if "CPF" not in line:
            continue
        j = idx - 1
        while j >= 0 and not lines[j].strip():
            j -= 1
        if j < 0:
            continue
        cand = lines[j].strip().replace("\n", " ").replace("\r", " ").strip()
        if (
            2 <= len(cand.split()) <= 6
            and not any(ch.isdigit() for ch in cand)
            and not HOLDER_EXCLUSION_KEYWORDS.search(cand)
            and all(w[0].isalpha() for w in cand.split() if w)
        ):
            return cand
    return ""

def _process_single_pdf(uploaded_file, use_gemini: bool = False):
    """
    Processa um único PDF e retorna os resultados.
    Roteamento (RODADA GEMINI):
    1) se use_gemini: detecta o banco via leitura parcial do texto,
    tenta a API Gemini (PDF nativo, sem OCR local);
    erro controlado ou 0 transações => cai no passo 2;
    2) pipeline local determinístico (PyMuPDF/pdfplumber/OCR + parsers).
    """
    info_gemini = {"used": False, "mismatches": []}
    
    # --- Caminho 1: Gemini (nuvem), somente se autorizado pelo operador ---
    if use_gemini:
        try:
            uploaded_file.seek(0)
            pdf_bytes = uploaded_file.read()
            try:
                pages_preview = extract_text_from_pdf(uploaded_file)
                full_text_preview = "\n".join(pages_preview[:5] if pages_preview else [])
                bank = detect_bank(full_text_preview)
            except Exception:
                bank = "generic"
            
            txs, data, mismatches = extract_transactions_via_gemini(
                pdf_bytes, uploaded_file.name, bank
            )
            if txs:
                holder = (data.get("titular") or "").strip() or None
                info_gemini["used"] = True
                info_gemini["mismatches"] = mismatches
                return (uploaded_file.name, True, txs, bank, holder, None, info_gemini)
            
            logger.warning(
                "Gemini retornou 0 transações para %s; usando fallback local.",
                uploaded_file.name,
            )
        except GeminiExtractionError as ge:
            logger.warning(
                "Gemini indisponível/erro em %s (%s); usando fallback local.",
                uploaded_file.name, ge,
            )
    
    # --- Caminho 2: pipeline local determinístico (fallback garantido) ---
    try:
        uploaded_file.seek(0)
        pages = extract_text_from_pdf(uploaded_file)
        if not pages or not any(p.strip() for p in pages):
            return (uploaded_file.name, False, [], None, None,
                    "Arquivo não pôde ser lido (protegido por senha, corrompido ou sem camada de texto)",
                    info_gemini)
        
        full_text = "\n".join(pages)
        bank = detect_bank(full_text)
        detected_holder = try_detect_holder_name(pages)
        txs = parse_statement(full_text, bank=bank, source_file=uploaded_file.name)
        return (uploaded_file.name, True, txs, bank, detected_holder, None, info_gemini)
    except Exception as e:
        logger.error("Erro ao processar %s: %s", uploaded_file.name, e)
        return (uploaded_file.name, False, [], None, None, str(e), info_gemini)

def build_review_dataframe(raw) -> pd.DataFrame:
    """
    Monta a tabela de revisão manual.
    Pré-marca "Incluir" apenas para créditos automáticos.
    """
    rows = []
    for idx, t in enumerate(raw):
        if t.needs_review:
            sinal, incluir, status = "Indeterminado", False, "Revisão manual obrigatória"
        elif t.is_credit:
            sinal, incluir, status = "Crédito", True, "Automático"
        else:
            sinal, incluir, status = "Débito", False, "Automático (fora da renda)"
        
        rows.append({
            "ID": idx,
            "Data": t.date.strftime("%d/%m/%Y"),
            "Descrição": t.description,
            "Valor": t.amount,
            "Sinal": sinal,
            "Status": status,
            "Incluir na apuração": incluir,
            "Motivo da exclusão (manual)": "",
        })
    return pd.DataFrame(rows)

# ---------------------------------------------------------------------------
# HERO SECTION (Header profissional com logo, título, autoria e badge)
# ---------------------------------------------------------------------------
def render_hero_section(gemini_ok: bool) -> None:
    """Renderiza o cabeçalho hero com logo SVG, título, autoria e badge de status."""
    badge_class = "badge-success" if gemini_ok else "badge-info"
    badge_text = "Gemini Ativo" if gemini_ok else "Modo Local"
    badge_dot = '<span class="badge-dot"></span>' if gemini_ok else ""
    
    st.markdown(
        f"""
        <div class="app-header">
            <div class="app-header-content">
                <div class="app-header-icon" aria-label="Ícone da aplicação">
                    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
                        <path d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" stroke-linecap="round" stroke-linejoin="round"/>
                    </svg>
                </div>
                <div class="app-header-text">
                    <h1>Apuração de Renda via Extratos PDF</h1>
                    <p>Consolidação inteligente, revisão humana e relatório executivo em segundos.</p>
                    <p class="app-header-author">Desenvolvido por Lana Gleizi Vieira Paes</p>
                </div>
            </div>
            <div class="badge {badge_class}" aria-label="Status do modo de extração">
                {badge_dot}
                {badge_text}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

# ---------------------------------------------------------------------------
# FOOTER (Autoria + Links)
# ---------------------------------------------------------------------------
def render_footer() -> None:
    """Renderiza o rodapé com autoria e links para GitHub/README."""
    st.markdown(
        """
        <div class="app-footer">
            <div class="app-footer-content">
                <p class="app-footer-author">Desenvolvido por Lana Gleizi Vieira Paes</p>
                <div class="app-footer-links">
                    <a href="https://github.com/sunstrix/apuracao-renda-extratos-LANA" target="_blank" rel="noopener noreferrer" aria-label="Repositório no GitHub">
                        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                            <path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.8 4 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
                        </svg>
                        GitHub
                    </a>
                    <a href="https://github.com/sunstrix/apuracao-renda-extratos-LANA/blob/main/README.md" target="_blank" rel="noopener noreferrer" aria-label="Documentação">
                        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                            <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"></path>
                            <polyline points="14 2 14 8 20 8"></polyline>
                            <line x1="16" y1="13" x2="8" y2="13"></line>
                            <line x1="16" y1="17" x2="8" y2="17"></line>
                            <polyline points="10 9 9 9 8 9"></polyline>
                        </svg>
                        Documentação
                    </a>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

def main():
    gemini_ok = gemini_available()
    
    # Hero section (com autoria no topo)
    render_hero_section(gemini_ok)
    
    # ------------------------------------------------------------------ #
    # Configuração de extração (RODADA GEMINI): toggle + consentimento
    # ------------------------------------------------------------------ #
    with st.sidebar:
        st.header("Configuração de Extração")
        use_gemini_cfg = st.checkbox(
            "Extração via Gemini (nuvem)",
            value=gemini_ok,
            disabled=not gemini_ok,
            help="Lê o PDF diretamente na API Gemini (sem OCR local). "
                 "Sem GEMINI_API_KEY no .env/secrets, o fluxo local é usado.",
        )
        consent_gemini = False
        if use_gemini_cfg:
            consent_gemini = st.checkbox(
                "Autorizo o envio destes PDFs à API Gemini "
                "(dados financeiros sensíveis).",
                value=False,
            )
            if not consent_gemini:
                st.caption("Sem consentimento, o fluxo local será usado.")
        use_gemini = use_gemini_cfg and consent_gemini
        if not gemini_ok:
            st.caption("Gemini: chave não configurada (.env). Fluxo local ativo.")

    for key, default in (("raw_transactions", None), ("metrics", None),
                         ("detected_holder", ""), ("institutions", None),
                         ("reviewed", False),
                         ("manual_inclusions", set()), ("manual_exclusions", {}),
                         ("duplicates_removed", 0)):
        if key not in st.session_state:
            st.session_state[key] = default

    uploaded_files = st.file_uploader(
        "Selecione os arquivos PDF dos extratos",
        type=["pdf"],
        accept_multiple_files=True,
        help="Aceita múltiplos PDFs de qualquer banco brasileiro. Extratos com períodos sobrepostos serão automaticamente deduplicados.",
    )
    holder_input = st.text_input(
        "Nome do Titular (opcional - será tentada a auto-detecção)",
        value=st.session_state.detected_holder or "",
        help="Informe o nome completo do titular das contas. Se deixado em branco, o sistema tentará detectá-lo automaticamente no extrato.",
    )

    # ------------------------------------------------------------------ #
    # Etapa 1: extração + parsing (com status/progresso e PARALELIZAÇÃO)
    # ------------------------------------------------------------------ #
    if st.button("Processar Extratos", type="primary", disabled=not uploaded_files):
        raw_all = []
        institutions = set()
        detected_holder = holder_input
        gemini_mismatches = []
        gemini_used_any = False
        failed_files = []
        
        with st.status("Processando extratos...", expanded=True) as status:
            progress = st.progress(0.0, text="Iniciando processamento paralelo...")
            total = len(uploaded_files)
            max_workers = min(6, total)
            completed_count = 0
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_file = {
                    executor.submit(_process_single_pdf, uf, use_gemini): uf
                    for uf in uploaded_files
                }
                for future in as_completed(future_to_file):
                    uf = future_to_file[future]
                    try:
                        (file_name, success, txs, bank, holder, error, info_gemini) = future.result()
                        completed_count += 1
                        
                        if info_gemini.get("used"):
                            gemini_used_any = True
                            gemini_mismatches.extend(info_gemini.get("mismatches", []))
                        
                        if success:
                            raw_all.extend(txs)
                            institutions.add(bank_display_name(bank))
                            if not detected_holder and holder:
                                detected_holder = holder
                            # CORREÇÃO: Emoji removido de f-string
                            selo = "\U0001F916 " if info_gemini.get("used") else "\u2705 "  # 🤖 ou ✅
                            status.write(f"{selo}{file_name}: {len(txs)} transações ({bank_display_name(bank)})")
                        else:
                            failed_files.append((file_name, error))
                            # CORREÇÃO: Emoji removido de f-string
                            status.write(f"\u26A0\uFE0F {file_name}: {error}")  # ⚠️
                        
                        progress.progress(
                            completed_count / total,
                            text=f"Processado {completed_count}/{total} arquivos"
                        )
                    except Exception as e:
                        completed_count += 1
                        logger.error("Erro inesperado ao processar %s: %s", uf.name, e)
                        failed_files.append((uf.name, str(e)))
                        # CORREÇÃO: Emoji removido de f-string
                        status.write(f"\u26A0\uFE0F {uf.name}: {e}")  # ⚠️
                        progress.progress(
                            completed_count / total,
                            text=f"Processado {completed_count}/{total} arquivos"
                        )
            
            progress.progress(1.0, text="Processamento concluído!")
            status.update(label="Processamento concluído!", state="complete")
        
        # CORREÇÃO CRÍTICA: Deduplicação ANTES da revisão manual
        # Assim os índices da tabela de revisão correspondem aos índices reais
        # da lista deduplicada, evitando que decisões do operador recaiam
        # sobre transações erradas.
        raw_all, duplicates_removed = deduplicate_transactions(raw_all)
        if duplicates_removed > 0:
            logger.info(
                "Deduplicação: %d transação(ões) duplicada(s) removida(s) antes da revisão.",
                duplicates_removed
            )
        
        # Toast de sucesso/erro
        if raw_all:
            st.toast(f"\u2705 {len(raw_all)} transações extraídas de {total - len(failed_files)} arquivo(s)", icon="\u2705")  # ✅
        if failed_files:
            st.toast(f"\u26A0\uFE0F {len(failed_files)} arquivo(s) com erro", icon="\u26A0\uFE0F")  # ⚠️
        
        if gemini_used_any and gemini_mismatches:
            st.warning(
                f"\u26A0\uFE0F Gemini: {len(gemini_mismatches)} seção(ões) com somatório divergente "
                "do total impresso pelo banco. Confira a tabela de auditoria antes de confirmar."
            )
        
        if failed_files:
            with st.expander(f"\u26A0\uFE0F {len(failed_files)} arquivo(s) com erro — clique para detalhes"):
                for fname, err in failed_files:
                    st.error(f"**{fname}**: {err}")
                    st.button(
                        f"Tentar novamente: {fname}",
                        key=f"retry_{fname}",
                        on_click=lambda f=fname: st.rerun(),
                    )
        
        st.session_state.raw_transactions = raw_all
        st.session_state.institutions = institutions
        st.session_state.detected_holder = detected_holder or holder_input
        st.session_state.metrics = None
        st.session_state.reviewed = False
        st.session_state.duplicates_removed = duplicates_removed
        if "review_df" in st.session_state:
            del st.session_state["review_df"]

    raw = st.session_state.raw_transactions
    if raw is None:
        render_footer()
        return
    if not raw:
        st.error("Nenhuma transação pôde ser extraída dos arquivos fornecidos.")
        render_footer()
        return
    
    holder_name = holder_input or st.session_state.detected_holder or "Titular Não Identificado"
    institutions = list(st.session_state.institutions or [])

    # ------------------------------------------------------------------ #
    # Etapa 2: transações brutas (antes das regras) para validação visual
    # ------------------------------------------------------------------ #
    with st.expander(f"Validar transações brutas extraídas ({len(raw)} lançamentos)"):
        df_raw = pd.DataFrame([{
            "Data": t.date.strftime("%d/%m/%Y"),
            "Descrição": t.description,
            "Valor": t.amount,
            "Banco": bank_display_name(t.bank) if t.bank else "-",
            "Arquivo": t.source_file or "-",
        } for t in raw])
        st.dataframe(df_raw, width="stretch", height=320)

    # ------------------------------------------------------------------ #
    # Etapa 3: revisão manual obrigatória (st.data_editor melhorado)
    # ------------------------------------------------------------------ #
    st.divider()
    st.subheader("Revisão Manual — obrigatória antes da exportação")
    st.caption(
        "Linhas 'Indeterminado' tiveram sinal indeterminado pelo parser: decida manualmente. "
        "Débitos ficam fora por padrão. Desmarque créditos que NÃO sejam renda "
        "recorrente (ex.: Pix de parente) e informe o motivo na última coluna."
    )
    
    if "review_df" not in st.session_state:
        st.session_state.review_df = build_review_dataframe(raw)
    
    # Resumo acima da tabela (contagens por categoria)
    df_preview = st.session_state.review_df
    n_credit = int((df_preview["Sinal"] == "Crédito").sum())
    n_debit = int((df_preview["Sinal"] == "Débito").sum())
    n_pending = int((df_preview["Sinal"] == "Indeterminado").sum())
    
    col_res1, col_res2, col_res3, col_res4 = st.columns(4)
    col_res1.metric("Total de Linhas", len(df_preview))
    col_res2.metric("Créditos Automáticos", n_credit)
    col_res3.metric("Débitos (excluídos)", n_debit)
    col_res4.metric("Pendentes de Revisão", n_pending)
    
    edited = st.data_editor(
        st.session_state.review_df,
        num_rows="fixed",
        width="stretch",
        height=420,
        column_config={
            "ID": st.column_config.NumberColumn(
                "ID",
                disabled=True,
                width="small",
                help="Índice único da transação na lista bruta.",
            ),
            "Data": st.column_config.TextColumn(
                "Data",
                disabled=True,
                width="small",
                help="Data do lançamento conforme extraída do extrato.",
            ),
            "Descrição": st.column_config.TextColumn(
                "Descrição",
                disabled=True,
                width="large",
                help="Descrição completa do lançamento + contraparte (se visível).",
            ),
            "Valor": st.column_config.NumberColumn(
                "Valor",
                disabled=True,
                format="R$ %.2f",
                width="small",
                help="Valor do lançamento em R$ (positivo para créditos, negativo para débitos).",
            ),
            "Sinal": st.column_config.SelectboxColumn(
                "Sinal Detectado",
                disabled=True,
                width="small",
                options=["Crédito", "Débito", "Indeterminado"],
                help="Direção do lançamento: Crédito (entrada), Débito (saída) ou Indeterminado (requer revisão manual).",
            ),
            "Status": st.column_config.TextColumn(
                "Status",
                disabled=True,
                width="medium",
                help="Status da classificação: Automático (crédito/débito) ou Revisão manual obrigatória (indeterminado).",
            ),
            "Incluir na apuração": st.column_config.CheckboxColumn(
                "Incluir na apuração",
                help="Marque para incluir este lançamento na apuração de renda. Débitos ficam fora por padrão.",
            ),
            "Motivo da exclusão (manual)": st.column_config.TextColumn(
                "Motivo da exclusão (manual)",
                help="Informe o motivo caso esteja excluindo um crédito que seria automaticamente incluído.",
            ),
        },
        key="review_editor",
    )
    
    # Aviso de pendentes
    pendentes = int(
        ((edited["Sinal"].astype(str) == "Indeterminado")
         & (~edited["Incluir na apuração"])).sum()
    )
    if pendentes:
        st.warning(
            f"\u26A0\uFE0F {pendentes} linha(s) indeterminada(s) sem decisão. Pelo padrão de "
            "segurança, elas serão EXCLUÍDAS da apuração e listadas na auditoria. "
            "Marque 'Incluir na apuração' nas que forem renda efetiva."
        )

    if st.button("Confirmar Revisão e Gerar Relatório", type="primary"):
        manual_inclusions = set()
        manual_exclusions = {}
        for _, row in edited.iterrows():
            idx = int(row["ID"])
            t = raw[idx]
            incluir = bool(row["Incluir na apuração"])
            motivo = str(row["Motivo da exclusão (manual)"] or "").strip()
            if t.needs_review:
                if incluir:
                    manual_inclusions.add(idx)
                else:
                    manual_exclusions[idx] = motivo or "Não confirmada como renda pelo operador na revisão"
            else:
                if (t.is_credit or float(t.amount) > 0) and not incluir:
                    manual_exclusions[idx] = motivo or "Excluída manualmente pelo operador"
        
        st.session_state.manual_inclusions = manual_inclusions
        st.session_state.manual_exclusions = manual_exclusions
        st.session_state.metrics = calculate_income_metrics(
            raw,
            holder_name=holder_name,
            manual_exclusions=manual_exclusions,
            manual_inclusions=manual_inclusions,
        )
        st.session_state.reviewed = True
        st.success("Revisão confirmada. Relatório e exportações liberados abaixo.")

    # ------------------------------------------------------------------ #
    # Etapa 4: resultados + exportações (em TABS para reduzir rolagem)
    # ------------------------------------------------------------------ #
    metrics = st.session_state.metrics
    if st.session_state.reviewed and metrics is not None:
        st.divider()
        st.subheader("Prévia dos Resultados")
        
        # Rastreabilidade IA e revisão
        n_gemini = sum(1 for t in raw if getattr(t, "extraction_source", "") == "gemini")
        revisao = metrics.get("revisao_manual", {})
        
        if n_gemini or revisao:
            info_parts = []
            if n_gemini:
                info_parts.append(f"\U0001F916 {n_gemini} lançamento(s) via IA (Gemini)")  # 🤖
            if revisao:
                info_parts.append(
                    f"Revisão: {len(revisao.get('incluidas', []))} confirmado(s) • "
                    f"{len(revisao.get('excluidas', []))} exclusão(ões)"
                )
            st.caption(" • ".join(info_parts))
        
        # BUG 1 FIX: KPI Cards usando st.metric nativo (estilizado via CSS)
        col_kpi1, col_kpi2, col_kpi3 = st.columns(3)
        with col_kpi1:
            st.metric(
                label="Total Geral Apurado",
                value=brl(metrics["total_geral"]),
                help="Soma de todas as entradas válidas",
            )
        with col_kpi2:
            st.metric(
                label="Média Mensal Geral",
                value=brl(metrics["media_mensal_geral"]),
                help="Total / número de meses com lançamentos",
            )
        with col_kpi3:
            st.metric(
                label="Média Meses Completos",
                value=brl(metrics["media_meses_completos"]),
                help="Total / meses com >20 dias cobertos",
            )
        
        # Tabs para organizar resultados
        tab_resumo, tab_validas, tab_auditoria, tab_export = st.tabs([
            "Resumo por Mês",
            "Entradas Válidas",
            "Auditoria",
            "Exportações",
        ])
        
        with tab_resumo:
            if metrics["resumo_mensal"]:
                df_resumo = pd.DataFrame(metrics["resumo_mensal"])
                df_resumo.columns = ["Mês/Ano", "Dias Cobertos", "Qtd Entradas Válidas", "Total Válido Mensal"]
                st.dataframe(df_resumo, width="stretch")
            else:
                st.info("Nenhum dado mensal consolidado disponível.")
        
        with tab_validas:
            if metrics["entradas_validas"]:
                df_validas = pd.DataFrame([{
                    "Data": t.date.strftime("%d/%m/%Y"),
                    "Descrição": t.description,
                    "Valor": t.amount,
                } for t in metrics["entradas_validas"]])
                st.dataframe(df_validas, width="stretch")
            else:
                st.info("Nenhuma entrada válida encontrada.")
        
        with tab_auditoria:
            if metrics["entradas_excluidas"]:
                df_exc = pd.DataFrame(metrics["entradas_excluidas"])
                df_exc["date"] = pd.to_datetime(df_exc["date"]).dt.strftime("%d/%m/%Y")
                df_exc.columns = ["Data", "Descrição Original", "Regra de Exclusão", "Valor"]
                st.dataframe(df_exc, width="stretch")
            else:
                st.info("Nenhum valor excluído.")
        
        with tab_export:
            st.subheader("Relatório Executivo e Exportações")
            st.caption("Gere os relatórios em PDF, Excel ou CSV para download.")
            
            col_pdf, col_xlsx, col_csv = st.columns(3)
            with col_pdf:
                # BUG 3 FIX: try/except explícito na geração do PDF
                try:
                    with st.spinner("Gerando PDF..."):
                        pdf_buffer = generate_report(metrics, holder_name, institutions)
                        pdf_bytes = pdf_buffer.getvalue()
                    st.download_button(
                        "Gerar Relatório PDF",
                        data=pdf_bytes,
                        file_name="relatorio_apuracao_renda.pdf",
                        mime="application/pdf",
                        type="primary",
                        width="stretch",
                    )
                except Exception as e:
                    logger.error("Erro ao gerar PDF: %s", e, exc_info=True)
                    st.error(f"Erro ao gerar PDF: {str(e)}")
            
            with col_xlsx:
                try:
                    with st.spinner("Gerando Excel..."):
                        xlsx_bytes = generate_excel(metrics, holder_name, institutions)
                    st.download_button(
                        "Baixar Excel",
                        data=xlsx_bytes,
                        file_name="apuracao_renda.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        width="stretch",
                    )
                except Exception as e:
                    logger.error("Erro ao gerar Excel: %s", e, exc_info=True)
                    st.error(f"Erro ao gerar Excel: {str(e)}")
            
            with col_csv:
                try:
                    with st.spinner("Gerando CSV..."):
                        csv_bytes = generate_csv(metrics)
                    st.download_button(
                        "Baixar CSV",
                        data=csv_bytes,
                        file_name="apuracao_renda.csv",
                        mime="text/csv",
                        width="stretch",
                    )
                except Exception as e:
                    logger.error("Erro ao gerar CSV: %s", e, exc_info=True)
                    st.error(f"Erro ao gerar CSV: {str(e)}")
    
    # Footer (mantido com autoria)
    render_footer()

if __name__ == "__main__":
    main()