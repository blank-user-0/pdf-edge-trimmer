#!/usr/bin/env python3
"""
PDF Edge Trimmer Helper

This tool permanently removes selected amounts from the edges of each PDF page
and saves the result as a new PDF. It is intended for lawful educational
formatting work. Do not use it to remove required copyright, license, or
attribution notices.

Run without arguments to open the browser helper:

    python3 pdf_bottom_trimmer.py

Or run from the command line:

    python3 pdf_bottom_trimmer.py --input in.pdf --output out.pdf --bottom-inches 0.5 --left-inches 0.25
"""

from __future__ import annotations

import argparse
import html
import io
import json
import os
import queue
import sys
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlencode, urlparse

try:
    import fitz
except ImportError as exc:  # pragma: no cover - exercised only on missing dependency
    raise SystemExit(
        "Missing dependency: PyMuPDF. Install it with: python3 -m pip install pymupdf"
    ) from exc

os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # pragma: no cover - exercised only on missing Tk installs
    tk = None
    filedialog = None
    messagebox = None
    ttk = None

try:
    from PIL import Image, ImageDraw, ImageTk
except ImportError as exc:  # pragma: no cover - exercised only on missing dependency
    raise SystemExit(
        "Missing dependency: Pillow. Install it with: python3 -m pip install pillow"
    ) from exc


DEFAULT_INPUT_PDF_TEXT = os.environ.get("PDF_EDGE_TRIMMER_DEFAULT_INPUT", "")
OUTPUT_DIR = Path(os.environ.get("PDF_EDGE_TRIMMER_OUTPUT_DIR", str(Path.cwd()))).expanduser()
DEFAULT_INPUT_PDF = Path(DEFAULT_INPUT_PDF_TEXT).expanduser() if DEFAULT_INPUT_PDF_TEXT else Path()
DEFAULT_OUTPUT_PDF = OUTPUT_DIR / "trimmed_edges.pdf"

POINTS_PER_INCH = 72.0
DEFAULT_TOP_INCHES = 0.00
DEFAULT_BOTTOM_INCHES = 0.50
DEFAULT_LEFT_INCHES = 0.00
DEFAULT_RIGHT_INCHES = 0.00
DEFAULT_TRIM_INCHES = DEFAULT_BOTTOM_INCHES
DEFAULT_OUTPUT_MODE = "compressed"
DEFAULT_JPEG_QUALITY = 60
MIN_REMAINING_HEIGHT_POINTS = 72.0
MIN_REMAINING_WIDTH_POINTS = 72.0

ProgressCallback = Optional[Callable[[int, int], None]]


class TrimError(ValueError):
    """Raised for user-fixable PDF trimming inputs."""


def parse_trim_inches(value: str | float) -> float:
    """Return a validated trim amount in inches."""
    trim_inches = parse_edge_inches(value, "Bottom")
    if trim_inches == 0:
        raise TrimError("Bottom-removal amount must be greater than zero.")
    return trim_inches


def parse_edge_inches(value: str | float, edge_name: str) -> float:
    """Return a validated non-negative edge trim amount in inches."""
    try:
        trim_inches = float(value)
    except (TypeError, ValueError) as exc:
        raise TrimError(f"Enter a numeric {edge_name.lower()} amount in inches.") from exc

    if trim_inches < 0:
        raise TrimError(f"{edge_name} amount cannot be negative.")
    return trim_inches


def trim_points_from_inches(trim_inches: float) -> float:
    return trim_inches * POINTS_PER_INCH


def parse_edge_trims(
    *,
    top_inches: str | float,
    bottom_inches: str | float,
    left_inches: str | float,
    right_inches: str | float,
) -> dict[str, float]:
    trims = {
        "top": parse_edge_inches(top_inches, "Top"),
        "bottom": parse_edge_inches(bottom_inches, "Bottom"),
        "left": parse_edge_inches(left_inches, "Left"),
        "right": parse_edge_inches(right_inches, "Right"),
    }
    if not any(value > 0 for value in trims.values()):
        raise TrimError("Enter at least one edge amount greater than zero.")
    return trims


def edge_points_from_inches(trims: dict[str, float]) -> dict[str, float]:
    return {edge: trim_points_from_inches(value) for edge, value in trims.items()}


def parse_output_mode(value: str | None) -> str:
    mode = (value or DEFAULT_OUTPUT_MODE).strip().lower()
    valid_modes = {"compressed", "redact", "crop"}
    if mode not in valid_modes:
        raise TrimError(f"Output mode must be one of: {', '.join(sorted(valid_modes))}.")
    return mode


def parse_jpeg_quality(value: str | int | None) -> int:
    try:
        quality = int(value if value not in (None, "") else DEFAULT_JPEG_QUALITY)
    except (TypeError, ValueError) as exc:
        raise TrimError("JPEG quality must be a whole number from 1 to 95.") from exc
    if quality < 1 or quality > 95:
        raise TrimError("JPEG quality must be between 1 and 95.")
    return quality


def visible_rect_for_page(page: fitz.Page, trim_points: dict[str, float]) -> fitz.Rect:
    page_rect = page.rect
    return fitz.Rect(
        page_rect.x0 + trim_points["left"],
        page_rect.y0 + trim_points["top"],
        page_rect.x1 - trim_points["right"],
        page_rect.y1 - trim_points["bottom"],
    )


def open_pdf_for_reading(input_pdf: Path) -> fitz.Document:
    if not input_pdf.exists():
        raise TrimError(f"Input PDF does not exist: {input_pdf}")
    if not input_pdf.is_file():
        raise TrimError(f"Input path is not a file: {input_pdf}")

    try:
        doc = fitz.open(input_pdf)
    except Exception as exc:
        raise TrimError(f"Could not open input PDF: {exc}") from exc

    if doc.needs_pass:
        doc.close()
        raise TrimError("Input PDF is encrypted or password-protected.")
    if doc.page_count == 0:
        doc.close()
        raise TrimError("Input PDF has no pages.")
    return doc


