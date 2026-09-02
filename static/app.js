// ============ 全局状态 ============
const state = {
  me: null,
  mode: 'upload',            // upload | record | paste
  audioFile: null,           // File 对象
  recordedBlob: null,        // MediaRecorder 输出
  recordedDuration: 0,
  polling: null,             // 列表轮询计时器
  currentDetailId: null,     // 抽屉里的 id
};

// ============ 工具函数 ============
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];
const esc = (s) => (s == null ? '' : String(s).replace(/[&<>"']/g, (c) => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c])));

function toast(msg, level='info', ms=2600) {
  const el = $('#toast');
  el.className = 'toast ' + level;
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add('hidden'), ms);
}

function fmtBytes(n) {
  if (!n) return '';
  const u = ['B','KB','MB','GB'];
  let i = 0; while (n >= 1024 && i < 3) { n /= 1024; i++; }
  return n.toFixed(n >= 10 ? 0 : 1) + u[i];
}
function fmtDur(sec) {
  if (!sec) return '';
  sec = Math.round(sec);
  const m = Math.floor(sec / 60), s = sec % 60;
  return `${m}:${String(s).padStart(2,'0')}`;
}

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!r.ok) throw new Error((data && (data.detail || data.error)) || `HTTP ${r.status}`);
  return data;
}

// ============ 初始化 ============
async function init() {
  // Tab 切换
  $$('.tab').forEach((t) => t.onclick = () => switchTab(t.dataset.tab));
  $('#refreshList').onclick = () => loadList();

  // 模式切换
  $$('.mode').forEach((m) => m.onclick = () => switchMode(m.dataset.mode));

  // 上传 dropzone
  const dz = $('#dropzone');
  const fileInput = $('#audioFile');
  dz.onclick = () => fileInput.click();
  fileInput.onchange = (e) => setAudioFile(e.target.files[0]);
  dz.ondragover = (e) => { e.preventDefault(); dz.classList.add('drag'); };
  dz.ondragleave = () => dz.classList.remove('drag');
  dz.ondrop = (e) => {
    e.preventDefault(); dz.classList.remove('drag');
    setAudioFile(e.dataTransfer.files[0]);
  };

  // 录音
  $('#btnRecStart').onclick = startRecord;
  $('#btnRecStop').onclick = stopRecord;
  $('#btnRecReset').onclick = resetRecord;

  // 表单提交
  $('#newForm').onsubmit = (e) => { e.preventDefault(); submit(); };

  // 抽屉关闭
  $$('#drawer [data-close]').forEach((el) => el.onclick = closeDrawer);

  // 载入用户信息
  try {
    state.me = await api('/api/me');
    $('#userName').textContent = '👤 ' + (state.me.name || state.me.email || '我');
    $('#asrChip').classList.toggle('hidden', state.me.asrReady);
    $('#aiChip').classList.toggle('hidden', state.me.aiReady);
    $('#dbChip').classList.toggle('hidden', state.me.dbReady);
    if (state.me.maxAudioMB) $('#maxMB').textContent = state.me.maxAudioMB;
  } catch (e) {
    toast('初始化失败：' + e.message, 'error');
  }
  await loadList();
}

