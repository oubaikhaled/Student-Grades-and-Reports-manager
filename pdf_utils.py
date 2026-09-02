import os
import io
import pandas as pd
import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# Safely resolve the absolute path to the fonts folder
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_PATH = os.path.join(BASE_DIR, "fonts", "arial.ttf")

try:
    pdfmetrics.registerFont(TTFont('ArabicFont', FONT_PATH))
    GLOBAL_FONT = 'ArabicFont'
except Exception as e:
    # If it fails, fallback to standard Helvetica so the app doesn't crash
    print(f"Warning: Could not load custom font. {e}")
    GLOBAL_FONT = 'Helvetica'

class PDFGenerator:
    @staticmethod
    def fix_arabic(text):
        if not text: return ""
        return get_display(arabic_reshaper.reshape(str(text)))

    @staticmethod
    def _get_base_styles():
        styles = getSampleStyleSheet()
        arabic_style = ParagraphStyle(name='ArabicStyle', fontName=GLOBAL_FONT, fontSize=12, leading=18, alignment=2)
        return styles, arabic_style

    @classmethod
    def generate_master_report(cls, homework_title, total_questions, grade_records):
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
        styles, _ = cls._get_base_styles()
        
        elements = [
            Paragraph(f"<b>Grade Report: {homework_title}</b>", styles["Title"]),
            Paragraph(f"Total Questions: {total_questions} | Enrolled Students: {len(grade_records)}", styles["Normal"]),
            Spacer(1, 16)
        ]

        table_data = [["Student ID", "Student Name", "Correct Answers", "Total", "Percentage (%)"]]
        for sid, name, score, percentage in grade_records:
            score_disp = str(score) if score is not None else "-"
            perc_disp = f"{percentage:.1f}%" if percentage is not None else "-"
            table_data.append([str(sid), cls.fix_arabic(name), score_disp, str(total_questions), perc_disp])

        t = Table(table_data, colWidths=[80, 200, 100, 60, 90])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#2C3E50")),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('ALIGN', (1, 1), (1, -1), 'RIGHT'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTNAME', (0, 1), (-1, -1), GLOBAL_FONT), # Now uses the dynamic font variable
            ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8F9F9")]),
        ]))
        
        elements.append(t)
        doc.build(elements)
        buffer.seek(0)
        return buffer

    @classmethod
    def generate_student_report(cls, student_name, homework_title, score, total_questions, percentage, report_text, image_bytes=None):
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
        styles, arabic_style = cls._get_base_styles()
        
        elements = [
            Paragraph(f"<b>{homework_title} Feedback</b>", styles["Title"]),
            Paragraph(f"Student: {cls.fix_arabic(student_name)}", arabic_style),
            Spacer(1, 16)
        ]

        score_disp = str(score) if score is not None else "-"
        perc_disp = f"{percentage:.1f}%" if percentage is not None else "-"

        t = Table([
            ["Metric", "Value"],
            ["Score", f"{score_disp} / {total_questions}"],
            ["Percentage", perc_disp]
        ], colWidths=[150, 150])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#2C3E50")),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTNAME', (0, 1), (-1, -1), GLOBAL_FONT), # Now uses the dynamic font variable
            ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.lightgrey),
        ]))
        
        elements.extend([t, Spacer(1, 24)])
        elements.extend([
            Paragraph("<b>Teacher's Report:</b>", styles["Normal"]),
            Spacer(1, 8),
            Paragraph(cls.fix_arabic(str(report_text) if pd.notna(report_text) and str(report_text).strip() else "No feedback provided."), arabic_style)
        ])

        if image_bytes:
            elements.extend([Spacer(1, 24), Paragraph("<b>Attached Solution / Notes:</b>", styles["Normal"]), Spacer(1, 8)])
            try:
                img = Image(io.BytesIO(image_bytes), width=400, height=250, kind='proportional')
                elements.append(img)
            except Exception:
                elements.append(Paragraph("(Error rendering attached image)", styles["Normal"]))

        doc.build(elements)
        buffer.seek(0)
        return buffer
