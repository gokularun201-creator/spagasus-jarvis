/* SPAGASUS JARVIS - dashboard logic */
(function () {
  const logEl = document.getElementById('log');
  const emptyEl = document.getElementById('empty');
  const cmdEl = document.getElementById('cmd');

  function addLine(role, text) {
    emptyEl.style.display = 'none';
    const d = document.createElement('div');
    d.className = 'line ' + role;
    const who = document.createElement('span');
    who.className = 'who';
    who.textContent = role === 'user' ? 'YOU' : 'JARVIS';
    d.appendChild(who);
    d.appendChild(document.createTextNode(text));
    logEl.appendChild(d);
    logEl.scrollTop = logEl.scrollHeight;
    return d;
  }

  function renderTurns(turns) {
    if (!turns) return;
    const known = logEl.querySelectorAll('.line').length;
    if (turns.length < known) { logEl.innerHTML = ''; emptyEl.style.display = 'none'; }
    turns.slice(known).forEach(t => { const el = addLine(t.role, t.text); attachStreamLine(el); });
  }

  function renderStats(s) {
    if (!s) return;
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    const bar = (id, pct) => { const el = document.getElementById(id); if (el) el.style.width = Math.min(100, pct) + '%'; };
    set('s-cpu', s.cpu + '%'); bar('b-cpu', s.cpu);
    set('s-ram', s.ram + '%'); bar('b-ram', s.ram);
    set('s-disk', s.disk_percent + '%'); bar('b-disk', s.disk_percent);
    if (s.battery >= 0) { set('s-bat', s.battery + '%'); bar('b-bat', s.battery); }
    else { set('s-bat', 'AC'); bar('b-bat', 100); }
  }

  let lastDevSig = '';
  function renderDevices(devices) {
    const el = document.getElementById('devices');
    const sig = devices ? devices.map(d => d.name + '|' + d.status + '|' + d.battery).join(',') : '';
    if (sig === lastDevSig) return;   // no DOM churn when nothing changed
    lastDevSig = sig;
    if (!devices || !devices.length) { el.innerHTML = '<div class="dev"><span>No devices connected.</span></div>'; return; }
    el.innerHTML = devices.map(d =>
      '<div class="dev"><span>' + esc(d.name) + '</span>' +
      '<span class="st"><i class="' + (d.status === 'online' ? 'on' : 'off') + '"></i>' +
      (d.battery >= 0 ? d.battery + '% ' : '') + esc(d.status.toUpperCase()) + '</span></div>'
    ).join('');
  }

  let lastActSig = '';
  function renderActions(actions) {
    const el = document.getElementById('actions');
    const sig = actions ? actions.slice(-8).map(a => a.action.slice(0, 40)).join(',') : '';
    if (sig === lastActSig) return;   // no DOM churn when nothing changed
    lastActSig = sig;
    if (!actions || !actions.length) { el.innerHTML = '<div class="act">No actions yet.</div>'; return; }
    el.innerHTML = actions.slice(-8).reverse().map(a =>
      '<div class="act"><b>' + esc(a.action.slice(0, 40)) + '</b></div>'
    ).join('');
  }

  function esc(s) {
    const d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
  }

  let lastStatus = '';
  let pingTimer = null;
  // live effects fire when JARVIS changes state: searching the web spins the
  // radar and pings data hits, executing throws an energy surge, speaking
  // exhales particles - driven by the same status the CSS animations watch.
  function onStatusChange(from, to) {
    if (!to) return;
    if (to === 'THINKING') {          // web search: radar sweep + data pings
      fxBurst(6);
      let n = 0;
      clearInterval(pingTimer);
      pingTimer = setInterval(() => { fxPing(); if (++n >= 7) clearInterval(pingTimer); }, 700);
    } else if (to === 'EXECUTING') {  // running automations: energy surge
      fxBurst(12);
    } else if (to === 'SPEAKING') {   // voice: gentle particle exhale
      fxBurst(5);
    } else if (to === 'LISTENING') {
      fxBurst(3);
    } else if (to === 'STANDBY' || to === 'ERROR') {
      clearInterval(pingTimer);
    }
  }

  // live 'you said' caption under the core - shows what SG just heard
  let saidTimer = null;
  function showSaid(text) {
    const el = document.getElementById('said');
    if (!el) return;
    const clean = (text || '').replace(/\s+/g, ' ').trim();
    clearTimeout(saidTimer);
    if (!clean) { el.textContent = ''; el.classList.remove('show'); return; }
    el.textContent = 'You said: "' + clean + '"';
    el.classList.add('show');
    // let the caption linger after JARVIS starts replying, then fade
    saidTimer = setTimeout(() => el.classList.remove('show'), 9000);
  }

  // ---- progressive reply reveal -------------------------------------
  // The backend streams the reply as segments (reply_begin / reply_seg /
  // reply_play / reply_done). The text appears on screen at the same speed
  // the voice speaks it: NO word highlighting - the text itself is simply
  // revealed progressively, paced by each segment's real playback duration.
  let stream = null;
  let revealTimer = null;

  function armStream(msg) {
    finalizeStream();
    stream = {
      id: msg.id,
      segs: (msg.segs || []).map(s => (typeof s === 'string' ? s : s.text)),
      durs: {},
      fullText: msg.text || '',
      line: null,
      t0: performance.now(),
      active: true
    };
    if (!revealTimer) revealTimer = setInterval(revealTick, 80);
  }
  function segStream(msg) {
    if (!stream || stream.id !== msg.id) return;
    stream.segs[msg.seg] = msg.text;
    stream.fullText = stream.segs.join(' ');
  }
  function playStream(msg) {
    if (!stream || stream.id !== msg.id) return;
    // real playback duration for this segment - drives the reveal pace
    stream.durs[msg.seg] = msg.dur > 0 ? msg.dur : Math.max(200, (stream.segs[msg.seg] || '').length * 66.7);
  }
  function doneStream(msg) {
    if (!stream || stream.id !== msg.id) return;
    stream.active = false;
    finalizeStream();
  }
  function lastJarvisLine() {
    const lines = logEl.querySelectorAll('.line.jarvis');
    return lines.length ? lines[lines.length - 1] : null;
  }
  function attachStreamLine(el) {
    if (!stream || !stream.active || stream.line || !el) return;
    if (!el.classList.contains('jarvis')) return;
    if (el !== lastJarvisLine()) return;   // only the newest JARVIS line streams
    const node = el.lastChild;
    const txt = (node && node.nodeType === 3) ? node.textContent.trim() : '';
    if (stream.fullText && txt && txt !== stream.fullText.trim()) return;
    stream.line = el;
    stream.t0 = performance.now();
    el.classList.add('streaming');
    if (node && node.nodeType === 3) node.textContent = '';   // start hidden - reveal follows the voice
  }
  function revealTick() {
    if (!stream || !stream.active) { finalizeStream(); return; }
    if (!stream.line) { attachStreamLine(lastJarvisLine()); if (!stream.line) return; }
    const segs = stream.segs;
    if (!segs.length) return;
    const elapsed = Math.max(0, performance.now() - stream.t0);
    let cum = 0, shown = 0;
    for (let i = 0; i < segs.length; i++) {
      const segLen = (segs[i] || '').length;
      const dur = stream.durs[i] || Math.max(200, segLen * 66.7);
      if (elapsed >= cum + dur) { shown += segLen; cum += dur; continue; }
      if (elapsed > cum) shown += Math.floor(segLen * ((elapsed - cum) / dur));
      break;
    }
    const full = stream.fullText;
    const node = stream.line.lastChild;
    if (node && node.nodeType === 3) node.textContent = full.slice(0, Math.min(shown, full.length));
    if (shown >= full.length) finalizeStream();
  }
  function finalizeStream() {
    if (revealTimer) { clearInterval(revealTimer); revealTimer = null; }
    if (stream && stream.line) {
      const node = stream.line.lastChild;
      if (node && node.nodeType === 3) node.textContent = stream.fullText || node.textContent;
      stream.line.classList.remove('streaming');
    }
    stream = null;
  }

  let lastTask = '';
  function applyState(s) {
    const status = s.status || 'STANDBY';
    if (status !== lastStatus) { onStatusChange(lastStatus, status); lastStatus = status; }
    setVoicePausedBySystem(status === 'SPEAKING' || !!s.media_busy);
    if (status === 'LISTENING') { showSaid(''); finalizeStream(); }   // fresh phrase: clear old caption + finish any reply reveal
    if (document.body.dataset.status !== status) document.body.dataset.status = status;
    document.getElementById('st').textContent = status;
    const task = s.task ? ('▸ ' + s.task) : 'Awaiting your command, boss…';
    if (task !== lastTask) { lastTask = task; document.getElementById('task').textContent = task; }
    renderStats(s.stats);
    renderDevices(s.devices);
    renderActions(s.actions);
    renderTurns(s.turns);
    if (typeof s.volume === 'number') {
      updateVolumeUI(s.volume, s.is_headset, s.audio_device);
    }
    if (typeof s.level === 'number') {
      const amb = s.ambient || 0.01;
      const lvl = Math.min(1, Math.max(0, (s.level - amb * 0.7) / (amb * 9)));
      setLvl(lvl);
    }
  }

  let curLvl = 0;
  function setLvl(l) {
    const nv = curLvl + (l - curLvl) * 0.4;
    curLvl = nv;
    // writing --lvl invalidates styles for the whole page, so only touch
    // the DOM when the value actually moved (level ticks arrive fast)
    const cur = parseFloat(document.documentElement.style.getPropertyValue('--lvl')) || 0;
    if (Math.abs(cur - nv) > 0.003) {
      document.documentElement.style.setProperty('--lvl', nv.toFixed(3));
    }
  }

  function tickClock() {
    const d = new Date();
    document.getElementById('clock').textContent =
      String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0') + ':' + String(d.getSeconds()).padStart(2, '0');
  }
  tickClock();
  setInterval(tickClock, 1000);

  // freeze the whole FX while the tab is hidden (CSS pauses animations)
  document.addEventListener('visibilitychange', () => {
    document.body.classList.toggle('hidden-tab', document.hidden);
  });

  // initial state
  fetch('/api/status').then(r => r.json()).then(applyState).catch(() => {});

  // realtime via WebSocket
  let ws = null;
  function connect() {
    ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
    ws.onmessage = e => {
      let msg; try { msg = JSON.parse(e.data); } catch (err) { return; }
      if (msg && msg.type === 'level') { setLvl(msg.level); return; }  // live mic feed
      if (msg && msg.type === 'heard') {
        showSaid(msg.text);
        if (msg.text && msg.text.trim()) {
          cmdEl.value = msg.text.trim();
          cmdEl.classList.add('listening');
          setTimeout(() => cmdEl.classList.remove('listening'), 2500);
        }
        return;
      }
      if (msg && msg.type === 'reply_begin') { armStream(msg); return; } // progressive reply reveal
      if (msg && msg.type === 'reply_seg') { segStream(msg); return; }
      if (msg && msg.type === 'reply_play') { playStream(msg); return; }
      if (msg && msg.type === 'reply_done') { doneStream(msg); return; }
      if (msg && msg.type === 'volume') { updateVolumeUI(msg.volume, msg.is_headset, msg.device); return; }
      applyState(msg);
    };
    ws.onclose = () => setTimeout(connect, 2000);
  }
  connect();

  // commands
  function sendCommand(text) {
    if (!text) return;
    fetch('/api/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text })
    }).then(r => r.json()).then(d => { if (d.reply) addLine('jarvis', d.reply); }).catch(() => {});
  }

  document.getElementById('wake').addEventListener('click', () => {
    fetch('/api/wake', { method: 'POST' }).catch(() => {});
  });

  cmdEl.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      const text = cmdEl.value.trim();
      if (text) { addLine('user', text); sendCommand(text); cmdEl.value = ''; }
    }
  });

  // ==================================================================
  // WAKE-WORD LISTENER
  // Keeps the dashboard mic armed only for "Hey Jarvis"; command capture is
  // handled by the backend mic session after the wake request starts.
  // ==================================================================
  const voiceBtn = document.getElementById('voiceBtn');
  let recognition = null;
  let isVoiceListening = false;
  let autoListenEnabled = true;
  let voicePausedBySystem = false;
  let voiceSubmitTimer = null;

  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (SpeechRecognition) {
    recognition = new SpeechRecognition();
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = 'en-US';

    recognition.onstart = () => {
      isVoiceListening = true;
      if (voiceBtn) {
        voiceBtn.classList.add('active');
        voiceBtn.innerHTML = '🔴 WAKE WORD';
      }
      cmdEl.classList.remove('listening');
      cmdEl.placeholder = 'Say "Hey Jarvis" to start listening';
    };

    recognition.onresult = (event) => {
      if (voicePausedBySystem) return;
      let interim = '';
      let final = '';
      for (let i = event.resultIndex; i < event.results.length; ++i) {
        if (event.results[i].isFinal) {
          final += event.results[i][0].transcript;
        } else {
          interim += event.results[i][0].transcript;
        }
      }

      const spokenText = (final || interim).trim();
      if (spokenText) {
        if (isWakePhrase(spokenText)) {
          clearTimeout(voiceSubmitTimer);
          cmdEl.value = '';
          showSaid('Hey Jarvis');
          fetch('/api/wake', { method: 'POST' }).catch(() => {});
        }
      }
    };

    recognition.onerror = (event) => {
      console.warn('SpeechRecognition error:', event.error);
      if (event.error === 'not-allowed') {
        autoListenEnabled = false;
        stopVoiceListener();
      }
    };

    recognition.onend = () => {
      isVoiceListening = false;
      if (autoListenEnabled && !voicePausedBySystem) {
        // immediately re-arm so it is continuously listening
        setTimeout(() => {
          if (autoListenEnabled && !voicePausedBySystem && !isVoiceListening) {
            try { recognition.start(); } catch (e) {}
          }
        }, 120);
      } else if (voicePausedBySystem) {
        // A system pause is temporary: keep autoListenEnabled intact so the
        // wake-word listener resumes when media/TTS clears.
        if (voiceBtn) {
          voiceBtn.classList.remove('active');
          voiceBtn.innerHTML = '⏸️ VOICE';
        }
      } else {
        stopVoiceListener();
      }
    };
  }

  function submitVoiceCommand(text) {
    if (voicePausedBySystem) return;
    if (!text) return;
    cmdEl.value = text;
    cmdEl.classList.remove('listening');
    addLine('user', text);
    sendCommand(text);
    setTimeout(() => {
      if (cmdEl.value === text) cmdEl.value = '';
    }, 1200);
  }

  function isWakePhrase(text) {
    const clean = String(text || '').toLowerCase().replace(/[^a-z0-9 ]+/g, ' ').replace(/\s+/g, ' ').trim();
    return /(^|\s)(hey|ok|okay|hi)?\s*(spagasus\s+)?(jarvis|jervis|darvis|jarvies|spagasus|pegasus)(\s|$)/.test(clean);
  }

  function startVoiceListener() {
    autoListenEnabled = true;
    if (voicePausedBySystem) return;
    if (!recognition) {
      fetch('/api/wake', { method: 'POST' }).catch(() => {});
      return;
    }
    try {
      recognition.start();
    } catch (e) {
      console.warn('Recognition start error:', e);
    }
  }

  function stopVoiceListener() {
    autoListenEnabled = false;
    isVoiceListening = false;
    if (voiceBtn) {
      voiceBtn.classList.remove('active');
      voiceBtn.innerHTML = '🎙️ VOICE';
    }
    if (cmdEl) {
      cmdEl.classList.remove('listening');
      cmdEl.placeholder = "Type a command or speak… (say 'Hey Jarvis', 'Open YouTube', etc.)";
    }
    if (recognition) {
      try { recognition.stop(); } catch (e) {}
    }
  }

  function setVoicePausedBySystem(paused) {
    if (voicePausedBySystem === paused) return;
    voicePausedBySystem = paused;
    clearTimeout(voiceSubmitTimer);
    if (paused) {
      isVoiceListening = false;
      if (voiceBtn) {
        voiceBtn.classList.remove('active');
        voiceBtn.innerHTML = '⏸️ VOICE';
      }
      if (cmdEl) {
        cmdEl.classList.remove('listening');
        cmdEl.placeholder = 'Voice paused while JARVIS speaks or media plays';
      }
      if (recognition) {
        try { recognition.stop(); } catch (e) {}
      }
      return;
    }
    if (voiceBtn) voiceBtn.innerHTML = '🎙️ VOICE';
    if (cmdEl) cmdEl.placeholder = "Type a command or speak… (say 'Hey Jarvis', 'Open YouTube', etc.)";
    if (autoListenEnabled && recognition && !isVoiceListening) {
      setTimeout(() => {
        if (autoListenEnabled && !voicePausedBySystem && !isVoiceListening) {
          try { recognition.start(); } catch (e) {}
        }
      }, 250);
    }
  }

  if (voiceBtn) {
    voiceBtn.addEventListener('click', () => {
      if (isVoiceListening || autoListenEnabled) {
        stopVoiceListener();
      } else {
        startVoiceListener();
      }
    });
  }

  // auto-start live voice listener immediately on page load
  setTimeout(() => startVoiceListener(), 800);

  // top nav: Home / Chat / Dashboard view switcher
  document.querySelectorAll('.nav-btn').forEach(b => b.addEventListener('click', () => {
    document.querySelectorAll('.nav-btn').forEach(x => x.classList.toggle('active', x === b));
    document.body.classList.remove('view-home', 'view-chat', 'view-dash');
    document.body.classList.add('view-' + b.dataset.view);
  }));

  // closing the dashboard closes the whole app: the server exits and the
  // supervisor stops instead of restarting (guard: only after 10s open)
  const loadTime = Date.now();
  window.addEventListener('pagehide', () => {
    if (Date.now() - loadTime > 10000) {
      navigator.sendBeacon('/api/shutdown');
    }
  });

  // ==================================================================
  // HAND TRACKING - camera control (the ULTRON effect): the whole UI
  // tilts and shakes following your hand, and an open palm wakes JARVIS
  // ==================================================================
  const camBtns = Array.from(document.querySelectorAll('.cam'));
  const camPip = document.getElementById('camPip');
  const camVideo = document.getElementById('camVideo');
  const camCanvas = document.getElementById('camCanvas');
  const camCtx = camCanvas.getContext('2d');
  const camNote = document.getElementById('camNote');
  // the 21 bone connections of a MediaPipe hand
  const HAND_EDGES = [[0,1],[1,2],[2,3],[3,4],[0,5],[5,6],[6,7],[7,8],[5,9],[9,10],[10,11],[11,12],
                      [9,13],[13,14],[14,15],[15,16],[13,17],[17,18],[18,19],[19,20],[0,17]];

  function drawSkeleton(lm) {
    const dpr = window.devicePixelRatio || 1;
    const rect = camCanvas.getBoundingClientRect();
    const targetW = Math.round((rect.width || 320) * dpr);
    const targetH = Math.round((rect.height || 240) * dpr);
    if (camCanvas.width !== targetW || camCanvas.height !== targetH) {
      camCanvas.width = targetW;
      camCanvas.height = targetH;
    }
    const w = camCanvas.width, h = camCanvas.height;
    camCtx.clearRect(0, 0, w, h);
    camCtx.lineWidth = Math.max(2 * dpr, w / 75);
    camCtx.strokeStyle = 'rgba(255,165,61,0.95)';
    camCtx.shadowColor = 'rgba(255,154,61,1)';
    camCtx.shadowBlur = 8 * dpr;
    camCtx.beginPath();
    for (const [a, b] of HAND_EDGES) {
      camCtx.moveTo(lm[a].x * w, lm[a].y * h);
      camCtx.lineTo(lm[b].x * w, lm[b].y * h);
    }
    camCtx.stroke();
    camCtx.shadowBlur = 10 * dpr;
    lm.forEach((p, i) => {
      camCtx.beginPath();
      camCtx.arc(p.x * w, p.y * h, Math.max(3.5 * dpr, w / 60), 0, Math.PI * 2);
      camCtx.fillStyle = i === 8 ? '#ffffff' : (i === 4 || i === 12 || i === 16 || i === 20 ? '#ffe9cf' : '#ff9a3d');
      camCtx.shadowColor = i === 8 ? '#00e5ff' : 'rgba(255,154,61,1)';
      camCtx.fill();
    });
  }

  function clearSkeleton() {
    camCtx.clearRect(0, 0, camCanvas.width, camCanvas.height);
  }
  const VISION_CDN = 'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/vision_bundle.mjs';
  const MODEL_URL = 'https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task';

  let handLandmarker = null;
  let handStream = null;
  let camOn = false;
  let lastCenter = null;
  let lastT = 0;
  let lastWake = 0;
  let curOpen = 0.55;    // smoothed hand openness (drives the --hand core size)
  let prevRawOpen = -1;  // raw openness from the previous frame (wake detection)
  let lastHandAng = null; // previous hand angle, for camera rotation
  let curCz = 0;         // camera-driven rotation of the core (degrees)

  let lastNote = '';
  function setCamNote(t) { if (t !== lastNote) { lastNote = t; camNote.textContent = t; } }

  function setCamBtn(txt) { camBtns.forEach(b => { b.textContent = txt; }); }

  // angle of the hand in the frame (wrist -> middle-finger base), using the
  // MIRRORED x so the direction matches what the user sees on screen.
  function handAngle(lm) {
    const wx = 1 - lm[0].x, wy = lm[0].y;
    const mx = 1 - lm[9].x, my = lm[9].y;
    return Math.atan2(my - wy, mx - wx) * 180 / Math.PI;
  }

  // how open is the hand? 0 = tight fist ... 1 = fully open fingers.
  // continuous value from the average spread of the 4 fingertips around the palm.
  function handOpenness(lm) {
    const palm = lm[9];
    const size = Math.hypot(lm[9].x - lm[0].x, lm[9].y - lm[0].y) || 1;
    let sum = 0;
    for (const i of [8, 12, 16, 20]) sum += Math.hypot(lm[i].x - palm.x, lm[i].y - palm.y);
    const ratio = sum / 4 / (size * 2.0);   // ~0.3 fist ... ~1.0 open
    return Math.max(0, Math.min(1, (ratio - 0.3) / 0.7));
  }

  // guarded CSS-var writer: every setProperty call invalidates styles for the
  // whole page, so only touch the DOM when the value actually moved
  const coreStyle = document.documentElement.style;
  function setVarIf(name, value, eps) {
    const cur = parseFloat(coreStyle.getPropertyValue(name)) || 0;
    if (Math.abs(cur - value) > eps) coreStyle.setProperty(name, value);
  }

  function applyHand(center, vel, detected) {
    // gentle tilt of the core toward the hand (smooth, barely moving)
    const t = 0.08; // heavy smoothing so it never feels shaky
    const tx = detected ? (center.x - 0.5) * -7 : 0;   // degrees
    const ty = detected ? (center.y - 0.5) * 5 : 0;
    const cx = parseFloat(coreStyle.getPropertyValue('--hx')) || 0;
    const cy = parseFloat(coreStyle.getPropertyValue('--hy')) || 0;
    setVarIf('--hx', (cx + (tx - cx) * t).toFixed(2) + 'deg', 0.01);
    setVarIf('--hy', (cy + (ty - cy) * t).toFixed(2) + 'deg', 0.01);
    // tiny, smooth shake only when the hand actually whips around
    let shk = 0;
    if (detected && vel > 0.05) shk = Math.min(0.35, (vel - 0.05) / 0.3);
    const now = performance.now();
    const shakeX = Math.sin(now * 0.05) * 1.0 * shk;
    const shakeY = Math.cos(now * 0.04) * 0.8 * shk;
    setVarIf('--hsx', shakeX.toFixed(2) + 'px', 0.05);
    setVarIf('--hsy', shakeY.toFixed(2) + 'px', 0.05);
    const handOn = detected ? 'on' : 'off';
    if (document.body.dataset.hand !== handOn) document.body.dataset.hand = handOn;
  }

  // hand tracking is throttled to ~20 fps: visually identical for a slow-moving
  // hand, but ~3x cheaper - MediaPipe detection is the most expensive thing this
  // page does and was the frame-rate killer behind the UI lag
  const DETECT_MS = 50;
  let lastDetect = 0;

  function loop(t) {
    if (!camOn || !handLandmarker) return;
    if (t - lastDetect < DETECT_MS) { requestAnimationFrame(loop); return; }
    lastDetect = t;
    const dt = Math.max(8, t - lastT) / 1000;
    lastT = t;
    try {
      const res = handLandmarker.detectForVideo(camVideo, t);
      if (res.landmarks && res.landmarks.length) {
        const lm = res.landmarks[0];
        drawSkeleton(lm);
        let sx = 0, sy = 0;
        for (const p of lm) { sx += p.x; sy += p.y; }
        const center = { x: sx / lm.length, y: sy / lm.length };
        let vel = 0;
        if (lastCenter) {
          vel = Math.hypot(center.x - lastCenter.x, center.y - lastCenter.y) / dt;
        }
        // camera rotation: twist your hand to spin the core, and sweeping it
        // sideways rotates it too (mirror-matched: right -> right, left -> left)
        const hAng = handAngle(lm);
        if (lastHandAng != null) {
          const dAng = Math.max(-16, Math.min(16, normDeg(hAng - lastHandAng) * 0.9));
          curCz += dAng;
        }
        lastHandAng = hAng;
        if (lastCenter) curCz -= (center.x - lastCenter.x) * 36;
        setVarIf('--cz', curCz.toFixed(1) + 'deg', 0.05);
        lastCenter = center;
        applyHand(center, vel, true);
        // continuous openness: fist = tiny core, slow finger opening = it grows
        const open = handOpenness(lm);
        curOpen += (open - curOpen) * 0.12;   // smooth, so size changes slowly
        setVarIf('--hand', curOpen.toFixed(3), 0.002);
        // a quick snap-open (fast 'slap') wakes JARVIS - slow opening just grows the core
        if (prevRawOpen >= 0 && open - prevRawOpen > 0.5 && performance.now() - lastWake > 3000) {
          lastWake = performance.now();
          setCamNote('WAKING… ✋');
          camPip.classList.add('gesture');
          fetch('/api/wake', { method: 'POST' }).catch(() => {});
        } else {
          camPip.classList.remove('gesture');
        }
        prevRawOpen = open;
        const spinTxt = Math.abs(curCz) > 3 ? ' · SPIN ' + Math.round(curCz) + '°' : '';
        setCamNote((open < 0.3 ? 'FIST ✊' : 'OPEN ' + Math.round(open * 100) + '% ✋') + spinTxt);
      } else {
        clearSkeleton();
        lastCenter = null;
        prevRawOpen = -1;
        lastHandAng = null;
        // no hand in view: the core slowly un-rotates back to center
        curCz += (0 - curCz) * 0.06;
        setVarIf('--cz', curCz.toFixed(1) + 'deg', 0.05);
        applyHand(null, 0, false);
        // no hand in view: the core shrinks back down while waiting
        curOpen += (0 - curOpen) * 0.08;
        setVarIf('--hand', curOpen.toFixed(3), 0.002);
        setCamNote('SHOW YOUR HAND…');
        camPip.classList.remove('gesture');
      }
    } catch (err) { /* one bad frame - keep going */ }
    requestAnimationFrame(loop);
  }

  async function enableCam() {
    setCamBtn('CAM …');
    // the camera API only exists on secure pages: http://localhost or https.
    // Opening the dashboard over http://<lan-ip> silently blocks getUserMedia -
    // that is the usual "camera not working" cause, so say it out loud.
    if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      console.error('CAM: camera needs a secure context (http://localhost or https), not a LAN IP');
      setCamBtn('CAM ✕');
      setCamNote('CAMERA NEEDS LOCALHOST / HTTPS');
      return;
    }
    try {
      if (!handLandmarker) {
        const mod = await import(VISION_CDN);
        const fileset = await mod.FilesetResolver.forVisionTasks(
          'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/wasm');
        try {
          handLandmarker = await mod.HandLandmarker.createFromOptions(fileset, {
            baseOptions: { modelAssetPath: MODEL_URL, delegate: 'GPU' },
            runningMode: 'VIDEO', numHands: 1,
          });
        } catch (gpuErr) {
          // WebGL2/GPU delegate fails on many machines (RDP, old GPUs,
          // some laptops) - MediaPipe then works fine on CPU, so retry.
          console.warn('CAM GPU delegate failed, retrying on CPU:', gpuErr);
          setCamNote('LOADING MODEL… (CPU)');
          handLandmarker = await mod.HandLandmarker.createFromOptions(fileset, {
            baseOptions: { modelAssetPath: MODEL_URL, delegate: 'CPU' },
            runningMode: 'VIDEO', numHands: 1,
          });
        }
      }
      handStream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: 'user', width: 320, height: 240 },
        audio: false,
      });
      camVideo.srcObject = handStream;
      await camVideo.play();
      camCanvas.width = camVideo.videoWidth || 320;
      camCanvas.height = camVideo.videoHeight || 240;
      camOn = true;
      document.body.dataset.cam = 'on';
      setCamBtn('CAM ON');
      setCamNote('SHOW YOUR HAND…');
      lastT = performance.now();
      requestAnimationFrame(loop);
    } catch (err) {
      console.error('CAM error:', err);
      setCamBtn('CAM ✕');
      // show WHY it failed instead of a generic message
      setCamNote('CAM UNAVAILABLE — ' + String((err && err.name) || err).slice(0, 40));
      disableCam();
    }
  }

  function disableCam() {
    camOn = false;
    clearSkeleton();
    if (handStream) { handStream.getTracks().forEach(t => t.stop()); handStream = null; }
    camVideo.srcObject = null;
    document.body.dataset.cam = 'off';
    delete document.body.dataset.hand;
    setCamBtn('◉ CAM');
    document.documentElement.style.setProperty('--hx', '0deg');
    document.documentElement.style.setProperty('--hy', '0deg');
    document.documentElement.style.setProperty('--hsx', '0px');
    document.documentElement.style.setProperty('--hsy', '0px');
    document.documentElement.style.setProperty('--hand', '0.55');   // default core size without camera
    // keep the real failure reason on screen; only restore the hint when there was no error
    if (!camNote.textContent.startsWith('CAM UNAVAILABLE')) setCamNote('CAMERA OFF — TAP ◉ CAM');
  }

  camBtns.forEach(b => b.addEventListener('click', () => {
    if (camOn) { disableCam(); } else { enableCam(); }
  }));

  // auto-start camera tracking on launch
  setTimeout(() => enableCam(), 900);

  // ==================================================================
  // TOUCH GESTURES - spin / move the SG core with your fingers:
  //   one finger drags the core, two fingers rotate it (and pinch to
  //   zoom), and double-tap (or double-click) resets it to center.
  // Uses Pointer Events when available, with a Touch Events fallback
  // for older browsers/webviews - exactly one path is active.
  // ==================================================================
  const centerEl = document.querySelector('.center');
  const coreWrap = document.querySelector('.core-wrap');
  let touch = { tz: 0, tx: 0, ty: 0, ts: 0 };  // rotation / pan / scale
  let lastAng = 0, lastDist = 0, lastMid = null, lastAng1 = 0, lastP1 = null;

  function readTouch() {
    const cs = getComputedStyle(document.documentElement);
    touch.tz = parseFloat(cs.getPropertyValue('--tz')) || 0;
    touch.tx = parseFloat(cs.getPropertyValue('--tx')) || 0;
    touch.ty = parseFloat(cs.getPropertyValue('--ty')) || 0;
    touch.ts = parseFloat(cs.getPropertyValue('--ts')) || 0;
  }
  function writeTouch() {
    const r = document.documentElement.style;
    r.setProperty('--tz', touch.tz.toFixed(2) + 'deg');
    r.setProperty('--tx', touch.tx.toFixed(1) + 'px');
    r.setProperty('--ty', touch.ty.toFixed(1) + 'px');
    r.setProperty('--ts', touch.ts.toFixed(3));
  }
  function resetTouch() {
    touch = { tz: 0, tx: 0, ty: 0, ts: 0 };
    writeTouch();
    curCz = 0;
    document.documentElement.style.setProperty('--cz', '0deg');
    coreWrap.classList.add('snap');
    setTimeout(() => coreWrap.classList.remove('snap'), 420);
  }

  function coreCenter() {
    const r = coreWrap.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  }
  function fingerAngle(x, y) {
    const c = coreCenter();
    return Math.atan2(y - c.y, x - c.x) * 180 / Math.PI;
  }
  function normDeg(a) { while (a > 180) a -= 360; while (a < -180) a += 360; return a; }

  // one finger: the core turns like a dial. A straight drag to the right
  // rotates it right, a drag left rotates it left, and arcing around the
  // core (turning it like a knob) works too - both feed the same rotation.
  function onOneDown(x, y) {
    lastAng1 = fingerAngle(x, y);
    lastP1 = { x, y };
  }
  function onMoveOne(cur) {
    const dx = cur.x - lastP1.x, dy = cur.y - lastP1.y;
    const c = coreCenter();
    const rx = cur.x - c.x, ry = cur.y - c.y;
    const r = Math.max(1, Math.hypot(rx, ry));
    const dAng = dx * 0.28 + (-(ry / r) * dx + (rx / r) * dy) * 0.45;
    readTouch();
    touch.tz += dAng;
    writeTouch();
    lastP1 = cur;
    if (Math.abs(dAng) > 2) { fxShock(); fxBurst(3); }
  }

  // two fingers: rotate with the pair, pinch to zoom, slide to move
  function onTwoDown(list) {
    readTouch();
    lastAng = Math.atan2(list[1].y - list[0].y, list[1].x - list[0].x) * 180 / Math.PI;
    lastDist = Math.hypot(list[1].x - list[0].x, list[1].y - list[0].y);
    lastMid = { x: (list[0].x + list[1].x) / 2, y: (list[0].y + list[1].y) / 2 };
  }
  function onMoveTwo(list) {
    const ang = Math.atan2(list[1].y - list[0].y, list[1].x - list[0].x) * 180 / Math.PI;
    const dist = Math.hypot(list[1].x - list[0].x, list[1].y - list[0].y);
    const mid = { x: (list[0].x + list[1].x) / 2, y: (list[0].y + list[1].y) / 2 };
    readTouch();
    const dAng = normDeg(ang - lastAng);
    touch.tz += dAng;
    if (lastDist > 0)
      touch.ts = Math.max(-0.3, Math.min(0.55, touch.ts + (dist / lastDist - 1) * 1.1));
    if (lastMid) {
      touch.tx += mid.x - lastMid.x;
      touch.ty += mid.y - lastMid.y;
    }
    writeTouch();
    lastAng = ang; lastDist = dist; lastMid = mid;
    if (Math.abs(dAng) > 2) { fxShock(); fxBurst(3); }
  }

  // ---- energy VFX: ambient particles + touch shockwaves -----------------
  const fxEl = document.getElementById('coreFx');
  function fxBurst(n) {
    if (!fxEl) return;
    if (fxEl.childElementCount > 20) return;
    for (let i = 0; i < n; i++) {
      const el = document.createElement('i');
      const ang = Math.random() * Math.PI * 2;
      const dist = 26 + Math.random() * 105;
      el.style.setProperty('--dx', (Math.cos(ang) * dist).toFixed(1) + 'px');
      el.style.setProperty('--dy', (Math.sin(ang) * dist).toFixed(1) + 'px');
      el.style.animationDelay = (Math.random() * 0.3).toFixed(2) + 's';
      fxEl.appendChild(el);
      setTimeout(() => el.remove(), 2500);
    }
  }
  function fxShock() {
    if (!fxEl || fxEl.childElementCount > 20) return;
    const el = document.createElement('b');
    fxEl.appendChild(el);
    setTimeout(() => el.remove(), 850);
  }
  function fxPing() {
    if (!fxEl || fxEl.childElementCount > 20) return;
    const el = document.createElement('u');
    const ang = Math.random() * Math.PI * 2;
    const dist = 30 + Math.random() * 90;
    el.style.setProperty('--px', (Math.cos(ang) * dist).toFixed(1) + 'px');
    el.style.setProperty('--py', (Math.sin(ang) * dist).toFixed(1) + 'px');
    fxEl.appendChild(el);
    setTimeout(() => el.remove(), 1200);
  }
  setInterval(() => { if (!document.hidden && document.body.dataset.status !== 'STANDBY') fxBurst(1); }, 1500);

  // full-screen rising embers (fireflies of the reactor)
  const embersEl = document.createElement('div');
  embersEl.className = 'embers';
  document.body.appendChild(embersEl);
  for (let i = 0; i < 10; i++) {
    const em = document.createElement('i');
    em.style.setProperty('--ex', (Math.random() * 100).toFixed(1) + '%');
    em.style.setProperty('--ed', (8 + Math.random() * 8).toFixed(1) + 's');
    em.style.setProperty('--edl', (-Math.random() * 15).toFixed(1) + 's');
    em.style.setProperty('--esw', ((Math.random() - 0.5) * 120).toFixed(0) + 'px');
    embersEl.appendChild(em);
  }

  if (window.PointerEvent) {
    const pts = new Map();                     // pointerId -> {x, y}
    centerEl.addEventListener('pointerdown', e => {
      e.preventDefault();   // stop the browser from grabbing the touch as scroll/zoom
      pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
      try { centerEl.setPointerCapture(e.pointerId); } catch (err) {}
      coreWrap.classList.add('touching');
      const list = [...pts.values()];
      if (list.length >= 2) onTwoDown(list); else onOneDown(e.clientX, e.clientY);
    });
    centerEl.addEventListener('pointermove', e => {
      if (!pts.has(e.pointerId)) return;
      const cur = { x: e.clientX, y: e.clientY };
      pts.set(e.pointerId, cur);
      const list = [...pts.values()];
      if (list.length === 1) onMoveOne(cur);
      else if (list.length >= 2) onMoveTwo(list);
    });
    const drop = e => {
      if (!pts.has(e.pointerId)) return;
      pts.delete(e.pointerId);
      if (pts.size === 0) coreWrap.classList.remove('touching');
      if (pts.size === 1) { const [p] = [...pts.values()]; lastP1 = { x: p.x, y: p.y }; lastAng1 = fingerAngle(p.x, p.y); }
    };
    centerEl.addEventListener('pointerup', drop);
    centerEl.addEventListener('pointercancel', drop);
  } else {
    // Touch Events fallback (no PointerEvent support)
    const tids = new Map();                    // identifier -> {x, y}
    centerEl.addEventListener('touchstart', e => {
      e.preventDefault();
      for (const t of e.changedTouches) tids.set(t.identifier, { x: t.clientX, y: t.clientY });
      coreWrap.classList.add('touching');
      const list = [...tids.values()];
      if (list.length >= 2) onTwoDown(list);
      else onOneDown(e.changedTouches[0].clientX, e.changedTouches[0].clientY);
    }, { passive: false });
    centerEl.addEventListener('touchmove', e => {
      e.preventDefault();
      for (const t of e.changedTouches) {
        if (!tids.has(t.identifier)) continue;
        const cur = { x: t.clientX, y: t.clientY };
        tids.set(t.identifier, cur);
        const list = [...tids.values()];
        if (list.length === 1) onMoveOne(cur);
        else if (list.length >= 2) onMoveTwo(list);
      }
    }, { passive: false });
    const tEnd = e => {
      for (const t of e.changedTouches) {
        tids.delete(t.identifier);
        if (tids.size === 1) { const [p] = [...tids.values()]; lastP1 = { x: p.x, y: p.y }; lastAng1 = fingerAngle(p.x, p.y); }
      }
      if (tids.size === 0) coreWrap.classList.remove('touching');
    };
    centerEl.addEventListener('touchend', tEnd);
    centerEl.addEventListener('touchcancel', tEnd);
  }

  // double-tap / double-click returns the core to its home position
  centerEl.addEventListener('dblclick', resetTouch);

  // ==================================================================
  // VOICE-REACTIVE RING EDGE - while you talk to SG, the ring edge
  // shimmers/vibrates with the live mic level. JS writes --vibx/--viby
  // on .core-wrap and the CSS `translate` property composes on top of
  // the ring's own spin (GPU compositor, no layout). Idle = zero cost.
  // ==================================================================
  function setVib(name, value, eps) {
    const cur = parseFloat(coreWrap.style.getPropertyValue(name)) || 0;
    if (Math.abs(cur - value) > eps) coreWrap.style.setProperty(name, value);
  }
  const VIB_SESSIONS = { LISTENING:1, THINKING:1, EXECUTING:1, SPEAKING:1 };
  function vibLoop() {
    const active = VIB_SESSIONS[document.body.dataset.status] === 1;
    const amp = active && curLvl > 0.03 ? Math.min(2.6, curLvl * 3.2) : 0;
    document.body.classList.toggle('voice', amp > 0.03);
    if (amp > 0.03) {
      const t = performance.now();
      setVib('--vibx', (Math.sin(t * 0.023) * amp).toFixed(2) + 'px', 0.04);
      setVib('--viby', (Math.cos(t * 0.017) * amp * 0.85).toFixed(2) + 'px', 0.04);
    } else if (coreWrap.style.getPropertyValue('--vibx')) {
      coreWrap.style.removeProperty('--vibx');
      coreWrap.style.removeProperty('--viby');
    }
    requestAnimationFrame(vibLoop);
  }
  requestAnimationFrame(vibLoop);

  // ==================================================================
  // HUD AUDIO SPECTRUM VISUALIZER (Canvas 60fps)
  // ==================================================================
  const specCanvas = document.getElementById('audioSpectrum');
  if (specCanvas) {
    const sCtx = specCanvas.getContext('2d');
    let sAngle = 0;
    function resizeSpec() {
      const rect = specCanvas.getBoundingClientRect();
      specCanvas.width = (rect.width || 400) * (window.devicePixelRatio || 1);
      specCanvas.height = (rect.height || 400) * (window.devicePixelRatio || 1);
    }
    resizeSpec();
    window.addEventListener('resize', resizeSpec);

    function drawSpectrum() {
      if (!sCtx) return;
      const w = specCanvas.width;
      const h = specCanvas.height;
      sCtx.clearRect(0, 0, w, h);
      const cx = w / 2;
      const cy = h / 2;
      const dpr = window.devicePixelRatio || 1;
      const baseR = Math.min(cx, cy) * 0.72;
      const bars = 48;
      const active = VIB_SESSIONS[document.body.dataset.status] === 1;
      const levelFactor = active ? Math.max(0.12, curLvl * 6.0) : 0.05;
      sAngle += 0.008;

      for (let i = 0; i < bars; i++) {
        const theta = (i / bars) * Math.PI * 2 + sAngle;
        const noise = Math.sin(i * 3.5 + sAngle * 4) * Math.cos(i * 1.5 - sAngle * 2);
        const barH = 4 + (Math.abs(noise) * 28 + 6) * levelFactor * dpr;
        const x1 = cx + Math.cos(theta) * baseR;
        const y1 = cy + Math.sin(theta) * baseR;
        const x2 = cx + Math.cos(theta) * (baseR + barH);
        const y2 = cy + Math.sin(theta) * (baseR + barH);

        sCtx.beginPath();
        sCtx.moveTo(x1, y1);
        sCtx.lineTo(x2, y2);
        sCtx.lineWidth = 2 * dpr;
        sCtx.lineCap = 'round';
        const st = document.body.dataset.status;
        if (st === 'SPEAKING') sCtx.strokeStyle = `rgba(255, 224, 138, ${0.3 + levelFactor * 0.7})`;
        else if (st === 'LISTENING') sCtx.strokeStyle = `rgba(255, 165, 61, ${0.4 + levelFactor * 0.6})`;
        else if (st === 'THINKING') sCtx.strokeStyle = `rgba(255, 194, 77, ${0.4 + levelFactor * 0.6})`;
        else if (st === 'EXECUTING') sCtx.strokeStyle = `rgba(255, 138, 30, ${0.4 + levelFactor * 0.6})`;
        else sCtx.strokeStyle = 'rgba(255, 154, 61, 0.12)';
        sCtx.stroke();
      }
      requestAnimationFrame(drawSpectrum);
    }
    requestAnimationFrame(drawSpectrum);
  }

  // ==================================================================
  // HUD QUICK DOCK HANDLERS
  // ==================================================================
  const dockUnlock = document.getElementById('dockUnlock');
  const dockPlay = document.getElementById('dockPlay');
  const dockNext = document.getElementById('dockNext');
  const dockMute = document.getElementById('dockMute');
  const dockDesktop = document.getElementById('dockDesktop');
  const dockTimer = document.getElementById('dockTimer');
  const dockVision = document.getElementById('dockVision');

  function postAction(url, body) {
    fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).catch(() => {});
  }

  if (dockUnlock) dockUnlock.addEventListener('click', () => {
    postAction('/api/action/phone_unlock', { pin: '1234' });
    addLine('user', 'Unlock mobile (PIN 1234)');
    addLine('jarvis', 'Waking mobile and entering PIN 1234...');
  });
  if (dockPlay) dockPlay.addEventListener('click', () => postAction('/api/action/media', { action: 'playpause' }));
  if (dockNext) dockNext.addEventListener('click', () => postAction('/api/action/media', { action: 'next' }));
  if (dockMute) dockMute.addEventListener('click', () => postAction('/api/action/media', { action: 'mute' }));
  if (dockDesktop) dockDesktop.addEventListener('click', () => postAction('/api/action/window', { action: 'minimize_all' }));
  if (dockTimer) dockTimer.addEventListener('click', () => {
    postAction('/api/timers', { seconds: 300, label: '5-Minute Timer' });
    addLine('user', 'Set a timer for 5 minutes.');
    addLine('jarvis', 'Timer set for 5 minutes.');
  });

  // ==================================================================
  // AUDIO VOLUME ADJUST CONTROLS (EARBUDS & SPEAKERS)
  // ==================================================================
  const volBar = document.getElementById('volBar');
  const volBadge = document.getElementById('volBadge');
  const volIcon = document.getElementById('volIcon');
  const volDeviceLabel = document.getElementById('volDeviceLabel');
  const volDownBtn = document.getElementById('volDownBtn');
  const volUpBtn = document.getElementById('volUpBtn');
  const volSlider = document.getElementById('volSlider');
  const volFill = document.getElementById('volFill');
  const volPercent = document.getElementById('volPercent');

  let currentVolume = 80;
  let previousVolume = 80;

  function updateVolumeUI(vol, isHeadset, devName) {
    currentVolume = Math.max(0, Math.min(100, parseInt(vol) || 0));
    if (volSlider) volSlider.value = currentVolume;
    if (volFill) volFill.style.width = currentVolume + '%';
    if (volPercent) volPercent.textContent = currentVolume + '%';
    if (volIcon) volIcon.textContent = isHeadset ? '🎧' : '🔊';
    if (volDeviceLabel) volDeviceLabel.textContent = isHeadset ? 'EARBUDS' : 'SPEAKERS';
    if (volBadge && devName) volBadge.title = devName + ' (Click to toggle mute)';
  }

  function setVolumeServer(data) {
    fetch('/api/volume', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data)
    })
      .then(r => r.json())
      .then(d => {
        if (typeof d.volume === 'number') {
          updateVolumeUI(d.volume, d.is_headset, d.device);
        }
      })
      .catch(() => {});
  }

  if (volSlider) {
    volSlider.addEventListener('input', (e) => {
      const v = parseInt(e.target.value);
      if (volFill) volFill.style.width = v + '%';
      if (volPercent) volPercent.textContent = v + '%';
    });
    volSlider.addEventListener('change', (e) => {
      setVolumeServer({ volume: parseInt(e.target.value) });
    });
  }

  if (volDownBtn) {
    volDownBtn.addEventListener('click', () => {
      setVolumeServer({ delta: -10 });
    });
  }

  if (volUpBtn) {
    volUpBtn.addEventListener('click', () => {
      setVolumeServer({ delta: 10 });
    });
  }

  if (volBadge) {
    volBadge.addEventListener('click', () => {
      if (currentVolume > 0) {
        previousVolume = currentVolume;
        setVolumeServer({ volume: 0 });
      } else {
        setVolumeServer({ volume: previousVolume || 80 });
      }
    });
  }

  // Fetch initial volume on dashboard start
  fetch('/api/volume')
    .then(r => r.json())
    .then(d => {
      if (typeof d.volume === 'number') {
        updateVolumeUI(d.volume, d.is_headset, d.device);
      }
    })
    .catch(() => {});

  // ==================================================================
  // AI SCREEN VISION MODAL HANDLERS
  // ==================================================================
  const visionOverlay = document.getElementById('visionOverlay');
  const visionClose = document.getElementById('visionClose');
  const visionImg = document.getElementById('visionImg');
  const visionInput = document.getElementById('visionInput');
  const visionAskBtn = document.getElementById('visionAskBtn');
  const visionResult = document.getElementById('visionResult');

  function openVisionModal(question = '') {
    if (!visionOverlay) return;
    visionOverlay.style.display = 'flex';
    visionResult.textContent = 'Capturing screen and analyzing with Gemini Multimodal AI...';
    const qParam = question ? '?question=' + encodeURIComponent(question) : '';
    fetch('/api/vision/analyze' + qParam)
      .then(r => r.json())
      .then(data => {
        if (data.b64) visionImg.src = 'data:image/png;base64,' + data.b64;
        visionResult.textContent = data.description || 'Analysis complete.';
      })
      .catch(err => {
        visionResult.textContent = 'Vision error: ' + err;
      });
  }

  if (dockVision) dockVision.addEventListener('click', () => openVisionModal());
  if (visionClose) visionClose.addEventListener('click', () => { visionOverlay.style.display = 'none'; });
  if (visionOverlay) visionOverlay.addEventListener('click', (e) => { if (e.target === visionOverlay) visionOverlay.style.display = 'none'; });
  if (visionAskBtn) visionAskBtn.addEventListener('click', () => {
    const q = (visionInput.value || '').trim();
    if (q) openVisionModal(q);
  });
  if (visionInput) visionInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      const q = (visionInput.value || '').trim();
      if (q) openVisionModal(q);
    }
  });
})();
