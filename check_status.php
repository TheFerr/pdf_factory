<?php
header('Content-Type: application/json');

$sessionId = $_GET['id'] ?? '';

// Защита от directory traversal
if (!preg_match('/^[a-f0-9]{24}$/', $sessionId)) {
    http_response_code(400);
    echo json_encode(['error' => 'Некорректный идентификатор сессии.']);
    exit;
}

$statusFile = __DIR__ . "/jobs/$sessionId/status.json";

if (!file_exists($statusFile)) {
    echo json_encode(['error' => 'Сессия не найдена или ещё не инициализирована.']);
    exit;
}

$content = file_get_contents($statusFile);
$data = json_decode($content, true);

if ($data === null) {
    // Файл может обновляться в момент чтения — отдаём "нейтральный" статус
    echo json_encode(['stage' => 1, 'status' => 'processing', 'percent' => 0, 'log' => []]);
    exit;
}

// Если есть готовые результирующие файлы — формируем ссылки для скачивания
if (!empty($data['result_files'])) {
    $data['result_files'] = array_map(function ($filename) use ($sessionId) {
        return [
            'name' => $filename,
            'url' => "download.php?id=$sessionId&file=" . urlencode($filename)
        ];
    }, $data['result_files']);
}

echo json_encode($data);
?>