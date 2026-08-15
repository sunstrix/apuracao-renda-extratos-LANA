import io
import logging
from datetime import datetime
from typing import Dict, List, Any

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT

logger = logging.getLogger(__name__)

# Paleta de cores corporativa
PRIMARY_BLUE = colors.HexColor("#1E3A8A")
LIGHT_BLUE = colors.HexColor("#DBEAFE")
GRAY_TEXT = colors.HexColor("#374151")
LIGHT_GRAY = colors.HexColor("#F3F4F6")
WHITE = colors.white


def format_currency(value: float) -> str:
    """Formata valor monetário no padrão brasileiro."""
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def format_date(date_obj: datetime) -> str:
    """Formata data no padrão brasileiro dd/mm/aaaa."""
    return date_obj.strftime("%d/%m/%Y")


def generate_report(metrics: Dict[str, Any], holder_name: str, institutions: List[str]) -> io.BytesIO:
    """
    Gera o relatório executivo em PDF e retorna o arquivo em memória (BytesIO).
    
    Args:
        metrics: Dicionário com os cálculos do income_calculator.
        holder_name: Nome do titular.
        institutions: Lista de instituições financeiras identificadas.
        
    Returns:
        Buffer BytesIO contendo o PDF gerado.
    """
    buffer = io.BytesIO()
    
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=2 * cm,
        leftMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        title="Relatório Executivo de Apuração de Renda"
    )
    
    styles = getSampleStyleSheet()
    
    # Estilos personalizados
    title_style = ParagraphStyle(
        "CustomTitle",
        parent=styles["Heading1"],
        fontSize=18,
        textColor=PRIMARY_BLUE,
        spaceAfter=30,
        alignment=TA_CENTER,
        fontName="Helvetica-Bold"
    )
    
    subtitle_style = ParagraphStyle(
        "CustomSubtitle",
        parent=styles["Normal"],
        fontSize=11,
        textColor=GRAY_TEXT,
        alignment=TA_CENTER,
        spaceAfter=20
    )
    
    section_title_style = ParagraphStyle(
        "SectionTitle",
        parent=styles["Heading2"],
        fontSize=13,
        textColor=PRIMARY_BLUE,
        spaceBefore=20,
        spaceAfter=10,
        fontName="Helvetica-Bold"
    )
    
    footer_style = ParagraphStyle(
        "Footer",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.gray,
        alignment=TA_CENTER
    )
    
    story = []
    
    # 1. Cabeçalho
    story.append(Paragraph("Relatório Executivo de Apuração de Renda", title_style))
    
    institutions_str = ", ".join(institutions) if institutions else "Não identificadas"
    header_text = f"<b>Titular:</b> {holder_name or 'Não informado'}<br/>"
    header_text += f"<b>Instituição(ões):</b> {institutions_str}<br/>"
    header_text += f"<b>Data de Geração:</b> {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    story.append(Paragraph(header_text, subtitle_style))
    story.append(Spacer(1, 0.5 * cm))
    
    # 2. Cards de KPIs (Tabela com 3 colunas)
    kpi_data = [
        [
            Paragraph("<b>Total Geral Apurado</b>", subtitle_style),
            Paragraph("<b>Média Mensal Geral</b>", subtitle_style),
            Paragraph("<b>Média Meses Completos</b>", subtitle_style)
        ],
        [
            Paragraph(f"<b>{format_currency(metrics.get('total_geral', 0))}</b>", 
                     ParagraphStyle("kpi_val", parent=subtitle_style, textColor=PRIMARY_BLUE, fontSize=14, alignment=TA_CENTER)),
            Paragraph(f"<b>{format_currency(metrics.get('media_mensal_geral', 0))}</b>", 
                     ParagraphStyle("kpi_val", parent=subtitle_style, textColor=PRIMARY_BLUE, fontSize=14, alignment=TA_CENTER)),
            Paragraph(f"<b>{format_currency(metrics.get('media_meses_completos', 0))}</b>", 
                     ParagraphStyle("kpi_val", parent=subtitle_style, textColor=PRIMARY_BLUE, fontSize=14, alignment=TA_CENTER))
        ]
    ]
    
    kpi_table = Table(kpi_data, colWidths=[5.5 * cm] * 3)
    kpi_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BLUE),
        ("GRID", (0, 0), (-1, -1), 1, WHITE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(kpi_table)
    story.append(Spacer(1, 1 * cm))
    
    # 3. Resumo Consolidado por Mês
    story.append(Paragraph("Resumo Consolidado por Mês", section_title_style))
    
    resumo_data = [["Mês/Ano", "Qtd Entradas Válidas", "Total Válido Mensal"]]
    for item in metrics.get("resumo_mensal", []):
        resumo_data.append([
            item["month_label"],
            str(item["qtd_entradas_validas"]),
            format_currency(item["total_valido"])
        ])
    
    # Linha de TOTAL
    resumo_data.append([
        "TOTAL",
        str(sum(item["qtd_entradas_validas"] for item in metrics.get("resumo_mensal", []))),
        format_currency(metrics.get("total_geral", 0))
    ])
    
    resumo_table = Table(resumo_data, colWidths=[5 * cm, 5 * cm, 5.5 * cm], repeatRows=1)
    resumo_table.setStyle(TableStyle([
        # Cabeçalho
        ("BACKGROUND", (0, 0), (-1, 0), PRIMARY_BLUE),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        
        # Zebra striping
        *[("BACKGROUND", (0, i), (-1, i), LIGHT_GRAY if i % 2 == 0 else WHITE) for i in range(1, len(resumo_data))],
        
        # Linha de Total em destaque
        ("BACKGROUND", (0, -1), (-1, -1), LIGHT_BLUE),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        
        # Bordas e padding
        ("GRID", (0, 0), (-1, -1), 0.5, colors.gray),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(resumo_table)
    story.append(Spacer(1, 1 * cm))
    
    # 4. Detalhamento de Entradas Válidas Consideradas
    story.append(Paragraph("Detalhamento de Entradas Válidas Consideradas", section_title_style))
    
    valid_txs = metrics.get("entradas_validas", [])
    if valid_txs:
        detail_data = [["Data", "Descrição da Entrada", "Valor"]]
        for tx in valid_txs:
            detail_data.append([
                format_date(tx.date),
                tx.description,
                format_currency(tx.amount)
            ])
        
        detail_table = Table(detail_data, colWidths=[3 * cm, 8.5 * cm, 4 * cm], repeatRows=1)
        detail_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), PRIMARY_BLUE),
            ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ALIGN", (0, 0), (0, -1), "CENTER"),
            ("ALIGN", (2, 0), (2, -1), "RIGHT"),
            *[("BACKGROUND", (0, i), (-1, i), LIGHT_GRAY if i % 2 == 0 else WHITE) for i in range(1, len(detail_data))],
            ("GRID", (0, 0), (-1, -1), 0.5, colors.gray),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(detail_table)
    else:
        story.append(Paragraph("Nenhuma entrada válida encontrada.", subtitle_style))
    
    story.append(Spacer(1, 1 * cm))
    
    # 5. Tabela de Auditoria (Valores Excluídos)
    story.append(Paragraph("Tabela de Auditoria (Valores Excluídos)", section_title_style))
    
    excluded_txs = metrics.get("entradas_excluidas", [])
    if excluded_txs:
        audit_data = [["Data Ref.", "Descrição Original", "Regra de Exclusão Aplicada", "Valor"]]
        for item in excluded_txs:
            audit_data.append([
                format_date(item["date"]),
                item["description"],
                item["reason"],
                format_currency(item["amount"])
            ])
        
        audit_table = Table(audit_data, colWidths=[3 * cm, 6 * cm, 4 * cm, 2.5 * cm], repeatRows=1)
        audit_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), PRIMARY_BLUE),
            ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ALIGN", (0, 0), (0, -1), "CENTER"),
            ("ALIGN", (3, 0), (3, -1), "RIGHT"),
            *[("BACKGROUND", (0, i), (-1, i), LIGHT_GRAY if i % 2 == 0 else WHITE) for i in range(1, len(audit_data))],
            ("GRID", (0, 0), (-1, -1), 0.5, colors.gray),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(audit_table)
    else:
        story.append(Paragraph("Nenhum valor excluído.", subtitle_style))
    
    story.append(Spacer(1, 2 * cm))
    
    # 6. Rodapé com nota metodológica
    footer_text = (
        "Nota Metodológica: Este relatório consolida entradas financeiras identificadas nos extratos fornecidos, "
        "excluindo transferências de mesma titularidade, rendimentos de investimentos e créditos de jogos/apostas, "
        "conforme regras de negócio configuradas. A média de meses completos considera apenas períodos com mais de 20 dias de extrato."
    )
    story.append(Paragraph(footer_text, footer_style))
    
    # Construção do PDF
    doc.build(story)
    buffer.seek(0)
    
    return buffer