def validate_trim_against_document(doc: fitz.Document, trim_inches: float) -> float:
    points = validate_edge_trims_against_document(
        doc,
        {
            "top": 0.0,
            "bottom": trim_inches,
            "left": 0.0,
            "right": 0.0,
        },
    )
    return points["bottom"]


def validate_edge_trims_against_document(
    doc: fitz.Document,
    trims: dict[str, float],
) -> dict[str, float]:
    trim_points = edge_points_from_inches(trims)
    min_height = min(page.rect.height for page in doc)
    min_width = min(page.rect.width for page in doc)
    remaining_height = min_height - trim_points["top"] - trim_points["bottom"]
    remaining_width = min_width - trim_points["left"] - trim_points["right"]

    if remaining_height < MIN_REMAINING_HEIGHT_POINTS:
        min_height_inches = min_height / POINTS_PER_INCH
        raise TrimError(
            "Top plus bottom trim is too large. The shortest page is "
            f"{min_height_inches:.2f} inches tall; leave at least 1 inch."
        )

    if remaining_width < MIN_REMAINING_WIDTH_POINTS:
        min_width_inches = min_width / POINTS_PER_INCH
        raise TrimError(
            "Left plus right trim is too large. The narrowest page is "
            f"{min_width_inches:.2f} inches wide; leave at least 1 inch."
        )

    return trim_points


def ensure_output_path(input_pdf: Path, output_pdf: Path, overwrite: bool) -> None:
    output_pdf.parent.mkdir(parents=True, exist_ok=True)

    try:
        same_file = input_pdf.resolve() == output_pdf.resolve()
    except FileNotFoundError:
        same_file = False

    if same_file:
        raise TrimError("Output PDF must be different from the input PDF.")
    if output_pdf.exists() and not overwrite:
        raise TrimError(
            f"Output PDF already exists: {output_pdf}. Choose a different path "
            "or allow overwrite."
        )


def trim_pdf_bottom(
    input_pdf: Path | str,
    output_pdf: Path | str,
    trim_inches: float | str,
    *,
    overwrite: bool = False,
    progress: ProgressCallback = None,
) -> None:
    """Permanently remove the selected bottom amount from every page."""
    trim_pdf_edges(
        input_pdf,
        output_pdf,
        top_inches=0.0,
        bottom_inches=trim_inches,
        left_inches=0.0,
        right_inches=0.0,
        overwrite=overwrite,
        progress=progress,
    )


def trim_pdf_edges(
    input_pdf: Path | str,
    output_pdf: Path | str,
    *,
    top_inches: float | str,
    bottom_inches: float | str,
    left_inches: float | str,
    right_inches: float | str,
    overwrite: bool = False,
    progress: ProgressCallback = None,
) -> None:
    """Permanently remove selected edge amounts from every page."""
    trim_pdf_edges_redacted(
        input_pdf,
        output_pdf,
        top_inches=top_inches,
        bottom_inches=bottom_inches,
        left_inches=left_inches,
        right_inches=right_inches,
        overwrite=overwrite,
        progress=progress,
    )


def trim_pdf_edges_redacted(
    input_pdf: Path | str,
    output_pdf: Path | str,
    *,
    top_inches: float | str,
    bottom_inches: float | str,
    left_inches: float | str,
    right_inches: float | str,
    overwrite: bool = False,
    progress: ProgressCallback = None,
) -> None:
    """Permanently redact selected edge areas while preserving remaining PDF objects."""
    input_path = Path(input_pdf).expanduser()
    output_path = Path(output_pdf).expanduser()
    trim_amounts = parse_edge_trims(
        top_inches=top_inches,
        bottom_inches=bottom_inches,
        left_inches=left_inches,
        right_inches=right_inches,
    )

    ensure_output_path(input_path, output_path, overwrite)
    doc = open_pdf_for_reading(input_path)
    temp_output = output_path.with_name(output_path.name + ".tmp")

    try:
        trim_points = validate_edge_trims_against_document(doc, trim_amounts)
        total_pages = doc.page_count

        if temp_output.exists():
            temp_output.unlink()

        for page_index in range(total_pages):
            page = doc.load_page(page_index)
            page_rect = page.rect
            redaction_rects = []

            if trim_points["top"] > 0:
                redaction_rects.append(
                    fitz.Rect(
                        page_rect.x0,
                        page_rect.y0,
                        page_rect.x1,
                        page_rect.y0 + trim_points["top"],
                    )
                )
            if trim_points["bottom"] > 0:
                redaction_rects.append(
                    fitz.Rect(
                        page_rect.x0,
                        page_rect.y1 - trim_points["bottom"],
                        page_rect.x1,
                        page_rect.y1,
                    )
                )
            if trim_points["left"] > 0:
                redaction_rects.append(
                    fitz.Rect(
                        page_rect.x0,
                        page_rect.y0,
                        page_rect.x0 + trim_points["left"],
                        page_rect.y1,
                    )
                )
            if trim_points["right"] > 0:
                redaction_rects.append(
                    fitz.Rect(
                        page_rect.x1 - trim_points["right"],
                        page_rect.y0,
                        page_rect.x1,
                        page_rect.y1,
                    )
                )

            for redaction_rect in redaction_rects:
                page.add_redact_annot(redaction_rect, fill=(1, 1, 1))
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_PIXELS,
                graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
                text=fitz.PDF_REDACT_TEXT_REMOVE,
            )

            cropbox = page.cropbox
            new_cropbox = fitz.Rect(
                cropbox.x0 + trim_points["left"],
                cropbox.y0 + trim_points["top"],
                cropbox.x1 - trim_points["right"],
                cropbox.y1 - trim_points["bottom"],
            )
            page.set_cropbox(new_cropbox)

            if progress:
                progress(page_index + 1, total_pages)

        doc.save(str(temp_output), garbage=4, deflate=True, clean=True)
        temp_output.replace(output_path)
    except Exception:
        if temp_output.exists():
            temp_output.unlink()
        raise
    finally:
        doc.close()


