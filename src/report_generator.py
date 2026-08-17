"""
Geração de artefatos de saída:
- generate_report(): PDF executivo (reportlab) com células em Paragraph
  (quebra de linha correta, sem estouro de coluna);
- generate_excel(): .xlsx com 3 abas (openpyxl);
- generate_csv(): .csv (utf-8-sig, separador ';' para Excel pt-BR).

Rastreabilidade da revisão manual (TAREFA 5):
- lançamentos válidos confirmados manualmente pelo operador recebem o
  marcador "*" na descrição + nota de rodapé explicativa;
- motivos de exclusão manual chegam na coluna de motivo da auditoria
  ("Excluída manualmente pelo usuário (motivo)"), produzidos pelo
  rules_engine a partir do Dict[int, str] da tela de revisão.

Compatibilidade: lê tx.manually_confirmed via getattr — transações sem o
atributo (fluxos antigos) comportam-se como não-manuais.
"""
import io
import csv
import logging
from datetime import datetime
from typing import Dict, List, Any
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

logger = logging.getLogger(__name__)

PRIMARY_BLUE = colors.HexColor("#1E3A8A")
LIGHT_BLUE = colors.HexColor("#DBEAFE")
GRAY_TEXT = colors.HexColor("#374151")
LIGHT_GRAY = colors.HexColor("#F3F4F6")
WHITE = colors.white

NOTA_CONFIRMACAO_MANUAL = (
    "* sinal de crédito/débito confirmado manualmente pelo operador"
)


def format_currency(value: float) -> str:
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def format_date(date_obj) -> str:
    return date_obj.strftime("%d/%m/%Y")


def _is_manual(tx) -> bool:
    """True se o lançamento foi confirmado manualmente pelo operador."""
    return bool(getattr(tx, "manually_confirmed", False))


def _build_cell_styles() -> Dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    cell = ParagraphStyle("Cell", parent=base["Normal"], fontName="Helvetica",
                          fontSize=7.5, leading=9.5, textColor=GRAY_TEXT)
    return {
        "cell_l": cell,
        "cell_c": ParagraphStyle("CellC", parent=cell, alignment=TA_CENTER),
        "cell_r": ParagraphStyle("CellR", parent=cell, alignment=TA_RIGHT),
        "head_l": ParagraphStyle("HeadL", parent=cell, fontName="Helvetica-Bold",
                                 fontSize=8, textColor=WHITE, alignment=TA_LEFT),
        "head_c": ParagraphStyle("HeadC", parent=cell, fontName="Helvetica-Bold",
                                 fontSize=8, textColor=WHITE, alignment=TA_CENTER),
        "head_r": ParagraphStyle("HeadR", parent=cell, fontName="Helvetica-Bold",
                                 fontSize=8, textColor=WHITE, alignment=TA_RIGHT),
        "tot_c": ParagraphStyle("TotC", parent=cell, fontName="Helvetica-Bold",
                                alignment=TA_CENTER),
        "tot_r": ParagraphStyle("TotR", parent=cell, fontName="Helvetica-Bold",
                                alignment=TA_RIGHT),
        "note": ParagraphStyle("Note", parent=base["Normal"], fontSize=8,
                               textColor=GRAY_TEXT),
    }


def _zebra(style_cmds: List, nrows: int, first_data_row: int = 1):
    for i in range(first_data_row, nrows):
        style_cmds.append(("BACKGROUND", (0, i), (-1, i),
                           LIGHT_GRAY if i % 2 == 0 else WHITE))
    style_cmds += [
        ("GRID", (0, 0), (-1, -1), 0.5, colors.gray),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]
    return style_cmds


