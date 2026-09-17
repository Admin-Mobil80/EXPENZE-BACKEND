"""Turning a PDF receipt into something the extraction path can read.

Plenty of real receipts are PDFs: a cab invoice emailed by the operator, a
hotel folio, a SaaS bill. The extraction path takes images, so before any of
that could be audited it had to be rendered - and until this existed those
receipts were charged a credit, stored, and then parked for a human.

Three decisions worth knowing about:

**Rendered at 200 DPI, not at whatever the page says.** A PDF page carries no
pixels, only instructions, so the resolution is ours to pick. Too low and a
handwritten total becomes unreadable; too high and a two-page invoice is a
20MB payload that costs more to send than the receipt is worth. 200 DPI is
about what a phone camera gets close to a page, which is what the extraction
was tuned on.

**Every page, up to a limit.** A hotel folio puts the total on page two often
enough that reading only page one would quietly under-report. The cap exists
because a 60-page statement is not a receipt and nobody should pay to have one
read.

**A PDF that will not open is not an error to shout about.** It is a person who
attached the wrong thing, or a scanner that produced something malformed. The
caller gets nothing back and parks it for a human, which is what would have
happened anyway.
"""
from __future__ import annotations

import base64
import io
import logging
from typing import Any

logger = logging.getLogger()

# What a phone camera achieves held close to a page. Below about 150 the model
# starts losing handwritten digits, which is the thing that matters most.
RENDER_DPI = 200
PDF_BASE_DPI = 72

# A receipt is a page or two. Anything longer is a statement, and rendering
# forty pages of one costs real money for no better answer.
MAX_PAGES = 4

# Keeps a single page inside a sane payload. A2-sized scans at 200 DPI are
# otherwise enormous, and the model gains nothing from the extra pixels.
MAX_EDGE_PX = 2200


def is_pdf(data: bytes, media_type: str = "") -> bool:
    """By content, not by what the sender called it.

    A phone that names a JPEG `.pdf`, or a mail client that mislabels the part,
    should not decide how we read the bytes.
    """
    if data[:5] == b"%PDF-":
        return True
    return media_type.split(";")[0].strip().lower() == "application/pdf"


def render(data: bytes, max_pages: int = MAX_PAGES) -> list[dict[str, str]]:
    """Every page as a PNG, in order, ready for the extraction path.

    Returns [] when the PDF cannot be opened or holds no pages - the caller
    treats that as "no readable original" rather than as a failure.
    """
    try:
        import pypdfium2 as pdfium
        from PIL import Image
    except Exception:
        logger.exception("PDF rendering is unavailable in this build")
        return []

    try:
        doc = pdfium.PdfDocument(io.BytesIO(data))
    except Exception:
        logger.info("could not open the PDF; not a readable receipt")
        return []

    pages: list[dict[str, str]] = []
    try:
        total = len(doc)
        if total > max_pages:
            logger.info("PDF has %d pages; reading the first %d", total, max_pages)
        for index in range(min(total, max_pages)):
            try:
                page = doc[index]
                bitmap = page.render(scale=RENDER_DPI / PDF_BASE_DPI)
                image = bitmap.to_pil().convert("RGB")

                # Only ever downwards: enlarging a low-resolution scan adds
                # pixels without adding any of the detail that was missing.
                if max(image.size) > MAX_EDGE_PX:
                    ratio = MAX_EDGE_PX / max(image.size)
                    image = image.resize(
                        (max(1, int(image.width * ratio)), max(1, int(image.height * ratio))),
                        Image.LANCZOS,
                    )

                buf = io.BytesIO()
                image.save(buf, format="PNG", optimize=True)
                pages.append({
                    "data": base64.b64encode(buf.getvalue()).decode(),
                    "media_type": "image/png",
                })
            except Exception:
                logger.exception("could not render page %d", index + 1)
    finally:
        try:
            doc.close()
        except Exception:
            pass

    logger.info("rendered %d page(s) from the PDF", len(pages))
    return pages