def trim_pdf_edges_visual_crop(
    input_pdf: Path | str,
    output_pdf: Path | str,
    *,
    top_inches: float | str,
    bottom_inches: float | str,
    left_inches: float | str,
    right_inches: float | str,
    overwrite: bool = False,
    progress: ProgressCallback = None,
) -> None:
    """Visually crop selected edges without permanently removing hidden PDF data."""
    input_path = Path(input_pdf).expanduser()
    output_path = Path(output_pdf).expanduser()
    trim_amounts = parse_edge_trims(
        top_inches=top_inches,
        bottom_inches=bottom_inches,
        left_inches=left_inches,
        right_inches=right_inches,
    )

    ensure_output_path(input_path, output_path, overwrite)
    doc = open_pdf_for_reading(input_path)
    temp_output = output_path.with_name(output_path.name + ".tmp")

    try:
        trim_points = validate_edge_trims_against_document(doc, trim_amounts)
        total_pages = doc.page_count

        if temp_output.exists():
            temp_output.unlink()

        for page_index in range(total_pages):
            page = doc.load_page(page_index)
            cropbox = page.cropbox
            page.set_cropbox(
                fitz.Rect(
                    cropbox.x0 + trim_points["left"],
                    cropbox.y0 + trim_points["top"],
                    cropbox.x1 - trim_points["right"],
                    cropbox.y1 - trim_points["bottom"],
                )
            )
            if progress:
                progress(page_index + 1, total_pages)

        doc.save(str(temp_output), garbage=4, deflate=True, clean=True)
        temp_output.replace(output_path)
    except Exception:
        if temp_output.exists():
            temp_output.unlink()
        raise
    finally:
        doc.close()


def trim_pdf_edges_compressed_image(
    input_pdf: Path | str,
    output_pdf: Path | str,
    *,
    top_inches: float | str,
    bottom_inches: float | str,
    left_inches: float | str,
    right_inches: float | str,
    jpeg_quality: int | str = DEFAULT_JPEG_QUALITY,
    overwrite: bool = False,
    progress: ProgressCallback = None,
) -> None:
    """Permanently trim edges by rebuilding pages as compressed JPEG images."""
    input_path = Path(input_pdf).expanduser()
    output_path = Path(output_pdf).expanduser()
    trim_amounts = parse_edge_trims(
        top_inches=top_inches,
        bottom_inches=bottom_inches,
        left_inches=left_inches,
        right_inches=right_inches,
    )
    quality = parse_jpeg_quality(jpeg_quality)

    ensure_output_path(input_path, output_path, overwrite)
    src = open_pdf_for_reading(input_path)
    dst = fitz.open()
    temp_output = output_path.with_name(output_path.name + ".tmp")

    try:
        trim_points = validate_edge_trims_against_document(src, trim_amounts)
        total_pages = src.page_count

        if temp_output.exists():
            temp_output.unlink()

        for page_index in range(total_pages):
            page = src.load_page(page_index)
            visible_rect = visible_rect_for_page(page, trim_points)
            pixmap = page.get_pixmap(
                matrix=fitz.Matrix(1, 1),
                clip=visible_rect,
                alpha=False,
            )
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            buffer = io.BytesIO()
            image.save(
                buffer,
                format="JPEG",
                quality=quality,
                optimize=True,
                progressive=False,
            )

            new_page = dst.new_page(width=visible_rect.width, height=visible_rect.height)
            new_page.insert_image(
                new_page.rect,
                stream=buffer.getvalue(),
                keep_proportion=False,
            )

            if progress:
                progress(page_index + 1, total_pages)

        dst.save(str(temp_output), garbage=4, deflate=True)
        temp_output.replace(output_path)
    except Exception:
        if temp_output.exists():
            temp_output.unlink()
        raise
    finally:
        src.close()
        dst.close()


def trim_pdf_edges_by_mode(
    input_pdf: Path | str,
    output_pdf: Path | str,
    *,
    top_inches: float | str,
    bottom_inches: float | str,
    left_inches: float | str,
    right_inches: float | str,
    output_mode: str = DEFAULT_OUTPUT_MODE,
    jpeg_quality: int | str = DEFAULT_JPEG_QUALITY,
    overwrite: bool = False,
    progress: ProgressCallback = None,
) -> None:
    mode = parse_output_mode(output_mode)
    if mode == "compressed":
        trim_pdf_edges_compressed_image(
            input_pdf,
            output_pdf,
            top_inches=top_inches,
            bottom_inches=bottom_inches,
            left_inches=left_inches,
            right_inches=right_inches,
            jpeg_quality=jpeg_quality,
            overwrite=overwrite,
            progress=progress,
        )
    elif mode == "crop":
        trim_pdf_edges_visual_crop(
            input_pdf,
            output_pdf,
            top_inches=top_inches,
            bottom_inches=bottom_inches,
            left_inches=left_inches,
            right_inches=right_inches,
            overwrite=overwrite,
            progress=progress,
        )
    else:
        trim_pdf_edges_redacted(
            input_pdf,
            output_pdf,
            top_inches=top_inches,
            bottom_inches=bottom_inches,
            left_inches=left_inches,
            right_inches=right_inches,
            overwrite=overwrite,
            progress=progress,
        )


def read_pdf_info(input_pdf: Path | str) -> tuple[int, float, float]:
    """Return page count plus first-page width/height in inches."""
    with open_pdf_for_reading(Path(input_pdf).expanduser()) as doc:
        first_page = doc.load_page(0)
        return (
            doc.page_count,
            first_page.rect.width / POINTS_PER_INCH,
            first_page.rect.height / POINTS_PER_INCH,
        )


