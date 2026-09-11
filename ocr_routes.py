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

logger = logging.getLogger("ocr_routes")

ocr_bp = Blueprint("ocr_bp", __name__, url_prefix="/api/ocr")

# Папка ocr_uploads удалена, данные хранятся в памяти (pdf_bytes)
JOBS = {}


@ocr_bp.route("/upload", methods=["POST"])
def upload_pdf():
    """Загрузка PDF для OCR-визуализации. Возвращает job_id и число страниц."""
    file = request.files.get("file")
    if not file or not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Пожалуйста, загрузите файл в формате PDF."}), 400

    job_id = uuid.uuid4().hex[:12]
    pdf_bytes = file.read()

    import fitz
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        page_count = len(doc)
        doc.close()
    except Exception as exc:
        return jsonify({"error": f"Не удалось открыть PDF: {exc}"}), 400

    JOBS[job_id] = {
        "filename": file.filename,
        "pdf_bytes": pdf_bytes,
        "page_count": page_count,
        "progress": {"percent": 0, "step": "uploaded"},
        "pages": {},
    }
    logger.info(f"[OCR] file={file.filename}")
    return jsonify({"job_id": job_id, "page_count": page_count})


@ocr_bp.route("/<job_id>/progress", methods=["GET"])
def get_progress(job_id):
    """Опрашивается фронтендом во время обработки страницы (тот же поллинг-паттерн,
    что уже используется в режиме PDF->DOCX)."""
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Задача не найдена"}), 404
    return jsonify(job["progress"])


@ocr_bp.route("/<job_id>/page/<int:page_num>", methods=["GET"])
def get_page_ocr(job_id, page_num):
    """
    Основной эндпоинт этапа:
    рендерит страницу, прогоняет через PaddleOCR, приводит к UDM через
    adapters.parse_paddle_to_udm и отдаёт фронтенду.

    Результат кэшируется в памяти на время жизни job'а (повторный запрос
    той же страницы не гоняет OCR заново).
    """
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Задача не найдена"}), 404

    if page_num < 1 or page_num > job["page_count"]:
        return jsonify({"error": f"Страница {page_num} вне диапазона (1..{job['page_count']})"}), 400

    if page_num in job["pages"]:
        return jsonify(job["pages"][page_num])

    def on_progress(percent, step):
        job["progress"] = {"percent": percent, "step": step}

    try:
        result = process_single_page(
            pdf_source=job["pdf_bytes"],
            page_number=page_num,
            job_id=job_id,
            on_progress=on_progress,
        )
    except OcrPipelineError as exc:
        job["progress"] = {"percent": 0, "step": "error"}
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        logger.exception("[OCR] unexpected error")
        job["progress"] = {"percent": 0, "step": "error"}
        return jsonify({"error": f"Внутренняя ошибка OCR: {exc}"}), 500

    result["image_url"] = f"/api/ocr/{job_id}/render/{page_num}"
    job["pages"][page_num] = result
    return jsonify(result)


@ocr_bp.route("/<job_id>/render/<int:page_num>", methods=["GET"])
def get_rendered_image(job_id, page_num):
    """Отдаёт PNG отрендеренной страницы (фон для overlay)."""
    job = JOBS.get(job_id)
    if not job or not job.get("pdf_bytes"):
        return jsonify({"error": "Задача не найдена"}), 404

    import io
    import fitz
    from flask import send_file
    try:
        doc = fitz.open(stream=job["pdf_bytes"], filetype="pdf")
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
    """Отдаёт normalized UDM JSON для диагностики."""
    job = JOBS.get(job_id)
    if not job or page_num not in job.get("pages", {}):
        return jsonify({"error": "Normalized-результат не найден"}), 404
    return jsonify(job["pages"][page_num])