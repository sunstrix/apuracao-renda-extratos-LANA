"""
Interface principal Streamlit da Apuração de Renda via Extratos PDF.

Fluxo: upload -> extração (com status/progresso) -> validação visual das
transações brutas (expander) -> regras de exclusão -> KPIs/tabelas ->
exportação PDF / Excel / CSV. Dados cacheados em session_state.
"""
import re
import logging

import streamlit as st
import pandas as pd

from src.pdf_extractor import extract_text_from_pdf
from src.bank_detector import detect_bank, bank_display_name
from src.transaction_parser import parse_statement
from src.income_calculator import calculate_income_metrics
from src.report_generator import generate_report, generate_excel, generate_csv

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

st.set_page_config(page_title="Apuração de Renda - Extratos PDF", page_icon="📊", layout="wide")


def brl(value: float) -> str:
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# Linha que parece nome próprio: 2+ palavras puramente alfabéticas
_NAME_LINE_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]+(?:\s+[A-Za-zÀ-ÖØ-öø-ÿ]+)+")
# Palavras que desqualificam uma linha como nome de pessoa
_NOT_NAME_RE = re.compile(
    r"(CPF|CNPJ|Agência|Agencia|Conta|Banco|^NU$|Movimenta|Saldo|Extrato|http|"
    r"Tem alguma|Caso a solução|Ouvidoria|Atendimento|CNPJ:)",
    re.IGNORECASE,
)


def try_detect_holder_name(text_pages) -> str:
    """
    Detecta o nome do titular em extratos Nubank/OCR.

    Estratégia principal: a linha imediatamente ACIMA da linha que contém
    "CPF" é o nome do titular no layout real do extrato, desde que essa
    linha pareça um nome próprio (2-6 palavras, sem dígitos).
    """
    lines = []
    for page in (text_pages or [])[:4]:
        lines.extend((page or "").splitlines())

    for idx, line in enumerate(lines):
        if not re.search(r"\bCPF\b", line, re.IGNORECASE):
            continue

        # Procura a linha não-vazia anterior
        j = idx - 1
        while j >= 0 and not lines[j].strip():
            j -= 1
        if j < 0:
            continue

        cand = lines[j].strip()
        cand = cand.replace("nll", "").strip()  # ruído comum de OCR

        if (
            2 <= len(cand.split()) <= 6
            and not re.search(r"\d", cand)
            and not _NOT_NAME_RE.search(cand)
            and _NAME_LINE_RE.fullmatch(cand)
        ):
            logger.info("Titular detectado: %s", cand)
            return cand

    # Fallback: rótulos explícitos (extratos de outros bancos)
    text = "\n".join(lines)
    for pattern in (
        r"Titular:\s*([A-Za-zÀ-ÖØ-öø-ÿ][^\n]{4,60})",
        r"Nome:\s*([A-Za-zÀ-ÖØ-öø-ÿ][^\n]{4,60})",
        r"Cliente:\s*([A-Za-zÀ-ÖØ-öø-ÿ][^\n]{4,60})",
    ):
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(1).strip().title()

    return ""


