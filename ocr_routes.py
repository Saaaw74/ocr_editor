# -*- coding: utf-8 -*-


import os
import uuid
import logging

from flask import Blueprint, request, jsonify, send_from_directory, current_app

from ocr_pipeline import (
    process_single_page,
    DEBUG_RAW_DIR,
    OcrPipelineError,
)

from job_manager import job_manager

logger = logging.getLogger("ocr_routes")

ocr_bp = Blueprint("ocr_bp", __name__, url_prefix="/api/ocr")


@ocr_bp.route("/upload", methods=["POST"])
def upload_pdf():
    """Загрузка PDF для OCR-визуализации. Возвращает job_id и число страниц."""
    file = request.files.get("file")
    if not file or not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Пожалуйста, загрузите файл в формате PDF."}), 400

    pdf_bytes = file.read()

    import fitz
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        page_count = len(doc)
        doc.close()
    except Exception as exc:
        return jsonify({"error": f"Не удалось открыть PDF: {exc}"}), 400

    job = job_manager.create_job(
        filename=file.filename,
        pdf_bytes=pdf_bytes,
        page_count=page_count,
        mode="ocr",
    )
    logger.info(f"[OCR] file={file.filename}")
    return jsonify({"job_id": job.job_id, "page_count": page_count})

@ocr_bp.route("/<job_id>/progress", methods=["GET"])
def get_progress(job_id):
    job = job_manager.get_job(job_id)
    if not job:
        return jsonify({"error": "Задача не найдена"}), 404
    return jsonify(job.progress)


@ocr_bp.route("/<job_id>/page/<int:page_num>", methods=["GET"])
def get_page_ocr(job_id, page_num):
    job = job_manager.get_job(job_id)
    if not job:
        return jsonify({"error": "Задача не найдена"}), 404

    if page_num < 1 or page_num > job.page_count:
        return jsonify({"error": f"Страница {page_num} вне диапазона (1..{job.page_count})"}), 400

    if page_num in job.pages:
        return jsonify(job.pages[page_num])

    def on_progress(percent, step):
        job.set_progress(percent, step)

    try:
        # Передаем путь к временному файлу на диске вместо байтов в RAM
        pdf_source = job.temp_pdf_path if os.path.exists(job.temp_pdf_path) else job.pdf_bytes
        result = process_single_page(
            pdf_source=pdf_source,
            page_number=page_num,
            job_id=job_id,
            on_progress=on_progress,
        )
    except OcrPipelineError as exc:
        job.set_progress(0, "error")
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        logger.exception("[OCR] unexpected error")
        job.set_progress(0, "error")
        return jsonify({"error": f"Внутренняя ошибка OCR: {exc}"}), 500

    result["image_url"] = f"/api/ocr/{job_id}/render/{page_num}"
    job.pages[page_num] = result
    return jsonify(result)


@ocr_bp.route("/<job_id>/render/<int:page_num>", methods=["GET"])
def get_rendered_image(job_id, page_num):
    job = job_manager.get_job(job_id)
    if not job:
        return jsonify({"error": "Задача не найдена"}), 404

    import io
    import fitz
    from flask import send_file
    try:
        # Читаем сразу с диска
        if os.path.exists(job.temp_pdf_path):
            doc = fitz.open(job.temp_pdf_path)
        else:
            doc = fitz.open(stream=job.pdf_bytes, filetype="pdf")

        page = doc[page_num - 1]
        pix = page.get_pixmap(dpi=150, alpha=False)
        img_bytes = pix.tobytes("png")
        doc.close()
        return send_file(io.BytesIO(img_bytes), mimetype="image/png")
    except Exception as exc:
        return jsonify({"error": f"Не удалось отрендерить страницу: {exc}"}), 500


@ocr_bp.route("/<job_id>/raw/<int:page_num>", methods=["GET"])
def get_raw_debug(job_id, page_num):
    """Отдаёт RAW результат Surya для диагностики (см. п.9, п.11 критериев готовности)."""
    filename = f"{job_id}_p{page_num}.json"
    if not os.path.exists(os.path.join(DEBUG_RAW_DIR, filename)):
        return jsonify({"error": "Raw-результат не найден"}), 404
    return send_from_directory(DEBUG_RAW_DIR, filename)


@ocr_bp.route("/<job_id>/normalized/<int:page_num>", methods=["GET"])
def get_normalized_debug(job_id, page_num):
    job = job_manager.get_job(job_id)
    if not job or page_num not in job.pages:
        return jsonify({"error": "Normalized-результат не найден"}), 404
    return jsonify(job.pages[page_num])