def render_preview_image(
    input_pdf: Path | str,
    page_number: int,
    trim_inches: float | str | None = None,
    *,
    top_inches: float | str = DEFAULT_TOP_INCHES,
    bottom_inches: float | str | None = None,
    left_inches: float | str = DEFAULT_LEFT_INCHES,
    right_inches: float | str = DEFAULT_RIGHT_INCHES,
    max_width: int = 620,
    max_height: int = 760,
) -> Image.Image:
    """Render a preview page with red cut lines and shaded removed areas."""
    input_path = Path(input_pdf).expanduser()
    if bottom_inches is None:
        bottom_inches = trim_inches if trim_inches is not None else DEFAULT_BOTTOM_INCHES
    trim_amounts = parse_edge_trims(
        top_inches=top_inches,
        bottom_inches=bottom_inches,
        left_inches=left_inches,
        right_inches=right_inches,
    )

    with open_pdf_for_reading(input_path) as doc:
        if page_number < 1 or page_number > doc.page_count:
            raise TrimError(f"Preview page must be between 1 and {doc.page_count}.")

        trim_points = validate_edge_trims_against_document(doc, trim_amounts)
        page = doc.load_page(page_number - 1)
        page_rect = page.rect

        scale = min(max_width / page_rect.width, max_height / page_rect.height, 1.0)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        image = image.convert("RGBA")

        top_y = int(round(trim_points["top"] * scale))
        bottom_y = int(round((page_rect.y1 - trim_points["bottom"]) * scale))
        left_x = int(round(trim_points["left"] * scale))
        right_x = int(round((page_rect.x1 - trim_points["right"]) * scale))

        top_y = max(0, min(top_y, image.height))
        bottom_y = max(0, min(bottom_y, image.height))
        left_x = max(0, min(left_x, image.width))
        right_x = max(0, min(right_x, image.width))

        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        if trim_points["top"] > 0:
            draw.rectangle((0, 0, image.width, top_y), fill=(255, 0, 0, 54))
        if trim_points["bottom"] > 0:
            draw.rectangle((0, bottom_y, image.width, image.height), fill=(255, 0, 0, 54))
        if trim_points["left"] > 0:
            draw.rectangle((0, 0, left_x, image.height), fill=(255, 0, 0, 54))
        if trim_points["right"] > 0:
            draw.rectangle((right_x, 0, image.width, image.height), fill=(255, 0, 0, 54))

        line_width = max(4, int(round(7 * scale)))
        border_width = max(2, int(round(3 * scale)))
        draw.rectangle(
            (0, 0, image.width - 1, image.height - 1),
            outline=(80, 80, 80, 255),
            width=border_width,
        )
        if trim_points["top"] > 0:
            draw.line((0, top_y, image.width, top_y), fill=(220, 0, 0, 255), width=line_width)
        if trim_points["bottom"] > 0:
            draw.line(
                (0, bottom_y, image.width, bottom_y),
                fill=(220, 0, 0, 255),
                width=line_width,
            )
        if trim_points["left"] > 0:
            draw.line((left_x, 0, left_x, image.height), fill=(220, 0, 0, 255), width=line_width)
        if trim_points["right"] > 0:
            draw.line(
                (right_x, 0, right_x, image.height),
                fill=(220, 0, 0, 255),
                width=line_width,
            )
        return Image.alpha_composite(image, overlay)


WEB_JOBS: dict[str, dict[str, object]] = {}
WEB_JOBS_LOCK = threading.Lock()


def get_param(params: dict[str, list[str]], name: str, default: str) -> str:
    values = params.get(name)
    if not values:
        return default
    return values[0]


def get_edge_form_values(params: dict[str, list[str]]) -> dict[str, str]:
    side_inches = get_param(params, "sides", "").strip()
    left_default = side_inches if side_inches else f"{DEFAULT_LEFT_INCHES:.2f}"
    right_default = side_inches if side_inches else f"{DEFAULT_RIGHT_INCHES:.2f}"
    left_inches = get_param(params, "left", left_default)
    right_inches = get_param(params, "right", right_default)
    if side_inches:
        left_inches = side_inches
        right_inches = side_inches
    return {
        "top": get_param(params, "top", f"{DEFAULT_TOP_INCHES:.2f}"),
        "bottom": get_param(params, "bottom", f"{DEFAULT_BOTTOM_INCHES:.2f}"),
        "left": left_inches,
        "right": right_inches,
        "sides": side_inches,
    }


def describe_active_cuts(edge_values: dict[str, str]) -> str:
    try:
        trims = {
            "Top": parse_edge_inches(edge_values["top"], "Top"),
            "Bottom": parse_edge_inches(edge_values["bottom"], "Bottom"),
            "Left": parse_edge_inches(edge_values["left"], "Left"),
            "Right": parse_edge_inches(edge_values["right"], "Right"),
        }
    except TrimError:
        return "Active cuts update after valid edge values are entered."

    active = [f"{name} {value:g} in" for name, value in trims.items() if value > 0]
    inactive = [name for name, value in trims.items() if value == 0]
    active_text = ", ".join(active) if active else "none"
    inactive_text = ", ".join(inactive) if inactive else "none"
    return f"Active cut lines: {active_text}. No cut line: {inactive_text}."


