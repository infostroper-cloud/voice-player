#!/usr/bin/env node
// speak-feed.js — озвучка ПРОМЕЖУТОЧНЫХ ответов Claude Code (текст перед вызовами инструментов).
//
// Зачем: голосовой хук speak-response.ps1 висит на событии Stop и читает только ПОСЛЕДНИЙ
// (финальный) текст ответа. Промежуточный текст — отдельные assistant-строки транскрипта,
// которые идут перед блоками tool_use, — никогда не озвучивался. Этот хук вешается на
// PostToolUse: после каждого инструмента он находит новые текстовые блоки ассистента
// (по закладке uuid, чтобы не повторять) и шлёт их в voice-player на 127.0.0.1:3737.
//
// Финальный ответ (последняя текстовая строка, после которой инструментов уже нет) этот хук
// НЕ трогает — его по-прежнему озвучивает speak-response.ps1 на Stop. Пересечения нет.
//
// Лёгкий и быстрый (Node, без сети-через-PowerShell). Никогда не падает, всегда exit 0.

const fs = require('fs');
const path = require('path');
const http = require('http');

const STATE_DIR = path.join(process.env.USERPROFILE || process.env.HOME || '.', '.claude', 'hooks', '.speak-state');

function cleanForSpeech(t) {
  if (!t) return '';
  t = t.replace(/```[\s\S]*?```/g, ' ');      // блоки кода
  t = t.replace(/`[^`]*`/g, ' ');              // инлайн-код
  t = t.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1'); // ссылки → текст
  t = t.replace(/[*#>`_]/g, '');               // markdown-символы
  t = t.replace(/\s+/g, ' ').trim();
  return t;
}

function readStdin() {
  try { return fs.readFileSync(0, 'utf8'); } catch (_) { return ''; }
}

function postToPlayer(payload) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (ok) => { if (!done) { done = true; resolve(ok); } };
    try {
      const body = Buffer.from(JSON.stringify(payload), 'utf8');
      const req = http.request({
        host: '127.0.0.1', port: 3737, path: '/api/enqueue', method: 'POST',
        headers: { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': body.length },
        timeout: 1500,
      }, (res) => {
        res.resume();
        finish(res.statusCode >= 200 && res.statusCode < 300);
      });
      req.on('error', () => finish(false));
      req.on('timeout', () => { try { req.destroy(); } catch (_) {} finish(false); });
      req.write(body); req.end();
    } catch (_) { finish(false); }
  });
}

(async () => {
  try {
    const raw = readStdin();
    if (!raw) return;
    let data; try { data = JSON.parse(raw); } catch (_) { return; }
    const tp = data.transcript_path;
    if (!tp || !fs.existsSync(tp)) return;

    const session8 = path.basename(tp).replace(/\.[^.]+$/, '').slice(0, 8);

    // собрать assistant-строки с непустым текстом: [{uuid, text}]
    const entries = [];
    const lines = fs.readFileSync(tp, 'utf8').split('\n');
    for (const line of lines) {
      const s = line.trim();
      if (!s) continue;
      let o; try { o = JSON.parse(s); } catch (_) { continue; }
      if (o.type !== 'assistant') continue;
      const content = o.message && o.message.content;
      if (!Array.isArray(content)) continue;
      const parts = [];
      for (const b of content) if (b && b.type === 'text' && b.text) parts.push(b.text);
      if (!parts.length) continue;
      const text = cleanForSpeech(parts.join('\n'));
      if (text) entries.push({ uuid: o.uuid || '', text });
    }
    if (!entries.length) return;

    // закладка: uuid последней уже отправленной текстовой строки
    fs.mkdirSync(STATE_DIR, { recursive: true });
    const stateFile = path.join(STATE_DIR, session8 + '.json');
    let lastUuid = null;
    try { lastUuid = JSON.parse(fs.readFileSync(stateFile, 'utf8')).last_uuid || null; } catch (_) {}

    // режим: --final-only (или env SPEAK_FINAL_ONLY=1) — читать ТОЛЬКО итоговый ответ,
    // без промежуточных шагов. Нужен на macOS, где этот скрипт — единственный голосовой хук.
    const finalOnly = process.argv.includes('--final-only') || process.env.SPEAK_FINAL_ONLY === '1';

    // новые = всё после закладки; если закладки нет — берём только последнюю (без бэкфилла истории)
    let newOnes;
    if (finalOnly) {
      newOnes = [entries[entries.length - 1]];     // только последний (итоговый) текстовый блок
    } else if (lastUuid) {
      const idx = entries.findIndex((e) => e.uuid === lastUuid);
      newOnes = idx >= 0 ? entries.slice(idx + 1) : [entries[entries.length - 1]];
    } else {
      newOnes = [entries[entries.length - 1]];
    }
    if (!newOnes.length) return;

    let anyReached = false;
    for (const e of newOnes) {
      const ok = await postToPlayer({
        source: 'claude-code',
        session: session8,
        author: 'Claude/' + session8,
        text: e.text,
      });
      if (ok) anyReached = true;
    }
    // двигаем закладку всегда (промежуточная речь эфемерна: если плеер был выключен — пропускаем)
    try {
      fs.writeFileSync(stateFile, JSON.stringify({ last_uuid: newOnes[newOnes.length - 1].uuid }), 'utf8');
    } catch (_) {}

    // macOS-фолбэк: если плеер не запущен и это финал хода — прочитать вслух родным голосом `say`.
    // (на Windows фолбэк делает speak-response.ps1, поэтому тут только не-Windows.)
    const event = data.hook_event_name || '';
    if (!anyReached && event === 'Stop' && process.platform === 'darwin') {
      try {
        const { spawn } = require('child_process');
        const text = newOnes[newOnes.length - 1].text.slice(0, 4000);
        spawn('say', ['-r', '210', text], { detached: true, stdio: 'ignore' }).unref();
      } catch (_) {}
    }
  } catch (_) {
    // тихо
  } finally {
    process.exit(0);
  }
})();
