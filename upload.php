<?php
header('Content-Type: application/json');

$MAX_FILES = 10;
$UPLOAD_ROOT = __DIR__ . '/jobs';

function respond($success, $data = []) {
    echo json_encode(array_merge(['success' => $success], $data));
    exit;
}

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    respond(false, ['message' => 'Некорректный метод запроса.']);
}

if (empty($_FILES['pdf_files']) || empty($_FILES['pdf_files']['name'][0])) {
    respond(false, ['message' => 'Файлы не переданы.']);
}

$fileCount = count($_FILES['pdf_files']['name']);
if ($fileCount > $MAX_FILES) {
    respond(false, ['message' => "Превышен лимит файлов ($MAX_FILES)."]);
}

$resultCount = isset($_POST['result_count']) ? (int)$_POST['result_count'] : 1;
if ($resultCount < 1) {
    respond(false, ['message' => 'Некорректное количество результирующих файлов.']);
}

// Генерируем уникальную сессию обработки
$sessionId = bin2hex(random_bytes(12));
$jobDir = "$UPLOAD_ROOT/$sessionId";
$pdfDir = "$jobDir/pdf";
$pngDir = "$jobDir/png";
$csvDir = "$jobDir/csv";

foreach ([$jobDir, $pdfDir, $pngDir, $csvDir] as $dir) {
    if (!mkdir($dir, 0775, true) && !is_dir($dir)) {
        respond(false, ['message' => 'Ошибка создания рабочей директории.']);
    }
}

// Сохраняем PDF файлы с проверкой MIME
$savedFiles = [];
for ($i = 0; $i < $fileCount; $i++) {
    if ($_FILES['pdf_files']['error'][$i] !== UPLOAD_ERR_OK) {
        continue;
    }

    $tmpPath = $_FILES['pdf_files']['tmp_name'][$i];
    $originalName = basename($_FILES['pdf_files']['name'][$i]);

    $finfo = finfo_open(FILEINFO_MIME_TYPE);
    $mime = finfo_file($finfo, $tmpPath);
    finfo_close($finfo);

    if ($mime !== 'application/pdf') {
        continue; // пропускаем не-PDF
    }

    $safeName = preg_replace('/[^a-zA-Z0-9._-]/', '_', $originalName);
    $destPath = "$pdfDir/$safeName";

    if (move_uploaded_file($tmpPath, $destPath)) {
        $savedFiles[] = $safeName;
    }
}

if (empty($savedFiles)) {
    respond(false, ['message' => 'Не удалось сохранить ни одного корректного PDF файла.']);
}

// Инициализируем файл статуса
$statusFile = "$jobDir/status.json";
$initialStatus = [
    'stage' => 1,
    'status' => 'processing',
    'percent' => 0,
    'current_file' => null,
    'processed_pages' => 0,
    'total_pages' => null,
    'log' => ['Инициализация обработки...', count($savedFiles) . ' файл(ов) принято.'],
    'result_files' => [],
    'error' => null
];
file_put_contents($statusFile, json_encode($initialStatus));

// Запускаем Python-агентов асинхронно (пример команды)
// Агент сам обновляет status.json на каждом шаге, включая переключение stage=2
$pdfListArg = escapeshellarg(implode(',', $savedFiles));
$cmd = sprintf(
    'nohup python3 %s --job_dir=%s --pdf_dir=%s --png_dir=%s --csv_dir=%s --status_file=%s --result_count=%d --files=%s > %s 2>&1 &',
    escapeshellarg(__DIR__ . '/agents/run_pipeline.py'),
    escapeshellarg($jobDir),
    escapeshellarg($pdfDir),
    escapeshellarg($pngDir),
    escapeshellarg($csvDir),
    escapeshellarg($statusFile),
    $resultCount,
    $pdfListArg,
    escapeshellarg("$jobDir/pipeline.log")
);
exec($cmd);

respond(true, ['session_id' => $sessionId]);
?>