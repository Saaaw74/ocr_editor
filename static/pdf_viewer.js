(function () {
  if (window.pdfjsLib) {
    pdfjsLib.GlobalWorkerOptions.workerSrc = '/static/pdf.worker.min.js';
  }

  // Тумблер режимов
  const modeNativeBtn = document.getElementById('pipeline-mode-native');
  const modeOcrBtn = document.getElementById('pipeline-mode-ocr');
  const hintText = document.getElementById('pipeline-hint-text');

  // Экраны
  const dropzone = document.getElementById('viewer-dropzone');
  const fileInput = document.getElementById('viewer-file-input');
  const uploadScreen = document.getElementById('viewer-upload-screen');
  const progressScreen = document.getElementById('viewer-progress-screen');
  const errorScreen = document.getElementById('viewer-error-screen');
  const workspace = document.getElementById('viewer-workspace');

  // Сообщения и ошибки
  const statusText = document.getElementById('viewer-status-text');
  const statusSub = document.getElementById('viewer-status-sub');
  const errorTitle = document.getElementById('viewer-error-title');
  const errorText = document.getElementById('viewer-error-text');
  const retryBtn = document.getElementById('viewer-retry-btn');
  const newFileBtn = document.getElementById('viewer-new-file-btn');

  // Холст
  const pageImg = document.getElementById('viewer-page-img');
  const pageCanvas = document.getElementById('viewer-page-canvas');
  const overlayLayer = document.getElementById('viewer-overlay-layer');
  const textLayerEl = document.getElementById('viewer-text-layer');
  
  const canvasStage = document.getElementById('viewer-canvas-stage');
  const pageIndicator = document.getElementById('viewer-page-indicator');
  const pageInput = document.getElementById('viewer-page-input');
  const pageTotal = document.getElementById('viewer-page-total');
  const prevPageBtn = document.getElementById('viewer-prev-page');
  
  const nextPageBtn = document.getElementById('viewer-next-page');
  const sourceBadge = document.getElementById('viewer-source-badge');

// Инструменты
  const toolSelectBtn = document.getElementById('viewer-tool-select');
  const toolEditBtn = document.getElementById('viewer-tool-edit');
  const toolAddTextBtn = document.getElementById('viewer-tool-add-text');
  const toolLineBtn = document.getElementById('viewer-tool-line');
  const linePanel = document.getElementById('viewer-line-panel');
  const lineColorInput = document.getElementById('viewer-line-color');
  const lineWidthSelect = document.getElementById('viewer-line-width');
  const eyeDropperBtn = document.getElementById('viewer-eyedropper-btn');
  const savePdfBtn = document.getElementById('viewer-save-pdf-btn');
  const exportDocxBtn = document.getElementById('viewer-export-docx-btn');
  const undoBtn = document.getElementById('viewer-undo-btn');
  const redoBtn = document.getElementById('viewer-redo-btn');

  // История правок для Undo / Redo
  let historyStack = [];
  let historyIndex = -1;
  const MAX_HISTORY = 40;

  function pushHistoryState() {
    if (historyIndex < historyStack.length - 1) {
      historyStack = historyStack.slice(0, historyIndex + 1);
    }
    historyStack.push(JSON.parse(JSON.stringify(documentModifications)));
    if (historyStack.length > MAX_HISTORY) {
      historyStack.shift();
    } else {
      historyIndex++;
    }
    updateUndoRedoButtons();
  }

  function updateUndoRedoButtons() {
    if (undoBtn) undoBtn.disabled = historyIndex <= 0;
    if (redoBtn) redoBtn.disabled = historyIndex >= historyStack.length - 1;
  }

  function applyHistoryState(index) {
    if (index < 0 || index >= historyStack.length) return;
    historyIndex = index;
    documentModifications = JSON.parse(JSON.stringify(historyStack[historyIndex]));
    updateUndoRedoButtons();
    renderPageOverlay();
  }

  function handleUndo() {
    if (historyIndex > 0) {
      applyHistoryState(historyIndex - 1);
    }
  }

  function handleRedo() {
    if (historyIndex < historyStack.length - 1) {
      applyHistoryState(historyIndex + 1);
    }
  }

  // Форматирование текста
  const formatPanel = document.getElementById('viewer-text-format-panel');
  const fontFamilySelect = document.getElementById('viewer-font-family');
  const fontSizeSelect = document.getElementById('viewer-font-size');
  const fmtBoldBtn = document.getElementById('viewer-fmt-bold');
  const fmtItalicBtn = document.getElementById('viewer-fmt-italic');
  const fmtUnderlineBtn = document.getElementById('viewer-fmt-underline');

  // Зум
  const zoomInBtn = document.getElementById('viewer-zoom-in');
  const zoomOutBtn = document.getElementById('viewer-zoom-out');
  const zoomValue = document.getElementById('viewer-zoom-value');

  let selectedPipeline = 'native'; // 'native' | 'ocr'
  let currentTool = 'select';       // 'select' | 'edit' | 'addText'
  let currentFontFamily = 'Arial';
  let currentFontSizePt = 11;
  let isBold = false;
  let isItalic = false;
  let isUnderline = false;

  let currentlyEditingBoxEl = null;
  let currentlyEditingBlockData = null;
  let activeFitTextFn = null;

  let viewerZoomScale = 1.33;
  let viewerJobId = null;
  let viewerCurrentPage = 1;
  let viewerTotalPages = 1;
  let viewerPageWidthPt = 595.0;
  let viewerPageHeightPt = 842.0;
  let progressTimer = null;
  let currentPageBlocks = [];
  let pdfjsDoc = null;

  let documentModifications = {};

  function resetDocumentState() {
    // Полная очистка всех правок предыдущего документа
    for (const key of Object.keys(documentModifications)) {
      delete documentModifications[key];
    }
    documentModifications = {};

    historyStack = [{}];
    historyIndex = 0;
    updateUndoRedoButtons();

    currentPageBlocks = [];
    currentlyEditingBoxEl = null;
    currentlyEditingBlockData = null;
    activeFitTextFn = null;
    viewerCurrentPage = 1;
    viewerTotalPages = 1;

    // Очищаем DOM оверлея
    if (overlayLayer) overlayLayer.innerHTML = '';

    if (textLayerEl) {
      textLayerEl.innerHTML = '';
      textLayerEl.style.transform = '';
      textLayerEl.style.width = '';
      textLayerEl.style.height = '';
      textLayerEl.style.removeProperty('--scale-factor');
      textLayerEl.style.display = '';
    }
  }

  function showToast(msg) {
    const toast = document.getElementById('toast');
    if (!toast) return;
    toast.textContent = msg;
    toast.classList.remove('hidden');
    requestAnimationFrame(() => { toast.style.opacity = '1'; });
    setTimeout(() => {
      toast.style.opacity = '0';
      setTimeout(() => toast.classList.add('hidden'), 200);
    }, 2600);
  }

  function setPipeline(pipeline) {
    selectedPipeline = pipeline;
    modeNativeBtn.classList.toggle('active', pipeline === 'native');
    modeOcrBtn.classList.toggle('active', pipeline === 'ocr');
    hintText.textContent = pipeline === 'native'
      ? 'Быстрое открытие с выделением, перемещением и редактированием текста.'
      : 'Распознавание сканов';
  }

  modeNativeBtn.addEventListener('click', () => setPipeline('native'));
  modeOcrBtn.addEventListener('click', () => setPipeline('ocr'));

  function setTool(tool) {
    currentTool = tool;
    toolSelectBtn.classList.toggle('active-tool', tool === 'select');
    toolEditBtn.classList.toggle('active-tool', tool === 'edit');
    if (toolAddTextBtn) toolAddTextBtn.classList.toggle('active-tool', tool === 'addText');
    if (toolLineBtn) toolLineBtn.classList.toggle('active-tool', tool === 'line');

    if (linePanel) linePanel.classList.toggle('hidden', tool !== 'line');
    if (formatPanel) formatPanel.classList.toggle('hidden', tool === 'line');

    overlayLayer.classList.toggle('editor-cursor-crosshair', tool === 'addText' || tool === 'line');
    renderPageOverlay();
  }

  toolSelectBtn.addEventListener('click', () => setTool('select'));
  toolEditBtn.addEventListener('click', () => setTool('edit'));
  if (toolAddTextBtn) toolAddTextBtn.addEventListener('click', () => setTool('addText'));
  if (toolLineBtn) toolLineBtn.addEventListener('click', () => setTool('line'));

  // Пипетка для захвата цвета со скана
  if (eyeDropperBtn) {
    if (!window.EyeDropper) {
      eyeDropperBtn.style.display = 'none'; 
    } else {
      eyeDropperBtn.addEventListener('click', async () => {
        try {
          const eyeDropper = new EyeDropper();
          const result = await eyeDropper.open();
          if (result && result.sRGBHex) {
            lineColorInput.value = result.sRGBHex;
          }
        } catch (e) {
          
        }
      });
    }
  }

  // Защищаем активный блок текста от потери фокуса при клике по кнопкам форматирования
  if (formatPanel) {
    formatPanel.addEventListener('mousedown', (e) => {
      if (e.target.tagName === 'BUTTON' || e.target.closest('button')) {
        e.preventDefault();
      }
    });
  }

  function applyFormatToActiveBlock() {
    if (!currentlyEditingBoxEl || !currentlyEditingBlockData) return;

    const currentPxPerPt = canvasStage ? (canvasStage.clientWidth / viewerPageWidthPt) : viewerZoomScale;

    currentlyEditingBoxEl.style.fontFamily = `"${currentFontFamily}", "Times New Roman", Arial, serif`;
    currentlyEditingBoxEl.style.fontSize = `${currentFontSizePt * currentPxPerPt}px`;
    currentlyEditingBoxEl.style.fontWeight = isBold ? '700' : '400';
    currentlyEditingBoxEl.style.fontStyle = isItalic ? 'italic' : 'normal';
    currentlyEditingBoxEl.style.textDecoration = isUnderline ? 'underline' : 'none';

    currentlyEditingBlockData.fontFamily = currentFontFamily;
    currentlyEditingBlockData.fontSize = currentFontSizePt;
    currentlyEditingBlockData.isBold = isBold;
    currentlyEditingBlockData.isItalic = isItalic;
    currentlyEditingBlockData.isUnderline = isUnderline;

    // Сразу фиксируем изменённый стиль в сохранённых модификациях документа
    const targetPage = currentlyEditingBlockData.pageNum || viewerCurrentPage;
    if (documentModifications[targetPage] && documentModifications[targetPage][currentlyEditingBlockData.id]) {
      const entry = documentModifications[targetPage][currentlyEditingBlockData.id];
      entry.fontSize = currentFontSizePt;
      entry.fontFamily = currentFontFamily;
      entry.isBold = isBold;
      entry.isItalic = isItalic;
      entry.isUnderline = isUnderline;
    }

    if (activeFitTextFn) activeFitTextFn();
  }

  if (fontFamilySelect) {
    fontFamilySelect.addEventListener('change', (e) => {
      currentFontFamily = e.target.value;
      applyFormatToActiveBlock();
      if (currentlyEditingBoxEl) currentlyEditingBoxEl.focus();
    });
  }

  if (fontSizeSelect) {
    fontSizeSelect.addEventListener('change', (e) => {
      currentFontSizePt = parseFloat(e.target.value);
      applyFormatToActiveBlock();
      if (currentlyEditingBoxEl) currentlyEditingBoxEl.focus();
    });
  }

  if (fmtBoldBtn) {
    fmtBoldBtn.addEventListener('click', () => {
      isBold = !isBold;
      fmtBoldBtn.classList.toggle('active-tool', isBold);
      applyFormatToActiveBlock();
    });
  }

  if (fmtItalicBtn) {
    fmtItalicBtn.addEventListener('click', () => {
      isItalic = !isItalic;
      fmtItalicBtn.classList.toggle('active-tool', isItalic);
      applyFormatToActiveBlock();
    });
  }

  if (fmtUnderlineBtn) {
    fmtUnderlineBtn.addEventListener('click', () => {
      isUnderline = !isUnderline;
      fmtUnderlineBtn.classList.toggle('active-tool', isUnderline);
      applyFormatToActiveBlock();
    });
  }

  function showScreen(name) {
    uploadScreen.classList.toggle('hidden', name !== 'upload');
    progressScreen.classList.toggle('hidden', name !== 'progress');
    errorScreen.classList.toggle('hidden', name !== 'error');
    workspace.classList.toggle('hidden', name !== 'workspace');
  }

  dropzone.addEventListener('click', () => fileInput.click());
  ['dragenter', 'dragover'].forEach((evt) => {
    dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.add('dragover'); });
  });
  ['dragleave', 'drop'].forEach((evt) => {
    dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.remove('dragover'); });
  });
  dropzone.addEventListener('drop', (e) => {
    const file = e.dataTransfer.files[0];
    if (file) uploadViewerFile(file);
  });
  fileInput.addEventListener('change', () => {
    if (fileInput.files[0]) uploadViewerFile(fileInput.files[0]);
  });

  async function uploadViewerFile(file) {
    if (!file.name.toLowerCase().endsWith('.pdf')) {
      showScreen('error');
      errorTitle.textContent = 'Не удалось обработать документ';
      errorText.textContent = 'Пожалуйста, выберите файл в формате PDF.';
      return;
    }

    resetDocumentState();

    showScreen('progress');
    statusText.textContent = 'Загрузка PDF…';
    statusSub.textContent = '';

    const formData = new FormData();
    formData.append('file', file);
    formData.append('mode', selectedPipeline);

    try {
      const resp = await fetch('/api/document/upload', { method: 'POST', body: formData });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || `Ошибка загрузки (HTTP ${resp.status})`);

      viewerJobId = data.job_id;
      viewerTotalPages = data.page_count;
      viewerCurrentPage = 1;

      await loadViewerPage(viewerCurrentPage);
    } catch (err) {
      showScreen('error');
      errorTitle.textContent = 'Не удалось открыть документ';
      errorText.textContent = err.message || 'Неизвестная ошибка.';
    }
  }

  function pollViewerProgress() {
    clearTimeout(progressTimer);
    progressTimer = setTimeout(async () => {
      if (!viewerJobId) return;
      try {
        const resp = await fetch(`/api/document/${viewerJobId}/progress`);
        const data = await resp.json();
        if (data.percent !== undefined) {
          statusText.textContent = `Обработка страницы… ${data.percent}%`;
          statusSub.textContent = data.step || '';
        }
        if (data.percent < 100) pollViewerProgress();
      } catch (err) {}
    }, 400);
  }

  async function renderPdfPage(pageObj) {
    const viewport = pageObj.getViewport({ scale: viewerZoomScale });
    const outputScale = window.devicePixelRatio || 1;

    const stageWidth = Math.floor(viewport.width);
    const stageHeight = Math.floor(viewport.height);

    if (canvasStage) {
      canvasStage.style.width = `${stageWidth}px`;
      canvasStage.style.height = `${stageHeight}px`;
    }

    pageCanvas.width = Math.floor(viewport.width * outputScale);
    pageCanvas.height = Math.floor(viewport.height * outputScale);
    pageCanvas.style.width = `${stageWidth}px`;
    pageCanvas.style.height = `${stageHeight}px`;

    pageCanvas.classList.remove('hidden');
    pageImg.classList.add('hidden');

    const canvasContext = pageCanvas.getContext('2d');
    const transform = outputScale !== 1 ? [outputScale, 0, 0, outputScale, 0, 0] : null;

    await pageObj.render({
      canvasContext: canvasContext,
      transform: transform,
      viewport: viewport
    }).promise;

    overlayLayer.style.width = `${stageWidth}px`;
    overlayLayer.style.height = `${stageHeight}px`;
  }

  async function renderPageOverlay() {
    if (currentTool === 'select' && pdfjsDoc) {
      overlayLayer.innerHTML = '';
      overlayLayer.style.display = 'none';

      textLayerEl.style.display = '';
      textLayerEl.innerHTML = '';
      textLayerEl.style.transform = '';

      const pageIndex = selectedPipeline === 'ocr' ? 1 : viewerCurrentPage;
      const page = await pdfjsDoc.getPage(pageIndex);
      const viewport = page.getViewport({ scale: viewerZoomScale });
      textLayerEl.style.setProperty('--scale-factor', viewport.scale);

      const textContent = await page.getTextContent();
      const textLayerRender = pdfjsLib.renderTextLayer({
        textContentSource: textContent,
        container: textLayerEl,
        viewport: viewport,
        textDivs: []
      });
      if (textLayerRender && textLayerRender.promise) {
        await textLayerRender.promise;
      }
      return;
    }

    textLayerEl.innerHTML = '';
    textLayerEl.style.display = 'none';
    overlayLayer.style.display = '';
    overlayLayer.className = 'editor-overlay-layer';
    overlayLayer.style.transform = '';
    overlayLayer.style.removeProperty('--scale-factor');
    overlayLayer.style.width = `${canvasStage ? canvasStage.clientWidth : Math.floor(viewerPageWidthPt * viewerZoomScale)}px`;
    overlayLayer.style.height = `${canvasStage ? canvasStage.clientHeight : Math.floor(viewerPageHeightPt * viewerZoomScale)}px`;
    overlayLayer.innerHTML = '';

    const pageMods = documentModifications[viewerCurrentPage] || {};

    let svgLayer = canvasStage.querySelector('.editor-svg-layer');
    if (!svgLayer) {
      svgLayer = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svgLayer.setAttribute('class', 'editor-svg-layer');
      canvasStage.appendChild(svgLayer);
    }
    svgLayer.innerHTML = '';
    svgLayer.setAttribute('viewBox', `0 0 ${viewerPageWidthPt} ${viewerPageHeightPt}`);

    // Рисуем существующие линии на странице (игнорируя любые красные артефакты)
    Object.entries(pageMods).forEach(([id, mod]) => {
      if (mod.type === 'line' && mod.p1 && mod.p2) {
        const strokeCol = (mod.color || '#000000').toLowerCase();
        // Полностью удаляем из состояния любые красные палки
        if (['#ff0000', '#f00', '#ef4444', '#e11d48', 'red'].includes(strokeCol)) {
          delete pageMods[id];
          return;
        }

        const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('x1', mod.p1[0]);
        line.setAttribute('y1', mod.p1[1]);
        line.setAttribute('x2', mod.p2[0]);
        line.setAttribute('y2', mod.p2[1]);
        line.setAttribute('stroke', mod.color || '#000000');
        line.setAttribute('stroke-width', mod.width || 1);
        line.setAttribute('stroke-linecap', 'square');
        line.setAttribute('class', 'drawn-line');
        line.dataset.id = id;
        line.title = 'Кликните для удаления линии';
        line.addEventListener('click', (e) => {
          if (currentTool === 'edit' || currentTool === 'line') {
            e.stopPropagation();
            delete documentModifications[viewerCurrentPage][id];
            pushHistoryState();
            renderPageOverlay();
          }
        });
        svgLayer.appendChild(line);
      }
    });

    Object.entries(pageMods).forEach(([id, mod]) => {
      if (!mod.isNew && mod.origBbox) {
        drawWhitePatch(id, mod.origBbox);
      }
    });

    const renderedIds = new Set();

    currentPageBlocks.forEach((block) => {
      if (block.type === 'picture') return;

      const lines = (block.lines && block.lines.length > 0)
        ? block.lines
        : [{ id: `${block.id}_l0`, bbox: block.bbox, text: block.raw_text || '' }];

      lines.forEach((line) => {
        renderedIds.add(line.id);
        const mod = pageMods[line.id];
        const currentBbox = mod ? mod.bbox : [line.bbox.x1, line.bbox.y1, line.bbox.x2, line.bbox.y2];
        const [x0, y0, x1, y1] = currentBbox;

        // Физические координаты оригинальных чернил на скане:
        // если сработал снап колонок — берем original_bbox, иначе текущий bbox строки
        const origBoxObj = line.original_bbox || line.bbox;
        const physicalBbox = [origBoxObj.x1, origBoxObj.y1, origBoxObj.x2, origBoxObj.y2];

        const boxEl = document.createElement('div');
        boxEl.className = 'ocr-text-box';
        boxEl.dataset.id = line.id;
        boxEl.spellcheck = false;
        boxEl.setAttribute('spellcheck', 'false');
        boxEl.setAttribute('autocorrect', 'off');
        boxEl.setAttribute('autocapitalize', 'off');
        boxEl.setAttribute('data-gramm', 'false');

        const leftPct = (x0 / viewerPageWidthPt) * 100;
        const topPct = (y0 / viewerPageHeightPt) * 100;
        const widthPct = ((x1 - x0) / viewerPageWidthPt) * 100;
        const heightPct = ((y1 - y0) / viewerPageHeightPt) * 100;

        boxEl.style.left = `${leftPct}%`;
        boxEl.style.top = `${topPct}%`;
        boxEl.style.width = `${widthPct}%`;
        boxEl.style.height = `${heightPct}%`;

        const hPt = y1 - y0;
        const fontMeta = block.font || {};
        // Берем спан строки, в котором хранятся точные параметры начертания и шрифт
        const firstSpan = (line.spans && line.spans.length > 0) ? line.spans[0] : null;

        const rawFontFam = (mod && mod.fontFamily) || (firstSpan && firstSpan.font_family) || fontMeta.family || 'Arial';
        const fontFam = `"${rawFontFam}", "Times New Roman", Arial, serif`;
        const fontSizePt = (mod && mod.fontSize) || (firstSpan && firstSpan.size_pt) || fontMeta.size_pt || Math.max(7, hPt * 0.82);

        // Приоритет: правка пользователя -> флаг спана -> вес родительского блока
        const isLineBold = (mod && mod.isBold !== undefined)
          ? Boolean(mod.isBold)
          : Boolean((firstSpan && firstSpan.is_bold) || (fontMeta.weight && fontMeta.weight >= 600));

        const isLineItalic = (mod && mod.isItalic !== undefined)
          ? Boolean(mod.isItalic)
          : Boolean((firstSpan && firstSpan.is_italic) || fontMeta.italic);

        const fontWeight = isLineBold ? 700 : 400;

        const lineScaleX = (firstSpan && firstSpan.scale_x) || fontMeta.scale_x || 1.0;
        const lineBaselineOffset = (firstSpan && firstSpan.baseline_offset) || fontMeta.baseline_offset || 0.0;

        // Переводим PDF pt в экранные пиксели строго по масштабу сцены
        const currentPxPerPt = canvasStage ? (canvasStage.clientWidth / viewerPageWidthPt) : viewerZoomScale;
        const screenFontSizePx = fontSizePt * currentPxPerPt;

        boxEl.style.fontFamily = fontFam;
        boxEl.style.fontSize = `${screenFontSizePx}px`;
        boxEl.style.fontWeight = fontWeight;
        boxEl.style.fontStyle = isLineItalic ? 'italic' : 'normal';
        boxEl.style.textDecoration = (mod && mod.isUnderline) ? 'underline' : 'none';
        boxEl.style.lineHeight = '1';

        if (mod) {
          boxEl.textContent = mod.text;
          boxEl.classList.add('is-modified');
          // Если текст стёрт — полностью убираем элемент с экрана и отключаем клики
          if (!mod.text || !mod.text.trim()) {
            boxEl.classList.add('is-empty');
            boxEl.style.display = 'none';
            boxEl.style.pointerEvents = 'none';
          }
        } else {
          boxEl.textContent = line.text || '';
        }

        const occScaleX = (mod && mod.scaleX) || lineScaleX;
        if (occScaleX && Math.abs(occScaleX - 1.0) > 0.02 && boxEl.textContent.trim() && !(mod && mod.isNew)) {
          const scaled = document.createElement('span');
          scaled.style.display = 'inline-block';
          scaled.style.transform = `scaleX(${occScaleX})`;
          scaled.style.transformOrigin = 'left top';
          scaled.textContent = boxEl.textContent;
          boxEl.textContent = '';
          boxEl.appendChild(scaled);
        }

        makeDraggableAndEditable(boxEl, {
          id: line.id,
          text: boxEl.textContent,
          bbox: currentBbox,
          origBbox: physicalBbox,
          isNew: false,
          pageNum: viewerCurrentPage,
          fontFamily: rawFontFam,
          fontSize: fontSizePt,
          isBold: isLineBold,
          isItalic: isLineItalic,
          font: fontMeta,
          scaleX: lineScaleX,
          baselineOffset: lineBaselineOffset
        });

        overlayLayer.appendChild(boxEl);
      });
    });

    // Отрисовка новых добавленных пользователем блоков
    Object.entries(pageMods).forEach(([id, mod]) => {
      if (!mod.isNew || renderedIds.has(id) || !mod.text || !mod.text.trim()) return;
      const [x0, y0, x1, y1] = mod.bbox;
      const boxEl = document.createElement('div');
      boxEl.className = 'ocr-text-box is-new is-modified';
      boxEl.dataset.id = id;
      boxEl.setAttribute('spellcheck', 'false');
      boxEl.textContent = mod.text;

      boxEl.style.left = `${(x0 / viewerPageWidthPt) * 100}%`;
      boxEl.style.top = `${(y0 / viewerPageHeightPt) * 100}%`;
      boxEl.style.width = `${((x1 - x0) / viewerPageWidthPt) * 100}%`;
      boxEl.style.height = `${((y1 - y0) / viewerPageHeightPt) * 100}%`;

      const currentPxPerPt = canvasStage ? (canvasStage.clientWidth / viewerPageWidthPt) : viewerZoomScale;
      const customFontSizePt = mod.fontSize || currentFontSizePt;
      const screenFontSizePx = customFontSizePt * currentPxPerPt;

      boxEl.style.fontFamily = `"${mod.fontFamily || currentFontFamily}", "Times New Roman", Arial, serif`;
      boxEl.style.fontSize = `${screenFontSizePx}px`;
      boxEl.style.fontWeight = mod.isBold ? 700 : 400;
      boxEl.style.fontStyle = mod.isItalic ? 'italic' : 'normal';
      boxEl.style.textDecoration = mod.isUnderline ? 'underline' : 'none';
      boxEl.style.lineHeight = `${(y1 - y0) * currentPxPerPt}px`;

      makeDraggableAndEditable(boxEl, {
        id: id,
        text: mod.text,
        bbox: [...mod.bbox],
        isNew: true,
        fontFamily: mod.fontFamily,
        fontSize: mod.fontSize,
        isBold: mod.isBold,
        isItalic: mod.isItalic,
        isUnderline: mod.isUnderline,
        font: mod.font,
        scaleX: mod.scaleX || (mod.font && mod.font.scale_x) || 1.0,
        baselineOffset: mod.baselineOffset || (mod.font && mod.font.baseline_offset) || 0.0
      });

      overlayLayer.appendChild(boxEl);
    });
  }

  function drawWhitePatch(blockId, origBbox) {
    const [x0, y0, x1, y1] = origBbox;

    const safe = [x0, y0, x1, y1];

    const old = overlayLayer.querySelector(`.white-patch[data-for="${blockId}"]`);
    if (old) old.remove();

    const patch = document.createElement('div');
    patch.className = 'white-patch';
    patch.dataset.for = blockId;
    patch.style.left = `${(safe[0] / viewerPageWidthPt) * 100}%`;
    patch.style.top = `${(safe[1] / viewerPageHeightPt) * 100}%`;
    patch.style.width = `${((safe[2] - safe[0]) / viewerPageWidthPt) * 100}%`;
    patch.style.height = `${((safe[3] - safe[1]) / viewerPageHeightPt) * 100}%`;

    overlayLayer.insertBefore(patch, overlayLayer.firstChild);
  }

  // ---- Направляющие выравнивания при перетаскивании (как в Acrobat / PowerPoint) ----
  // Порог примагничивания в pt страницы (не в экранных px), чтобы поведение
  // не "плыло" при разных уровнях зума.
  const ALIGN_SNAP_THRESHOLD_PT = 4.0;

  function ensureAlignGuides() {
    let vGuide = canvasStage.querySelector('.align-guide-v');
    let hGuide = canvasStage.querySelector('.align-guide-h');
    if (!vGuide) {
      vGuide = document.createElement('div');
      vGuide.className = 'align-guide align-guide-v';
      canvasStage.appendChild(vGuide);
    }
    if (!hGuide) {
      hGuide = document.createElement('div');
      hGuide.className = 'align-guide align-guide-h';
      canvasStage.appendChild(hGuide);
    }
    return { vGuide, hGuide };
  }

  function hideAlignGuides() {
    const { vGuide, hGuide } = ensureAlignGuides();
    vGuide.style.display = 'none';
    hGuide.style.display = 'none';
  }

  function makeDraggableAndEditable(boxEl, blockData) {
    let isDragging = false;
    let startX, startY, origLeft, origTop;
    let hasMoved = false;

    boxEl.addEventListener('mousedown', (e) => {
      if (currentTool !== 'edit' || boxEl.classList.contains('is-editing')) return;
      isDragging = true;
      hasMoved = false;
      startX = e.clientX;
      startY = e.clientY;

      const rect = boxEl.getBoundingClientRect();
      const parentRect = overlayLayer.getBoundingClientRect();
      origLeft = rect.left - parentRect.left;
      origTop = rect.top - parentRect.top;

      const boxWidthPx = rect.width;
      const boxHeightPx = rect.height;

      function onMouseMove(ev) {
        if (!isDragging) return;
        const dx = ev.clientX - startX;
        const dy = ev.clientY - startY;

        if (Math.abs(dx) > 3 || Math.abs(dy) > 3) {
          hasMoved = true;
          boxEl.classList.add('is-dragging');
        }

        let newLeftPx = origLeft + dx;
        let newTopPx = origTop + dy;

        // --- Привязка к центру листа по вертикали/горизонтали ---
        const stageWidthPx = parentRect.width;
        const stageHeightPx = parentRect.height;
        const pxPerPt = stageWidthPx / viewerPageWidthPt;
        const snapThresholdPx = ALIGN_SNAP_THRESHOLD_PT * pxPerPt;

        const pageCenterXpx = stageWidthPx / 2;
        const pageCenterYpx = stageHeightPx / 2;

        const boxCenterXpx = newLeftPx + boxWidthPx / 2;
        const boxCenterYpx = newTopPx + boxHeightPx / 2;

        const { vGuide, hGuide } = ensureAlignGuides();

        if (Math.abs(boxCenterXpx - pageCenterXpx) <= snapThresholdPx) {
          newLeftPx = pageCenterXpx - boxWidthPx / 2;
          vGuide.style.left = `${pageCenterXpx}px`;
          vGuide.style.display = 'block';
        } else {
          vGuide.style.display = 'none';
        }

        if (Math.abs(boxCenterYpx - pageCenterYpx) <= snapThresholdPx) {
          newTopPx = pageCenterYpx - boxHeightPx / 2;
          hGuide.style.top = `${pageCenterYpx}px`;
          hGuide.style.display = 'block';
        } else {
          hGuide.style.display = 'none';
        }

        boxEl.style.left = `${(newLeftPx / parentRect.width) * 100}%`;
        boxEl.style.top = `${(newTopPx / parentRect.height) * 100}%`;
      }

      function onMouseUp() {
        if (!isDragging) return;
        isDragging = false;
        boxEl.classList.remove('is-dragging');
        hideAlignGuides();
        document.removeEventListener('mousemove', onMouseMove);
        document.removeEventListener('mouseup', onMouseUp);

        if (hasMoved) {
          const parentRect = overlayLayer.getBoundingClientRect();
          const curLeftPx = boxEl.getBoundingClientRect().left - parentRect.left;
          const curTopPx = boxEl.getBoundingClientRect().top - parentRect.top;

          const x0_pt = (curLeftPx / parentRect.width) * viewerPageWidthPt;
          const y0_pt = (curTopPx / parentRect.height) * viewerPageHeightPt;
          const w_pt = blockData.bbox[2] - blockData.bbox[0];
          const h_pt = blockData.bbox[3] - blockData.bbox[1];

          const origBbox = blockData.origBbox || [...blockData.bbox];
          const newBbox = [x0_pt, y0_pt, x0_pt + w_pt, y0_pt + h_pt];
          blockData.bbox = newBbox;
          blockData.origBbox = origBbox;

          if (!blockData.isNew) {
            drawWhitePatch(blockData.id, origBbox);
          }

          // Определяем номер страницы строго из данных блока или текущей страницы
          const targetPage = blockData.pageNum || viewerCurrentPage;
          if (!documentModifications[targetPage]) {
            documentModifications[targetPage] = {};
          }

          const existing = documentModifications[targetPage][blockData.id] || {};
          // Сохраняем реальный выбранный кегль, а не перетираем его формулой от высоты
          const preservedFontSize = blockData.fontSize || existing.fontSize || (h_pt * 0.85);

          documentModifications[targetPage][blockData.id] = {
            text: existing.text !== undefined ? existing.text : boxEl.textContent,
            bbox: newBbox,
            origBbox: origBbox,
            fontSize: parseFloat(Number(preservedFontSize).toFixed(1)),
            fontFamily: blockData.fontFamily || existing.fontFamily || currentFontFamily,
            isBold: blockData.isBold !== undefined ? blockData.isBold : existing.isBold,
            isItalic: blockData.isItalic !== undefined ? blockData.isItalic : existing.isItalic,
            isUnderline: blockData.isUnderline !== undefined ? blockData.isUnderline : existing.isUnderline,
            scaleX: blockData.scaleX || existing.scaleX || 1.0,
            baselineOffset: blockData.baselineOffset || existing.baselineOffset || 0.0,
            isNew: Boolean(blockData.isNew)
          };
          boxEl.classList.add('is-modified');
          pushHistoryState();
        }
      }

      document.addEventListener('mousemove', onMouseMove);
      document.addEventListener('mouseup', onMouseUp);
    });

    boxEl.addEventListener('click', (e) => {
      if (currentTool !== 'edit') return;
      e.stopPropagation();
      if (!hasMoved) {
        enableInlineEdit(boxEl, blockData);
      }
    });
  }

  function enableInlineEdit(boxEl, blockData) {
    if (boxEl.classList.contains('is-editing')) return;

    // Запоминаем исходные значения до начала редактирования
    const initialText = boxEl.textContent.trim();
    const initialFamily = blockData.fontFamily;
    const initialSize = blockData.fontSize;
    const initialBold = Boolean(blockData.isBold);
    const initialItalic = Boolean(blockData.isItalic);
    const initialUnderline = Boolean(blockData.isUnderline);
    const targetPage = blockData.pageNum || viewerCurrentPage;
    const wasAlreadyModified = Boolean(documentModifications[targetPage] && documentModifications[targetPage][blockData.id]);

    currentlyEditingBoxEl = boxEl;
    currentlyEditingBlockData = blockData;

    // Синхронизируем панель шрифтов
    const fontMeta = blockData.font || {};
    let rawFamily = blockData.fontFamily || fontMeta.family || 'Arial';
    // Очищаем постфиксы сабсетов и начертаний
    let cleanFamily = rawFamily.replace(/^[A-Z]{6}\+/, '').replace(/(MT|PSMT|PS|Bold|Italic|Regular)$/i, '').trim() || rawFamily;
    if (/times/i.test(cleanFamily)) cleanFamily = 'Times New Roman';
    if (/arial/i.test(cleanFamily)) cleanFamily = 'Arial';
    if (/courier/i.test(cleanFamily)) cleanFamily = 'Courier New';

    currentFontFamily = cleanFamily;
    if (fontFamilySelect) {
      const norm = (s) => (s || '').toLowerCase().replace(/[\s\-_]/g, '');
      const matchedOpt = Array.from(fontFamilySelect.options).find(opt => norm(opt.value) === norm(cleanFamily));
      
      if (matchedOpt) {
        fontFamilySelect.value = matchedOpt.value;
        currentFontFamily = matchedOpt.value;
      } else if (cleanFamily) {
        const newOpt = document.createElement('option');
        newOpt.value = cleanFamily;
        newOpt.textContent = cleanFamily;
        fontFamilySelect.appendChild(newOpt);
        fontFamilySelect.value = cleanFamily;
      }
    }

    const hPt = blockData.bbox[3] - blockData.bbox[1];
    const rawSize = blockData.fontSize || fontMeta.size_pt || Math.max(7.5, hPt * 0.75);
    currentFontSizePt = parseFloat(Number(rawSize).toFixed(1));

    if (fontSizeSelect) {
      const nearestInt = String(Math.round(currentFontSizePt));
      fontSizeSelect.value = nearestInt;
    }

    isBold = blockData.isBold !== undefined ? Boolean(blockData.isBold) : Boolean(fontMeta.weight >= 600);
    isItalic = blockData.isItalic !== undefined ? Boolean(blockData.isItalic) : Boolean(fontMeta.italic);
    isUnderline = Boolean(blockData.isUnderline);

    // Принудительно фиксируем инлайн-стили элемента при переходе в режим редактирования,
    // чтобы CSS-класс .is-editing не сбросил вес шрифта в дефолтный браузерный
    const currentPxPerPt = canvasStage ? (canvasStage.clientWidth / viewerPageWidthPt) : viewerZoomScale;
    boxEl.style.fontFamily = `"${currentFontFamily}", "Times New Roman", Arial, serif`;
    boxEl.style.fontSize = `${currentFontSizePt * currentPxPerPt}px`;
    boxEl.style.fontWeight = isBold ? '700' : '400';
    boxEl.style.fontStyle = isItalic ? 'italic' : 'normal';
    boxEl.style.textDecoration = isUnderline ? 'underline' : 'none';

    if (fmtBoldBtn) fmtBoldBtn.classList.toggle('active-tool', isBold);
    if (fmtItalicBtn) fmtItalicBtn.classList.toggle('active-tool', isItalic);
    if (fmtUnderlineBtn) fmtUnderlineBtn.classList.toggle('active-tool', isUnderline);

    boxEl.spellcheck = false;
    boxEl.setAttribute('spellcheck', 'false');
    boxEl.setAttribute('autocomplete', 'off');
    boxEl.setAttribute('autocorrect', 'off');
    boxEl.setAttribute('autocapitalize', 'off');
    boxEl.setAttribute('data-gramm', 'false');
    boxEl.contentEditable = 'true';
    boxEl.classList.add('is-editing');
    boxEl.focus();

    // Ставим мигающий курсор в конец текста
    const range = document.createRange();
    range.selectNodeContents(boxEl);
    range.collapse(false);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);

    // Сбрасываем жесткую ширину при редактировании, чтобы блок тянулся за текстом
    function updateBoxWidth() {
      boxEl.style.width = 'auto';
      boxEl.style.minWidth = '20px';
    }

    activeFitTextFn = updateBoxWidth;
    updateBoxWidth();
    boxEl.addEventListener('input', updateBoxWidth);

    function finishEdit(e) {
      // Игнорируем blur, если клик произошел внутри верхней панели форматирования
      if (e && e.relatedTarget && formatPanel && formatPanel.contains(e.relatedTarget)) {
        return;
      }

      boxEl.contentEditable = 'false';
      boxEl.classList.remove('is-editing');
      boxEl.removeEventListener('input', updateBoxWidth);

      if (currentlyEditingBoxEl === boxEl) {
        currentlyEditingBoxEl = null;
        currentlyEditingBlockData = null;
      }
      if (activeFitTextFn === updateBoxWidth) activeFitTextFn = null;

      const newText = boxEl.textContent.trim();
      
      // Определяем номер страницы строго из данных блока или текущей страницы
      const targetPage = blockData.pageNum || viewerCurrentPage;
      if (!documentModifications[targetPage]) {
        documentModifications[targetPage] = {};
      }

      const existing = documentModifications[targetPage][blockData.id] || {};

      // Считываем точные геометрические координаты блока относительно сцены
      const parentRect = overlayLayer.getBoundingClientRect();
      const boxRect = boxEl.getBoundingClientRect();

      const curLeftPx = boxRect.left - parentRect.left;
      const curTopPx = boxRect.top - parentRect.top;

      const x0_pt = (curLeftPx / parentRect.width) * viewerPageWidthPt;
      const y0_pt = (curTopPx / parentRect.height) * viewerPageHeightPt;

      const currentPxPerPt = canvasStage ? (canvasStage.clientWidth / viewerPageWidthPt) : viewerZoomScale;
      const newWidthPt = currentPxPerPt > 0 ? (boxRect.width / currentPxPerPt) : (blockData.bbox[2] - blockData.bbox[0]);
      const newHeightPt = currentPxPerPt > 0 ? (boxRect.height / currentPxPerPt) : (blockData.bbox[3] - blockData.bbox[1]);

      const updatedBbox = [x0_pt, y0_pt, x0_pt + newWidthPt, y0_pt + newHeightPt];
      blockData.bbox = updatedBbox;

      // Устанавливаем ширину элемента в % от страницы
      boxEl.style.width = `${(newWidthPt / viewerPageWidthPt) * 100}%`;

      const effOrigBbox = existing.origBbox || blockData.origBbox || blockData.bbox;

      // 1. Если текст полностью удалили
      if (!newText) {
        if (blockData.isNew) {
          delete documentModifications[targetPage][blockData.id];
        } else {
          documentModifications[targetPage][blockData.id] = {
            text: '',
            bbox: updatedBbox,
            origBbox: effOrigBbox,
            fontSize: blockData.fontSize || currentFontSizePt,
            fontFamily: blockData.fontFamily || currentFontFamily,
            isBold: Boolean(blockData.isBold),
            isItalic: Boolean(blockData.isItalic),
            isUnderline: false,
            scaleX: blockData.scaleX || 1.0,
            baselineOffset: blockData.baselineOffset || 0.0,
            isNew: false
          };
          drawWhitePatch(blockData.id, effOrigBbox);
        }
        // Физически вырезаем пустой бокс из DOM, чтобы он не оставлял контуров и подчеркиваний
        boxEl.removeEventListener('blur', finishEdit);
        boxEl.remove();
        pushHistoryState();
        return;
      }

      // 2. Проверяем, менялся ли блок на самом деле
      const textChanged = newText !== initialText;
      const fontFamChanged = (blockData.fontFamily || currentFontFamily) !== initialFamily;
      const fontSizeChanged = Math.abs((blockData.fontSize || currentFontSizePt) - (initialSize || 0)) > 0.1;
      const boldChanged = Boolean(blockData.isBold) !== initialBold;
      const italicChanged = Boolean(blockData.isItalic) !== initialItalic;
      const underlineChanged = Boolean(blockData.isUnderline) !== initialUnderline;

      const hasActualChanges = textChanged || fontFamChanged || fontSizeChanged || boldChanged || italicChanged || underlineChanged;

      // Если изменений не было и блок до этого не был изменён — возвращаем его в исходное состояние
      if (!hasActualChanges && !wasAlreadyModified && !blockData.isNew) {
        boxEl.classList.remove('is-modified');
        boxEl.style.width = `${((blockData.bbox[2] - blockData.bbox[0]) / viewerPageWidthPt) * 100}%`;
        // Удаляем белую плашку, если случайно появилась
        const oldPatch = overlayLayer.querySelector(`.white-patch[data-for="${blockData.id}"]`);
        if (oldPatch) oldPatch.remove();

        boxEl.removeEventListener('blur', finishEdit);
        return;
      }

      // 3. Если изменения действительно были — сохраняем модификацию
      documentModifications[targetPage][blockData.id] = {
        text: newText,
        bbox: updatedBbox,
        origBbox: effOrigBbox,
        fontSize: blockData.fontSize || currentFontSizePt,
        fontFamily: blockData.fontFamily || currentFontFamily,
        isBold: Boolean(blockData.isBold),
        isItalic: Boolean(blockData.isItalic),
        isUnderline: Boolean(blockData.isUnderline),
        scaleX: blockData.scaleX || 1.0,
        baselineOffset: blockData.baselineOffset || 0.0,
        isNew: Boolean(blockData.isNew)
      };

      if (!blockData.isNew) {
        drawWhitePatch(blockData.id, effOrigBbox);
      }

      boxEl.classList.add('is-modified');
      boxEl.removeEventListener('blur', finishEdit);
      pushHistoryState();
    }

    boxEl.addEventListener('blur', finishEdit);
    boxEl.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        boxEl.blur();
      }
    });
  }

  // --- Загрузка и показ страницы ---
  async function loadViewerPage(pageNum) {
    showScreen('progress');
    statusText.textContent = 'Обработка страницы… 0%';
    statusSub.textContent = selectedPipeline === 'native' ? 'Рендеринг страницы…' : 'PaddleOCR + TextLayer…';
    pollViewerProgress();

    try {
      const resp = await fetch(`/api/document/${viewerJobId}/page/${pageNum}`);
      const data = await resp.json();
      clearTimeout(progressTimer);

      if (!resp.ok) {
        throw new Error(data.error || 'Не удалось обработать страницу.');
      }

      viewerPageWidthPt = data.width_pt;
      viewerPageHeightPt = data.height_pt;
      currentPageBlocks = data.blocks || [];
      viewerCurrentPage = pageNum;
      if (pageInput) {
        pageInput.value = pageNum;
        pageInput.max = viewerTotalPages;
      }
      if (pageTotal) {
        pageTotal.textContent = `/ ${viewerTotalPages}`;
      }

      sourceBadge.textContent = data.mode === 'native' ? 'NATIVE' : 'OCR SCAN';
      sourceBadge.classList.toggle('badge-native', data.mode === 'native');
      sourceBadge.classList.toggle('badge-ocr', data.mode === 'ocr');

      showScreen('workspace');

      // Инициализируем PDF.js:
      // Для Native — исходный PDF, для OCR — сгенерированный Searchable PDF
      const pdfUrl = data.mode === 'native' 
        ? `/api/document/${viewerJobId}/file` 
        : data.searchable_pdf_url;

      const loadingTask = pdfjsLib.getDocument(pdfUrl);
      pdfjsDoc = await loadingTask.promise;
      const pageIndex = data.mode === 'native' ? pageNum : 1;
      const pageObj = await pdfjsDoc.getPage(pageIndex);
      
      await renderPdfPage(pageObj);
      await renderPageOverlay();

    } catch (err) {
      clearTimeout(progressTimer);
      showScreen('error');
      errorTitle.textContent = 'Не удалось обработать документ';
      errorText.textContent = err.message || 'Соединение с сервером потеряно.';
    }
  }

  // --- Пагинация ---
  function goToPage(targetPage) {
    const page = parseInt(targetPage, 10);
    if (isNaN(page)) {
      if (pageInput) pageInput.value = viewerCurrentPage;
      return;
    }
    const validPage = Math.max(1, Math.min(viewerTotalPages, page));
    if (pageInput) pageInput.value = validPage;
    if (validPage !== viewerCurrentPage) {
      viewerCurrentPage = validPage;
      loadViewerPage(validPage);
    }
  }

  if (pageInput) {
    pageInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        pageInput.blur();
      }
    });

    pageInput.addEventListener('change', () => {
      goToPage(pageInput.value);
    });

    pageInput.addEventListener('focus', () => {
      pageInput.select();
    });
  }

  prevPageBtn.addEventListener('click', () => {
    if (viewerCurrentPage > 1) {
      viewerCurrentPage--;
      loadViewerPage(viewerCurrentPage);
    }
  });

  nextPageBtn.addEventListener('click', () => {
    if (viewerCurrentPage < viewerTotalPages) {
      viewerCurrentPage++;
      loadViewerPage(viewerCurrentPage);
    }
  });

  // --- Сохранение PDF (Модальное окно выбора режима) ---
  const saveModalOverlay = document.getElementById('save-modal-overlay');
  const saveModalDialog = document.getElementById('save-modal-dialog');
  const btnSaveRaster = document.getElementById('btn-save-raster');
  const btnSaveSearchable = document.getElementById('btn-save-searchable');

  function openSaveModal() {
    if (saveModalOverlay) saveModalOverlay.classList.remove('hidden');
  }

  function closeSaveModal() {
    if (saveModalOverlay) saveModalOverlay.classList.add('hidden');
  }

  if (saveModalOverlay) {
    // Закрытие при клике вне области модального окна
    saveModalOverlay.addEventListener('click', (e) => {
      if (!saveModalDialog.contains(e.target)) {
        closeSaveModal();
      }
    });
  }

  async function executePdfSave(exportMode) {
    if (!viewerJobId) return;
    closeSaveModal();

    const formattedMods = {};
    for (const [pageNum, blocksObj] of Object.entries(documentModifications)) {
      formattedMods[pageNum] = Object.values(blocksObj);
    }

    const origLabel = savePdfBtn.textContent;
    savePdfBtn.disabled = true;
    savePdfBtn.textContent = exportMode === 'raster' ? 'Запекание скана…' : 'Сохранение…';

    try {
      const resp = await fetch(`/api/document/${viewerJobId}/save`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ 
          modifications: formattedMods,
          export_mode: exportMode
        })
      });

      if (!resp.ok) {
        const errData = await resp.json().catch(() => ({}));
        throw new Error(errData.error || `Ошибка сервера (${resp.status})`);
      }

      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = exportMode === 'raster' ? `scan_document.pdf` : `edited_document.pdf`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      showToast(exportMode === 'raster' ? 'PDF сохранён как скан!' : 'PDF с текстом успешно сохранён!');
    } catch (err) {
      showToast(`Ошибка сохранения: ${err.message}`);
    } finally {
      savePdfBtn.disabled = false;
      savePdfBtn.textContent = origLabel;
    }
  }

  if (savePdfBtn) {
    savePdfBtn.addEventListener('click', () => {
      if (!viewerJobId) return;
      openSaveModal();
    });
  }

  if (btnSaveRaster) {
    btnSaveRaster.addEventListener('click', () => executePdfSave('raster'));
  }

  if (btnSaveSearchable) {
    btnSaveSearchable.addEventListener('click', () => executePdfSave('searchable'));
  }

  // --- Экспорт в Word (.docx), учитывает текущие несохранённые правки ---
  if (exportDocxBtn) {
    exportDocxBtn.addEventListener('click', async () => {
      if (!viewerJobId) return;

      const formattedMods = {};
      for (const [pageNum, blocksObj] of Object.entries(documentModifications)) {
        formattedMods[pageNum] = Object.values(blocksObj);
      }

      const origLabel = exportDocxBtn.textContent;
      exportDocxBtn.disabled = true;
      exportDocxBtn.textContent = 'Экспорт…';

      try {
        const resp = await fetch(`/api/document/${viewerJobId}/export/docx`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ modifications: formattedMods })
        });

        if (!resp.ok) {
          const errData = await resp.json().catch(() => ({}));
          throw new Error(errData.error || `Ошибка сервера (${resp.status})`);
        }

        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `document.docx`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
        showToast('Документ Word готов!');
      } catch (err) {
        showToast(`Ошибка экспорта: ${err.message}`);
      } finally {
        exportDocxBtn.disabled = false;
        exportDocxBtn.textContent = origLabel;
      }
    });
  }

  function requestNewFile() {
    if (!confirm('Закрыть текущий файл и открыть новый?')) return;

    // Немедленно освобождаем память и удаляем временный PDF на сервере
    if (viewerJobId) {
      try {
        fetch(`/api/document/${viewerJobId}`, { method: 'DELETE' });
      } catch (e) {
        console.warn('Не удалось уведомить сервер об удалении задачи:', e);
      }
    }

    fileInput.value = '';
    viewerJobId = null;
    pdfjsDoc = null;
    resetDocumentState();
    showScreen('upload');
  }

  retryBtn.addEventListener('click', () => {
    if (viewerJobId) {
      try {
        fetch(`/api/document/${viewerJobId}`, { method: 'DELETE' });
      } catch (e) {
        console.warn('Не удалось уведомить сервер об удалении задачи:', e);
      }
    }

    fileInput.value = '';
    viewerJobId = null;
    pdfjsDoc = null;
    resetDocumentState();
    showScreen('upload');
  });

  if (newFileBtn) newFileBtn.addEventListener('click', requestNewFile);

  function findInheritedStyle(x_pt, y_pt) {
    let nearest = null;
    let bestDist = Infinity;
    for (const block of currentPageBlocks) {
      if (block.type === 'picture' || !block.bbox) continue;
      const cb = block.bbox;
      if (cb.x2 <= cb.x1 || cb.y2 <= cb.y1) continue;
      const cx = (cb.x1 + cb.x2) / 2;
      const cy = (cb.y1 + cb.y2) / 2;
      const dist = Math.hypot(x_pt - cx, y_pt - cy) || 0;
      if (dist < bestDist) {
        bestDist = dist;
        nearest = block;
      }
    }
    const base = nearest || currentPageBlocks[0] || null;
    const fontMeta = (base && base.font) || {};
    const spanFont = (() => {
      const l0 = base && base.lines && base.lines[0];
      return (l0 && l0.spans && l0.spans[0]) || null;
    })();
    return {
      family: spanFont && spanFont.font_family || fontMeta.family || null,
      size_pt: spanFont && spanFont.size_pt || fontMeta.size_pt || null,
      weight: fontMeta.weight,
      italic: spanFont && spanFont.is_italic !== undefined ? Boolean(spanFont.is_italic)
        : Boolean(fontMeta.italic),
      bold: Boolean((fontMeta.weight || 400) >= 600),
      scale_x: spanFont && spanFont.scale_x || fontMeta.scale_x || 1.0,
      baseline_offset: spanFont && spanFont.baseline_offset || fontMeta.baseline_offset || 0.0,
    };
  }

  // --- Создание нового текста по клику на холст ---
  overlayLayer.addEventListener('click', (e) => {
    if (currentTool !== 'addText') return;

    if (e.target.closest('.ocr-text-box')) return;

    const rect = overlayLayer.getBoundingClientRect();
    const clickX_px = e.clientX - rect.left;
    const clickY_px = e.clientY - rect.top;

    const x0_pt = (clickX_px / rect.width) * viewerPageWidthPt;
    const y0_pt = (clickY_px / rect.height) * viewerPageHeightPt;

    const inheritStyle = findInheritedStyle(x0_pt, y0_pt);
    const effFamily = inheritStyle.family || currentFontFamily || 'Times New Roman';
    const effSize = parseFloat(Number(inheritStyle.size_pt || currentFontSizePt || 10.0).toFixed(1));
    const effBold = inheritStyle.bold !== undefined ? inheritStyle.bold : Boolean(isBold);
    const effItalic = inheritStyle.italic !== undefined ? inheritStyle.italic : Boolean(isItalic);

    currentFontFamily = effFamily;
    currentFontSizePt = effSize;
    isBold = effBold;
    isItalic = effItalic;

    const boxHeightPt = effSize * 1.2;
    const boxWidthPt = 120.0;

    const newBlockId = `custom_${Date.now()}`;
    const customBlock = {
      id: newBlockId,
      text: '',
      bbox: [x0_pt, y0_pt, x0_pt + boxWidthPt, y0_pt + boxHeightPt],
      fontSize: effSize,
      fontFamily: effFamily,
      isBold: effBold,
      isItalic: effItalic,
      isUnderline: isUnderline,
      scaleX: inheritStyle.scale_x || 1.0,
      baselineOffset: inheritStyle.baseline_offset || 0.0,
      isNew: true
    };

    const newBoxEl = document.createElement('div');
    newBoxEl.className = 'ocr-text-box is-new';
    newBoxEl.dataset.id = newBlockId;
    newBoxEl.spellcheck = false;
    newBoxEl.setAttribute('spellcheck', 'false');
    newBoxEl.setAttribute('autocorrect', 'off');
    newBoxEl.setAttribute('autocapitalize', 'off');
    newBoxEl.setAttribute('data-gramm', 'false');
    newBoxEl.style.left = `${(x0_pt / viewerPageWidthPt) * 100}%`;
    newBoxEl.style.top = `${(y0_pt / viewerPageHeightPt) * 100}%`;
    newBoxEl.style.minWidth = '80px';
    newBoxEl.style.height = `${(boxHeightPt / viewerPageHeightPt) * 100}%`;

    newBoxEl.style.fontFamily = `"${effFamily}", Arial, sans-serif`;
    newBoxEl.style.fontSize = `${effSize}pt`;
    newBoxEl.style.fontWeight = effBold ? 700 : 400;
    newBoxEl.style.fontStyle = effItalic ? 'italic' : 'normal';
    newBoxEl.style.textDecoration = isUnderline ? 'underline' : 'none';
    newBoxEl.style.lineHeight = `${boxHeightPt}pt`;

    overlayLayer.appendChild(newBoxEl);
    makeDraggableAndEditable(newBoxEl, customBlock);

    // Переключаем тулбар в режим edit без сброса DOM-дерева
    currentTool = 'edit';
    toolSelectBtn.classList.remove('active-tool');
    toolEditBtn.classList.add('active-tool');
    if (toolAddTextBtn) toolAddTextBtn.classList.remove('active-tool');
    overlayLayer.classList.remove('editor-cursor-crosshair');

    setTimeout(() => {
      enableInlineEdit(newBoxEl, customBlock);
    }, 10);
  });

  // --- Управление масштабом (Зум) ---
  async function applyZoom(delta) {
    if (!pdfjsDoc) return;
    const newScale = Math.max(0.66, Math.min(3.5, viewerZoomScale + delta));
    if (newScale === viewerZoomScale) return;

    viewerZoomScale = Math.round(newScale * 100) / 100;
    if (zoomValue) {
      zoomValue.textContent = `${Math.round((viewerZoomScale / 1.33) * 100)}%`;
    }

    const pageIndex = selectedPipeline === 'ocr' ? 1 : viewerCurrentPage;
    const pageObj = await pdfjsDoc.getPage(pageIndex);
    await renderPdfPage(pageObj);
    await renderPageOverlay();
  }

  if (zoomInBtn) zoomInBtn.addEventListener('click', () => applyZoom(0.2));
  if (zoomOutBtn) zoomOutBtn.addEventListener('click', () => applyZoom(-0.2));

  // --- Рисование прямых линий (как в Paint) ---
  let isDrawingLine = false;
  let lineStartPt = null;
  let activeSvgLine = null;

  overlayLayer.addEventListener('mousedown', (e) => {
    // Рисовать можно ТОЛЬКО если строго выбран инструмент line и клик не по текстовому боксу
    if (currentTool !== 'line' || e.target.closest('.ocr-text-box')) {
      isDrawingLine = false;
      return;
    }
    isDrawingLine = true;

    const rect = overlayLayer.getBoundingClientRect();
    const x0_pt = ((e.clientX - rect.left) / rect.width) * viewerPageWidthPt;
    const y0_pt = ((e.clientY - rect.top) / rect.height) * viewerPageHeightPt;
    lineStartPt = [x0_pt, y0_pt];

    const svgLayer = canvasStage.querySelector('.editor-svg-layer');
    if (svgLayer) {
      let strokeColor = lineColorInput ? lineColorInput.value : '#000000';
      // Защита от системного красного
      if (['#ff0000', '#f00', '#ef4444', '#e11d48'].includes(strokeColor.toLowerCase())) {
        strokeColor = '#000000';
      }
      activeSvgLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      activeSvgLine.setAttribute('x1', x0_pt);
      activeSvgLine.setAttribute('y1', y0_pt);
      activeSvgLine.setAttribute('x2', x0_pt);
      activeSvgLine.setAttribute('y2', y0_pt);
      activeSvgLine.setAttribute('stroke', strokeColor);
      activeSvgLine.setAttribute('stroke-width', lineWidthSelect ? lineWidthSelect.value : '1');
      activeSvgLine.setAttribute('stroke-linecap', 'square');
      svgLayer.appendChild(activeSvgLine);
    }
  });

  window.addEventListener('mousemove', (e) => {
    if (!isDrawingLine || !activeSvgLine || !lineStartPt) return;

    const rect = overlayLayer.getBoundingClientRect();
    let curX_pt = ((e.clientX - rect.left) / rect.width) * viewerPageWidthPt;
    let curY_pt = ((e.clientY - rect.top) / rect.height) * viewerPageHeightPt;

    // Если зажат Shift — привязываем линию строго горизонтально или вертикально
    if (e.shiftKey) {
      const dx = Math.abs(curX_pt - lineStartPt[0]);
      const dy = Math.abs(curY_pt - lineStartPt[1]);
      if (dx > dy) {
        curY_pt = lineStartPt[1]; // Строго горизонтально
      } else {
        curX_pt = lineStartPt[0]; // Строго вертикально
      }
    }

    activeSvgLine.setAttribute('x2', curX_pt);
    activeSvgLine.setAttribute('y2', curY_pt);
  });

  window.addEventListener('mouseup', (e) => {
    if (!isDrawingLine || !activeSvgLine || !lineStartPt) return;
    isDrawingLine = false;

    const x2 = parseFloat(activeSvgLine.getAttribute('x2'));
    const y2 = parseFloat(activeSvgLine.getAttribute('y2'));

    // Минимальная длина линии 15 pt (отсекает случайные клики и дрожание руки)
    if (currentTool === 'line' && Math.hypot(x2 - lineStartPt[0], y2 - lineStartPt[1]) > 15) {
      if (!documentModifications[viewerCurrentPage]) {
        documentModifications[viewerCurrentPage] = {};
      }
      let chosenColor = lineColorInput ? lineColorInput.value : '#000000';
      if (['#ff0000', '#f00', '#ef4444', '#e11d48'].includes(chosenColor.toLowerCase())) {
        chosenColor = '#000000';
      }
      const lineId = `line_${Date.now()}`;
      documentModifications[viewerCurrentPage][lineId] = {
        type: 'line',
        p1: [lineStartPt[0], lineStartPt[1]],
        p2: [x2, y2],
        color: chosenColor,
        width: parseFloat(lineWidthSelect ? lineWidthSelect.value : '1'),
        isUserCreated: true
      };
      pushHistoryState();
    } else {
      activeSvgLine.remove();
    }
    activeSvgLine = null;
    lineStartPt = null;
    renderPageOverlay();
  });

  // Слушатели кнопок Undo / Redo и сочетаний клавиш
  if (undoBtn) undoBtn.addEventListener('click', handleUndo);
  if (redoBtn) redoBtn.addEventListener('click', handleRedo);

  window.addEventListener('keydown', (e) => {
    if (currentlyEditingBoxEl) return;

    if ((e.ctrlKey || e.metaKey) && !e.altKey) {
      if (e.key.toLowerCase() === 'z') {
        e.preventDefault();
        if (e.shiftKey) {
          handleRedo();
        } else {
          handleUndo();
        }
      } else if (e.key.toLowerCase() === 'y') {
        e.preventDefault();
        handleRedo();
      }
    }
  });
})();