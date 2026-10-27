/* ============================================================================
   FacePass 前端主逻辑
   - 摄像头采集：getUserMedia + Canvas 限宽压缩（默认 480px / 7fps）
   - 通信：WebSocket 单帧一问一答（天然背压，不会堆积）
   - UI：状态机驱动，任何分支都有反馈（扫描 / 眨眼 / 成功 / 重复 / 失败 / 环境异常）
   ========================================================================== */
(function () {
  'use strict';

  var FPS_INTERVAL = Math.round(1000 / 7);
  var FRAME_MAX_WIDTH = 480;
  var JPEG_QUALITY = 0.72;
  var RESULT_HOLD_MS = 3400;
  var FAIL_HOLD_MS = 2600;
  var ENROLL_SAMPLES = 3;

  var $ = function (sel) { return document.querySelector(sel); };

  var el = {
    stage: $('#stage'),
    stageCard: $('.stage-card'),
    video: $('#video'),
    overlay: $('#overlay'),
    fallback: $('#cameraFallback'),
    fallbackTitle: $('#cameraFallback p'),
    fallbackHint: $('#cameraFallback span'),
    stageTagText: $('#stageTagText'),
    stageClock: $('#stageClock'),
    brandDate: $('#brandDate'),
    sideDate: $('#sideDate'),
    statusTitle: $('#statusTitle'),
    statusHint: $('#statusHint'),
    statusOrb: $('#statusOrb'),
    progressWrap: $('#progressWrap'),
    progressBar: $('#progressBar'),
    metaRow: $('#metaRow'),
    alertBar: $('#alertBar'),
    alertTitle: $('#alertTitle'),
    alertHint: $('#alertHint'),
    alertBtn: $('#alertBtn'),
    recordList: $('#recordList'),
    recordEmpty: $('#recordEmpty'),
    todayCount: $('#todayCount'),
    empCount: $('#empCount'),
    clock: $('#clock'),
    ruleStart: $('#ruleStart'),
    ruleEnd: $('#ruleEnd'),
    ruleThreshold: $('#ruleThreshold'),
    envPill: $('#envPill'),
    envLabel: $('#envLabel'),
    themeBtn: $('#themeBtn'),
    enrollBtn: $('#enrollBtn'),
    enrollDialog: $('#enrollDialog'),
    inputEmpNo: $('#inputEmpNo'),
    inputName: $('#inputName'),
    inputDept: $('#inputDept'),
    captureBtn: $('#captureBtn'),
    captureHint: $('#captureHint'),
    captureState: $('#captureState'),
    closeEnroll: $('#closeEnroll'),
    cancelEnroll: $('#cancelEnroll'),
    drawer: $('#drawer'),
    drawerMask: $('#drawerMask'),
    drawerBody: $('#drawerBody'),
    drawerTime: $('#drawerTime'),
    drawerClose: $('#drawerClose'),
    diagBtn: $('#diagBtn'),
    recheckBtn: $('#recheckBtn'),
    toast: $('#toast')
  };

  var frameCanvas = document.createElement('canvas');
  var frameCtx = frameCanvas.getContext('2d');

  var S = {
    ws: null,
    wsOpen: false,
    wsAttempts: 0,
    wsClosedByUser: false,
    engineReady: false,
    dbReady: false,
    health: null,
    cameraReady: false,
    cameraError: false,
    paused: true,
    inFlight: false,
    pumpTimer: null,
    frameTimer: null,
    resultTimer: null,
    healthTimer: null,
    overviewTimer: null,
    phase: 'idle',
    lastTitle: '',
    lastHint: '',
    recordsHtml: '',
    toastTimer: null,
    enrolling: false,
    drawerTimer: null,
    config: {}
  };

  var tracker = { cur: null, target: null, raf: null, visible: false };

  // ------------------------------------------------------------------ 小工具
  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  function toast(message, tone, duration) {
    el.toast.textContent = message;
    el.toast.dataset.tone = tone || '';
    el.toast.dataset.show = 'true';
    clearTimeout(S.toastTimer);
    S.toastTimer = setTimeout(function () {
      el.toast.dataset.show = 'false';
    }, duration || 3200);
  }

  function statusTone(status) {
    if (status === '迟到') return 'late';
    if (status === '早退') return 'early';
    if (status === '正常') return 'normal';
    return 'out';
  }

  var WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

  function dateText(now) {
    return (now.getMonth() + 1) + ' 月 ' + now.getDate() + ' 日 · ' + WEEKDAYS[now.getDay()];
  }

  // ------------------------------------------------------------------ 时钟
  function pad(n) { return n < 10 ? '0' + n : String(n); }

  function tickClock() {
    var now = new Date();
    var text = pad(now.getHours()) + ':' + pad(now.getMinutes());
    el.clock.textContent = text;
    el.stageClock.textContent = text + ':' + pad(now.getSeconds());
    el.brandDate.textContent = dateText(now);
    el.sideDate.textContent = dateText(now);
  }

  // ------------------------------------------------------------------ 状态 UI
  function setStage(state) {
    S.phase = state;
    if (el.stage.dataset.state !== state) {
      el.stage.dataset.state = state;
    }
  }

  function setTag(text) {
    if (el.stageTagText.textContent !== text) el.stageTagText.textContent = text;
  }

  function setStatus(title, hint, tone) {
    if (S.lastTitle !== title) {
      el.statusTitle.textContent = title;
      S.lastTitle = title;
    }
    if (S.lastHint !== hint) {
      el.statusHint.textContent = hint;
      S.lastHint = hint;
    }
    if (tone) {
      el.statusOrb.dataset.tone = tone;
      el.stageCard.dataset.tone = tone === 'scan' || tone === 'idle' ? '' : tone;
    }
  }

  function setProgress(value, tone) {
    var percent = Math.max(0, Math.min(100, Math.round((value || 0) * 100)));
    el.progressBar.style.width = percent + '%';
    el.progressWrap.dataset.tone = tone || '';
  }

  function showAlert(title, hint, tone) {
    el.alertTitle.textContent = title;
    el.alertHint.textContent = hint || '';
    el.alertBar.hidden = false;
    el.alertBar.dataset.tone = tone || 'error';
  }

  function hideAlert() {
    el.alertBar.hidden = true;
  }

  function shake() {
    el.stage.classList.remove('shake');
    void el.stage.offsetWidth;
    el.stage.classList.add('shake');
    setTimeout(function () { el.stage.classList.remove('shake'); }, 600);
  }

  function clearMeta() {
    el.metaRow.hidden = true;
    el.metaRow.innerHTML = '';
  }

  function addChip(label, value) {
    var chip = document.createElement('span');
    chip.className = 'chip';
    chip.innerHTML = esc(label) + ' <b>' + esc(value) + '</b>';
    el.metaRow.appendChild(chip);
  }

  function addStatusChip(status) {
    var chip = document.createElement('span');
    chip.className = 'chip status';
    chip.dataset.tone = statusTone(status);
    chip.textContent = status;
    el.metaRow.appendChild(chip);
  }

  function addActionChip(label, handler) {
    var chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'chip action';
    chip.textContent = label;
    chip.addEventListener('click', handler);
    el.metaRow.appendChild(chip);
  }

  // ------------------------------------------------------------------ 取景框绘制
  function roundRect(ctx, x, y, w, h, r) {
    var radius = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + radius, y);
    ctx.lineTo(x + w - radius, y);
    ctx.quadraticCurveTo(x + w, y, x + w, y + radius);
    ctx.lineTo(x + w, y + h - radius);
    ctx.quadraticCurveTo(x + w, y + h, x + w - radius, y + h);
    ctx.lineTo(x + radius, y + h);
    ctx.quadraticCurveTo(x, y + h, x, y + h - radius);
    ctx.lineTo(x, y + radius);
    ctx.quadraticCurveTo(x, y, x + radius, y);
    ctx.closePath();
  }

  function clearBox() {
    tracker.visible = false;
    tracker.target = null;
    tracker.cur = null;
    if (tracker.raf) {
      cancelAnimationFrame(tracker.raf);
      tracker.raf = null;
    }
    var canvas = el.overlay;
    var ctx = canvas.getContext('2d');
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
  }

  function setBox(bbox) {
    if (!Array.isArray(bbox) || bbox.length !== 4) {
      clearBox();
      return;
    }
    tracker.visible = true;
    tracker.target = bbox;
    if (!tracker.cur) tracker.cur = bbox.slice();
    if (!tracker.raf) tracker.raf = requestAnimationFrame(stepBox);
  }

  function stepBox() {
    tracker.raf = null;
    if (!tracker.visible || !tracker.target) {
      clearBox();
      return;
    }
    var cur = tracker.cur;
    var target = tracker.target;
    var moving = false;
    for (var i = 0; i < 4; i += 1) {
      var diff = target[i] - cur[i];
      if (Math.abs(diff) > 0.0008) {
        cur[i] += diff * 0.3;
        moving = true;
      } else {
        cur[i] = target[i];
      }
    }
    paintBox(cur);
    if (moving) tracker.raf = requestAnimationFrame(stepBox);
  }

  function paintBox(box) {
    var canvas = el.overlay;
    var rect = el.stage.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return;

    var dpr = Math.min(2, window.devicePixelRatio || 1);
    var cw = Math.round(rect.width * dpr);
    var ch = Math.round(rect.height * dpr);
    if (canvas.width !== cw || canvas.height !== ch) {
      canvas.width = cw;
      canvas.height = ch;
    }

    var ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, rect.width, rect.height);

    var vw = el.video.videoWidth || rect.width;
    var vh = el.video.videoHeight || rect.height;
    var scale = Math.max(rect.width / vw, rect.height / vh);
    var dw = vw * scale;
    var dh = vh * scale;
    var dx = (rect.width - dw) / 2;
    var dy = (rect.height - dh) / 2;

    // 摄像头画面是镜像显示的，x 轴需要翻转
    var bx = dx + (1 - box[0] - box[2]) * dw;
    var by = dy + box[1] * dh;
    var bw = box[2] * dw;
    var bh = box[3] * dh;

    ctx.save();
    ctx.strokeStyle = 'rgba(255,255,255,0.78)';
    ctx.lineWidth = 1.6;
    ctx.shadowColor = 'rgba(10,132,255,0.65)';
    ctx.shadowBlur = 14;
    roundRect(ctx, bx, by, bw, bh, Math.min(18, bw * 0.16));
    ctx.stroke();
    ctx.restore();
  }

  function resizeOverlay() {
    clearBox();
  }

  // ------------------------------------------------------------------ 环境检测
  function applyConfig(config) {
    if (!config) return;
    S.config = config;
    if (config.work_start) el.ruleStart.textContent = config.work_start;
    if (config.work_end) el.ruleEnd.textContent = config.work_end;
    if (typeof config.match_threshold === 'number') {
      el.ruleThreshold.textContent = config.match_threshold.toFixed(2);
    }
  }

  function refreshHealth() {
    return fetch('/api/health', { cache: 'no-store' })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        applyHealth(data);
        return data;
      })
      .catch(function () {
        el.envPill.dataset.tone = 'bad';
        el.envLabel.textContent = '服务未响应';
        showAlert('无法连接后端服务', '请确认 python -m app.main 仍在运行，然后刷新页面。');
      });
  }

  function applyHealth(data) {
    S.health = data;
    S.engineReady = !!(data.engine && data.engine.ready);
    S.dbReady = !!(data.db && data.db.ok);
    applyConfig(data.config);
    renderDrawer(data);

    if (data.ok) {
      el.envPill.dataset.tone = 'ok';
      el.envLabel.textContent = '环境就绪';
    } else if (!S.dbReady) {
      el.envPill.dataset.tone = 'bad';
      el.envLabel.textContent = '数据库未就绪';
    } else {
      el.envPill.dataset.tone = 'warn';
      el.envLabel.textContent = '识别引擎未就绪';
    }

    if (data.ok && !S.cameraError) {
      hideAlert();
    }

    if (data.warnings && data.warnings.length) {
      console.warn('[FacePass] 配置提醒：', data.warnings.join(' | '));
    }

    if (
      data.ok &&
      S.cameraReady &&
      !S.enrolling &&
      (S.phase === 'error' || S.phase === 'idle')
    ) {
      setTimeout(resetSession, 300);
    }
  }

  function drawerRow(title, okState, detail, extra) {
    var html = '<div class="check-row" data-ok="' + okState + '">';
    html += '<span class="dot"></span><div class="check-main"><strong>' + esc(title) + '</strong>';
    if (detail) html += '<p>' + esc(detail) + '</p>';
    if (extra) html += '<code>' + esc(extra) + '</code>';
    html += '</div></div>';
    return html;
  }

  var MODEL_LABELS = {
    shape_predictor: '68 点关键点模型',
    face_recognition: 'ResNet 128 维特征模型',
    cnn_detector: 'CNN 人脸检测模型'
  };

  function renderDrawer(data) {
    var html = '';
    html += drawerRow(
      'MySQL 数据库',
      data.db.ok ? 'true' : 'false',
      data.db.ok ? '已连接 · MySQL ' + (data.db.version || '') + ' · ' + (data.db.database || '') : data.db.message,
      data.db.ok ? '' : data.db.hint
    );
    if (data.db.ok) {
      html += drawerRow(
        '数据表结构',
        data.db.tables_ok ? 'true' : 'false',
        data.db.tables_ok
          ? 'employees / face_encodings / attendance_records 均已就绪'
          : '缺少：' + (data.db.missing_tables || []).join('、'),
        data.db.tables_ok ? '' : 'python scripts/init_db.py'
      );
    }

    html += drawerRow(
      'dlib 运行时',
      data.engine.dlib_available ? 'true' : 'false',
      data.engine.dlib_available
        ? '已安装，检测器 ' + String(data.engine.detector || 'hog').toUpperCase()
        : 'Python 中无法 import dlib',
      data.engine.dlib_available ? '' : 'pip install dlib==19.24.6（建议 Python 3.9~3.11）'
    );

    Object.keys(data.engine.models || {}).forEach(function (key) {
      var item = data.engine.models[key];
      var label = MODEL_LABELS[key] || key;
      html += drawerRow(
        label,
        item.exists ? 'true' : 'false',
        item.exists ? '已找到 · ' + (item.size_text || '') : (item.bz2_present ? '只有 .bz2，需要解压' : '文件不存在'),
        item.path
      );
    });

    html += drawerRow(
      '摄像头',
      S.cameraReady ? 'true' : (S.cameraError ? 'false' : 'warn'),
      S.cameraReady ? '已获取视频流' : (S.cameraError ? '无法访问摄像头' : '正在启动…'),
      ''
    );

    html += '<div class="drawer-note"><b>当前参数</b><br>' +
      '上下班 ' + esc(data.config.work_start) + ' / ' + esc(data.config.work_end) +
      ' · 迟到宽限 ' + esc(data.config.late_grace_minutes) + ' 分钟<br>' +
      '匹配阈值 ' + esc(data.config.match_threshold) + ' · EAR 阈值 ' + esc(data.config.ear_threshold) +
      ' · 活体超时 ' + esc(data.config.liveness_timeout_seconds) + 's<br>' +
      '重复打卡窗口 ' + esc(data.config.duplicate_window_seconds) + 's · 单帧宽度 ' +
      esc(data.config.frame_max_width) + 'px<br>' +
      '模型目录 <code>' + esc(data.models_dir) + '</code>' +
      '</div>';

    if (!data.ok) {
      var steps = [];
      if (!data.db.ok) steps.push('在小皮面板启动 MySQL，确认 .env 中 DB_USER / DB_PASSWORD 正确；或运行 <b>python scripts/init_db.py</b> 自动建库建表。');
      if (data.db.ok && !data.db.tables_ok) steps.push('执行 <b>python scripts/init_db.py</b> 创建缺失的数据表。');
      if (!data.engine.dlib_available) steps.push('安装 dlib：<b>pip install dlib==19.24.6</b>（Python 3.12+ 无预编译包，建议 3.10/3.11）。');
      var missingModels = Object.keys(data.engine.models || {}).filter(function (k) { return !data.engine.models[k].exists; });
      if (missingModels.length) steps.push('从 <b>http://dlib.net/files/</b> 下载模型 .bz2，解压后放进 <b>' + esc(data.models_dir) + '</b>。');
      if (steps.length) {
        html += '<div class="drawer-note"><b>修复步骤</b><ol>' +
          steps.map(function (s) { return '<li>' + s + '</li>'; }).join('') +
          '</ol></div>';
      }
    }

    html += '<div class="drawer-note" style="border-style:solid">' +
      '<b>关于活体检测</b><br>基于 dlib 68 点关键点计算 EAR，默认阈值 ' +
      esc(data.config.ear_threshold) + '、连续 2 帧判定眨眼；' +
      '首次识别会用前 3 帧校准个人睁眼基线，自动适配偏小的眼型。' +
      '它能挡住静态照片和大部分屏幕翻拍，但不等同于金融级 3D 活体，请勿用于高安全场景。' +
      '</div>';

    el.drawerBody.innerHTML = html;
    el.drawerTime.textContent = '自检于 ' + (data.time || '');
  }

  function openDrawer() {
    clearTimeout(S.drawerTimer);
    el.drawer.hidden = false;
    el.drawerMask.hidden = false;
    requestAnimationFrame(function () { el.drawer.dataset.open = 'true'; });
  }

  function closeDrawer() {
    el.drawer.dataset.open = 'false';
    clearTimeout(S.drawerTimer);
    S.drawerTimer = setTimeout(function () {
      el.drawer.hidden = true;
      el.drawerMask.hidden = true;
    }, 320);
  }

  // ------------------------------------------------------------------ 摄像头
  function showCameraError(title, hint) {
    S.cameraError = true;
    S.cameraReady = false;
    S.paused = true;
    el.fallback.hidden = false;
    el.fallback.dataset.error = 'true';
    el.fallbackTitle.textContent = title;
    el.fallbackHint.textContent = hint;
    setStage('idle');
    setStatus(title, hint, 'error');
    setTag('摄像头异常');
    showAlert(title, hint, 'error');
  }

  function startCamera() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showCameraError(
        '当前浏览器不支持摄像头',
        '请使用最新版 Chrome / Edge，并通过 http://127.0.0.1 或 https 访问本页面。'
      );
      return;
    }

    setStatus('正在启动摄像头…', '首次使用请允许浏览器访问摄像头', 'idle');
    setTag('连接中');

    navigator.mediaDevices
      .getUserMedia({
        audio: false,
        video: {
          facingMode: 'user',
          width: { ideal: 1280 },
          height: { ideal: 720 },
          frameRate: { ideal: 24, max: 30 }
        }
      })
      .then(function (stream) {
        el.video.srcObject = stream;
        return el.video.play().catch(function () {});
      })
      .then(function () {
        S.cameraReady = true;
        S.cameraError = false;
        el.fallback.hidden = true;
        el.fallback.dataset.error = 'false';
        resizeOverlay();
        if (S.engineReady) {
          hideAlert();
          resetSession();
        } else {
          setStage('idle');
          setStatus('识别引擎未就绪', '请点击右上角的环境状态查看具体缺少什么', 'error');
          setTag('未就绪');
        }
      })
      .catch(function (err) {
        var name = err && err.name;
        var map = {
          NotAllowedError: ['摄像头权限被拒绝', '点击地址栏左侧的摄像头图标，允许访问后刷新页面。'],
          PermissionDeniedError: ['摄像头权限被拒绝', '请在浏览器设置中允许本站使用摄像头，然后刷新页面。'],
          NotFoundError: ['未检测到摄像头', '请连接摄像头；笔记本请确认没有物理遮挡或 Fn 快捷键关闭。'],
          DevicesNotFoundError: ['未检测到摄像头', '请连接摄像头后刷新页面。'],
          NotReadableError: ['摄像头被其他程序占用', '关闭会议软件 / 相机应用后刷新页面。'],
          TrackStartError: ['摄像头启动失败', '摄像头可能被其他程序占用，请关闭后重试。'],
          OverconstrainedError: ['摄像头不支持所需分辨率', '请更换摄像头，或修改 app.js 中的分辨率约束。']
        };
        var item = map[name] || ['无法启动摄像头', (err && err.message) || '请检查设备与浏览器权限。'];
        showCameraError(item[0], item[1]);
      });
  }

  // ------------------------------------------------------------------ WebSocket
  function wsUrl() {
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return proto + '//' + location.host + '/ws/checkin';
  }

  function connectWS() {
    if (S.ws && (S.ws.readyState === WebSocket.OPEN || S.ws.readyState === WebSocket.CONNECTING)) {
      return;
    }
    var ws;
    try {
      ws = new WebSocket(wsUrl());
    } catch (err) {
      scheduleReconnect();
      return;
    }
    S.ws = ws;
    ws.binaryType = 'arraybuffer';

    ws.onopen = function () {
      S.wsOpen = true;
      S.wsAttempts = 0;
      if (S.engineReady) {
        schedulePump(120);
      }
    };

    ws.onmessage = function (event) {
      var msg;
      try {
        msg = JSON.parse(event.data);
      } catch (err) {
        return;
      }
      handleMessage(msg);
    };

    ws.onclose = function () {
      S.wsOpen = false;
      S.inFlight = false;
      clearTimeout(S.frameTimer);
      if (!S.wsClosedByUser) {
        setStatus('连接已断开，正在重连…', '若持续断开，请确认后端服务仍在运行', 'warn');
        scheduleReconnect();
      }
    };

    ws.onerror = function () { /* onclose 会接管重连 */ };
  }

  function scheduleReconnect() {
    S.wsAttempts += 1;
    var delay = Math.min(6000, 700 * Math.pow(1.6, Math.min(S.wsAttempts, 6)));
    setTimeout(connectWS, delay);
  }

  function sendRaw(payload) {
    if (S.ws && S.ws.readyState === WebSocket.OPEN) {
      S.ws.send(JSON.stringify(payload));
      return true;
    }
    return false;
  }

  function handleMessage(msg) {
    S.inFlight = false;
    clearTimeout(S.frameTimer);

    if (msg.type === 'hello') {
      onHello(msg);
    } else if (msg.type === 'state') {
      renderState(msg);
    } else if (msg.type === 'result') {
      renderResult(msg);
    } else if (msg.type === 'error') {
      renderError(msg);
    }

    if (!S.paused) schedulePump(FPS_INTERVAL);
  }

  function onHello(msg) {
    applyConfig(msg.config);
    var engine = msg.engine || {};
    S.engineReady = !!engine.ready;
    if (!S.engineReady) {
      S.paused = true;
      setStage('idle');
      setTag('未就绪');
      setStatus('识别引擎未就绪', engine.message || '缺少 dlib 或模型文件', 'error');
      setProgress(0, 'error');
      showAlert('识别引擎未就绪', engine.hint || engine.message || '请查看环境诊断。');
      refreshHealth();
      return;
    }
    if (S.cameraReady && S.phase !== 'success' && S.phase !== 'duplicate' && S.phase !== 'error') {
      resetSession();
    }
  }

  function renderState(msg) {
    var state = msg.state;
    var views = {
      no_face: {
        stage: 'no_face',
        tag: '寻找人脸',
        title: '请正对摄像头',
        hint: msg.hint || '把面部放在取景框内，保持光线均匀',
        tone: 'scan'
      },
      scanning: {
        stage: 'scanning',
        tag: '扫描中',
        title: '请自然眨一次眼',
        hint: '保持面部在框内 · 正在做活体检测',
        tone: 'scan'
      },
      blink: {
        stage: 'blink',
        tag: '活体通过',
        title: '已检测到眨眼',
        hint: '正在确认身份…',
        tone: 'scan'
      },
      await_open: {
        stage: 'blink',
        tag: '比对中',
        title: '请睁开眼睛',
        hint: msg.hint || '保持正对镜头，马上就好',
        tone: 'scan'
      },
      ready: {
        stage: 'verifying',
        tag: '比对中',
        title: '正在比对身份…',
        hint: '与本地人脸库比对中',
        tone: 'scan'
      }
    };
    var view = views[state] || views.scanning;

    setStage(view.stage);
    setTag(view.tag);
    setStatus(view.title, view.hint, view.tone);
    if (typeof msg.progress === 'number') setProgress(msg.progress, '');
    if (msg.bbox) setBox(msg.bbox);
    else if (state === 'no_face') clearBox();
  }

  function renderResult(msg) {
    S.paused = true;
    clearTimeout(S.resultTimer);
    clearBox();
    setProgress(1, msg.ok ? 'ok' : 'error');
    el.metaRow.hidden = false;
    el.metaRow.innerHTML = '';
    el.alertBar.hidden = true;

    if (msg.code === 'success') {
      setStage('success');
      setTag('验证通过');
      setStatus(msg.message, (msg.punch_type || '') + ' · ' + (msg.status || '') + ' · ' + (msg.time || ''), 'ok');
      addChip('工号', msg.employee ? msg.employee.emp_no : '');
      if (msg.employee && msg.employee.department) addChip('部门', msg.employee.department);
      addChip('时间', msg.time);
      addStatusChip(msg.status);
      if (typeof msg.liveness_score === 'number') {
        addChip('活体', msg.liveness_score.toFixed(2));
      }
      S.resultTimer = setTimeout(resetSession, RESULT_HOLD_MS);
      refreshOverview();
      return;
    }

    if (msg.code === 'duplicate') {
      setStage('duplicate');
      setTag('请勿重复');
      var last = msg.last || {};
      setStatus(msg.message, (last.time || '') + ' · ' + (last.punch_type || '') + ' · ' + (last.status || ''), 'warn');
      if (msg.employee) {
        addChip('工号', msg.employee.emp_no);
        if (msg.employee.department) addChip('部门', msg.employee.department);
      }
      if (last.time) addChip('上次打卡', last.time);
      addStatusChip(last.status);
      S.resultTimer = setTimeout(resetSession, FAIL_HOLD_MS);
      return;
    }

    // unknown / liveness_failed / 其他失败
    setStage('error');
    setTag('未通过');
    shake();
    var hint = msg.hint || '请调整后重试';
    if (msg.code === 'unknown' && typeof msg.distance === 'number') {
      hint = '最接近距离 ' + msg.distance.toFixed(2) + '，判定阈值 ' + Number(msg.threshold).toFixed(2) + '。' + hint;
    }
    setStatus(msg.message || '识别失败', hint, 'error');
    if (msg.code === 'unknown' && typeof msg.distance === 'number') {
      addChip('匹配距离', msg.distance.toFixed(3));
      addChip('阈值', Number(msg.threshold).toFixed(2));
    }
    S.resultTimer = setTimeout(resetSession, FAIL_HOLD_MS);
  }

  function renderError(msg) {
    var code = msg.code || 'error';
    var fatal = code === 'face_engine_unavailable' || code === 'db_error' || code === 'engine_error';

    if (code === 'face_engine_unavailable') {
      S.engineReady = false;
      S.paused = true;
      setStage('idle');
      setTag('未就绪');
      setStatus('识别引擎未就绪', msg.message || '', 'error');
      showAlert('识别引擎未就绪', msg.hint || msg.message || '请查看环境诊断。');
      refreshHealth();
      return;
    }

    if (code === 'db_error') {
      S.paused = true;
      setStage('error');
      setTag('数据库异常');
      shake();
      setStatus('数据库未就绪', msg.message || '', 'error');
      showAlert(msg.message || '数据库异常', msg.hint || '请查看环境诊断。');
      clearMeta();
      el.metaRow.hidden = false;
      addActionChip('重新检测', function () {
        refreshHealth().then(function () { resetSession(); });
      });
      refreshHealth();
      return;
    }

    if (fatal) {
      S.paused = true;
      setStage('error');
      setTag('识别异常');
      shake();
      setStatus(msg.message || '识别异常', msg.hint || '', 'error');
      clearMeta();
      el.metaRow.hidden = false;
      addActionChip('重新尝试', resetSession);
      return;
    }

    // 单帧问题：不打断会话，下一帧继续
    toast(msg.message || '画面异常，正在重试…', 'error', 1800);
    S.paused = false;
    schedulePump(320);
  }

  function resetSession() {
    clearTimeout(S.resultTimer);
    S.resultTimer = null;
    clearMeta();
    clearBox();
    el.progressWrap.dataset.tone = '';
    setProgress(0, '');

    if (!S.engineReady || !S.cameraReady) {
      setStage('idle');
      return;
    }
    if (S.health && !S.dbReady) {
      setStage('idle');
      setStatus('数据库未就绪', '打卡记录无法写入，请先修复数据库连接', 'error');
      showAlert('数据库未就绪', '请在环境诊断中查看具体原因。');
      return;
    }

    hideAlert();
    sendRaw({ type: 'reset' });
    setStage('scanning');
    setTag('扫描中');
    setStatus('请自然眨一次眼', '保持面部在框内，系统会自动完成活体检测', 'scan');
    S.paused = false;
    schedulePump(160);
  }

  // ------------------------------------------------------------------ 采集循环
  function schedulePump(delay) {
    clearTimeout(S.pumpTimer);
    S.pumpTimer = setTimeout(pump, delay);
  }

  function pump() {
    S.pumpTimer = null;
    if (S.paused || S.inFlight) return;
    if (!S.wsOpen || !S.engineReady || !S.cameraReady) return;

    var video = el.video;
    if (!video.videoWidth || video.readyState < 2) {
      schedulePump(200);
      return;
    }

    var ratio = Math.min(1, FRAME_MAX_WIDTH / video.videoWidth);
    var width = Math.max(2, Math.round(video.videoWidth * ratio));
    var height = Math.max(2, Math.round(video.videoHeight * ratio));
    if (frameCanvas.width !== width || frameCanvas.height !== height) {
      frameCanvas.width = width;
      frameCanvas.height = height;
    }
    frameCtx.drawImage(video, 0, 0, width, height);

    frameCanvas.toBlob(function (blob) {
      if (!blob || !S.wsOpen || S.paused) return;
      S.inFlight = true;
      blob
        .arrayBuffer()
        .then(function (buffer) {
          if (!S.wsOpen) {
            S.inFlight = false;
            return;
          }
          try {
            S.ws.send(buffer);
          } catch (err) {
            S.inFlight = false;
            schedulePump(400);
            return;
          }
          clearTimeout(S.frameTimer);
          S.frameTimer = setTimeout(function () {
            S.inFlight = false;
            if (!S.paused) schedulePump(300);
          }, 5000);
        })
        .catch(function () {
          S.inFlight = false;
          schedulePump(300);
        });
    }, 'image/jpeg', JPEG_QUALITY);
  }

  // ------------------------------------------------------------------ 今日记录
  function refreshOverview() {
    return fetch('/api/overview', { cache: 'no-store' })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (data) {
        el.empCount.textContent = data.employee_count;
        el.todayCount.textContent = data.today_count + ' 人';
        el.ruleStart.textContent = data.work_start;
        el.ruleEnd.textContent = data.work_end;
        var html = (data.records || []).map(recordHtml).join('');
        if (html !== S.recordsHtml) {
          S.recordsHtml = html;
          el.recordList.innerHTML = html;
        }
        el.recordEmpty.hidden = (data.records || []).length > 0;
      })
      .catch(function () {
        S.recordsHtml = '';
        el.recordList.innerHTML = '';
        el.recordEmpty.hidden = false;
        el.recordEmpty.querySelector('p').textContent = '暂时读不到今天的记录';
        el.recordEmpty.querySelector('span').textContent = '请打开环境诊断检查数据库';
        el.empCount.textContent = '—';
      });
  }

  function recordHtml(record) {
    var name = record.employee_name || '未知';
    var initial = name.trim().charAt(0) || '?';
    var sub = (record.department || record.emp_no || '') + ' · ' + (record.punch_type || '');
    return (
      '<li class="record-item">' +
      '<div class="avatar">' + esc(initial) + '</div>' +
      '<div class="record-main"><strong>' + esc(name) + '</strong><span>' + esc(sub) + '</span></div>' +
      '<div class="record-side"><span class="record-time">' + esc(record.time || '') + '</span>' +
      '<span class="tag" data-tone="' + statusTone(record.status) + '">' + esc(record.status || '') + '</span></div>' +
      '</li>'
    );
  }

  // ------------------------------------------------------------------ 录入员工
  function setCapture(tone, text) {
    el.captureState.dataset.tone = tone;
    el.captureHint.textContent = text;
  }

  function openEnroll() {
    if (!S.engineReady) {
      toast('识别引擎未就绪，请先修复环境', 'error');
      openDrawer();
      return;
    }
    if (!S.cameraReady) {
      toast('摄像头未就绪，无法采集样本', 'error');
      return;
    }
    setCapture('idle', '填写信息后，点击「采集并录入」。请让本人单独出现在画面中。');
    el.enrollDialog.showModal();
    setTimeout(function () { el.inputEmpNo.focus(); }, 80);
  }

  function snapshot(maxWidth, quality) {
    return new Promise(function (resolve) {
      var video = el.video;
      if (!video.videoWidth) {
        resolve(null);
        return;
      }
      var ratio = Math.min(1, maxWidth / video.videoWidth);
      var canvas = document.createElement('canvas');
      canvas.width = Math.round(video.videoWidth * ratio);
      canvas.height = Math.round(video.videoHeight * ratio);
      canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
      canvas.toBlob(resolve, 'image/jpeg', quality);
    });
  }

  function captureAndEnroll() {
    if (S.enrolling) return;

    var empNo = el.inputEmpNo.value.trim();
    var name = el.inputName.value.trim();
    var dept = el.inputDept.value.trim();

    if (!/^[A-Za-z0-9_-]{2,32}$/.test(empNo)) {
      setCapture('error', '工号需为 2~32 位字母 / 数字 / 下划线 / 短横线，例如 E001');
      el.inputEmpNo.focus();
      return;
    }
    if (!name) {
      setCapture('error', '请填写姓名');
      el.inputName.focus();
      return;
    }
    if (!S.cameraReady || !el.video.videoWidth) {
      setCapture('error', '摄像头画面未就绪，请稍候再试');
      return;
    }

    S.enrolling = true;
    el.captureBtn.disabled = true;

    var files = [];
    var chain = Promise.resolve();

    for (var i = 0; i < ENROLL_SAMPLES; i += 1) {
      (function (index) {
        chain = chain.then(function () {
          setCapture('busy', '正在采集第 ' + (index + 1) + '/' + ENROLL_SAMPLES + ' 张…请保持正对镜头');
          return sleep(index === 0 ? 150 : 520)
            .then(function () { return snapshot(720, 0.9); })
            .then(function (blob) {
              if (!blob) throw new Error('画面采集失败');
              files.push(new File([blob], 'sample-' + (index + 1) + '.jpg', { type: 'image/jpeg' }));
            });
        });
      })(i);
    }

    chain
      .then(function () {
        setCapture('busy', '正在提取人脸特征并写入数据库…');
        var form = new FormData();
        form.append('emp_no', empNo);
        form.append('name', name);
        form.append('department', dept);
        files.forEach(function (file) { form.append('files', file, file.name); });

        return fetch('/api/employees/enroll', { method: 'POST', body: form });
      })
      .then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (data) {
          if (res.ok && data.ok) {
            var extra = data.failures && data.failures.length
              ? '（' + data.failures.length + ' 张未通过）'
              : '';
            setCapture('ok', (data.message || '录入成功') + extra);
            toast((data.message || '录入成功') + extra, 'ok');
            setTimeout(function () { el.enrollDialog.close(); }, 900);
            refreshOverview();
            refreshHealth();
            return;
          }
          var message = data.message || '';
          var hint = data.hint || '';
          if (!message) {
            if (typeof data.detail === 'string') message = data.detail;
            else if (Array.isArray(data.detail)) {
              message = data.detail.map(function (item) { return item.msg || ''; }).join('；');
            }
          }
          setCapture('error', (message || '录入失败（HTTP ' + res.status + '）') + (hint ? '：' + hint : ''));
        });
      })
      .catch(function (err) {
        setCapture('error', '录入失败：' + (err && err.message ? err.message : err));
      })
      .then(function () {
        S.enrolling = false;
        el.captureBtn.disabled = false;
      });
  }

  // ------------------------------------------------------------------ 主题 / 生命周期
  function initTheme() {
    var saved = null;
    try { saved = localStorage.getItem('facepass-theme'); } catch (err) { saved = null; }
    var theme = saved === 'light' || saved === 'dark' ? saved : 'dark';
    document.documentElement.dataset.theme = theme;
  }

  function toggleTheme() {
    var next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('facepass-theme', next); } catch (err) { /* 忽略隐私模式 */ }
  }

  function bindEvents() {
    el.themeBtn.addEventListener('click', toggleTheme);
    el.enrollBtn.addEventListener('click', openEnroll);
    el.captureBtn.addEventListener('click', captureAndEnroll);
    el.closeEnroll.addEventListener('click', function () { el.enrollDialog.close(); });
    el.cancelEnroll.addEventListener('click', function () { el.enrollDialog.close(); });

    el.diagBtn.addEventListener('click', function () { openDrawer(); refreshHealth(); });
    el.envPill.addEventListener('click', function () { openDrawer(); refreshHealth(); });
    el.alertBtn.addEventListener('click', function () { openDrawer(); refreshHealth(); });
    el.drawerClose.addEventListener('click', closeDrawer);
    el.drawerMask.addEventListener('click', closeDrawer);
    el.recheckBtn.addEventListener('click', function () {
      el.recheckBtn.disabled = true;
      refreshHealth().then(function () {
        el.recheckBtn.disabled = false;
        toast('已重新检测环境', '', 1800);
      });
    });

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && !el.drawer.hidden) closeDrawer();
    });

    window.addEventListener('resize', function () {
      resizeOverlay();
    });

    document.addEventListener('visibilitychange', function () {
      if (document.hidden) {
        clearTimeout(S.pumpTimer);
      } else if (!S.paused) {
        schedulePump(140);
      }
    });

    window.addEventListener('beforeunload', function () {
      S.wsClosedByUser = true;
      if (S.ws) S.ws.close();
    });

    el.enrollDialog.addEventListener('close', function () {
      setCapture('idle', '填写信息后，点击「采集并录入」。请让本人单独出现在画面中。');
      el.captureBtn.disabled = false;
      S.enrolling = false;
    });
  }

  function startPolling() {
    S.healthTimer = setInterval(function () {
      if (!document.hidden) refreshHealth();
    }, 20000);
    S.overviewTimer = setInterval(function () {
      if (!document.hidden) refreshOverview();
    }, 8000);
  }

  function init() {
    initTheme();
    bindEvents();
    tickClock();
    setInterval(tickClock, 1000);

    refreshHealth();
    refreshOverview();
    startCamera();
    connectWS();
    startPolling();
  }

  init();
})();
