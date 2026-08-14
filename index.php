<?php
session_start();
?>
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PDF Batch Processor</title>
<link rel="stylesheet" href="style.css">
</head>
<body>

<div class="app-container">

    <header class="app-header">
        <h1>PDF Code Extractor</h1>
        <p class="subtitle">Пакетная обработка PDF → распознавание кодов → экспорт в CSV</p>
    </header>

    <main class="main-grid">

        <!-- Форма загрузки -->
        <section class="card upload-card">
            <h2><span class="step-badge">1</span> Загрузка файлов</h2>

            <form id="uploadForm" enctype="multipart/form-data">
                <div class="dropzone" id="dropzone">
                    <input type="file" id="pdfFiles" name="pdf_files[]" accept="application/pdf" multiple>
                    <div class="dropzone-content">
                        <svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                            <path d="M12 16V4M12 4L7 9M12 4l5 5"/>
                            <path d="M4 16v3a2 2 0 002 2h12a2 2 0 002-2v-3"/>
                        </svg>
                        <p><strong>Перетащите PDF файлы</strong> или нажмите для выбора</p>
                        <span class="hint">Максимум 10 файлов, только .pdf</span>
                    </div>
                </div>

                <ul id="fileList" class="file-list"></ul>

                <div class="form-row">
                    <label for="resultCount">Количество результирующих CSV файлов:</label>
                    <input type="number" id="resultCount" name="result_count" min="1" max="20" value="1" required>
                </div>

                <button type="submit" id="submitBtn" class="btn-primary">
                    <span class="btn-text">Начать обработку</span>
                    <span class="btn-loader" hidden></span>
                </button>
                <p id="formError" class="error-msg" hidden></p>
            </form>
        </section>

        <!-- Этап 1: конвертация -->
        <section class="card stage-card" id="stage1Card">
            <h2><span class="step-badge">2</span> Конвертация PDF → PNG</h2>
            <div class="progress-wrapper">
                <div class="progress-bar-track">
                    <div class="progress-bar-fill" id="progress1"></div>
                </div>
                <span class="progress-percent" id="progress1Percent">0%</span>
            </div>
            <p class="progress-detail" id="detail1">Ожидание запуска...</p>
            <div class="log-box" id="log1"></div>
        </section>

        <!-- Этап 2: распознавание -->
        <section class="card stage-card" id="stage2Card">
            <h2><span class="step-badge">3</span> Распознавание кодов</h2>
            <div class="progress-wrapper">
                <div class="progress-bar-track">
                    <div class="progress-bar-fill" id="progress2"></div>
                </div>
                <span class="progress-percent" id="progress2Percent">0%</span>
            </div>
            <p class="progress-detail" id="detail2">Ожидание завершения первого этапа...</p>
            <div class="log-box" id="log2"></div>
        </section>

        <!-- Результаты -->
        <section class="card result-card" id="resultCard" hidden>
            <h2><span class="step-badge done">✓</span> Результаты</h2>
            <p class="result-intro">Готовые файлы для скачивания:</p>
            <ul class="download-list" id="downloadList"></ul>
        </section>

    </main>

    <footer class="app-footer">
        <span>PDF Code Extractor &copy; <?= date('Y') ?></span>
    </footer>
</div>

<script src="script.js"></script>
</body>
</html>