from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch
from datetime import datetime
from pathlib import Path

def generate_notes_pdf(notes, output_path="notes_export.pdf"):
    """
    notes: list of tuples (id, text, created_at, pinned)
    output_path: file to save
    """
    c = canvas.Canvas(output_path, pagesize=A4)
    width, height = A4

    y = height - 1 * inch

    # Title
    c.setFont("Helvetica-Bold", 20)
    c.drawString(1 * inch, y, "Your Notes Export")
    y -= 0.4 * inch

    # Timestamp
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, y, f"Generated on: {datetime.utcnow().isoformat()}")
    y -= 0.3 * inch

    # Divider
    c.line(1 * inch, y, width - 1 * inch, y)
    y -= 0.4 * inch

    c.setFont("Helvetica", 12)

    for nid, text, created_at, pinned in notes:
        star = "⭐ " if pinned else ""
        line = f"{star}{nid}) {text}   ({created_at})"

        # If line too long, wrap manually
        max_chars = 80
        lines = [line[i:i+max_chars] for i in range(0, len(line), max_chars)]

        for l in lines:
            if y < 1 * inch:
                c.showPage()
                y = height - 1 * inch
                c.setFont("Helvetica", 12)

            c.drawString(1 * inch, y, l)
            y -= 0.25 * inch

        y -= 0.1 * inch

    c.save()
    return output_path
