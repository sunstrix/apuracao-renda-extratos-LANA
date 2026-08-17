"""
Interface principal Streamlit da Apuração de Renda via Extratos PDF.
Fluxo (com revisão humana obrigatória antes da exportação):
upload -> extração (st.status + st.progress, PARALELA via ThreadPoolExecutor)
-> parsing por banco;
expander com TODAS as transações brutas (antes das regras);
tabela editável (st.data_editor) de revisão manual:
Sinal Detectado (Crédito / Débito / ⚠️ Indeterminado);
checkbox "Incluir na apuração" (pré-marcado só p/ créditos automáticos);
coluna "Motivo da exclusão (manual)";
botão "Confirmar revisão e gerar relatório" -> calculate_income_metrics()
recebendo manual_exclusions / manual_inclusions;
downloads PDF / Excel / CSV habilitados somente após a confirmação.

RODADA GEMINI (integração híbrida):
- Toggle "Extração via Gemini (nuvem)" no sidebar — habilitado apenas com
  GEMINI_API_KEY configurada (.env / secrets do Cloud);
- Checkbox de CONSENTIMENTO explícito (LGPD): sem ele, nada sai da máquina;
- Roteamento por arquivo: Gemini primeiro (se autorizado); erro da API ou
  zero transações => fallback AUTOMÁTICO para o pipeline local;
- Divergências do gabarito (somatórios do banco vs extração IA) viram
  banner de aviso após o processamento;
- Rastreabilidade: transações com extraction_source="gemini" recebem selo
  🤖 na prévia de resultados.
"""
import logging
import re
import streamlit as st
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from src.pdf_extractor import extract_text_from_pdf
from src.bank_detector import detect_bank, bank_display_name
from src.transaction_parser import parse_statement
from src.income_calculator import calculate_income_metrics
from src.report_generator import generate_report, generate_excel, generate_csv
from src.gemini_extractor import (
    GeminiExtractionError,
    extract_transactions_via_gemini,
    gemini_available,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

st.set_page_config(page_title="Apuração de Renda - Extratos PDF", page_icon="📊", layout="wide")

# ---------------------------------------------------------------------------
# PERF-7: Regex compiladas FORA da função para evitar recompilação a cada chamada
# ---------------------------------------------------------------------------
HOLDER_EXCLUSION_KEYWORDS = re.compile(
    r"(CPF|CNPJ|AGÊNCIA|AGENCIA|CONTA|BANCO|MOVIMENTA|SALDO|EXTRATO|NU\s|VALORES)",
    re.IGNORECASE
)

def brl(value: float) -> str:
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

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
        cand = lines[j].strip().replace("nll", "").replace("nU", "").strip()
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
    1) se use_gemini: tenta a API Gemini (PDF nativo, sem OCR local);
       erro controlado ou 0 transações => cai no passo 2;
    2) pipeline local determinístico (PyMuPDF/pdfplumber/OCR + parsers).

    Returns:
        Tupla (nome, sucesso, transacoes, banco, titular, erro, info_gemini)
        onde info_gemini = {"used": bool, "mismatches": [divergências]}.
    """
    info_gemini = {"used": False, "mismatches": []}

    # --- Caminho 1: Gemini (nuvem), somente se autorizado pelo operador ---
    if use_gemini:
        try:
            uploaded_file.seek(0)
            pdf_bytes = uploaded_file.read()
            txs, data, mismatches = extract_transactions_via_gemini(
                pdf_bytes, uploaded_file.name
            )
            if txs:
                holder = (data.get("titular") or "").strip() or None
                info_gemini["used"] = True
                info_gemini["mismatches"] = mismatches
                return (uploaded_file.name, True, txs, "nubank", holder, None,
                        info_gemini)
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
        pages = extract_text_from_pdf(uploaded_file)
        if not pages or not any(p.strip() for p in pages):
            return (uploaded_file.name, False, [], None, None,
                    "Arquivo não pôde ser lido (protegido por senha, corrompido ou sem camada de texto)",
                    info_gemini)

        full_text = "\n".join(pages)
        bank = detect_bank(full_text)
        detected_holder = try_detect_holder_name(pages)
        txs = parse_statement(full_text, bank=bank, source_file=uploaded_file.name)
        return (uploaded_file.name, True, txs, bank, detected_holder, None,
                info_gemini)

    except Exception as e:
        logger.error("Erro ao processar %s: %s", uploaded_file.name, e)
        return (uploaded_file.name, False, [], None, None, str(e), info_gemini)

def build_review_dataframe(raw) -> pd.DataFrame:
    """
    Monta a tabela de revisão manual.
    Pré-marca "Incluir" apenas para créditos automáticos
    (needs_review=False e is_credit=True). Débitos e indeterminados
    começam desmarcados; linhas needs_review são destacadas com ⚠️.
    """
    rows = []
    for idx, t in enumerate(raw):
        if t.needs_review:
            sinal, incluir, status = "⚠️ Indeterminado", False, "⚠️ Revisão manual obrigatória"
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

def main():
    st.title("📊 Apuração de Renda via Extratos PDF")
    st.write("Anexe múltiplos extratos bancários em PDF para consolidar e gerar o relatório executivo.")

    # ------------------------------------------------------------------ #
    # Configuração de extração (RODADA GEMINI): toggle + consentimento
    # ------------------------------------------------------------------ #
    with st.sidebar:
        st.header("⚙️ Configuração de Extração")
        gemini_ok = gemini_available()
        use_gemini_cfg = st.checkbox(
            "☁️ Extração via Gemini (nuvem)",
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
                          ("manual_inclusions", set()), ("manual_exclusions", {})):
        if key not in st.session_state:
            st.session_state[key] = default

    uploaded_files = st.file_uploader(
        "Selecione os arquivos PDF dos extratos", type=["pdf"], accept_multiple_files=True
    )

    holder_input = st.text_input(
        "Nome do Titular (opcional - será tentada a auto-detecção)",
        value=st.session_state.detected_holder or "",
    )

    # ------------------------------------------------------------------ #
    # Etapa 1: extração + parsing (com status/progresso e PARALELIZAÇÃO)
    # ------------------------------------------------------------------ #
    if st.button("🚀 Processar Extratos", type="primary", disabled=not uploaded_files):
        raw_all = []
        institutions = set()
        detected_holder = holder_input
        gemini_mismatches = []
        gemini_used_any = False

        with st.status("Processando extratos...", expanded=True) as status:
            progress = st.progress(0.0, text="Iniciando processamento paralelo...")
            total = len(uploaded_files)
            max_workers = min(6, total)  # Nunca mais workers que arquivos
            completed_count = 0

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_file = {
                    executor.submit(_process_single_pdf, uf, use_gemini): uf
                    for uf in uploaded_files
                }

                for future in as_completed(future_to_file):
                    uf = future_to_file[future]
                    try:
                        (file_name, success, txs, bank, holder, error,
                         info_gemini) = future.result()
                        completed_count += 1

                        if info_gemini.get("used"):
                            gemini_used_any = True
                            gemini_mismatches.extend(info_gemini.get("mismatches", []))

                        if success:
                            raw_all.extend(txs)
                            institutions.add(bank_display_name(bank))
                            if not detected_holder and holder:
                                detected_holder = holder
                            selo = "🤖 " if info_gemini.get("used") else "✅ "
                            status.write(f"{selo}{file_name}: {len(txs)} transações "
                                         f"({bank_display_name(bank)})")
                        else:
                            st.warning(f"⚠️ {file_name}: {error}")
                            status.write(f"⚠️ {file_name}: {error}")

                        progress.progress(completed_count / total,
                                          text=f"Processado {completed_count}/{total} arquivos")

                    except Exception as e:
                        completed_count += 1
                        logger.error("Erro inesperado ao processar %s: %s", uf.name, e)
                        st.error(f"❌ Erro inesperado ao processar '{uf.name}': {e}")
                        progress.progress(completed_count / total,
                                          text=f"Processado {completed_count}/{total} arquivos")

            progress.progress(1.0, text="Processamento concluído!")
            status.update(label="Processamento concluído!", state="complete")

        # Gabarito do banco: divergências detectadas na extração via IA.
        if gemini_used_any and gemini_mismatches:
            st.warning(
                f"⚠️ Gemini: {len(gemini_mismatches)} seção(ões) com somatório divergente "
                "do total impresso pelo banco. Confira a tabela de auditoria antes de confirmar."
            )

        st.session_state.raw_transactions = raw_all
        st.session_state.institutions = institutions
        st.session_state.detected_holder = detected_holder or holder_input
        st.session_state.metrics = None
        st.session_state.reviewed = False
        if "review_df" in st.session_state:
            del st.session_state["review_df"]

    raw = st.session_state.raw_transactions
    if raw is None:
        return
    if not raw:
        st.error("Nenhuma transação pôde ser extraída dos arquivos fornecidos.")
        return

    holder_name = holder_input or st.session_state.detected_holder or "Titular Não Identificado"
    institutions = list(st.session_state.institutions or [])

    # ------------------------------------------------------------------ #
    # Etapa 2: transações brutas (antes das regras) para validação visual
    # ------------------------------------------------------------------ #
    with st.expander(f"🔎 Validar transações brutas extraídas ({len(raw)} lançamentos)"):
        df_raw = pd.DataFrame([{
            "Data": t.date.strftime("%d/%m/%Y"),
            "Descrição": t.description,
            "Valor": t.amount,
            "Banco": bank_display_name(t.bank) if t.bank else "-",
            "Arquivo": t.source_file or "-",
        } for t in raw])
        st.dataframe(df_raw, use_container_width=True, height=320)

    # ------------------------------------------------------------------ #
    # Etapa 3: revisão manual obrigatória (st.data_editor)
    # ------------------------------------------------------------------ #
    st.divider()
    st.subheader("🖊️ Revisão Manual — obrigatória antes da exportação")
    st.caption(
        "Linhas ⚠️ tiveram sinal indeterminado pelo parser: decida manualmente. "
        "Débitos ficam fora por padrão. Desmarque créditos que NÃO sejam renda "
        "recorrente (ex.: Pix de parente) e informe o motivo na última coluna."
    )

    if "review_df" not in st.session_state:
        st.session_state.review_df = build_review_dataframe(raw)

    edited = st.data_editor(
        st.session_state.review_df,
        num_rows="fixed",
        use_container_width=True,
        height=420,
        column_config={
            "ID": st.column_config.NumberColumn("ID", disabled=True, width="small"),
            "Data": st.column_config.TextColumn("Data", disabled=True, width="small"),
            "Descrição": st.column_config.TextColumn("Descrição", disabled=True, width="large"),
            "Valor": st.column_config.NumberColumn("Valor", disabled=True,
                                                   format="R$ %.2f", width="small"),
            "Sinal": st.column_config.TextColumn("Sinal Detectado", disabled=True, width="small"),
            "Status": st.column_config.TextColumn("Status", disabled=True, width="medium"),
            "Incluir na apuração": st.column_config.CheckboxColumn("Incluir na apuração"),
            "Motivo da exclusão (manual)": st.column_config.TextColumn("Motivo da exclusão (manual)"),
        },
        key="review_editor",
    )

    # H1 (reaplicado): aviso ao vivo com a contagem de linhas ⚠️ sem decisão.
    pendentes = int(
        ((edited["Status"].astype(str).str.startswith("⚠️"))
         & (~edited["Incluir na apuração"])).sum()
    )
    if pendentes:
        st.warning(
            f"⚠️ {pendentes} linha(s) indeterminada(s) sem decisão. Pelo padrão de "
            "segurança, elas serão EXCLUÍDAS da apuração e listadas na auditoria. "
            "Marque 'Incluir na apuração' nas que forem renda efetiva."
        )

    if st.button("✅ Confirmar revisão e gerar relatório", type="primary"):
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
                    manual_exclusions[idx] = (
                        motivo or "Não confirmada como renda pelo operador na revisão"
                    )
            else:
                if (t.is_credit or t.amount > 0) and not incluir:
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
    # Etapa 4: resultados + exportações (somente após confirmação)
    # ------------------------------------------------------------------ #
    metrics = st.session_state.metrics
    if st.session_state.reviewed and metrics is not None:
        st.divider()
        st.subheader("📈 Prévia dos Resultados")
        col1, col2, col3 = st.columns(3)
        col1.metric("Total Geral Apurado", brl(metrics["total_geral"]))
        col2.metric("Média Mensal Geral", brl(metrics["media_mensal_geral"]))
        col3.metric("Média Meses Completos", brl(metrics["media_meses_completos"]))

        # Rastreabilidade IA (RODADA GEMINI)
        n_gemini = sum(1 for t in raw if getattr(t, "extraction_source", "") == "gemini")
        if n_gemini:
            st.caption(
                f"🤖 {n_gemini} lançamento(s) extraído(s) via IA (Gemini), validados "
                "contra os somatórios impressos pelo banco."
            )

        revisao = metrics.get("revisao_manual", {})
        st.caption(
            f"Revisão: {len(revisao.get('incluidas', []))} lançamento(s) confirmado(s) "
            f"manualmente como renda • {len(revisao.get('excluidas', []))} exclusão(ões) "
            f"manuais/pendentes."
        )

        st.subheader("Resumo Consolidado por Mês")
        if metrics["resumo_mensal"]:
            df_resumo = pd.DataFrame(metrics["resumo_mensal"])
            df_resumo.columns = ["Mês/Ano", "Qtd Entradas Válidas", "Total Válido Mensal"]
            st.dataframe(df_resumo, use_container_width=True)
        else:
            st.info("Nenhum dado mensal consolidado disponível.")

        st.subheader("Entradas Válidas Consideradas")
        if metrics["entradas_validas"]:
            df_validas = pd.DataFrame([{
                "Data": t.date.strftime("%d/%m/%Y"),
                "Descrição": t.description,
                "Valor": t.amount,
            } for t in metrics["entradas_validas"]])
            st.dataframe(df_validas, use_container_width=True)
        else:
            st.info("Nenhuma entrada válida encontrada.")

        st.subheader("Auditoria (Valores Excluídos)")
        if metrics["entradas_excluidas"]:
            df_exc = pd.DataFrame(metrics["entradas_excluidas"])
            df_exc["date"] = pd.to_datetime(df_exc["date"]).dt.strftime("%d/%m/%Y")
            df_exc.columns = ["Data", "Descrição Original", "Regra de Exclusão", "Valor"]
            st.dataframe(df_exc, use_container_width=True)
        else:
            st.info("Nenhum valor excluído.")

        st.divider()
        st.subheader("📄 Relatório Executivo e Exportações")
        col_pdf, col_xlsx, col_csv = st.columns(3)

        with col_pdf:
            with st.spinner("Gerando PDF..."):
                pdf_bytes = generate_report(metrics, holder_name, institutions).getvalue()
            st.download_button("📥 Gerar Relatório PDF", data=pdf_bytes,
                               file_name="relatorio_apuracao_renda.pdf",
                               mime="application/pdf", type="primary")

        with col_xlsx:
            with st.spinner("Gerando Excel..."):
                xlsx_bytes = generate_excel(metrics, holder_name, institutions)
            st.download_button("📥 Baixar Excel", data=xlsx_bytes,
                               file_name="apuracao_renda.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        with col_csv:
            with st.spinner("Gerando CSV..."):
                csv_bytes = generate_csv(metrics)
            st.download_button("📥 Baixar CSV", data=csv_bytes,
                               file_name="apuracao_renda.csv", mime="text/csv")

if __name__ == "__main__":
    main()