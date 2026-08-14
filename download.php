<?php
$sessionId = $_GET['id'] ?? '';
$file = $_GET['file'] ?? '';

if (!preg_match('/^[a-f0-9]{24}$/', $sessionId) || !preg_match('/^[a-zA-Z0-9._-]+\.csv$/', $file)) {
    http_response_code(400);
    exit('Некорректный запрос.');
}

$path = __DIR__ . "/jobs/$sessionId/csv/$file";

if (!file_exists($path)) {
    http_response_code(404);
    exit('Файл не найден.');
}

header('Content-Type: text/csv');
header('Content-Disposition: attachment; filename="' . $file . '"');
header('Content-Length: ' . filesize($path));
readfile($path);
exit;
?>