def generate_report(metrics: Dict[str, Any], holder_name: str,
                    institutions: List[str]) -> io.BytesIO:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        rightMargin=2 * cm, leftMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
        title="Relatório Executivo de Apuração de Renda",
    )
    styles = getSampleStyleSheet()
    S = _build_cell_styles()

    title_style = ParagraphStyle("CustomTitle", parent=styles["Heading1"], fontSize=18,
                                 textColor=PRIMARY_BLUE, spaceAfter=30, alignment=TA_CENTER,
                                 fontName="Helvetica-Bold")
    subtitle_style = ParagraphStyle("CustomSubtitle", parent=styles["Normal"], fontSize=11,
                                    textColor=GRAY_TEXT, alignment=TA_CENTER, spaceAfter=20)
    section_title_style = ParagraphStyle("SectionTitle", parent=styles["Heading2"],
                                         fontSize=13, textColor=PRIMARY_BLUE,
                                         spaceBefore=20, spaceAfter=10,
                                         fontName="Helvetica-Bold")
    footer_style = ParagraphStyle("Footer", parent=styles["Normal"], fontSize=8,
                                  textColor=colors.gray, alignment=TA_CENTER)
    kpi_value_style = ParagraphStyle("kpi_val", parent=subtitle_style,
                                     textColor=PRIMARY_BLUE, fontSize=14,
                                     alignment=TA_CENTER)

    story = []
    story.append(Paragraph("Relatório Executivo de Apuração de Renda", title_style))
    institutions_str = ", ".join(institutions) if institutions else "Não identificadas"
    header_text = (f"<b>Titular:</b> {escape(holder_name or 'Não informado')}<br/>"
                   f"<b>Instituição(ões):</b> {escape(institutions_str)}<br/>"
                   f"<b>Data de Geração:</b> {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    story.append(Paragraph(header_text, subtitle_style))
    story.append(Spacer(1, 0.5 * cm))

    # --- Cards de KPI ---
    kpi_data = [
        [Paragraph("<b>Total Geral Apurado</b>", subtitle_style),
         Paragraph("<b>Média Mensal Geral</b>", subtitle_style),
         Paragraph("<b>Média Meses Completos</b>", subtitle_style)],
        [Paragraph(f"<b>{format_currency(metrics.get('total_geral', 0))}</b>", kpi_value_style),
         Paragraph(f"<b>{format_currency(metrics.get('media_mensal_geral', 0))}</b>", kpi_value_style),
         Paragraph(f"<b>{format_currency(metrics.get('media_meses_completos', 0))}</b>", kpi_value_style)],
    ]
    kpi_table = Table(kpi_data, colWidths=[5.5 * cm] * 3)
    kpi_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BLUE),
        ("GRID", (0, 0), (-1, -1), 1, WHITE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(kpi_table)
    story.append(Spacer(1, 1 * cm))

    # --- Resumo Consolidado por Mês ---
    story.append(Paragraph("Resumo Consolidado por Mês", section_title_style))
    resumo_data = [[Paragraph("Mês/Ano", S["head_c"]),
                    Paragraph("Qtd Entradas Válidas", S["head_c"]),
                    Paragraph("Total Válido Mensal", S["head_r"])]]
    for item in metrics.get("resumo_mensal", []):
        resumo_data.append([
            Paragraph(item["month_label"], S["cell_c"]),
            Paragraph(str(item["qtd_entradas_validas"]), S["cell_c"]),
            Paragraph(format_currency(item["total_valido"]), S["cell_r"]),
        ])
    resumo_data.append([
        Paragraph("TOTAL", S["tot_c"]),
        Paragraph(str(sum(i["qtd_entradas_validas"] for i in metrics.get("resumo_mensal", []))), S["tot_c"]),
        Paragraph(format_currency(metrics.get("total_geral", 0)), S["tot_r"]),
    ])
    resumo_table = Table(resumo_data, colWidths=[4 * cm, 6.5 * cm, 6.5 * cm], repeatRows=1)
    resumo_table.setStyle(TableStyle(_zebra([
        ("BACKGROUND", (0, 0), (-1, 0), PRIMARY_BLUE),
        ("BACKGROUND", (0, len(resumo_data) - 1), (-1, len(resumo_data) - 1), LIGHT_BLUE),
    ], len(resumo_data))))
    story.append(resumo_table)
    story.append(Spacer(1, 1 * cm))

    # --- Detalhamento de Entradas Válidas (com marcador de confirmação manual) ---
    story.append(Paragraph("Detalhamento de Entradas Válidas Consideradas", section_title_style))
    valid_txs = metrics.get("entradas_validas", [])
    manual_count = 0
    if valid_txs:
        detail_data = [[Paragraph("Data", S["head_c"]),
                        Paragraph("Descrição da Entrada", S["head_l"]),
                        Paragraph("Valor", S["head_r"])]]
        for tx in valid_txs:
            desc = escape(tx.description or "-")
            if _is_manual(tx):
                desc += " *"
                manual_count += 1
            detail_data.append([
                Paragraph(format_date(tx.date), S["cell_c"]),
                Paragraph(desc, S["cell_l"]),
                Paragraph(format_currency(tx.amount), S["cell_r"]),
            ])
        detail_table = Table(detail_data, colWidths=[2.5 * cm, 11 * cm, 3.5 * cm], repeatRows=1)
        detail_table.setStyle(TableStyle(_zebra([
            ("BACKGROUND", (0, 0), (-1, 0), PRIMARY_BLUE),
        ], len(detail_data))))
        story.append(detail_table)
        if manual_count > 0:
            story.append(Spacer(1, 0.2 * cm))
            story.append(Paragraph(NOTA_CONFIRMACAO_MANUAL, S["note"]))
    else:
        story.append(Paragraph("Nenhuma entrada válida encontrada.", subtitle_style))
    story.append(Spacer(1, 1 * cm))

    # --- Tabela de Auditoria (motivos automáticos E manuais na mesma coluna) ---
    story.append(Paragraph("Tabela de Auditoria (Valores Excluídos)", section_title_style))
    excluded_txs = metrics.get("entradas_excluidas", [])
    if excluded_txs:
        audit_data = [[Paragraph("Data Ref.", S["head_c"]),
                       Paragraph("Descrição Original", S["head_l"]),
                       Paragraph("Regra de Exclusão Aplicada", S["head_l"]),
                       Paragraph("Valor", S["head_r"])]]
        for item in excluded_txs:
            audit_data.append([
                Paragraph(format_date(item["date"]), S["cell_c"]),
                Paragraph(escape(item["description"] or "-"), S["cell_l"]),
                Paragraph(escape(item["reason"] or "-"), S["cell_l"]),
                Paragraph(format_currency(item["amount"]), S["cell_r"]),
            ])
        audit_table = Table(audit_data, colWidths=[2.5 * cm, 6.5 * cm, 4.5 * cm, 3.5 * cm], repeatRows=1)
        audit_table.setStyle(TableStyle(_zebra([
            ("BACKGROUND", (0, 0), (-1, 0), PRIMARY_BLUE),
        ], len(audit_data))))
        story.append(audit_table)
    else:
        story.append(Paragraph("Nenhum valor excluído.", subtitle_style))
    story.append(Spacer(1, 2 * cm))

    footer_text = (
        "Nota Metodológica: Este relatório consolida entradas financeiras identificadas nos "
        "extratos fornecidos, excluindo transferências de mesma titularidade, rendimentos de "
        "investimentos e créditos de jogos/apostas, conforme regras de negócio configuradas. "
        "A média de meses completos considera apenas períodos com mais de 20 dias de extrato. "
        "Lançamentos marcados com '*' tiveram o sinal confirmado manualmente pelo operador."
    )
    story.append(Paragraph(footer_text, footer_style))
    doc.build(story)
    buffer.seek(0)
    return buffer


def _excel_header_style(ws, ncols: int):
    from openpyxl.styles import Font, PatternFill
    fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
    for col in range(1, ncols + 1):
        cell = ws.cell(row=1, column=col)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = fill


def generate_excel(metrics: Dict[str, Any], holder_name: str = "",
                   institutions: List[str] = None) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()

    ws1 = wb.active
    ws1.title = "Resumo_Mensal"
    ws1.append(["Mês/Ano", "Qtd Entradas Válidas", "Total Válido Mensal"])
    _excel_header_style(ws1, 3)
    for item in metrics.get("resumo_mensal", []):
        ws1.append([item["month_label"], item["qtd_entradas_validas"],
                    round(item["total_valido"], 2)])
    ws1.append(["TOTAL",
                sum(i["qtd_entradas_validas"] for i in metrics.get("resumo_mensal", [])),
                round(metrics.get("total_geral", 0.0), 2)])
    ws1.append([])
    ws1.append(["Titular", holder_name or "Não informado"])
    ws1.append(["Instituições", ", ".join(institutions or []) or "Não identificadas"])
    ws1.append(["Total Geral Apurado", round(metrics.get("total_geral", 0.0), 2)])
    ws1.append(["Média Mensal Geral", round(metrics.get("media_mensal_geral", 0.0), 2)])
    ws1.append(["Média Meses Completos", round(metrics.get("media_meses_completos", 0.0), 2)])
    ws1.column_dimensions["A"].width = 18
    ws1.column_dimensions["B"].width = 22
    ws1.column_dimensions["C"].width = 22

    ws2 = wb.create_sheet("Entradas_Validas")
    ws2.append(["Data", "Descrição da Entrada", "Valor", "Banco", "Obs."])
    _excel_header_style(ws2, 5)
    manual_any = False
    for tx in metrics.get("entradas_validas", []):
        manual = _is_manual(tx)
        manual_any = manual_any or manual
        ws2.append([format_date(tx.date), tx.description, round(tx.amount, 2),
                    getattr(tx, "bank", ""), "*" if manual else ""])
    if manual_any:
        ws2.append([])
        ws2.append([NOTA_CONFIRMACAO_MANUAL])
    ws2.column_dimensions["A"].width = 12
    ws2.column_dimensions["B"].width = 80
    ws2.column_dimensions["C"].width = 14
    ws2.column_dimensions["D"].width = 14
    ws2.column_dimensions["E"].width = 8

    ws3 = wb.create_sheet("Auditoria_Excluidos")
    ws3.append(["Data Ref.", "Descrição Original", "Regra de Exclusão Aplicada", "Valor"])
    _excel_header_style(ws3, 4)
    for item in metrics.get("entradas_excluidas", []):
        ws3.append([format_date(item["date"]), item["description"], item["reason"],
                    round(item["amount"], 2)])
    ws3.column_dimensions["A"].width = 12
    ws3.column_dimensions["B"].width = 80
    ws3.column_dimensions["C"].width = 50
    ws3.column_dimensions["D"].width = 14

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def generate_csv(metrics: Dict[str, Any]) -> bytes:
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")

    w.writerow(["RESUMO MENSAL"])
    w.writerow(["Mês/Ano", "Qtd Entradas Válidas", "Total Válido Mensal"])
    for item in metrics.get("resumo_mensal", []):
        w.writerow([item["month_label"], item["qtd_entradas_validas"],
                    f"{item['total_valido']:.2f}".replace(".", ",")])
    w.writerow(["TOTAL",
                sum(i["qtd_entradas_validas"] for i in metrics.get("resumo_mensal", [])),
                f"{metrics.get('total_geral', 0.0):.2f}".replace(".", ",")])
    w.writerow([])

    w.writerow(["ENTRADAS VÁLIDAS"])
    w.writerow(["Data", "Descrição da Entrada", "Valor", "Banco", "Obs."])
    manual_any = False
    for tx in metrics.get("entradas_validas", []):
        manual = _is_manual(tx)
        manual_any = manual_any or manual
        w.writerow([format_date(tx.date), tx.description,
                    f"{tx.amount:.2f}".replace(".", ","), getattr(tx, "bank", ""),
                    "*" if manual else ""])
    if manual_any:
        w.writerow([])
        w.writerow([NOTA_CONFIRMACAO_MANUAL])
    w.writerow([])

    w.writerow(["AUDITORIA - EXCLUÍDOS"])
    w.writerow(["Data Ref.", "Descrição Original", "Regra de Exclusão Aplicada", "Valor"])
    for item in metrics.get("entradas_excluidas", []):
        w.writerow([format_date(item["date"]), item["description"], item["reason"],
                    f"{item['amount']:.2f}".replace(".", ",")])

    return out.getvalue().encode("utf-8-sig")