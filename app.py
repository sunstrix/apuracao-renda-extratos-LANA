import streamlit as st
import pandas as pd
import io
import os
import re
import logging

from src.pdf_extractor import extract_text_from_pdf, get_pdf_metadata
from src.transaction_parser import parse_pdf_pages
from src.income_calculator import calculate_income_metrics
from src.report_generator import generate_report

# Configuração básica da página
st.set_page_config(
    page_title="Apuração de Renda - Extratos PDF",
    page_icon="📊",
    layout="wide"
)

# Configuração de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def try_detect_holder_name(text_pages: list[str]) -> str:
    """
    Tenta detectar o nome do titular nas primeiras linhas do extrato.
    Estratégia simples: procura por padrões comuns como "Titular: Nome" ou "CPF/CNPJ".
    """
    if not text_pages:
        return ""
    
    # Analisa apenas a primeira página para economizar processamento
    first_page_text = text_pages[0]
    
    # Padrões comuns em extratos
    patterns = [
        r'Titular:\s*([A-Z\s]+?)(?:\n|CPF|CNPJ)',
        r'Nome:\s*([A-Z\s]+?)(?:\n|CPF|CNPJ)',
        r'Cliente:\s*([A-Z\s]+?)(?:\n|CPF|CNPJ)',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, first_page_text, re.IGNORECASE)
        if match:
            return match.group(1).strip().title()
            
    return ""


def try_detect_institution(file_name: str, text_pages: list[str]) -> str:
    """
    Tenta detectar a instituição financeira pelo nome do arquivo ou metadados/texto.
    """
    # Tenta pelo nome do arquivo primeiro
    base_name = os.path.splitext(file_name)[0]
    # Remove datas ou números comuns do nome do arquivo
    clean_name = re.sub(r'\d{2}[-_]\d{2}[-_]\d{4}|\d{4}', '', base_name).strip(" -_")
    if clean_name:
        return clean_name.replace("-", " ").replace("_", " ").title()
        
    # Tenta pelo texto da primeira página
    if text_pages:
        first_page = text_pages[0]
        # Procura por palavras-chave de bancos comuns (lista extensível)
        banks = ["Banco", "Caixa", "Itaú", "Bradesco", "Santander", "Nubank", "Inter", "C6", "Sicredi", "Sicoob"]
        for bank in banks:
            if bank.lower() in first_page.lower():
                return bank
                
    return "Instituição Desconhecida"


