import io
import math

import streamlit as st

from portal.config import MAX_IMG_WIDTH


def stitch_images(uploaded_files):
    """Merge uploaded images into a clean 1- or 2-column contact sheet so large multi-page homeworks fit in PDF."""
    if not uploaded_files:
        return None
    from PIL import Image as PILImage, ImageOps
    try:
        images = []
        target_w = 850 if len(uploaded_files) > 3 else MAX_IMG_WIDTH
        for f in uploaded_files[:24]:
            if isinstance(f, bytes):
                f = io.BytesIO(f)
            im = ImageOps.exif_transpose(PILImage.open(f)).convert("RGB")
            if im.width > target_w:
                new_h = int(im.height * target_w / im.width)
                im = im.resize((target_w, new_h), PILImage.Resampling.LANCZOS)
            images.append(im)

        cols = 2 if len(images) >= 4 else 1
        rows = math.ceil(len(images) / cols)
        col_w = max(i.width for i in images)
        row_heights = [
            max(images[r * cols + c].height for c in range(cols) if r * cols + c < len(images))
            for r in range(rows)
        ]

        stitched = PILImage.new("RGB", (col_w * cols, sum(row_heights)), color=(255, 255, 255))
        y = 0
        for r in range(rows):
            for c in range(cols):
                idx = r * cols + c
                if idx < len(images):
                    stitched.paste(images[idx], (c * col_w, y))
            y += row_heights[r]

        out = io.BytesIO()
        stitched.save(out, format="JPEG", quality=78, optimize=True)
        return out.getvalue()
    except Exception as e:
        st.error(f"Image processing failed: {e}")
        return None


def register_heif_opener():
    """Register the HEIC/HEIF opener for iPhone photos if pillow-heif is installed."""
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass


def is_pdf_upload(f) -> bool:
    """Phone pickers may drop the file extension, so also trust the browser's MIME type."""
    return str(f.name).lower().endswith(".pdf") or getattr(f, "type", "") == "application/pdf"


@st.cache_data(show_spinner=False, max_entries=200)
def _upload_thumbnail(file_id: str, _raw_bytes: bytes):
    """Small JPEG preview, rendered on the server so HEIC photos preview in every browser."""
    from PIL import Image as PILImage, ImageOps

    register_heif_opener()
    try:
        with PILImage.open(io.BytesIO(_raw_bytes)) as raw_im:
            # Decode JPEGs at reduced scale: a 50 MP phone photo would otherwise take
            # hundreds of MB of server memory just to make a small preview.
            raw_im.draft("RGB", (660, 660))
            im = ImageOps.exif_transpose(raw_im).convert("RGB")
            im.thumbnail((330, 330))
            out = io.BytesIO()
            im.save(out, format="JPEG", quality=80)
            return out.getvalue()
    except Exception:
        return None


@st.cache_data(show_spinner=False)
def _placeholder_tile(label: str):
    from PIL import Image as PILImage, ImageDraw

    im = PILImage.new("RGB", (330, 330), color=(235, 235, 235))
    ImageDraw.Draw(im).text((165, 165), label, fill=(90, 90, 90), anchor="mm", font_size=48)
    out = io.BytesIO()
    im.save(out, format="PNG")
    return out.getvalue()


def upload_preview(f):
    """Preview image for one uploaded file (a plain tile for PDFs and unreadable images)."""
    if is_pdf_upload(f):
        return _placeholder_tile("PDF")
    return _upload_thumbnail(f.file_id, f.getvalue()) or _placeholder_tile("?")


def convert_uploads_to_pdf_bytes(uploaded_files) -> bytes:
    """
    Converts mobile phone photos (JPG, PNG, WEBP, HEIC/HEIF) and/or PDFs
    into a single compressed, memory-safe multi-page PDF.
    """
    import pymupdf
    from PIL import Image as PILImage, ImageOps

    register_heif_opener()

    if not isinstance(uploaded_files, (list, tuple)):
        uploaded_files = [uploaded_files]

    if len(uploaded_files) == 1 and is_pdf_upload(uploaded_files[0]):
        uploaded_files[0].seek(0)
        return uploaded_files[0].read()

    merged_doc = pymupdf.open()
    try:
        for f in uploaded_files:
            f.seek(0)
            raw_bytes = f.read()
            if not raw_bytes:
                continue

            if is_pdf_upload(f):
                with pymupdf.open(stream=raw_bytes, filetype="pdf") as src_pdf:
                    merged_doc.insert_pdf(src_pdf)
            else:
                with PILImage.open(io.BytesIO(raw_bytes)) as raw_im:
                    im = ImageOps.exif_transpose(raw_im).convert("RGB")
                    max_dim = 1500
                    if max(im.width, im.height) > max_dim:
                        scale = max_dim / float(max(im.width, im.height))
                        im = im.resize(
                            (int(im.width * scale), int(im.height * scale)),
                            PILImage.Resampling.LANCZOS,
                        )
                    img_buf = io.BytesIO()
                    im.save(img_buf, format="JPEG", quality=82, optimize=True)
                    w, h = im.width, im.height
                    img_bytes = img_buf.getvalue()

                rect = pymupdf.Rect(0, 0, w, h)
                page = merged_doc.new_page(width=w, height=h)
                page.insert_image(rect, stream=img_bytes)

        if len(merged_doc) == 0:
            raise ValueError("No valid pages could be read from the uploaded files.")

        return merged_doc.tobytes(deflate=True)
    finally:
        merged_doc.close()