function switchTab(name) {
  $$('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
  $$('.tab-panel').forEach((p) => p.classList.toggle('active', p.id === 'tab-' + name));
  if (name === 'trend') loadTrend();
  if (name === 'list') loadList();
}

function switchMode(mode) {
  state.mode = mode;
  $$('.mode').forEach((m) => m.classList.toggle('active', m.dataset.mode === mode));
  $$('.mode-panel').forEach((p) => p.classList.toggle('hidden', p.dataset.panel !== mode));
  updateSubmitTip();
}

// ============ 上传录音 ============
function setAudioFile(file) {
  if (!file) return;
  if (state.me && !state.me.asrReady) {
    toast('服务端未配置 ASR，无法上传录音。可先用「粘贴文字」模式', 'warn', 3500);
    return;
  }
  const maxMB = (state.me && state.me.maxAudioMB) || 25;
  if (file.size > maxMB * 1024 * 1024) {
    toast(`文件超过 ${maxMB}MB，请压缩或分段`, 'error');
    return;
  }
  state.audioFile = file;
  const p = $('#uploadPreview');
  p.innerHTML = `
    <div class="file-line">
      <span class="file-name">📄 ${esc(file.name)}</span>
      <span class="file-meta">${fmtBytes(file.size)} · ${esc(file.type || '未知类型')}</span>
      <button type="button" class="btn ghost small" id="clearFile">移除</button>
    </div>
    <audio controls src="${URL.createObjectURL(file)}"></audio>
  `;
  p.classList.remove('hidden');
  $('#clearFile').onclick = () => {
    state.audioFile = null;
    $('#audioFile').value = '';
    p.classList.add('hidden');
    p.innerHTML = '';
    updateSubmitTip();
  };
  updateSubmitTip();
}

// ============ 现场实时录音 ============
let recorder = null;
let recChunks = [];
let recStartTs = 0;
let recTimer = null;

async function startRecord() {
  if (!state.me || !state.me.asrReady) {
    toast('服务端未配置 ASR，无法实时录音', 'warn'); return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    // 优先 webm/opus（体积小、Whisper 支持）
    let mime = 'audio/webm;codecs=opus';
    if (!MediaRecorder.isTypeSupported(mime)) mime = 'audio/webm';
    if (!MediaRecorder.isTypeSupported(mime)) mime = '';
    recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    recChunks = [];
    recorder.ondataavailable = (e) => e.data && e.data.size && recChunks.push(e.data);
    recorder.onstop = () => {
      const type = recorder.mimeType || 'audio/webm';
      const blob = new Blob(recChunks, { type });
      state.recordedBlob = blob;
      state.recordedDuration = Math.round((Date.now() - recStartTs) / 1000);
      const url = URL.createObjectURL(blob);
      const preview = $('#recPreview');
      preview.innerHTML = `
        <div class="file-line">
          <span class="file-name">🎙️ 录音 ${fmtDur(state.recordedDuration)}</span>
          <span class="file-meta">${fmtBytes(blob.size)} · ${esc(type)}</span>
        </div>
        <audio controls src="${url}"></audio>
      `;
      preview.classList.remove('hidden');
      stream.getTracks().forEach((t) => t.stop());
      updateSubmitTip();
    };
    recorder.start();
    recStartTs = Date.now();
    $('#recStatus').textContent = '正在录音…';
    $('#btnRecStart').disabled = true;
    $('#btnRecStop').disabled = false;
    $('#btnRecReset').disabled = false;
    recTimer = setInterval(() => {
      const sec = Math.round((Date.now() - recStartTs) / 1000);
      $('#recTime').textContent = fmtDur(sec);
    }, 500);
  } catch (e) {
    toast('无法访问麦克风：' + e.message, 'error');
  }
}

function stopRecord() {
  if (!recorder || recorder.state !== 'recording') return;
  recorder.stop();
  clearInterval(recTimer);
  $('#recStatus').textContent = '录音完成';
  $('#btnRecStart').disabled = false;
  $('#btnRecStop').disabled = true;
}

function resetRecord() {
  if (recorder && recorder.state === 'recording') recorder.stop();
  clearInterval(recTimer);
  state.recordedBlob = null;
  state.recordedDuration = 0;
  $('#recStatus').textContent = '未开始';
  $('#recTime').textContent = '00:00';
  $('#btnRecStart').disabled = false;
  $('#btnRecStop').disabled = true;
  $('#btnRecReset').disabled = true;
  $('#recPreview').classList.add('hidden');
  $('#recPreview').innerHTML = '';
  updateSubmitTip();
}

// ============ 提交 ============
function updateSubmitTip() {
  const tip = $('#submitTip');
  if (state.mode === 'upload') {
    tip.textContent = state.audioFile ? '提交后进入队列，转写和分析在后台跑' : '请先选择或拖入录音文件';
  } else if (state.mode === 'record') {
    tip.textContent = state.recordedBlob ? '录音已就绪，可提交' : '还没有录音';
  } else {
    tip.textContent = '至少粘贴 50 字';
  }
}

async function submit() {
  const form = $('#newForm');
  const fd = new FormData();
  fd.append('company', form.company.value.trim());
  fd.append('role_title', form.role_title.value.trim());
  fd.append('round_name', form.round_name.value.trim());
  fd.append('interview_date', form.interview_date.value);

  if (state.mode === 'upload') {
    if (!state.audioFile) return toast('请先选择录音文件', 'warn');
    fd.append('audio', state.audioFile);
    fd.append('source', 'upload');
  } else if (state.mode === 'record') {
    if (!state.recordedBlob) return toast('请先录一段音频', 'warn');
    const ext = /webm/.test(state.recordedBlob.type) ? 'webm' : 'm4a';
    fd.append('audio', state.recordedBlob, `recording.${ext}`);
    fd.append('duration_sec', String(state.recordedDuration || ''));
    fd.append('source', 'live');
  } else {
    const text = $('#transcriptInput').value.trim();
    if (text.length < 50) return toast('转写内容太短，至少 50 字', 'warn');
    fd.append('transcript', text);
    fd.append('source', 'paste');
  }

  const btn = $('#submitBtn');
  btn.disabled = true; btn.textContent = '提交中…';
  try {
    const r = await api('/api/interviews', { method: 'POST', body: fd });
    toast('已提交，任务在后台处理', 'ok');
    // 重置表单
    form.reset();
    state.audioFile = null;
    $('#uploadPreview').classList.add('hidden');
    $('#uploadPreview').innerHTML = '';
    resetRecord();
    $('#transcriptInput').value = '';
    switchTab('list');
    // 立即打开新记录的卡片
    setTimeout(() => openDetail(r.id), 400);
  } catch (e) {
    toast('提交失败：' + e.message, 'error', 5000);
  } finally {
    btn.disabled = false; btn.textContent = '🚀 提交复盘';
  }
}

// ============ 档案库 ============
async function loadList() {
  try {
    const { items } = await api('/api/interviews');
    renderList(items);
    // 有进行中的任务就开轮询
    const running = items.some((it) => ['queued','transcribing','polishing','analyzing'].includes(it.status));
    if (running) startListPoll();
    else stopListPoll();
  } catch (e) {
    toast('加载列表失败：' + e.message, 'error');
  }
}

function startListPoll() {
  if (state.polling) return;
  state.polling = setInterval(loadList, 4000);
}
function stopListPoll() {
  if (state.polling) { clearInterval(state.polling); state.polling = null; }
}

function statusBadge(it) {
  const map = {
    queued:       ['⏳ 排队中', 'queued'],
    transcribing: ['🎧 转写中', 'progress'],
    polishing:    ['📝 整理对话稿', 'progress'],
    analyzing:    ['🤖 分析中', 'progress'],
    done:         ['✅ 已完成', 'done'],
    failed:       ['❌ 失败',   'failed'],
  };
  const [label, cls] = map[it.status] || [it.status || '', ''];
  const stage = it.stage ? `<span class="stage">${esc(it.stage)}</span>` : '';
  return `<span class="badge ${cls}">${label}</span>${stage}`;
}

function scoreDot(s) {
  if (s == null) return '';
  const cls = s >= 80 ? 'high' : s >= 60 ? 'mid' : 'low';
  return `<span class="score-dot ${cls}">${s}</span>`;
}

function outcomeBadge(o) {
  const m = { offer:['🎉 拿到 offer','offer'], passed:['👍 已过',
'passed'], rejected:['❌ 被拒','rejected'], pending:['⏱ 等结果','pending'] };
  const [label, cls] = m[o] || m.pending;
  return `<span class="outcome ${cls}">${label}</span>`;
}

function renderList(items) {
  const box = $('#cardList');
  $('#listEmpty').classList.toggle('hidden', items.length > 0);
  box.innerHTML = items.map((it) => `
    <div class="review-card" data-id="${it.id}">
      <div class="card-head">
        <div class="card-title">
          <b>${esc(it.company || '未命名公司')}</b>
          <span class="muted"> · ${esc(it.roleTitle || '未填岗位')}</span>
          ${it.roundName ? `<span class="tag">${esc(it.roundName)}</span>` : ''}
        </div>
        <div class="card-right">
          ${scoreDot(it.overallScore)}
          ${outcomeBadge(it.outcome)}
        </div>
      </div>
      <div class="card-meta">
        <span>${esc(it.interviewDate || it.createdAt?.slice(0,10) || '')}</span>
        ${it.hasAudio ? `<span>· 🎧 ${esc(it.audioName || '录音')}</span>` : ''}
        ${it.durationSec ? `<span>· ⏱ ${fmtDur(it.durationSec)}</span>` : ''}
      </div>
      <div class="card-status">${statusBadge(it)}</div>
      ${it.verdict ? `<div class="verdict">${esc(it.verdict)}</div>` : ''}
      ${it.errorMsg ? `<div class="err">⚠️ ${esc(it.errorMsg)}</div>` : ''}
      ${it.weakestTopics?.length ? `<div class="weak">薄弱：${it.weakestTopics.map(esc).join('、')}</div>` : ''}
      <div class="card-actions">
        <button class="btn small" data-act="open">查看报告</button>
        ${it.status === 'failed' ? `<button class="btn small ghost" data-act="retry">重试</button>` : ''}
        <button class="btn small danger" data-act="del">删除</button>
      </div>
    </div>
  `).join('');

  box.querySelectorAll('.review-card').forEach((el) => {
    const id = +el.dataset.id;
    el.querySelector('[data-act="open"]').onclick = () => openDetail(id);
    const retry = el.querySelector('[data-act="retry"]');
    if (retry) retry.onclick = () => retryAnalyze(id);
    el.querySelector('[data-act="del"]').onclick = () => delItem(id);
  });
}

async function delItem(id) {
  if (!confirm('确定删除？连同录音一起删除，无法恢复。')) return;
  try { await api('/api/interviews/' + id, { method: 'DELETE' }); toast('已删除','ok'); loadList(); }
  catch (e) { toast('删除失败：' + e.message, 'error'); }
}
async function retryAnalyze(id) {
  try { await api('/api/interviews/' + id + '/reanalyze', { method: 'POST' }); toast('已重新排队','ok'); loadList(); }
  catch (e) { toast('重试失败：' + e.message, 'error'); }
}

// ============ 报告详情 ============
async function openDetail(id) {
  state.currentDetailId = id;
  const drawer = $('#drawer');
  drawer.classList.remove('hidden');
  $('#drawerContent').innerHTML = '<div class="loading">加载中…</div>';
  await refreshDetail();
}

function closeDrawer() {
  $('#drawer').classList.add('hidden');
  state.currentDetailId = null;
  if (state._detailPoll) { clearInterval(state._detailPoll); state._detailPoll = null; }
}

async function refreshDetail() {
  const id = state.currentDetailId;
  if (!id) return;
  try {
    const d = await api('/api/interviews/' + id);
    $('#drawerContent').innerHTML = renderDetail(d);
    bindDetail(d);
    // 未完成就轮询
    if (['queued','transcribing','polishing','analyzing'].includes(d.status)) {
      if (!state._detailPoll) state._detailPoll = setInterval(refreshDetail, 3000);
    } else {
      if (state._detailPoll) { clearInterval(state._detailPoll); state._detailPoll = null; }
    }
  } catch (e) {
    $('#drawerContent').innerHTML = `<div class="err">加载失败：${esc(e.message)}</div>`;
  }
}

function renderDetail(d) {
  const a = d.analysis || {};
  const running = ['queued','transcribing','polishing','analyzing'].includes(d.status);
  if (running) {
    return `
      <h2>${esc(d.company || '未命名')} · ${esc(d.roleTitle || '未填岗位')}${d.roundName ? ' · ' + esc(d.roundName) : ''}</h2>
      <div class="progress-box">
        <div class="progress-spin"></div>
        <div class="progress-label">${statusBadge(d)}</div>
        <div class="tip">页面会自动刷新，处理完成后即可看到报告。</div>
      </div>
    `;
  }
  if (d.status === 'failed') {
    return `
      <h2>${esc(d.company || '未命名')}</h2>
      <div class="err">❌ 处理失败：${esc(d.errorMsg || '未知错误')}</div>
      <button class="btn primary" id="btnRetry">重试</button>
    `;
  }
  // 已完成
  const qa = (d.qaItems || []).map((q) => `
    <div class="qa">
      <div class="qa-head">
        <span class="qa-seq">#${q.seq}</span>
        <span class="qa-cat">${esc(q.category || '其他')}</span>
        <span class="qa-score ${(q.score||0) >= 8 ? 'high' : (q.score||0) >= 6 ? 'mid' : 'low'}">${q.score ?? '-'}/10</span>
      </div>
      <div class="qa-q"><b>问：</b>${esc(q.question || '')}</div>
      ${q.answer_digest ? `<div class="qa-a"><b>我：</b>${esc(q.answer_digest)}</div>` : ''}
      ${q.strengths ? `<div class="qa-s"><b>✅ 亮点：</b>${esc(q.strengths)}</div>` : ''}
      ${q.issues ? `<div class="qa-i"><b>⚠️ 问题：</b>${esc(q.issues)}</div>` : ''}
      ${q.better_answer ? `<details><summary>👉 更优答案示范</summary><div class="qa-better">${esc(q.better_answer)}</div></details>` : ''}
    </div>
  `).join('');

  const habits = a.speech_habits || {};
  const focus = a.interviewer_focus || {};
  const progress = a.progress;

  const outcomes = [['pending','等结果'],['passed','过了'],['offer','拿到 offer'],['rejected','被拒']];

  return `
    <h2>${esc(d.company || '未命名')} · ${esc(d.roleTitle || '未填岗位')}${d.roundName ? ' · ' + esc(d.roundName) : ''}</h2>
    <div class="detail-meta">
      ${d.interviewDate ? `<span>📅 ${esc(d.interviewDate)}</span>` : ''}
      ${d.hasAudio ? `<span>· 🎧 <a href="/api/interviews/${d.id}/audio" target="_blank">录音</a></span>` : ''}
      <span class="spacer"></span>
      <label>结果：
        <select id="outcomeSel">
          ${outcomes.map(([v,l]) => `<option value="${v}" ${d.outcome===v?'selected':''}>${l}</option>`).join('')}
        </select>
      </label>
    </div>

    <div class="overall">
      <div class="score-big ${d.overallScore>=80?'high':d.overallScore>=60?'mid':'low'}">${d.overallScore ?? '-'}</div>
      <div class="verdict-big">
        <div class="verdict-txt">${esc(a.verdict || '')}</div>
        <div class="verdict-sub">通过可能：<b>${esc(a.pass_likelihood || '-')}</b></div>
      </div>
    </div>

    ${a.summary ? `<div class="block"><h3>📋 总体复盘</h3><p>${esc(a.summary)}</p></div>` : ''}

    ${a.weakest_topics?.length ? `<div class="block"><h3>🎯 薄弱主题</h3><div class="tags">${a.weakest_topics.map(t=>`<span class="tag">${esc(t)}</span>`).join('')}</div></div>` : ''}

    <div class="block"><h3>❓ 逐题诊断（${(d.qaItems||[]).length} 题）</h3>${qa || '<p class="muted">无</p>'}</div>

    ${(habits.top_fixes?.length || habits.filler_words?.length) ? `
      <div class="block"><h3>🗣️ 表达习惯诊断</h3>
        ${habits.pace ? `<p><b>节奏：</b>${esc(habits.pace)}</p>` : ''}
        ${habits.verbosity ? `<p><b>啰嗦度：</b>${esc(habits.verbosity)}</p>` : ''}
        ${habits.structure ? `<p><b>结构：</b>${esc(habits.structure)}</p>` : ''}
        ${habits.filler_words?.length ? `<p><b>口头禅：</b>${habits.filler_words.map(f=>`「${esc(f.word)}」× ${f.count}`).join('，')}</p>` : ''}
        ${habits.top_fixes?.length ? `<ul>${habits.top_fixes.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>` : ''}
      </div>` : ''}

    ${(focus.signals?.length || focus.unmet_expectations?.length) ? `
      <div class="block"><h3>🔍 面试官关注点</h3>
        ${focus.signals?.length ? `<p><b>他真正在意：</b></p><ul>${focus.signals.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>` : ''}
        ${focus.unmet_expectations?.length ? `<p><b>没听到但想听：</b></p><ul>${focus.unmet_expectations.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>` : ''}
        ${focus.next_round_prep?.length ? `<p><b>下轮该准备：</b></p><ul>${focus.next_round_prep.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>` : ''}
      </div>` : ''}

    ${a.action_items?.length ? `<div class="block"><h3>✅ 行动清单</h3><ul>${a.action_items.map(x=>`<li>${esc(x)}</li>`).join('')}</ul></div>` : ''}

    ${progress ? `
      <div class="block"><h3>📈 跨场次进步追踪</h3>
        ${progress.trend ? `<p><b>整体趋势：</b>${esc(progress.trend)}</p>` : ''}
        ${progress.improved?.length ? `<p><b>进步：</b></p><ul>${progress.improved.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>` : ''}
        ${progress.regressed?.length ? `<p><b>退步：</b></p><ul>${progress.regressed.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>` : ''}
        ${progress.recurring_issues?.length ? `<p><b>反复出现：</b></p><ul>${progress.recurring_issues.map(x=>`<li>${esc(x.issue)}（${x.times}次）— ${esc(x.verdict)}</li>`).join('')}</ul>` : ''}
      </div>` : ''}

    ${d.transcript ? `<details class="block"><summary><b>📄 展开转写全文</b></summary><pre class="transcript">${esc(d.transcript)}</pre></details>` : ''}
  `;
}

function bindDetail(d) {
  const sel = document.getElementById('outcomeSel');
  if (sel) sel.onchange = async () => {
    try {
      const fd = new FormData(); fd.append('outcome', sel.value);
      await api('/api/interviews/' + d.id + '/outcome', { method: 'POST', body: fd });
      toast('已更新结果', 'ok'); loadList();
    } catch (e) { toast('更新失败：' + e.message, 'error'); }
  };
  const retry = document.getElementById('btnRetry');
  if (retry) retry.onclick = () => retryAnalyze(d.id).then(refreshDetail);
}

// ============ 成长趋势 ============
async function loadTrend() {
  try {
    const t = await api('/api/trend');
    renderTrend(t);
  } catch (e) {
    toast('加载趋势失败：' + e.message, 'error');
  }
}

function renderTrend(t) {
  $('#trendEmpty').classList.toggle('hidden', t.series.length > 0);
  // 简易 SVG 折线
  const box = $('#trendChart');
  if (t.series.length === 0) { box.innerHTML = ''; return; }
  const w = 720, h = 260, pad = 34;
  const xs = t.series.map((_,i) => pad + i * (w - 2*pad) / Math.max(1, t.series.length - 1));
  const ys = t.series.map(s => h - pad - (s.score || 0) * (h - 2*pad) / 100);
  const pts = xs.map((x,i) => `${x.toFixed(1)},${ys[i].toFixed(1)}`).join(' ');
  const grid = [0, 25, 50, 75, 100].map(v => {
    const y = h - pad - v * (h - 2*pad) / 100;
    return `<line x1="${pad}" x2="${w-pad}" y1="${y}" y2="${y}" stroke="#eee" /><text x="4" y="${y+4}" font-size="10" fill="#999">${v}</text>`;
  }).join('');
  const dots = t.series.map((s, i) => `
    <circle cx="${xs[i]}" cy="${ys[i]}" r="5" fill="${s.score>=80?'#22c55e':s.score>=60?'#f59e0b':'#ef4444'}">
      <title>${s.label} ${s.date} · ${s.score}分</title>
    </circle>
    <text x="${xs[i]}" y="${h-14}" font-size="10" fill="#666" text-anchor="middle">${s.date.slice(5)}</text>
  `).join('');
  box.innerHTML = `<svg viewBox="0 0 ${w} ${h}" width="100%" style="max-width:${w}px">
    ${grid}
    <polyline fill="none" stroke="#4f46e5" stroke-width="2" points="${pts}" />
    ${dots}
  </svg>`;

  $('#catList').innerHTML = t.categories.length ? t.categories.map(c => `
    <div class="cat-row">
      <span class="cat-name">${esc(c.category)}</span>
      <span class="cat-bar"><span style="width:${(c.avgScore||0)*10}%; background:${c.avgScore>=8?'#22c55e':c.avgScore>=6?'#f59e0b':'#ef4444'}"></span></span>
      <span class="cat-score">${c.avgScore ?? '-'}/10 · ${c.count}次</span>
    </div>
  `).join('') : '<p class="muted">暂无数据</p>';

  $('#weakList').innerHTML = t.weakQuestions.length ? t.weakQuestions.map(w => `
    <div class="weak-row" data-id="${w.interviewId}">
      <div><span class="tag">${esc(w.category)}</span> <b>${w.score}/10</b> · ${esc(w.company || '')} ${esc(w.date || '')}</div>
      <div class="muted">${esc(w.question || '')}</div>
    </div>
  `).join('') : '<p class="muted">暂无薄弱题</p>';
  $$('#weakList .weak-row').forEach(r => r.onclick = () => openDetail(+r.dataset.id));
}

init();