def main():
    st.title("📊 Apuração de Renda via Extratos PDF")
    st.write("Anexe múltiplos extratos bancários em PDF para consolidar e gerar o relatório executivo.")

    for key in ("raw_transactions", "metrics", "detected_holder", "institutions"):
        if key not in st.session_state:
            st.session_state[key] = None if key in ("raw_transactions", "metrics") else ("" if key == "detected_holder" else set())

    uploaded_files = st.file_uploader("Selecione os arquivos PDF dos extratos",
                                      type=["pdf"], accept_multiple_files=True)

    holder_input = st.text_input("Nome do Titular (opcional - será tentada a auto-detecção)",
                                 value=st.session_state.detected_holder or "")

    if st.button("🚀 Processar Extratos", type="primary", disabled=not uploaded_files):
        raw_all = []
        institutions = set()
        detected_holder = holder_input

        with st.status("Processando extratos...", expanded=True) as status:
            progress = st.progress(0.0, text="Iniciando processamento...")
            total = len(uploaded_files)

            for idx, uploaded_file in enumerate(uploaded_files):
                status.update(label=f"Processando {uploaded_file.name} ({idx + 1}/{total})...")
                try:
                    text_pages = extract_text_from_pdf(uploaded_file)
                    if not text_pages or not any(t.strip() for t in text_pages):
                        st.warning(f"⚠️ O arquivo '{uploaded_file.name}' não pôde ser lido "
                                   f"(protegido por senha, corrompido ou sem camada de texto).")
                        continue

                    full_text = "\n".join(text_pages)
                    bank = detect_bank(full_text)
                    institutions.add(bank_display_name(bank))

                    if not detected_holder:
                        detected_holder = try_detect_holder_name(text_pages)

                    txs = parse_statement(full_text, bank=bank, source_file=uploaded_file.name)
                    status.write(f"✅ {uploaded_file.name}: {len(txs)} transações "
                                 f"({bank_display_name(bank)})")
                    raw_all.extend(txs)
                except Exception as e:
                    logger.error("Erro ao processar %s: %s", uploaded_file.name, e)
                    st.error(f"❌ Erro inesperado ao processar '{uploaded_file.name}': {e}")

                progress.progress((idx + 1) / total,
                                  text=f"Processando {uploaded_file.name} ({idx + 1}/{total})")

            progress.progress(1.0, text="Processamento concluído!")
            status.update(label="Processamento concluído!", state="complete")

        st.session_state.raw_transactions = raw_all
        st.session_state.institutions = institutions
        st.session_state.detected_holder = detected_holder or holder_input
        st.session_state.metrics = calculate_income_metrics(raw_all) if raw_all else None

    raw = st.session_state.raw_transactions
    metrics = st.session_state.metrics

    if raw is not None:
        st.divider()

        # --- Validação visual ANTES das regras (Item 5) ---
        with st.expander(f"🔎 Validar transações brutas extraídas ({len(raw)} lançamentos)"):
            if raw:
                df_raw = pd.DataFrame([{
                    "Data": t.date.strftime("%d/%m/%Y"),
                    "Descrição": t.description,
                    "Valor": t.amount,
                    "Banco": bank_display_name(t.bank) if t.bank else "-",
                    "Arquivo": t.source_file or "-",
                } for t in raw])
                st.dataframe(df_raw, use_container_width=True, height=320)
            else:
                st.info("Nenhuma transação bruta extraída. Verifique os dumps em logs/.")

        if metrics is None or not raw:
            st.error("Nenhuma transação pôde ser extraída dos arquivos fornecidos.")
            return

        st.subheader("📈 Prévia dos Resultados")
        col1, col2, col3 = st.columns(3)
        col1.metric("Total Geral Apurado", brl(metrics["total_geral"]))
        col2.metric("Média Mensal Geral", brl(metrics["media_mensal_geral"]))
        col3.metric("Média Meses Completos", brl(metrics["media_meses_completos"]))
        st.divider()

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

        holder_name = holder_input or st.session_state.detected_holder or "Titular Não Identificado"
        institutions = list(st.session_state.institutions or [])

        col_pdf, col_xlsx, col_csv = st.columns(3)
        with col_pdf:
            pdf_bytes = generate_report(metrics, holder_name, institutions).getvalue()
            st.download_button("📥 Gerar Relatório PDF", data=pdf_bytes,
                               file_name="relatorio_apuracao_renda.pdf",
                               mime="application/pdf", type="primary")
        with col_xlsx:
            xlsx_bytes = generate_excel(metrics, holder_name, institutions)
            st.download_button("📥 Baixar Excel", data=xlsx_bytes,
                               file_name="apuracao_renda.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        with col_csv:
            csv_bytes = generate_csv(metrics)
            st.download_button("📥 Baixar CSV", data=csv_bytes,
                               file_name="apuracao_renda.csv", mime="text/csv")


if __name__ == "__main__":
    main()