def render_web_page(
    params: dict[str, list[str]] | None = None,
    *,
    job_id: str = "",
    error: str = "",
) -> str:
    params = params or {}
    input_pdf = get_param(params, "input", str(DEFAULT_INPUT_PDF))
    output_pdf = get_param(params, "output", str(DEFAULT_OUTPUT_PDF))
    edge_values = get_edge_form_values(params)
    top_inches = edge_values["top"]
    bottom_inches = edge_values["bottom"]
    left_inches = edge_values["left"]
    right_inches = edge_values["right"]
    side_inches = edge_values["sides"]
    output_mode = get_param(params, "mode", DEFAULT_OUTPUT_MODE)
    jpeg_quality = get_param(params, "quality", str(DEFAULT_JPEG_QUALITY))
    page_number = get_param(params, "page", "1")
    overwrite_checked = "checked" if get_param(params, "overwrite", "on") == "on" else ""
    mode_selected = {
        "compressed": "selected" if output_mode == "compressed" else "",
        "redact": "selected" if output_mode == "redact" else "",
        "crop": "selected" if output_mode == "crop" else "",
    }

    preview_query = urlencode(
        {
            "input": input_pdf,
            "top": top_inches,
            "bottom": bottom_inches,
            "left": left_inches,
            "right": right_inches,
            "page": page_number,
        }
    )
    preview_src = f"/preview.png?{preview_query}"

    try:
        pages, width_inches, height_inches = read_pdf_info(input_pdf)
        info_text = (
            f"{pages} pages; first page {width_inches:.2f} x "
            f"{height_inches:.2f} inches."
        )
    except Exception as exc:
        info_text = f"PDF info unavailable: {exc}"

    escaped = {
        "input": html.escape(input_pdf, quote=True),
        "output": html.escape(output_pdf, quote=True),
        "top": html.escape(top_inches, quote=True),
        "bottom": html.escape(bottom_inches, quote=True),
        "left": html.escape(left_inches, quote=True),
        "right": html.escape(right_inches, quote=True),
        "sides": html.escape(side_inches, quote=True),
        "mode": html.escape(output_mode, quote=True),
        "quality": html.escape(jpeg_quality, quote=True),
        "page": html.escape(page_number, quote=True),
        "preview_src": html.escape(preview_src, quote=True),
        "info": html.escape(info_text),
        "active_cuts": html.escape(describe_active_cuts(edge_values)),
        "error": html.escape(error),
        "job_id": html.escape(job_id, quote=True),
    }

    error_block = (
        f'<div class="alert error">{escaped["error"]}</div>'
        if error
        else ""
    )
    job_block = (
        f"""
        <section class="panel">
          <h2>Generation Progress</h2>
          <div class="progress-wrap">
            <div id="progress-bar" class="progress-bar" style="width: 0%"></div>
          </div>
          <p id="job-status">Starting...</p>
        </section>
        """
        if job_id
        else ""
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PDF Edge Trimmer</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f4f6f8;
      color: #18212f;
    }}
    body {{
      margin: 0;
      padding: 28px;
      background: #f4f6f8;
    }}
    main {{
      max-width: 1180px;
      margin: 0 auto;
    }}
    h1 {{
      margin: 0 0 6px;
      font-size: 28px;
      line-height: 1.2;
    }}
    h2 {{
      margin: 0 0 14px;
      font-size: 18px;
    }}
    p {{
      margin: 0 0 14px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: minmax(320px, 420px) 1fr;
      gap: 18px;
      align-items: start;
    }}
    .panel {{
      background: #ffffff;
      border: 1px solid #d8dee8;
      border-radius: 8px;
      padding: 18px;
      box-shadow: 0 1px 2px rgba(16, 24, 40, 0.06);
    }}
    label {{
      display: block;
      margin: 14px 0 6px;
      font-weight: 650;
      font-size: 14px;
    }}
    input[type="text"], input[type="number"] {{
      width: 100%;
      box-sizing: border-box;
      border: 1px solid #bcc6d4;
      border-radius: 6px;
      padding: 10px 11px;
      font-size: 14px;
      background: #ffffff;
      color: #18212f;
    }}
    select {{
      width: 100%;
      box-sizing: border-box;
      border: 1px solid #bcc6d4;
      border-radius: 6px;
      padding: 10px 11px;
      font-size: 14px;
      background: #ffffff;
      color: #18212f;
    }}
    .row {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }}
    .edge-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }}
    .full {{
      grid-column: 1 / -1;
    }}
    .actions {{
      display: flex;
      gap: 10px;
      align-items: center;
      margin-top: 18px;
      flex-wrap: wrap;
    }}
    button {{
      border: 0;
      border-radius: 6px;
      padding: 10px 14px;
      font-weight: 700;
      font-size: 14px;
      cursor: pointer;
    }}
    button.primary {{
      background: #146ef5;
      color: #ffffff;
    }}
    button.secondary {{
      background: #e8edf5;
      color: #162033;
    }}
    .muted {{
      color: #5b6575;
      font-size: 13px;
      line-height: 1.45;
    }}
    .alert {{
      border-radius: 6px;
      padding: 10px 12px;
      margin: 14px 0;
      font-size: 14px;
    }}
    .error {{
      background: #fff1f0;
      border: 1px solid #ffccc7;
      color: #8a1f11;
    }}
    .preview {{
      min-height: 620px;
      display: flex;
      align-items: flex-start;
      justify-content: center;
      overflow: auto;
      background: #eef2f7;
    }}
    .preview img {{
      max-width: 100%;
      height: auto;
      background: #ffffff;
      box-shadow: 0 1px 4px rgba(16, 24, 40, 0.2);
    }}
    .progress-wrap {{
      width: 100%;
      height: 16px;
      overflow: hidden;
      background: #e8edf5;
      border-radius: 999px;
      margin-bottom: 10px;
    }}
    .progress-bar {{
      height: 100%;
      background: #168a45;
      transition: width 0.2s ease;
    }}
    @media (max-width: 900px) {{
      body {{ padding: 16px; }}
      .grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main>
    <h1>PDF Edge Trimmer</h1>
    <p class="muted">
      Permanently removes selected amounts from the top, bottom, left, and
      right edges of each page and saves a new PDF. For lawful educational
      formatting only; do not remove required copyright, license, or
      attribution notices.
    </p>
    {error_block}
    <div class="grid">
      <section class="panel">
        <h2>Settings</h2>
        <form method="get" action="/">
          <label for="input">Input PDF path</label>
          <input id="input" name="input" type="text" value="{escaped['input']}">

          <label for="output">Output PDF path</label>
          <input id="output" name="output" type="text" value="{escaped['output']}">

          <label for="mode">Output mode</label>
          <select id="mode" name="mode">
            <option value="compressed" {mode_selected['compressed']}>Permanent compressed image - small file</option>
            <option value="redact" {mode_selected['redact']}>Permanent redaction - larger file, keeps remaining PDF text</option>
            <option value="crop" {mode_selected['crop']}>Visual crop only - smallest file, hidden content remains</option>
          </select>

          <div class="row">
            <div>
              <label for="quality">JPEG quality</label>
              <input id="quality" name="quality" type="number" step="1" min="1" max="95" value="{escaped['quality']}">
            </div>
          </div>

          <div class="edge-grid">
            <div class="full">
              <label for="sides">Both side edges (inches)</label>
              <input id="sides" name="sides" type="number" step="0.01" min="0" value="{escaped['sides']}" placeholder="Optional: fills left and right">
            </div>
            <div>
              <label for="top">Top edge (inches)</label>
              <input id="top" name="top" type="number" step="0.01" min="0" value="{escaped['top']}">
            </div>
            <div>
              <label for="bottom">Bottom edge (inches)</label>
              <input id="bottom" name="bottom" type="number" step="0.01" min="0" value="{escaped['bottom']}">
            </div>
            <div>
              <label for="left">Left edge (inches)</label>
              <input id="left" name="left" type="number" step="0.01" min="0" value="{escaped['left']}">
            </div>
            <div>
              <label for="right">Right edge (inches)</label>
              <input id="right" name="right" type="number" step="0.01" min="0" value="{escaped['right']}">
            </div>
          </div>

          <div class="row">
            <div>
              <label for="page">Preview page</label>
              <input id="page" name="page" type="number" step="1" min="1" value="{escaped['page']}">
            </div>
          </div>

          <label>
            <input name="overwrite" type="checkbox" {overwrite_checked}>
            Overwrite output PDF if it already exists
          </label>

          <div class="actions">
            <button class="secondary" type="submit">Preview Cut Line</button>
            <button class="primary" type="submit" formmethod="post" formaction="/generate">
              Generate Trimmed PDF
            </button>
          </div>
        </form>
        <p class="muted" style="margin-top: 14px;">{escaped['info']}</p>
        <p class="muted">{escaped['active_cuts']}</p>
        <p class="muted">Compressed mode keeps the removed edges unrecoverable and targets a small file, but the output pages become images and lose searchable/OCR text. Quality 60 is close to the original size in testing.</p>
      </section>

      <section class="panel">
        <h2>Preview</h2>
        <div class="preview">
          <img src="{escaped['preview_src']}" alt="PDF page preview with edge trim guides">
        </div>
      </section>
    </div>
    {job_block}
  </main>
  <script>
    const jobId = "{escaped['job_id']}";
    if (jobId) {{
      const bar = document.getElementById("progress-bar");
      const status = document.getElementById("job-status");
      const timer = setInterval(async () => {{
        const response = await fetch("/status?job=" + encodeURIComponent(jobId));
        const job = await response.json();
        const pct = job.total ? Math.round((job.page / job.total) * 100) : 0;
        bar.style.width = pct + "%";
        status.textContent = job.message || "Working...";
        if (job.state === "done" || job.state === "error") {{
          clearInterval(timer);
          if (job.state === "done") {{
            bar.style.width = "100%";
          }}
        }}
      }}, 700);
    }}
  </script>
</body>
</html>
"""


class PdfBottomTrimmerWebHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def send_bytes(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, body: str, status: int = 200) -> None:
        self.send_bytes(status, "text/html; charset=utf-8", body.encode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == "/":
            self.send_html(render_web_page(params))
            return

        if parsed.path == "/preview.png":
            try:
                input_pdf = get_param(params, "input", str(DEFAULT_INPUT_PDF))
                edge_values = get_edge_form_values(params)
                page_number = int(get_param(params, "page", "1"))
                image = render_preview_image(
                    input_pdf,
                    page_number,
                    top_inches=edge_values["top"],
                    bottom_inches=edge_values["bottom"],
                    left_inches=edge_values["left"],
                    right_inches=edge_values["right"],
                    max_width=820,
                    max_height=560,
                ).convert("RGB")
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                self.send_bytes(200, "image/png", buffer.getvalue())
            except Exception as exc:
                body = f"Preview error: {exc}".encode("utf-8")
                self.send_bytes(400, "text/plain; charset=utf-8", body)
            return

        if parsed.path == "/status":
            job_id = get_param(params, "job", "")
            with WEB_JOBS_LOCK:
                job = dict(WEB_JOBS.get(job_id, {"state": "error", "message": "Unknown job."}))
            self.send_bytes(200, "application/json", json.dumps(job).encode("utf-8"))
            return

        self.send_html(render_web_page(error="Page not found."), status=404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/generate":
            self.send_html(render_web_page(error="Page not found."), status=404)
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        params = parse_qs(body)

        input_pdf = get_param(params, "input", str(DEFAULT_INPUT_PDF))
        output_pdf = get_param(params, "output", str(DEFAULT_OUTPUT_PDF))
        edge_values = get_edge_form_values(params)
        output_mode = get_param(params, "mode", DEFAULT_OUTPUT_MODE)
        jpeg_quality = get_param(params, "quality", str(DEFAULT_JPEG_QUALITY))
        overwrite = get_param(params, "overwrite", "") == "on"
        job_id = uuid.uuid4().hex

        with WEB_JOBS_LOCK:
            WEB_JOBS[job_id] = {
                "state": "running",
                "page": 0,
                "total": 0,
                "message": "Starting...",
                "output": output_pdf,
            }

        def progress(page: int, total: int) -> None:
            with WEB_JOBS_LOCK:
                WEB_JOBS[job_id].update(
                    {
                        "page": page,
                        "total": total,
                        "message": f"Processed page {page} of {total}...",
                    }
                )

        def worker() -> None:
            try:
                trim_pdf_edges_by_mode(
                    input_pdf,
                    output_pdf,
                    top_inches=edge_values["top"],
                    bottom_inches=edge_values["bottom"],
                    left_inches=edge_values["left"],
                    right_inches=edge_values["right"],
                    output_mode=output_mode,
                    jpeg_quality=jpeg_quality,
                    overwrite=overwrite,
                    progress=progress,
                )
            except Exception as exc:
                with WEB_JOBS_LOCK:
                    WEB_JOBS[job_id].update(
                        {
                            "state": "error",
                            "message": f"Error: {exc}",
                        }
                    )
            else:
                with WEB_JOBS_LOCK:
                    total = WEB_JOBS[job_id].get("total", 0)
                    WEB_JOBS[job_id].update(
                        {
                            "state": "done",
                            "page": total,
                            "message": f"Done. Saved trimmed PDF: {output_pdf}",
                        }
                    )

        threading.Thread(target=worker, daemon=True).start()
        self.send_html(render_web_page(params, job_id=job_id))


def run_web_ui(*, open_browser: bool = True, port: int = 0) -> int:
    server = ThreadingHTTPServer(("127.0.0.1", port), PdfBottomTrimmerWebHandler)
    host, port = server.server_address
    url = f"http://{host}:{port}/"
    print(f"PDF Edge Trimmer is running at {url}")
    print("Keep this Terminal window open while using the browser UI.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping PDF Edge Trimmer.")
    finally:
        server.server_close()
    return 0


class PdfBottomTrimmerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("PDF Edge Trimmer")
        self.root.minsize(880, 760)

        self.input_var = tk.StringVar(value=str(DEFAULT_INPUT_PDF))
        self.output_var = tk.StringVar(value=str(DEFAULT_OUTPUT_PDF))
        self.trim_var = tk.StringVar(value=f"{DEFAULT_TRIM_INCHES:.2f}")
        self.page_var = tk.StringVar(value="1")
        self.info_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="Choose settings, preview a page, then generate a new PDF.")
        self.progress_var = tk.DoubleVar(value=0)

        self.preview_photo: ImageTk.PhotoImage | None = None
        self.worker_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker_thread: threading.Thread | None = None

        self._build_ui()
        self.refresh_pdf_info(show_errors=False)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=16)
        outer.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(7, weight=1)

        ttk.Label(outer, text="Input PDF").grid(row=0, column=0, sticky="w", pady=(0, 6))
        ttk.Entry(outer, textvariable=self.input_var).grid(
            row=0, column=1, sticky="ew", padx=(8, 8), pady=(0, 6)
        )
        ttk.Button(outer, text="Browse...", command=self.browse_input).grid(
            row=0, column=2, sticky="ew", pady=(0, 6)
        )

        ttk.Label(outer, text="Output PDF").grid(row=1, column=0, sticky="w", pady=(0, 6))
        ttk.Entry(outer, textvariable=self.output_var).grid(
            row=1, column=1, sticky="ew", padx=(8, 8), pady=(0, 6)
        )
        ttk.Button(outer, text="Browse...", command=self.browse_output).grid(
            row=1, column=2, sticky="ew", pady=(0, 6)
        )

        settings = ttk.Frame(outer)
        settings.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 6))
        settings.columnconfigure(5, weight=1)

        ttk.Label(settings, text="Bottom to remove (inches)").grid(row=0, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.trim_var, width=10).grid(
            row=0, column=1, sticky="w", padx=(8, 24)
        )
        ttk.Label(settings, text="Preview page").grid(row=0, column=2, sticky="w")
        ttk.Entry(settings, textvariable=self.page_var, width=8).grid(
            row=0, column=3, sticky="w", padx=(8, 24)
        )
        ttk.Button(settings, text="Refresh Info", command=self.refresh_pdf_info).grid(
            row=0, column=4, padx=(0, 8)
        )
        ttk.Label(settings, textvariable=self.info_var).grid(row=0, column=5, sticky="w")

        actions = ttk.Frame(outer)
        actions.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(2, 10))
        ttk.Button(actions, text="Preview Cut Line", command=self.preview_page).pack(
            side="left", padx=(0, 8)
        )
        self.generate_button = ttk.Button(
            actions, text="Generate Trimmed PDF", command=self.generate_pdf
        )
        self.generate_button.pack(side="left")

        notice = (
            "For lawful educational formatting only. Do not remove required copyright, "
            "license, or attribution notices."
        )
        ttk.Label(outer, text=notice, foreground="#555555").grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )

        self.progress = ttk.Progressbar(
            outer, variable=self.progress_var, maximum=100, mode="determinate"
        )
        self.progress.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(0, 6))

        ttk.Label(outer, textvariable=self.status_var).grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )

        preview_frame = ttk.Frame(outer, relief="sunken", borderwidth=1)
        preview_frame.grid(row=7, column=0, columnspan=3, sticky="nsew")
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)

        self.preview_label = ttk.Label(
            preview_frame,
            text="Preview will appear here.",
            anchor="center",
            justify="center",
        )
        self.preview_label.grid(row=0, column=0, sticky="nsew")

    def browse_input(self) -> None:
        filename = filedialog.askopenfilename(
            title="Choose input PDF",
            filetypes=(("PDF files", "*.pdf"), ("All files", "*.*")),
            initialdir=str(DEFAULT_INPUT_PDF.parent if DEFAULT_INPUT_PDF.exists() else OUTPUT_DIR),
        )
        if filename:
            self.input_var.set(filename)
            input_path = Path(filename)
            self.output_var.set(str(input_path.with_name(input_path.stem + "_trimmed_edges.pdf")))
            self.refresh_pdf_info(show_errors=True)

    def browse_output(self) -> None:
        filename = filedialog.asksaveasfilename(
            title="Choose output PDF",
            filetypes=(("PDF files", "*.pdf"), ("All files", "*.*")),
            defaultextension=".pdf",
            initialdir=str(OUTPUT_DIR),
            initialfile=Path(self.output_var.get()).name or DEFAULT_OUTPUT_PDF.name,
        )
        if filename:
            self.output_var.set(filename)

    def refresh_pdf_info(self, *, show_errors: bool = True) -> None:
        try:
            pages, width_inches, height_inches = read_pdf_info(self.input_var.get())
        except Exception as exc:
            self.info_var.set("")
            self.status_var.set("Could not read PDF information.")
            if show_errors:
                messagebox.showerror("PDF information error", str(exc))
            return

        self.info_var.set(
            f"{pages} pages; first page {width_inches:.2f} x {height_inches:.2f} in"
        )
        self.status_var.set("PDF information loaded.")

    def preview_page(self) -> None:
        try:
            page_number = int(self.page_var.get())
            image = render_preview_image(
                self.input_var.get(),
                page_number,
                self.trim_var.get(),
            )
        except TrimError as exc:
            messagebox.showerror("Preview error", str(exc))
            return
        except ValueError:
            messagebox.showerror("Preview error", "Preview page must be a whole number.")
            return
        except Exception as exc:
            messagebox.showerror("Preview error", str(exc))
            return

        self.preview_photo = ImageTk.PhotoImage(image)
        self.preview_label.configure(image=self.preview_photo, text="")
        self.status_var.set(f"Preview loaded for page {page_number}.")

    def generate_pdf(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("Already running", "PDF generation is already in progress.")
            return

        input_path = Path(self.input_var.get()).expanduser()
        output_path = Path(self.output_var.get()).expanduser()

        if output_path.exists():
            should_overwrite = messagebox.askyesno(
                "Overwrite output?",
                f"The output PDF already exists:\n\n{output_path}\n\nOverwrite it?",
            )
            if not should_overwrite:
                return

        self.progress_var.set(0)
        self.generate_button.configure(state="disabled")
        self.status_var.set("Generating trimmed PDF...")

        def progress(page: int, total: int) -> None:
            self.worker_queue.put(("progress", (page, total)))

        def worker() -> None:
            try:
                trim_pdf_bottom(
                    input_path,
                    output_path,
                    self.trim_var.get(),
                    overwrite=True,
                    progress=progress,
                )
            except Exception as exc:
                self.worker_queue.put(("error", str(exc)))
            else:
                self.worker_queue.put(("done", str(output_path)))

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()
        self.root.after(150, self.poll_worker_queue)

    def poll_worker_queue(self) -> None:
        while True:
            try:
                kind, payload = self.worker_queue.get_nowait()
            except queue.Empty:
                break

            if kind == "progress":
                page, total = payload
                percent = (page / total) * 100
                self.progress_var.set(percent)
                self.status_var.set(f"Processed page {page} of {total}...")
            elif kind == "error":
                self.generate_button.configure(state="normal")
                self.status_var.set("PDF generation failed.")
                messagebox.showerror("Generation error", str(payload))
            elif kind == "done":
                self.generate_button.configure(state="normal")
                self.progress_var.set(100)
                self.status_var.set(f"Saved trimmed PDF: {payload}")
                messagebox.showinfo("Done", f"Saved trimmed PDF:\n\n{payload}")

        if self.worker_thread and self.worker_thread.is_alive():
            self.root.after(150, self.poll_worker_queue)


def run_gui() -> int:
    if tk is None:
        print("Tkinter is not available in this Python installation.", file=sys.stderr)
        return 1
    root = tk.Tk()
    root.geometry("980x820+80+80")
    PdfBottomTrimmerApp(root)
    root.update_idletasks()
    root.deiconify()
    root.lift()
    root.focus_force()
    try:
        root.attributes("-topmost", True)
        root.after(1200, lambda: root.attributes("-topmost", False))
    except tk.TclError:
        pass
    root.mainloop()
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Permanently remove fixed amounts from PDF page edges. "
            "Run without arguments to open the browser helper."
        )
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Open the browser helper.",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="With --web, start the local server without opening a browser.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Local browser-helper port. Defaults to an available random port.",
    )
    parser.add_argument(
        "--tk",
        action="store_true",
        help="Open the older Tk desktop helper instead of the browser helper.",
    )
    parser.add_argument("--input", help="Input PDF path.")
    parser.add_argument("--output", help="Output PDF path.")
    parser.add_argument(
        "--trim-inches",
        type=float,
        help="Legacy bottom-only amount to remove in inches.",
    )
    parser.add_argument("--top-inches", type=float, help="Top edge amount to remove in inches.")
    parser.add_argument("--bottom-inches", type=float, help="Bottom edge amount to remove in inches.")
    parser.add_argument("--left-inches", type=float, help="Left edge amount to remove in inches.")
    parser.add_argument("--right-inches", type=float, help="Right edge amount to remove in inches.")
    parser.add_argument(
        "--mode",
        choices=("compressed", "redact", "crop"),
        default=DEFAULT_OUTPUT_MODE,
        help=(
            "Output mode: compressed keeps file size small but rasterizes pages; "
            "redact permanently removes edges while preserving remaining PDF objects; "
            "crop only changes the visible page box."
        ),
    )
    parser.add_argument(
        "--jpeg-quality",
        type=int,
        default=DEFAULT_JPEG_QUALITY,
        help="JPEG quality for compressed mode, 1-95. Default: 60.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace the output PDF if it already exists.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        return run_web_ui()

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.tk:
        return run_gui()
    if args.web:
        return run_web_ui(open_browser=not args.no_open, port=args.port)

    edge_args = [
        args.top_inches,
        args.bottom_inches,
        args.left_inches,
        args.right_inches,
        args.trim_inches,
    ]
    if not args.input or not args.output or all(value is None for value in edge_args):
        parser.error(
            "--input, --output, and at least one edge amount are required in "
            "command-line mode."
        )

    top_inches = args.top_inches if args.top_inches is not None else 0.0
    bottom_inches = (
        args.bottom_inches
        if args.bottom_inches is not None
        else args.trim_inches
        if args.trim_inches is not None
        else 0.0
    )
    left_inches = args.left_inches if args.left_inches is not None else 0.0
    right_inches = args.right_inches if args.right_inches is not None else 0.0

    def print_progress(page: int, total: int) -> None:
        print(f"Processed page {page} of {total}", flush=True)

    try:
        trim_pdf_edges_by_mode(
            args.input,
            args.output,
            top_inches=top_inches,
            bottom_inches=bottom_inches,
            left_inches=left_inches,
            right_inches=right_inches,
            output_mode=args.mode,
            jpeg_quality=args.jpeg_quality,
            overwrite=args.overwrite,
            progress=print_progress,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Saved trimmed PDF: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
