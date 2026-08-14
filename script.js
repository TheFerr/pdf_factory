document.addEventListener('DOMContentLoaded', () => {

    const MAX_FILES = 10;
    const dropzone = document.getElementById('dropzone');
    const fileInput = document.getElementById('pdfFiles');
    const fileListEl = document.getElementById('fileList');
    const uploadForm = document.getElementById('uploadForm');
    const submitBtn = document.getElementById('submitBtn');
    const formError = document.getElementById('formError');
    const resultCountInput = document.getElementById('resultCount');

    const stage1Card = document.getElementById('stage1Card');
    const stage2Card = document.getElementById('stage2Card');
    const resultCard = document.getElementById('resultCard');
    const downloadList = document.getElementById('downloadList');

    const progress1 = document.getElementById('progress1');
    const progress1Percent = document.getElementById('progress1Percent');
    const detail1 = document.getElementById('detail1');
    const log1 = document.getElementById('log1');

    const progress2 = document.getElementById('progress2');
    const progress2Percent = document.getElementById('progress2Percent');
    const detail2 = document.getElementById('detail2');
    const log2 = document.getElementById('log2');

    let selectedFiles = [];
    let isProcessing = false;
    let pollTimer = null;
    let currentSessionId = null;

    // Логи, которые уже отрисованы, чтобы не дублировать
    let renderedLogCount = { 1: 0, 2: 0 };

    // ---------- Работа с файлами (drag&drop, выбор) ----------

    dropzone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropzone.classList.add('dragover');
    });

    dropzone.addEventListener('dragleave', () => {
        dropzone.classList.remove('dragover');
    });

    dropzone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropzone.classList.remove('dragover');
        handleFiles(e.dataTransfer.files);
    });

    fileInput.addEventListener('change', () => {
        handleFiles(fileInput.files);
    });

    function handleFiles(fileListRaw) {
        const incoming = Array.from(fileListRaw).filter(f => f.type === 'application/pdf');

        for (const file of incoming) {
            if (selectedFiles.length >= MAX_FILES) {
                showError(`Достигнут лимит: не более ${MAX_FILES} файлов.`);
                break;
            }
            // Избегаем дублей по имени+размеру
            const exists = selectedFiles.some(f => f.name === file.name && f.size === file.size);
            if (!exists) {
                selectedFiles.push(file);
            }
        }
        renderFileList();
        clearError();
    }

    function renderFileList() {
        fileListEl.innerHTML = '';
        selectedFiles.forEach((file, idx) => {
            const li = document.createElement('li');
            li.innerHTML = `
                <span>${escapeHtml(file.name)}</span>
                <span class="file-size">${formatSize(file.size)}</span>
                <span class="remove-file" data-idx="${idx}">✕</span>
            `;
            fileListEl.appendChild(li);
        });

        fileListEl.querySelectorAll('.remove-file').forEach(el => {
            el.addEventListener('click', () => {
                const idx = parseInt(el.dataset.idx, 10);
                selectedFiles.splice(idx, 1);
                renderFileList();
            });
        });
    }

    function formatSize(bytes) {
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
        return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    }

    function escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    function showError(msg) {
        formError.textContent = msg;
        formError.hidden = false;
    }

    function clearError() {
        formError.hidden = true;
        formError.textContent = '';
    }

    // ---------- Отправка формы ----------

    uploadForm.addEventListener('submit', (e) => {
        e.preventDefault();

        if (isProcessing) {
            showError('Дождитесь завершения текущей обработки.');
            return;
        }

        if (selectedFiles.length === 0) {
            showError('Выберите хотя бы один PDF файл.');
            return;
        }

        if (selectedFiles.length > MAX_FILES) {
            showError(`Не более ${MAX_FILES} файлов за раз.`);
            return;
        }

        const resultCount = parseInt(resultCountInput.value, 10);
        if (!resultCount || resultCount < 1) {
            showError('Укажите корректное количество результирующих файлов.');
            return;
        }

        startProcessing(resultCount);
    });

    function lockForm(lock) {
        isProcessing = lock;
        submitBtn.disabled = lock;
        fileInput.disabled = lock;
        resultCountInput.disabled = lock;
        dropzone.style.pointerEvents = lock ? 'none' : 'auto';

        submitBtn.querySelector('.btn-text').textContent = lock ? 'Обработка...' : 'Начать обработку';
        submitBtn.querySelector('.btn-loader').hidden = !lock;
    }

    function resetStages() {
        [progress1, progress2].forEach(p => {
            p.style.width = '0%';
            p.classList.remove('complete');
        });
        progress1Percent.textContent = '0%';
        progress2Percent.textContent = '0%';
        detail1.textContent = 'Ожидание запуска...';
        detail2.textContent = 'Ожидание завершения первого этапа...';
        log1.innerHTML = '';
        log2.innerHTML = '';
        renderedLogCount = { 1: 0, 2: 0 };
        stage1Card.classList.remove('active');
        stage2Card.classList.remove('active');
        resultCard.hidden = true;
        downloadList.innerHTML = '';
    }

    function startProcessing(resultCount) {
        clearError();
        resetStages();
        lockForm(true);
        stage1Card.classList.add('active');

        const formData = new FormData();
        selectedFiles.forEach(file => formData.append('pdf_files[]', file));
        formData.append('result_count', resultCount);

        fetch('upload.php', {
            method: 'POST',
            body: formData
        })
        .then(resp => resp.json())
        .then(data => {
            if (!data.success) {
                throw new Error(data.message || 'Ошибка загрузки файлов.');
            }
            currentSessionId = data.session_id;
            startPolling(currentSessionId);
        })
        .catch(err => {
            showError(err.message);
            lockForm(false);
        });
    }

    // ---------- Polling статуса обработки ----------

    function startPolling(sessionId) {
        pollTimer = setInterval(() => {
            fetch(`check_status.php?id=${encodeURIComponent(sessionId)}&_=${Date.now()}`)
                .then(resp => resp.json())
                .then(handleStatusUpdate)
                .catch(() => {
                    // Сетевая ошибка — пропускаем цикл, попробуем снова
                });
        }, 1200);
    }

    function stopPolling() {
        if (pollTimer) {
            clearInterval(pollTimer);
            pollTimer = null;
        }
    }

    function handleStatusUpdate(data) {
        // data: { stage, status, percent, current_file, processed_pages, total_pages, log, result_files, error }

        if (data.error) {
            showError('Ошибка обработки: ' + data.error);
            stopPolling();
            lockForm(false);
            return;
        }

        if (data.stage === 1) {
            updateStageUI(1, progress1, progress1Percent, detail1, log1, data);
            if (data.status === 'done') {
                stage2Card.classList.add('active');
            }
        }

        if (data.stage === 2) {
            stage2Card.classList.add('active');
            updateStageUI(2, progress2, progress2Percent, detail2, log2, data);

            if (data.status === 'done') {
                stopPolling();
                renderResults(data.result_files);
                lockForm(false);
            }
        }
    }

    function updateStageUI(stageNum, barEl, percentEl, detailEl, logEl, data) {
        const percent = Math.min(100, Math.max(0, data.percent || 0));
        barEl.style.width = percent + '%';
        percentEl.textContent = percent + '%';

        if (data.status === 'done') {
            barEl.classList.add('complete');
            detailEl.textContent = `Завершено (${data.total_pages || ''} стр.)`;
        } else {
            const fileInfo = data.current_file ? `Файл: ${data.current_file}` : '';
            const pageInfo = (data.processed_pages != null && data.total_pages != null)
                ? ` — страница ${data.processed_pages} из ${data.total_pages}`
                : '';
            detailEl.textContent = `${fileInfo}${pageInfo}`;
        }

        appendLogs(logEl, data.log || [], stageNum);
    }

    function appendLogs(logEl, logArray, stageNum) {
        const already = renderedLogCount[stageNum];
        const newLines = logArray.slice(already);

        newLines.forEach(line => {
            const div = document.createElement('div');
            div.className = 'log-line';
            div.textContent = '> ' + line;
            logEl.appendChild(div);
        });

        if (newLines.length > 0) {
            renderedLogCount[stageNum] = logArray.length;
            logEl.scrollTop = logEl.scrollHeight;
        }
    }

    function renderResults(files) {
        if (!files || files.length === 0) return;

        downloadList.innerHTML = '';
        files.forEach(file => {
            const li = document.createElement('li');
            li.innerHTML = `
                <a href="${file.url}" download="${file.name}">
                    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M12 4v12m0 0l-4-4m4 4l4-4"/>
                        <path d="M4 20h16"/>
                    </svg>
                    ${escapeHtml(file.name)}
                </a>
            `;
            downloadList.appendChild(li);
        });

        resultCard.hidden = false;
        resultCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
});