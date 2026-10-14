import os
import io
import functools
import pandas as pd
import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.graphics.shapes import Drawing, Rect, String, Line
from reportlab.graphics.charts.linecharts import HorizontalLineChart

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_PATH = os.path.join(BASE_DIR, "fonts", "arial.ttf")

PAGE_SIZE = A4
PAGE_WIDTH, PAGE_HEIGHT = PAGE_SIZE

TOP_MARGIN = 115
BOTTOM_MARGIN = 135
SIDE_MARGIN = 40

try:
    pdfmetrics.registerFont(TTFont('ArabicFont', FONT_PATH))
    GLOBAL_FONT = 'ArabicFont'
except Exception as e:
    print(f"Warning: Could not load custom font. {e}")
    GLOBAL_FONT = 'Helvetica'


@functools.lru_cache(maxsize=1)
def _get_cached_bg_reader() -> ImageReader | None:
    """Finds and caches internal.jpg in memory once so PDF builds never hit the disk."""
    candidate_names = [
        "internal.jpg", "internal.jpeg", "internal.png",
        "report_bg.png", "report_bg.jpg", "report_bg.jpeg",
        "background.jpg", "background.png"
    ]
    search_dirs = [
        BASE_DIR,
        os.path.join(BASE_DIR, "assets", "images"),
        os.path.join(BASE_DIR, "assets"),
        os.getcwd(),
    ]
    for folder in search_dirs:
        for fname in candidate_names:
            full_path = os.path.join(folder, fname)
            if os.path.isfile(full_path):
                try:
                    return ImageReader(full_path)
                except Exception as e:
                    print(f"Warning: Could not load background {full_path}: {e}")
    return None