def main():
    st.title("📊 Apuração de Renda via Extratos PDF")
    st.write("Anexe múltiplos extratos bancários em PDF para consolidar e gerar o relatório executivo.")

    # Inicialização do estado da sessão
    if "processed_data" not in st.session_state:
        st.session_state.processed_data = None
    if "metrics" not in st.session_state:
        st.session_state.metrics = None
    if "detected_holder" not in st.session_state:
        st.session_state.detected_holder = ""
    if "detected_institutions" not in st.session_state:
        st.session_state.detected_institutions = set()

    # Área de Upload
    uploaded_files = st.file_uploader(
        "Selecione os arquivos PDF dos extratos",
        type=["pdf"],
        accept_multiple_files=True
    )

    # Campo para Nome do Titular
    holder_name_input = st.text_input(
        "Nome do Titular (opcional - será tentada a auto-detecção)",
        value=st.session_state.detected_holder
    )

    # Botão de Processamento
    if st.button("🚀 Processar Extratos", type="primary", disabled=not uploaded_files):
        if not uploaded_files:
            st.warning("Por favor, anexe pelo menos um arquivo PDF.")
            return

        # Resetar estado anterior
        st.session_state.processed_data = []
        st.session_state.detected_institutions = set()
        st.session_state.detected_holder = holder_name_input
        
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        total_files = len(uploaded_files)
        all_transactions = []
        
        for index, uploaded_file in enumerate(uploaded_files):
            status_text.text(f"Processando arquivo {index + 1} de {total_files}: {uploaded_file.name}")
            
            try:
                # 1. Extração de Texto
                text_pages = extract_text_from_pdf(uploaded_file)
                
                if not text_pages or not any(text.strip() for text in text_pages):
                    st.warning(f"⚠️ O arquivo '{uploaded_file.name}' não pôde ser lido (pode estar protegido por senha, corrompido ou sem camada de texto).")
                    continue
                
                # 2. Auto-detecção de Titular e Instituição
                if not st.session_state.detected_holder:
                    detected_holder = try_detect_holder_name(text_pages)
                    if detected_holder:
                        st.session_state.detected_holder = detected_holder
                
                institution = try_detect_institution(uploaded_file.name, text_pages)
                st.session_state.detected_institutions.add(institution)
                
                # 3. Parsing das Transações
                transactions = parse_pdf_pages(text_pages)
                all_transactions.extend(transactions)
                
            except Exception as e:
                logger.error(f"Erro ao processar {uploaded_file.name}: {e}")
                st.error(f"❌ Erro inesperado ao processar '{uploaded_file.name}': {e}")
            
            # Atualiza progresso
            progress_bar.progress((index + 1) / total_files)
        
        status_text.text("Processamento concluído!")
        
        if not all_transactions:
            st.error("Nenhuma transação pôde ser extraída dos arquivos fornecidos.")
            return
            
        # 4. Cálculo das Métricas
        with st.spinner("Calculando métricas de renda..."):
            st.session_state.metrics = calculate_income_metrics(all_transactions)
            st.session_state.processed_data = all_transactions
            
        st.success("✅ Extratos processados com sucesso! Confira a prévia abaixo.")

    # Área de Resultados (Prévia)
    if st.session_state.metrics is not None:
        metrics = st.session_state.metrics
        
        st.divider()
        st.subheader("📈 Prévia dos Resultados")
        
        # KPIs
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Total Geral Apurado", f"R$ {metrics['total_geral']:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
        with col2:
            st.metric("Média Mensal Geral", f"R$ {metrics['media_mensal_geral']:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
        with col3:
            st.metric("Média Meses Completos", f"R$ {metrics['media_meses_completos']:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
        
        st.divider()
        
        # Tabela Consolidada por Mês
        st.subheader("Resumo Consolidado por Mês")
        if metrics["resumo_mensal"]:
            df_resumo = pd.DataFrame(metrics["resumo_mensal"])
            df_resumo.columns = ["Mês/Ano", "Qtd Entradas Válidas", "Total Válido Mensal"]
            st.dataframe(df_resumo, use_container_width=True)
        else:
            st.info("Nenhum dado mensal consolidado disponível.")
            
        # Tabela de Entradas Válidas
        st.subheader("Entradas Válidas Consideradas")
        if metrics["entradas_validas"]:
            valid_data = [
                {
                    "Data": tx.date.strftime("%d/%m/%Y"),
                    "Descrição": tx.description,
                    "Valor": tx.amount
                }
                for tx in metrics["entradas_validas"]
            ]
            df_validas = pd.DataFrame(valid_data)
            st.dataframe(df_validas, use_container_width=True)
        else:
            st.info("Nenhuma entrada válida encontrada.")
            
        # Tabela de Auditoria
        st.subheader("Auditoria (Valores Excluídos)")
        if metrics["entradas_excluidas"]:
            df_excluidas = pd.DataFrame(metrics["entradas_excluidas"])
            df_excluidas["date"] = pd.to_datetime(df_excluidas["date"]).dt.strftime("%d/%m/%Y")
            df_excluidas.columns = ["Data", "Descrição Original", "Regra de Exclusão", "Valor"]
            st.dataframe(df_excluidas, use_container_width=True)
        else:
            st.info("Nenhum valor excluído.")
            
        st.divider()
        
        # Botão de Geração do PDF
        st.subheader("📄 Relatório Executivo")
        
        # Prepara os dados para o gerador de relatório
        holder_name = holder_name_input or st.session_state.detected_holder or "Titular Não Identificado"
        institutions = list(st.session_state.detected_institutions) if st.session_state.detected_institutions else ["Não Identificada"]
        
        if st.button("📥 Gerar Relatório PDF"):
            with st.spinner("Gerando PDF..."):
                try:
                    pdf_buffer = generate_report(metrics, holder_name, institutions)
                    
                    st.download_button(
                        label="💾 Baixar Relatório PDF",
                        data=pdf_buffer,
                        file_name="relatorio_apuracao_renda.pdf",
                        mime="application/pdf",
                        type="primary"
                    )
                    st.success("Relatório gerado com sucesso! Clique no botão acima para baixar.")
                except Exception as e:
                    logger.error(f"Erro ao gerar PDF: {e}")
                    st.error(f"Erro ao gerar o relatório PDF: {e}")


if __name__ == "__main__":
    main()