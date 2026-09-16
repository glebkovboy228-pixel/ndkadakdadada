/* ========================================================
   JARVIS // FRIDAY OS — FRONTEND CONTROLLER
   Bridge between pywebview backend (jarvis.py) and UI
   ======================================================== */

(function () {
    'use strict';

    /* ===== WINDOW CONTROLS (frameless) ===== */
    var isFs = false;

    document.getElementById('tb-min')?.addEventListener('click', function () {
        if (state.apiReady) window.pywebview.api.minimize_window();
    });

    document.getElementById('tb-max')?.addEventListener('click', function () {
        if (state.apiReady) {
            isFs = !isFs;
            window.pywebview.api.toggle_fullscreen();
            document.body.classList.toggle('fs', isFs);
            var btn = document.getElementById('tb-max');
            if (btn) btn.innerHTML = isFs ? '&#9635;' : '&#9633;';
        }
    });

    document.getElementById('tb-close')?.addEventListener('click', function () {
        if (state.apiReady) window.pywebview.api.close_window();
    });

    // F11 = fullscreen toggle
    document.addEventListener('keydown', function (e) {
        if (e.key === 'F11') {
            e.preventDefault();
            document.getElementById('tb-max')?.click();
        }
    });

    // Auto-enter fullscreen on launch (optional)
    // setTimeout(function() { document.getElementById('tb-max')?.click(); }, 1000);

    var state = {
        listening: false, speaking: false, thinking: false, muted: false,
        exchanges: 0, sessionStart: Date.now(), mood: 'neutral',
        apiReady: false, lastLogId: 0,
    };

    /* ===== ELEMENTS ===== */
    var $ = function (id) { return document.getElementById(id); };

    var els = {
        clock: $('clock'), date: $('date'),
        sysLog: $('sys-log'),
        transcript: $('transcript'),
        cmdInput: $('cmd-input'), sendBtn: $('send-btn'), voiceBtn: $('voice-btn'),
        vitalSession: $('vital-session'), vitalMic: $('vital-mic'),
        vitalSentiment: $('vital-sentiment'), vitalExchanges: $('vital-exchanges'),
        vitalVoice: $('vital-voice'),
        cardCpu: $('card-cpu'), cardMem: $('card-mem'),
        cardNet: $('card-net'), cardReactor: $('card-reactor'),
    };

    /* ===== NAVIGATION ===== */
    var navBtns = document.querySelectorAll('.nav-btn');
    var panels = document.querySelectorAll('.panel');

    navBtns.forEach(function (btn) {
        btn.addEventListener('click', function () {
            var target = btn.getAttribute('data-panel');
            navBtns.forEach(function (b) { b.classList.remove('active'); });
            panels.forEach(function (p) { p.classList.remove('active'); });
            btn.classList.add('active');
            var panel = $('panel-' + target);
            if (panel) panel.classList.add('active');

            // Re-init 3D on first armor view
            if (target === 'armor' && window.Armor3D && !Armor3D.renderer) {
                setTimeout(function () { Armor3D.init(); }, 100);
            }
            // Resize radar when mission opens
            if (target === 'mission' && window.Radar) {
                setTimeout(function () { Radar.init(); }, 100);
            }
        });
    });

    /* ===== CLOCK & SESSION ===== */
    function tickClock() {
        var now = new Date();
        if (els.clock) els.clock.textContent = now.toLocaleTimeString('en-GB', { hour12: false });
        if (els.date) els.date.textContent = now.toLocaleDateString('en-GB', {
            weekday: 'short', day: '2-digit', month: 'short', year: 'numeric'
        }).toUpperCase();
        var s = Math.floor((Date.now() - state.sessionStart) / 1000);
        if (els.vitalSession) {
            els.vitalSession.textContent =
                String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
        }
    }
    setInterval(tickClock, 1000);
    tickClock();

    /* ===== SYSTEM GRAPHS (live canvas) ===== */
    var graphData = {
        cpu: [], mem: [], net: [], reactor: []
    };
    var graphCtx = {};

    function initGraphs() {
        ['cpu', 'mem', 'net', 'reactor'].forEach(function (key) {
            var card = $('card-' + key);
            if (!card) return;
            var canvas = card.querySelector('.sys-graph');
            if (!canvas) return;
            var ctx = canvas.getContext('2d');
            graphCtx[key] = ctx;
            for (var i = 0; i < 40; i++) graphData[key].push(0.3);
        });
    }
    initGraphs();

    function drawGraph(key, value) {
        var ctx = graphCtx[key];
        if (!ctx) return;
        var canvas = ctx.canvas;
        var w = canvas.width, h = canvas.height;
        var data = graphData[key];

        data.push(value);
        if (data.length > 40) data.shift();

        ctx.clearRect(0, 0, w, h);

        // Grid
        ctx.strokeStyle = 'rgba(0,229,255,0.05)';
        ctx.lineWidth = 1;
        for (var i = 0; i <= 4; i++) {
            ctx.beginPath();
            ctx.moveTo(0, (h / 4) * i);
            ctx.lineTo(w, (h / 4) * i);
            ctx.stroke();
        }

        // Fill area
        ctx.beginPath();
        ctx.moveTo(0, h);
        for (var i = 0; i < data.length; i++) {
            var x = (i / (data.length - 1)) * w;
            var y = h - data[i] * h * 0.9;
            if (i === 0) ctx.lineTo(x, y);
            else ctx.lineTo(x, y);
        }
        ctx.lineTo(w, h);
        ctx.closePath();
        var grad = ctx.createLinearGradient(0, 0, 0, h);
        grad.addColorStop(0, 'rgba(0,229,255,0.3)');
        grad.addColorStop(1, 'rgba(0,229,255,0)');
        ctx.fillStyle = grad;
        ctx.fill();

        // Line
        ctx.beginPath();
        for (var i = 0; i < data.length; i++) {
            var x = (i / (data.length - 1)) * w;
            var y = h - data[i] * h * 0.9;
            if (i === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        }
        ctx.strokeStyle = '#00e5ff';
        ctx.lineWidth = 1.5;
        ctx.shadowColor = '#00e5ff';
        ctx.shadowBlur = 6;
        ctx.stroke();
        ctx.shadowBlur = 0;

        // Last point dot
        var lastX = w, lastY = h - data[data.length - 1] * h * 0.9;
        ctx.beginPath();
        ctx.arc(lastX - 2, lastY, 2, 0, Math.PI * 2);
        ctx.fillStyle = '#00e5ff';
        ctx.fill();
    }

    // Fake graphs until backend connects
    function fakeGraphs() {
        drawGraph('cpu', 0.3 + Math.random() * 0.2);
        drawGraph('mem', 0.45 + Math.random() * 0.1);
        drawGraph('net', 0.2 + Math.random() * 0.3);
        drawGraph('reactor', 0.85 + Math.random() * 0.1);

        var cpuEl = els.cardCpu?.querySelector('.sys-value');
        var memEl = els.cardMem?.querySelector('.sys-value');
        var netEl = els.cardNet?.querySelector('.sys-value');
        var reaEl = els.cardReactor?.querySelector('.sys-value');
        if (cpuEl) cpuEl.textContent = Math.round(30 + Math.random() * 20) + '%';
        if (memEl) memEl.textContent = Math.round(45 + Math.random() * 10) + '%';
        if (netEl) netEl.textContent = Math.round(20 + Math.random() * 30) + ' Mbps';
        if (reaEl) reaEl.textContent = Math.round(85 + Math.random() * 10) + '%';
    }
    setInterval(fakeGraphs, 1000);

    /* ===== LOG ===== */
    function escapeHtml(s) {
        var d = document.createElement('div');
        d.textContent = s;
        return d.innerHTML;
    }

    function logLine(who, text, cls) {
        if (!els.sysLog) return;
        var p = document.createElement('div');
        p.className = 'log-entry' + (cls ? ' ' + cls : '');
        var t = new Date().toLocaleTimeString('en-GB', { hour12: false });
        var clsMap = { ok: 'log-ok', warn: 'log-warn', err: 'log-err', sys: '' };
        var whoClass = cls === 'user' ? 'log-warn' : (cls === 'sys' ? '' : '');
        p.innerHTML =
            '<span class="log-time">[' + t + ']</span> ' +
            '<b>' + escapeHtml(who) + '</b> ' + escapeHtml(text);
        els.sysLog.appendChild(p);
        els.sysLog.scrollTop = els.sysLog.scrollHeight;

        // Keep max 50 entries
        while (els.sysLog.children.length > 50) {
            els.sysLog.removeChild(els.sysLog.firstChild);
        }
    }

    /* ===== TRANSCRIPT ===== */
    function setTranscript(text) {
        if (!els.transcript) return;
        var p = document.createElement('p');
        p.className = 'transcript-line ' + (text.startsWith('YOU') ? 'user' : 'ai');
        p.textContent = text;
        els.transcript.appendChild(p);
        els.transcript.scrollTop = els.transcript.scrollHeight;
    }

    /* ===== SENTIMENT ===== */
    var LEX = {
        positive: ['good', 'great', 'awesome', 'love', 'happy', 'thanks', 'excellent', 'nice', 'cool', 'отлично', 'спасибо', 'хорошо', 'класс', 'супер'],
        negative: ['bad', 'sad', 'tired', 'angry', 'upset', 'hate', 'worried', 'stressed', 'плохо', 'грустно', 'устал', 'злюсь'],
        urgent: ['help', 'emergency', 'urgent', 'now', 'broken', 'error', 'помогите', 'срочно', 'ошибка'],
    };

    function analyzeSentiment(text) {
        var t = text.toLowerCase(), pos = 0, neg = 0, urg = 0;
        LEX.positive.forEach(function (w) { if (t.indexOf(w) >= 0) pos++; });
        LEX.negative.forEach(function (w) { if (t.indexOf(w) >= 0) neg++; });
        LEX.urgent.forEach(function (w) { if (t.indexOf(w) >= 0) urg++; });
        if (urg > 0) return 'urgent';
        if (neg > pos) return 'empathetic';
        if (pos > neg) return 'positive';
        return 'neutral';
    }

    function reflectSentiment(sent) {
        var labels = { neutral: 'Neutral', positive: 'Positive', empathetic: 'Concerned', urgent: 'Urgent' };
        if (els.vitalSentiment) els.vitalSentiment.textContent = labels[sent] || 'Neutral';
    }

    /* ===== MOOD COLORS ===== */
    var MOODS = {
        neutral: { accent: '#00e5ff', dim: '#00b8d4' },
        positive: { accent: '#ffd54f', dim: '#ffb300' },
        empathetic: { accent: '#b98fef', dim: '#7c4dff' },
        urgent: { accent: '#ff1744', dim: '#d50000' },
    };

    function setMood(mood) {
        state.mood = mood;
        var m = MOODS[mood] || MOODS.neutral;
        document.documentElement.style.setProperty('--cyan', m.accent);
        document.documentElement.style.setProperty('--cyan-dim', m.dim);
    }

    /* ===== WAVEFORM CONTROL ===== */
    function pulseWaveform() {
        if (window.Waveform) {
            Waveform.setListening(true);
            setTimeout(function () { Waveform.setListening(false); }, 2500);
        }
    }

    /* ===== COMMS — INPUT & VOICE ===== */
    function submitText() {
        var v = els.cmdInput.value;
        if (!v.trim()) return;
        els.cmdInput.value = '';
        if (state.apiReady) {
            window.pywebview.api.send_text(v);
        } else {
            logLine('SYS', 'Backend not connected. Cannot send command.', 'sys');
        }
    }

    if (els.sendBtn) els.sendBtn.addEventListener('click', submitText);
    if (els.cmdInput) els.cmdInput.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') submitText();
    });

    // Quick command chips
    document.querySelectorAll('.cmd-chip').forEach(function (chip) {
        chip.addEventListener('click', function () {
            if (els.cmdInput) {
                els.cmdInput.value = chip.textContent;
                els.cmdInput.focus();
            }
        });
    });

    // Voice button
    if (els.voiceBtn) els.voiceBtn.addEventListener('click', async function () {
        if (!state.apiReady) {
            logLine('SYS', 'Backend not connected.', 'sys');
            return;
        }
        try {
            var listening = await window.pywebview.api.toggle_mic();
            state.listening = listening;
            if (listening) {
                els.voiceBtn.classList.add('listening');
                logLine('SYS', 'Microphone enabled.', 'sys');
            } else {
                els.voiceBtn.classList.remove('listening');
                logLine('SYS', 'Microphone paused.', 'sys');
            }
        } catch (e) { }
    });

    /* ===== PYWEBVIEW BACKEND POLLING ===== */
    function waitForApi(attempts) {
        attempts = attempts || 0;
        if (window.pywebview && window.pywebview.api) {
            state.apiReady = true;
            onApiReady();
        } else if (attempts < 150) {
            setTimeout(function () { waitForApi(attempts + 1); }, 100);
        } else {
            logLine('SYS', 'Backend not connected after 15s. Running in offline mode.', 'sys');
        }
    }

    function onApiReady() {
        logLine('SYS', 'Backend connected. JARVIS online.', 'sys');
        startPolling();
    }

    function startPolling() {
        setInterval(pollLogs, 350);
        setInterval(pollStatus, 400);
        setInterval(pollMicLevel, 200);
        setInterval(pollSystemInfo, 3000);
    }

    async function pollLogs() {
        if (!state.apiReady) return;
        try {
            var logs = await window.pywebview.api.get_logs(state.lastLogId);
            if (logs && logs.length) {
                for (var i = 0; i < logs.length; i++) {
                    state.lastLogId = Math.max(state.lastLogId, logs[i].id + 1);
                    processLogEntry(logs[i]);
                }
            }
        } catch (e) { }
    }

    function processLogEntry(log) {
        var msg = log.msg;

        // 3D model commands
        if (msg === 'MODEL:open') {
            logLine('JARVIS', 'Открываю модель костюма, босс.');
            pulseWaveform();
            return;
        }
        if (msg === 'MODEL:close') {
            logLine('JARVIS', 'Убираю модель, босс.');
            return;
        }

        // Satellite commands
        if (msg.indexOf('SATELLITE:open:') === 0) {
            var city = msg.substring('SATELLITE:open:'.length).trim();
            logLine('JARVIS', 'Открываю спутниковую карту ' + (city || ''));
            pulseWaveform();
            return;
        }
        if (msg.indexOf('SATELLITE:panorama:') === 0) {
            var pcity = msg.substring('SATELLITE:panorama:'.length).trim();
            logLine('JARVIS', 'Открываю панораму улиц ' + (pcity || ''));
            pulseWaveform();
            return;
        }
        if (msg === 'SATELLITE:close') {
            logLine('JARVIS', 'Убираю карту, босс.');
            return;
        }

        // User speech
        if (msg.indexOf('Вы сказали:') === 0 || msg.indexOf('Текстовая команда:') === 0) {
            var text = msg.replace(/^(Вы сказали:|Текстовая команда:)\s*/, '');
            setTranscript('YOU: ' + text);
            logLine('YOU', text, 'user');
            state.exchanges++;
            if (els.vitalExchanges) els.vitalExchanges.textContent = state.exchanges;
            reflectSentiment(analyzeSentiment(text));
            return;
        }

        // JARVIS response
        if (msg.indexOf('Пятница:') === 0) {
            var t2 = msg.replace(/^Пятница:\s*/, '');
            setTranscript('JARVIS: ' + t2);
            logLine('JARVIS', t2);
            if (!state.muted) {
                if (els.vitalVoice) els.vitalVoice.textContent = 'Speaking';
                pulseWaveform();
                setTimeout(function () {
                    if (els.vitalVoice) els.vitalVoice.textContent = state.muted ? 'Muted' : 'Idle';
                }, 2500);
            }
            return;
        }

        // Stop command
        if (msg.indexOf('Получен СТОП') === 0) {
            logLine('SYS', 'Processing interrupted by user.', 'sys');
            state.thinking = false;
            return;
        }

        // Thinking
        if (msg.indexOf('Думаю') === 0) {
            state.thinking = true;
            if (els.vitalVoice) els.vitalVoice.textContent = 'Thinking';
            return;
        }

        // System/LLM/Web search messages
        if (msg.indexOf('Веб-поиск:') === 0 || msg.indexOf('Найден контекст') === 0 ||
            msg.indexOf('[LLM]') === 0 || msg.indexOf('Использую веб-контекст') === 0 ||
            msg.indexOf('LLM') === 0 || msg.indexOf('Распознавание') === 0) {
            logLine('SYS', msg, 'sys');
            return;
        }

        // ASR ready
        if (msg.indexOf('готов') >= 0 || msg.indexOf('GigaAM') >= 0) {
            logLine('SYS', msg, 'sys');
            state.thinking = false;
            if (els.vitalVoice) els.vitalVoice.textContent = 'Idle';
            return;
        }

        // System info prefixes
        if (msg.indexOf('Программ:') === 0 || msg.indexOf('Микрофон:') === 0 ||
            msg.indexOf('ASR:') === 0 || msg.indexOf('TTS:') === 0 ||
            msg.indexOf('Стоп:') === 0 || msg.indexOf('FALLBACK:') === 0 ||
            msg.indexOf('Карты:') === 0 || msg.indexOf('Калибровка:') === 0 ||
            msg.indexOf('ОБУЧЕНИЕ:') === 0 || msg.indexOf('Уже обрабатываю') === 0) {
            logLine('SYS', msg, 'sys');
            return;
        }

        // Action results
        if (msg.indexOf('Готово:') === 0 || msg.indexOf('Открываю:') === 0 ||
            msg.indexOf('Закрываю:') === 0 || msg.indexOf('Система:') === 0 ||
            msg.indexOf('Время:') === 0 || msg.indexOf('Погода:') === 0 ||
            msg.indexOf('Шутка:') === 0 || msg.indexOf('Таймер:') === 0 ||
            msg.indexOf('Скриншот:') === 0 || msg.indexOf('Новости:') === 0 ||
            msg.indexOf('Батарея:') === 0 || msg.indexOf('Перевод:') === 0 ||
            msg.indexOf('Музыка:') === 0 || msg.indexOf('Сайт:') === 0 ||
            msg.indexOf('Поиск:') === 0 || msg.indexOf('Запускаю:') === 0 ||
            msg.indexOf('Ищу:') === 0 || msg.indexOf('Включаю:') === 0 ||
            msg.indexOf('Открываю музыку') === 0 || msg.indexOf('Останавливаю') === 0 ||
            msg.indexOf('Громче') === 0 || msg.indexOf('Тише') === 0 ||
            msg.indexOf('Без звука') === 0 || msg.indexOf('Звук включ') === 0 ||
            msg.indexOf('Блокирую') === 0 || msg.indexOf('Спящий') === 0 ||
            msg.indexOf('Компьютер выключится') === 0 || msg.indexOf('Перезагрузка') === 0 ||
            msg.indexOf('Отменила') === 0 || msg.indexOf('Тест:') === 0 ||
            msg.indexOf('Микрофон работает') === 0 || msg.indexOf('Микрофон не') === 0 ||
            msg.indexOf('Уровень:') === 0 || msg.indexOf('После ресэмплинга') === 0 ||
            msg.indexOf('Отмена:') === 0) {
            var t3 = msg.substring(msg.indexOf(': ') + 2);
            if (t3 === msg) t3 = msg;
            logLine('JARVIS', t3);
            if (!state.muted) pulseWaveform();
            return;
        }

        // Errors
        if (msg.indexOf('Ошибка') === 0 || msg.indexOf('ОШИБКА') === 0) {
            logLine('SYS', msg, 'err');
            return;
        }

        // Default
        logLine('SYS', msg, 'sys');
    }

    async function pollStatus() {
        if (!state.apiReady) return;
        try {
            var st = await window.pywebview.api.get_status();
            if (st) {
                if (st.processing) {
                    state.thinking = true;
                    if (els.vitalVoice) els.vitalVoice.textContent = 'Thinking';
                } else if (st.listening && !state.thinking) {
                    if (els.vitalVoice) els.vitalVoice.textContent = 'Listening';
                } else if (!st.listening) {
                    if (els.vitalVoice) els.vitalVoice.textContent = 'Paused';
                }
            }
        } catch (e) { }
    }

    async function pollMicLevel() {
        if (!state.apiReady) return;
        try {
            var level = await window.pywebview.api.get_mic_level();
            if (level !== undefined && level !== null) {
                if (els.vitalMic) els.vitalMic.textContent = level + '%';
            }
        } catch (e) { }
    }

    async function pollSystemInfo() {
        if (!state.apiReady) return;
        try {
            var info = await window.pywebview.api.get_system_info();
            if (info) {
                var cpuEl = els.cardCpu?.querySelector('.sys-value');
                var memEl = els.cardMem?.querySelector('.sys-value');
                if (cpuEl) cpuEl.textContent = Math.round(info.cpu) + '%';
                if (memEl) memEl.textContent = Math.round(info.memory) + '%';
                drawGraph('cpu', info.cpu / 100);
                drawGraph('mem', info.memory / 100);
            }
        } catch (e) { }
    }

    /* ===== INIT ===== */
    setMood('neutral');
    logLine('SYS', 'Interface loaded. Connecting to backend...', 'sys');
    logLine('SYS', 'FRIDAY OS v2.0 — Neural interface online.', 'sys');
    waitForApi();
})();