class PDFGenerator:
    @staticmethod
    def _draw_background(canvas, doc):
        """Draws the cached internal.jpg across the entire page."""
        canvas.saveState()
        bg_reader = _get_cached_bg_reader()
        if bg_reader is not None:
            try:
                canvas.drawImage(
                    bg_reader,
                    0,
                    0,
                    width=PAGE_WIDTH,
                    height=PAGE_HEIGHT,
                    preserveAspectRatio=False,
                    mask='auto'
                )
            except Exception as e:
                print(f"Warning: Failed to draw PDF background: {e}")
        canvas.restoreState()

    @staticmethod
    def fix_arabic(text):
        if not text:
            return ""
        return get_display(arabic_reshaper.reshape(str(text)))

    @staticmethod
    def _get_base_styles():
        styles = getSampleStyleSheet()
        arabic_style = ParagraphStyle(
            name='ArabicStyle',
            fontName=GLOBAL_FONT,
            fontSize=11,
            leading=16,
            alignment=2
        )
        return styles, arabic_style

    @classmethod
    def _create_doc(cls, buffer):
        return SimpleDocTemplate(
            buffer,
            pagesize=PAGE_SIZE,
            rightMargin=SIDE_MARGIN,
            leftMargin=SIDE_MARGIN,
            topMargin=TOP_MARGIN,
            bottomMargin=BOTTOM_MARGIN
        )

    @classmethod
    def _build_trajectory_chart(cls, title: str, chart_data: list[dict], hex_color: str, width=248, height=130) -> Drawing:
        """Builds a vector line chart matching the Streamlit line_chart in Student Profile."""
        d = Drawing(width, height)
        # Background card box
        d.add(Rect(0, 0, width, height, fillColor=colors.HexColor("#FAFCFF"), strokeColor=colors.HexColor("#D6E0F0"), strokeWidth=0.75, rx=4, ry=4))
        d.add(String(10, height - 16, title, fontName="Helvetica-Bold", fontSize=9.5, fillColor=colors.HexColor("#1E293B")))

        scores = [float(item["Score"]) for item in chart_data if item.get("Score") is not None]
        labels = [cls.fix_arabic(str(item.get("Assignment", "")))[:10] for item in chart_data if item.get("Score") is not None]

        if not scores:
            d.add(String(width / 2, height / 2 - 4, "Not enough data to plot.", fontName="Helvetica", fontSize=8.5, fillColor=colors.grey, textAnchor="middle"))
            return d

        if len(scores) == 1:
            # Duplicate single point so HorizontalLineChart can draw a flat line across the chart
            scores = [scores[0], scores[0]]
            labels = [labels[0], labels[0]]

        lc = HorizontalLineChart()
        lc.x = 28
        lc.y = 20
        lc.width = width - 40
        lc.height = height - 44
        lc.data = [scores]

        lc.valueAxis.valueMin = 0
        lc.valueAxis.valueMax = 100
        lc.valueAxis.valueStep = 25
        lc.valueAxis.labels.fontName = "Helvetica"
        lc.valueAxis.labels.fontSize = 7
        lc.valueAxis.strokeColor = colors.HexColor("#CBD5E1")
        lc.valueAxis.visibleGrid = 1
        lc.valueAxis.gridStrokeColor = colors.HexColor("#E2E8F0")
        lc.valueAxis.gridStrokeWidth = 0.4

        lc.categoryAxis.categoryNames = labels
        lc.categoryAxis.labels.fontName = GLOBAL_FONT
        lc.categoryAxis.labels.fontSize = 6.5
        lc.categoryAxis.labels.dy = -8
        lc.categoryAxis.strokeColor = colors.HexColor("#CBD5E1")
        if len(labels) > 6:
            lc.categoryAxis.labels.angle = 20

        lc.lines[0].strokeColor = colors.HexColor(hex_color)
        lc.lines[0].strokeWidth = 2.0
        d.add(lc)
        return d

    @classmethod
    def generate_master_report(cls, homework_title, total_questions, grade_records):
        buffer = io.BytesIO()
        doc = cls._create_doc(buffer)
        styles, _ = cls._get_base_styles()

        elements = [
            Paragraph(f"<b>Grade Report: {homework_title}</b>", styles["Title"]),
            Paragraph(
                f"Maximum Score / Total Questions: {total_questions} | Enrolled Students: {len(grade_records)}",
                styles["Normal"]
            ),
            Spacer(1, 14)
        ]

        table_data = [["Student ID", "Student Name", "Score", "Out Of", "Percentage (%)"]]
        for sid, name, score, percentage in grade_records:
            score_disp = str(score) if score is not None else "-"
            perc_disp = f"{percentage:.1f}%" if percentage is not None else "-"
            table_data.append([str(sid), cls.fix_arabic(name), score_disp, str(total_questions), perc_disp])

        t = Table(table_data, colWidths=[75, 195, 85, 65, 95], repeatRows=1)
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#2B5B9E")),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('ALIGN', (1, 1), (1, -1), 'RIGHT'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTNAME', (0, 1), (-1, -1), GLOBAL_FONT),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4F7FB")]),
        ]))

        elements.append(t)
        doc.build(elements, onFirstPage=cls._draw_background, onLaterPages=cls._draw_background)
        buffer.seek(0)
        return buffer

    @classmethod
    def generate_student_report(cls, student_name, homework_title, score, total_questions, percentage, report_text,
                                image_bytes=None, video_link=None):
        buffer = io.BytesIO()
        doc = cls._create_doc(buffer)
        styles, arabic_style = cls._get_base_styles()

        elements = [
            Paragraph(f"<b>{homework_title} Feedback</b>", styles["Title"]),
            Paragraph(f"Student: {cls.fix_arabic(student_name)}", arabic_style),
            Spacer(1, 12)
        ]

        score_disp = str(score) if score is not None else "-"
        perc_disp = f"{percentage:.1f}%" if percentage is not None else "-"

        t = Table([
            ["Metric", "Value"],
            ["Score", f"{score_disp} / {total_questions}"],
            ["Percentage", perc_disp]
        ], colWidths=[150, 150])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#2B5B9E")),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTNAME', (0, 1), (-1, -1), GLOBAL_FONT),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ('BACKGROUND', (0, 1), (-1, -1), colors.white),
        ]))

        elements.extend([t, Spacer(1, 18)])

        raw_report = str(report_text) if pd.notna(report_text) and str(report_text).strip() else "No feedback provided."
        formatted_report = "<br/>".join([cls.fix_arabic(line) for line in raw_report.split('\n')])

        elements.extend([
            Paragraph("<b>Teacher's Report:</b>", styles["Normal"]),
            Spacer(1, 6),
            Paragraph(formatted_report, arabic_style)
        ])

        if image_bytes:
            elements.extend(
                [Spacer(1, 14), Paragraph("<b>Attached Solution / Notes:</b>", styles["Normal"]), Spacer(1, 6)]
            )
            try:
                img = Image(io.BytesIO(image_bytes), width=460, height=360, kind='proportional')
                elements.append(img)
            except Exception:
                elements.append(Paragraph("(Error rendering attached image)", styles["Normal"]))

        if video_link:
            clean_link = str(video_link).strip()
            link_label = "لمشاهدة فيديو الواجب، اضغط هنا"
            bidi_label = cls.fix_arabic(link_label)
            link_html = f'<para align="center"><a href="{clean_link}" color="blue"><u>{bidi_label}</u></a></para>'
            elements.extend([
                Spacer(1, 16),
                Paragraph(link_html, arabic_style)
            ])

        doc.build(elements, onFirstPage=cls._draw_background, onLaterPages=cls._draw_background)
        buffer.seek(0)
        return buffer

    @classmethod
    def generate_student_profile_report(
        cls,
        student_info,
        status_text,
        avg_display,
        hw_records,
        qz_records,
        hw_avg_display="N/A",
        weaknesses=None,
        hw_chart_data=None,
        qz_chart_data=None,
    ):
        """
        Generates a Student Profile PDF that mirrors the exact UI layout of StudentProfileView:
        1. Top 3-Column Summary Card (Student Info | Quiz Average | Homework Average + Status)
        2. Side-by-Side Performance Timeline Charts (Homework Trajectory | Quiz Trajectory)
        3. Side-by-Side Priority Focus Areas (<70%) & Teacher's Improvement Plan
        4. Side-by-Side Homework History & Quiz History Tables
        """
        buffer = io.BytesIO()
        doc = cls._create_doc(buffer)
        styles, arabic_style = cls._get_base_styles()

        left_style = ParagraphStyle(
            name="ProfileLeft",
            fontName=GLOBAL_FONT,
            fontSize=9.5,
            leading=13.5,
            alignment=0,
            textColor=colors.HexColor("#1E293B"),
        )
        metric_style = ParagraphStyle(
            name="ProfileMetric",
            fontName=GLOBAL_FONT,
            fontSize=9.5,
            leading=13.5,
            alignment=1,
            textColor=colors.HexColor("#1E293B"),
        )
        section_hdr_style = ParagraphStyle(
            name="SectionHdr",
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=14,
            textColor=colors.HexColor("#1E293B"),
        )
        cell_style = ParagraphStyle(
            name="TableCell",
            fontName=GLOBAL_FONT,
            fontSize=8.5,
            leading=11,
            alignment=1,
        )

        # Clean emojis out of status text so Helvetica/Arial renders cleanly
        clean_status = (
            str(status_text)
            .replace("🌟", "")
            .replace("👍", "")
            .replace("⚠️", "")
            .replace("⚪", "")
            .strip()
        )
        if "Excellent" in clean_status:
            status_color = "#15803D"
        elif "Average" in clean_status and "Below" not in clean_status:
            status_color = "#D97706"
        elif "Below" in clean_status:
            status_color = "#DC2626"
        else:
            status_color = "#64748B"

        grp = student_info.get("group_number")
        grp_disp = str(grp).strip() if pd.notna(grp) and str(grp).strip() else "N/A"
        s_phone = student_info.get("phone")
        s_phone_disp = str(s_phone).strip() if pd.notna(s_phone) and str(s_phone).strip() else "N/A"
        p_phone = student_info.get("phone_parent")
        p_phone_disp = str(p_phone).strip() if pd.notna(p_phone) and str(p_phone).strip() else "N/A"

        # -------------------------------------------------------------------------
        # 1. TOP SUMMARY CARD (Matches 3-column bordered container in UI)
        # -------------------------------------------------------------------------
        stu_name_fixed = cls.fix_arabic(student_info.get("name", ""))
        col1_html = (
            f"<font size='13'><b>{stu_name_fixed}</b></font><br/>"
            f"<b>ID:</b> {student_info.get('id', '')} &nbsp;&nbsp;|&nbsp;&nbsp; <b>Group:</b> {cls.fix_arabic(grp_disp)}<br/>"
            f"<b>Student Phone:</b> {s_phone_disp} &nbsp;&nbsp;|&nbsp;&nbsp; <b>Parent Phone:</b> {p_phone_disp}"
        )
        col2_html = (
            f"<font size='8' color='#64748B'>QUIZ AVERAGE</font><br/>"
            f"<font size='14' color='#2B5B9E'><b>{avg_display}</b></font><br/>"
            f"<font size='8' color='#475569'>{len(qz_records or [])} Quizzes</font>"
        )
        col3_html = (
            f"<font size='8' color='#64748B'>HOMEWORK AVERAGE</font><br/>"
            f"<font size='14' color='#2B5B9E'><b>{hw_avg_display}</b></font><br/>"
            f"<font size='8.5' color='{status_color}'><b>{clean_status}</b></font>"
        )

        header_card = Table(
            [[Paragraph(col1_html, left_style), Paragraph(col2_html, metric_style), Paragraph(col3_html, metric_style)]],
            colWidths=[265, 125, 125],
        )
        header_card.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
            ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#CBD5E1")),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ]))

        elements = [header_card, Spacer(1, 10)]

        # -------------------------------------------------------------------------
        # 2. SIDE-BY-SIDE PERFORMANCE TIMELINES (Line Charts)
        # -------------------------------------------------------------------------
        if hw_chart_data is None:
            hw_chart_data = []
            for r in reversed(hw_records or []):
                raw_p = str(r.get("Percentage", "")).replace("%", "").strip()
                try:
                    hw_chart_data.append({"Assignment": r.get("Homework", ""), "Score": float(raw_p)})
                except ValueError:
                    pass

        if qz_chart_data is None:
            qz_chart_data = []
            for r in reversed(qz_records or []):
                raw_p = str(r.get("Percentage", "")).replace("%", "").strip()
                try:
                    qz_chart_data.append({"Assignment": r.get("Quiz", ""), "Score": float(raw_p)})
                except ValueError:
                    pass

        elements.append(Paragraph("<b>Performance Timelines</b>", section_hdr_style))
        elements.append(Spacer(1, 4))

        hw_drawing = cls._build_trajectory_chart("Homework Trajectory", hw_chart_data, "#1f77b4", width=250, height=118)
        qz_drawing = cls._build_trajectory_chart("Quiz Trajectory", qz_chart_data, "#ff7f0e", width=250, height=118)

        charts_table = Table([[hw_drawing, qz_drawing]], colWidths=[257.5, 257.5])
        charts_table.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        elements.extend([charts_table, Spacer(1, 10)])

        # -------------------------------------------------------------------------
        # 3. SIDE-BY-SIDE PRIORITY FOCUS AREAS & TEACHER'S IMPROVEMENT PLAN
        # -------------------------------------------------------------------------
        if weaknesses is None:
            weaknesses = []
            for item in hw_chart_data:
                if item["Score"] < 70:
                    weaknesses.append({"Topic": f"{item['Assignment']} (HW)", "Percentage": item["Score"]})
            for item in qz_chart_data:
                if item["Score"] < 70:
                    weaknesses.append({"Topic": f"{item['Assignment']} (Quiz)", "Percentage": item["Score"]})
            weaknesses.sort(key=lambda x: x["Percentage"])

        if weaknesses:
            lines = [
                f"<font color='#B91C1C'>• <b>{cls.fix_arabic(w['Topic'])}</b>: {w['Percentage']:.1f}%</font>"
                for w in weaknesses[:3]
            ]
            focus_body = "<br/>".join(lines)
            focus_bg = colors.HexColor("#FEF2F2")
            focus_border = colors.HexColor("#FCA5A5")
        else:
            focus_body = "<font color='#15803D'><b>No critical weaknesses detected! All assignments are at 70% or above.</b></font>"
            focus_bg = colors.HexColor("#F0FDF4")
            focus_border = colors.HexColor("#86EFAC")

        focus_html = (
            "<b>Priority Focus Areas</b><br/>"
            "<font size='8' color='#64748B'>Top 3 weakest topics (Below 70%)</font><br/><br/>"
            + focus_body
        )

        notes = student_info.get("notes", "")
        if pd.notna(notes) and str(notes).strip():
            formatted_notes = "<br/>".join([cls.fix_arabic(line) for line in str(notes).split("\n")])
        else:
            formatted_notes = "<font color='#64748B'>No teacher notes recorded yet.</font>"

        notes_html = (
            "<b>Teacher's Improvement Plan</b><br/>"
            "<font size='8' color='#64748B'>Actionable advice, goals, and notes</font><br/><br/>"
            + formatted_notes
        )

        focus_box = Table([[Paragraph(focus_html, left_style)]], colWidths=[250])
        focus_box.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), focus_bg),
            ("BOX", (0, 0), (-1, -1), 0.8, focus_border),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ]))

        notes_box = Table([[Paragraph(notes_html, left_style)]], colWidths=[250])
        notes_box.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
            ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#CBD5E1")),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ]))

        mid_row = Table([[focus_box, notes_box]], colWidths=[257.5, 257.5])
        mid_row.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        elements.extend([mid_row, Spacer(1, 12)])

        # -------------------------------------------------------------------------
        # 4. SIDE-BY-SIDE HOMEWORK HISTORY & QUIZ HISTORY TABLES
        # -------------------------------------------------------------------------
        hw_data = [["Assignment", "Score", "Out Of", "%"]]
        if not hw_records:
            hw_data.append(["No homework grades", "-", "-", "-"])
        else:
            for row in hw_records:
                p_val = row.get("Percentage", "-")
                if isinstance(p_val, (int, float)) and pd.notna(p_val):
                    p_str = f"{p_val:.1f}%"
                else:
                    p_str = str(p_val) if pd.notna(p_val) else "-"
                hw_data.append([
                    Paragraph(cls.fix_arabic(str(row.get("Homework", ""))), cell_style),
                    str(row.get("Score", "-")),
                    str(row.get("Out Of", "-")),
                    p_str,
                ])

        hw_table = Table(hw_data, colWidths=[115, 42, 42, 51], repeatRows=1)
        hw_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2B5B9E")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("FONTNAME", (0, 1), (-1, -1), GLOBAL_FONT),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
            ("TOPPADDING", (0, 0), (-1, 0), 6),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4F7FB")]),
        ]))

        qz_data = [["Quiz", "Score", "Out Of", "%"]]
        if not qz_records:
            qz_data.append(["No quiz grades", "-", "-", "-"])
        else:
            for row in qz_records:
                p_val = row.get("Percentage", "-")
                if isinstance(p_val, (int, float)) and pd.notna(p_val):
                    p_str = f"{p_val:.1f}%"
                else:
                    p_str = str(p_val) if pd.notna(p_val) else "-"
                qz_data.append([
                    Paragraph(cls.fix_arabic(str(row.get("Quiz", ""))), cell_style),
                    str(row.get("Score", "-")),
                    str(row.get("Out Of", "-")),
                    p_str,
                ])

        qz_table = Table(qz_data, colWidths=[115, 42, 42, 51], repeatRows=1)
        qz_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2B5B9E")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("FONTNAME", (0, 1), (-1, -1), GLOBAL_FONT),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4F7FB")]),
        ]))

        history_headers = Table(
            [[Paragraph("<b>Homework History</b>", section_hdr_style), Paragraph("<b>Quiz History</b>", section_hdr_style)]],
            colWidths=[257.5, 257.5],
        )
        history_headers.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ]))

        history_tables = Table([[hw_table, qz_table]], colWidths=[257.5, 257.5])
        history_tables.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))

        elements.extend([history_headers, Spacer(1, 4), history_tables])

        doc.build(elements, onFirstPage=cls._draw_background, onLaterPages=cls._draw_background)
        buffer.seek(0)
        return buffer
