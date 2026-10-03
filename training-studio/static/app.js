'use strict';
// 职业培训分镜工作台 —— 纯前端调用 REST API。所有写操作携带 expected_rev（版本条件）。
const state = { users: [], userId: null, teamId: null, courses: [], course: null,
  assets: [], tab: 'outline', quota: null };

async function api(method, path, body) {
  const opt = { method, headers: { 'Content-Type': 'application/json' } };
  if (state.userId) opt.headers['X-User-Id'] = state.userId;
  if (body !== undefined) opt.body = JSON.stringify(body);
  const res = await fetch(path, opt);
  let data = {};
  try { data = await res.json(); } catch (e) {}
  if (!res.ok) {
    const err = new Error(data.detail || (data.error || ('HTTP ' + res.status)));
    err.status = res.status; err.data = data;
    throw err;
  }
  return data;
}
function toast(msg, kind = 'info') {
  const box = document.getElementById('toast');
  const d = document.createElement('div');
  d.className = kind; d.textContent = msg;
  box.appendChild(d);
  setTimeout(() => d.remove(), 3600);
}
function esc(s) { return String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function kindTag(k) { return { lecture: '讲师台词', demo: '操作演示', exercise: '练习提示' }[k] || k; }

// ---------- 启动 ----------
async function boot() {
  // 演示环境引导：若无团队则创建一个并建四个演示账号
  let me = null;
  try {
    // 每次启动重置演示团队（仅当库为空）。为保留数据，先尝试已有 id=1
    state.teamId = 1;
    await api('GET', `/teams/${state.teamId}/users`);
  } catch (e) {
    const t = await api('POST', '/teams', { name: '职业培训部' });
    state.teamId = t.id;
  }
  const seed = [['管理员', 'admin'], ['老师甲', 'editor'], ['老师乙', 'editor'], ['观察者', 'viewer']];
  const existing = await api('GET', `/teams/${state.teamId}/users`).catch(() => ({ users: [] }));
  for (const [name, role] of seed) {
    const found = (existing.users || []).find(u => u.name === name);
    if (found) state.users.push(found);
    else {
      const u = await api('POST', `/teams/${state.teamId}/users`, { name, role });
      state.users.push(u.users);
    }
  }
  state.userId = Number(localStorage.getItem('uid')) || state.users.find(u => u.role === 'editor').id;
  const sel = document.getElementById('userSel');
  sel.innerHTML = state.users.map(u =>
    `<option value="${u.id}" ${u.id === state.userId ? 'selected' : ''}>${esc(u.name)}（${u.role}${u.active ? '' : '·已离职'}）</option>`).join('');
  sel.onchange = async () => {
    state.userId = Number(sel.value); localStorage.setItem('uid', state.userId);
    await reloadAll();
  };
  await reloadAll();
}

async function reloadAll() {
  await Promise.all([loadCourses(), loadAssets(), loadQuota()]);
  renderCourseList(); renderAssets();
  if (state.course) await openCourse(state.course.course.id, true);
  else document.getElementById('main').innerHTML = '<div class="card muted">请选择或新建一门课程。</div>';
}
async function loadQuota() {
  state.quota = await api('GET', `/teams/${state.teamId}/quota`).catch(() => null);
}
async function loadCourses() {
  const r = await api('GET', `/teams/${state.teamId}/courses`).catch(() => ({ courses: [] }));
  state.courses = r.courses || [];
}
async function loadAssets() {
  const r = await api('GET', `/teams/${state.teamId}/assets`).catch(() => ({ assets: [] }));
  state.assets = r.assets || [];
}
function activeUser() { return state.users.find(u => u.id === state.userId); }
function canWrite() { const u = activeUser(); return u && u.active && u.role !== 'viewer'; }

// ---------- 课程列表 ----------
function renderCourseList() {
  document.getElementById('courseList').innerHTML = state.courses.length
    ? state.courses.map(c =>
      `<div class="course-item ${state.course && state.course.course.id === c.id ? 'active' : ''}" onclick="openCourse(${c.id})">
         <span>📘 ${esc(c.title)}</span><span class="tag">${esc(c.status)}</span></div>`).join('')
    : '<div class="muted">暂无课程</div>';
}
async function newCourse() {
  const title = prompt('课程名称'); if (!title) return;
  if (!canWrite()) return toast('当前身份无编辑权限', 'err');
  const r = await api('POST', '/courses', { team_id: state.teamId, title });
  await openCourse(r.course.id);
}

// ---------- 课程主视图 ----------
async function openCourse(cid, keepTab) {
  state.course = await api('GET', `/courses/${cid}`);
  renderCourseList();
  if (!keepTab) state.tab = 'outline';
  renderMain();
}
function renderMain() {
  const c = state.course; if (!c) return;
  const co = c.course;
  const tabs = [['outline', '分镜大纲'], ['coverage', '目标覆盖'], ['freeze', '开班与冻结'],
    ['export', '课件包 / 导出'], ['history', '历史与撤销']];
  document.getElementById('main').innerHTML = `
   <div class="card">
     <div class="row">
       <input id="courseTitle" value="${esc(co.title)}" style="font-weight:650;flex:1;min-width:220px"
         ${canWrite() ? '' : 'disabled'} />
       <span class="tag">版本 rev${co.head_rev}</span>
       <span class="tag ${co.status === 'published' ? 'ok' : 'warn'}">${esc(co.status)}</span>
       <button class="small" onclick="saveCourseTitle()">保存标题</button>
       <button class="small ghost" onclick="addObjective()">＋ 训练目标</button>
     </div>
     <div class="muted" style="margin-top:6px">导出额度剩余：<b>${state.quota ? state.quota.export_remaining : '-'}</b> 次 ·
       目标 ${c.objectives.length} · 章节 ${c.chapters.length} · 考核点 ${c.assessment_points.length}</div>
   </div>
   <div class="tabs">${tabs.map(([k, n]) => `<button class="${state.tab === k ? 'on' : ''}" onclick="setTab('${k}')">${n}</button>`).join('')}</div>
   <div id="tabBody"></div>`;
  ({ outline: renderOutline, coverage: renderCoverage, freeze: renderFreeze,
     export: renderExport, history: renderHistory }[state.tab])();
}
function setTab(t) { state.tab = t; renderMain(); }
async function saveCourseTitle() {
  if (!guardWrite()) return;
  const v = document.getElementById('courseTitle').value;
  try {
    await api('PUT', `/courses/${state.course.course.id}`,
      { title: v, expected_rev: state.course.course.head_rev });
    toast('标题已保存', 'ok'); await reloadAll();
  } catch (e) { toast(e.message, 'err'); }
}
function guardWrite() {
  if (!canWrite()) { toast('当前身份（' + activeUser().name + '）无编辑权限：只读模式', 'err'); return false; }
  return true;
}

// ---------- 目标 ----------
async function addObjective() {
  if (!guardWrite()) return;
  const code = prompt('目标编号（如 OBJ-1）'); if (!code) return;
  const title = prompt('目标描述'); if (!title) return;
  try {
    await api('POST', `/courses/${state.course.course.id}/objectives`, { code, title });
    toast('已新增训练目标', 'ok'); await reloadAll();
  } catch (e) { toast(e.message, 'err'); }
}
async function renameObjective(oid, rev, oldTitle) {
  if (!guardWrite()) return;
  const title = prompt('修改目标描述（训练目标改版）', oldTitle); if (title === null) return;
  try {
    await api('PUT', `/objectives/${oid}`, { title, expected_rev: rev });
    toast('目标已改版；覆盖报告会按当前版本重新计算', 'ok'); await reloadAll();
  } catch (e) { toast(e.message, 'err'); }
}

// ---------- 大纲（章节 / 片段 / 考核点） ----------
function renderOutline() {
  const c = state.course; const cid = c.course.id;
  const objOpts = c.objectives.map(o => `<option value="${o.id}">${esc(o.code)} ${esc(o.title)}</option>`).join('');
  const chapters = c.chapters.map((ch, idx) => {
    const segs = ch.segments.map(s => segHtml(s)).join('') || '<div class="muted">暂无片段</div>';
    return `<div class="chapter">
      <div class="hd">
        <b>第${idx + 1}章</b>
        <input value="${esc(ch.title)}" data-ch="${ch.id}" style="flex:1;min-width:120px" ${canWrite() ? '' : 'disabled'} id="cht${ch.id}"/>
        <span class="smalltxt">rev${ch.head_rev}</span>
        <span class="smalltxt">前置：${(ch.requires || []).map(r => '#' + r).join('、') || '无'}</span>
        <button class="small" onclick="saveChapter(${ch.id})">存</button>
        <button class="small ghost" onclick="editRequires(${ch.id})">前置</button>
        <button class="small ghost" onclick="moveChapter(${ch.id},-1)">↑</button>
        <button class="small ghost" onclick="moveChapter(${ch.id},1)">↓</button>
        <button class="small danger" onclick="delChapter(${ch.id})">删章</button>
      </div>
      <div class="bd">
        ${segs}
        <div class="row" style="margin-top:8px">
          <button class="small ghost" onclick="addSeg(${ch.id},'lecture')">＋ 讲师台词</button>
          <button class="small ghost" onclick="addSeg(${ch.id},'demo')">＋ 操作演示</button>
          <button class="small ghost" onclick="addSeg(${ch.id},'exercise')">＋ 练习提示</button>
        </div>
      </div>
    </div>`;
  }).join('') || '<div class="muted">还没有章节</div>';

  document.getElementById('tabBody').innerHTML = `
   <div class="grid2">
     <div class="card">
       <h2>分镜结构
        <button class="small ghost" onclick="addChapter()">＋ 新章节</button>
        <span class="sp" style="flex:1"></span>
        <button class="small" onclick="runValidation()">🔄 重新验证</button>
       </h2>
       ${chapters}
     </div>
     <div>
       <div class="card">
         <h2>训练目标（考核点须引用支撑教学片段）</h2>
         ${c.objectives.length ? `<table><tr><th>编号</th><th>目标</th><th></th></tr>${
           c.objectives.map(o => `<tr><td>${esc(o.code)}</td><td>${esc(o.title)}</td>
             <td><button class="small ghost" onclick="renameObjective(${o.id},${o.head_rev || 1},${JSON.stringify(o.title).replace(/"/g, '&quot;')})">改版</button></td></tr>`).join('')
         }</table>` : '<div class="muted">先添加训练目标</div>'}
       </div>
       <div class="card">
        <h2>校验结果（前置关系 / 素材一致性）</h2>
        <div id="validationBox" class="muted">点击“重新验证”或任意结构写操作后自动刷新。</div>
       </div>
     </div>
   </div>`;
  runValidation();
}

function segHtml(s) {
  const av = s.asset_version;
  const tcs = (s.timecodes || []).map(t => `<span class="tag warn">${esc(t.label)}@${t.at_ms}ms</span>`).join(' ');
  return `<div class="seg ${s.kind}">
    <div class="row">
      <span class="tag ${s.kind}">${kindTag(s.kind)}</span>
      <input id="st${s.id}" value="${esc(s.title)}" style="flex:1;min-width:120px" ${canWrite() ? '' : 'disabled'}/>
      <span class="tag ${s.status === 'ok' ? 'ok' : 'bad'}">${s.status === 'ok' ? '正常' : '待修复'}</span>
      <span class="smalltxt">rev${s.head_rev}</span>
      <button class="small" onclick="saveSeg(${s.id})">存</button>
      ${s.kind === 'demo' ? `<button class="small ghost" onclick="openBind(${s.id})">素材/时间码</button>` : ''}
      <button class="small danger" onclick="delSeg(${s.id})">删</button>
    </div>
    <textarea id="ss${s.id}" placeholder="${s.kind === 'exercise' ? '练习提示…' : '讲师台词…'}" ${canWrite() ? '' : 'disabled'}>${esc(s.script)}</textarea>
    ${av ? `<div class="smalltxt" style="margin-top:4px">绑定素材版本 <code>#${av.id} v${av.version}</code>
       画面 ${av.video_uri ? '✓' : '✗'} · 字幕 ${av.subtitle_uri ? '✓' : '✗'} · 说明 ${av.notes_uri ? '✓' : '✗'}
       · 音轨 ${av.has_audio ? '✓' : '✗缺失'} · 时长 ${av.duration_ms}ms</div>` : ''}
    ${tcs ? `<div class="row" style="margin-top:4px">${tcs}</div>` : ''}
  </div>`;
}

async function addChapter() {
  if (!guardWrite()) return;
  const title = prompt('章节标题'); if (!title) return;
  await wrapErr(async () => {
    await api('POST', `/courses/${state.course.course.id}/chapters`, { title, requires: [] });
    toast('章节已新增', 'ok'); await reloadAll();
  });
}
async function saveChapter(chid) {
  if (!guardWrite()) return;
  const ch = state.course.chapters.find(x => x.id === chid);
  const title = document.getElementById('cht' + chid).value;
  await wrapErr(async () => {
    await api('PUT', `/chapters/${chid}`, { title, requires: ch.requires, expected_rev: ch.head_rev });
    toast('章节已保存', 'ok'); await reloadAll();
  });
}
async function editRequires(chid) {
  const ch = state.course.chapters.find(x => x.id === chid);
  const others = state.course.chapters.filter(x => x.id !== chid).map(x => `#${x.id} ${x.title}`).join('\n');
  const cur = (ch.requires || []).join(',');
  const v = prompt('前置章节 id（逗号分隔，须排在本章之前）。\n可选：\n' + others, cur);
  if (v === null) return;
  const requires = v.split(',').map(x => parseInt(x.trim(), 10)).filter(Boolean);
  await wrapErr(async () => {
    await api('PUT', `/chapters/${chid}`, { requires, expected_rev: ch.head_rev, title: ch.title });
    toast('前置关系已更新，已自动重新验证', 'ok'); await reloadAll();
  });
}
async function moveChapter(chid, dir) {
  if (!guardWrite()) return;
  const ids = state.course.chapters.slice().sort((a, b) =>
    a.position - b.position || a.id - b.id).map(x => x.id);
  const i = ids.indexOf(chid); const j = i + dir;
  if (j < 0 || j >= ids.length) return;
  [ids[i], ids[j]] = [ids[j], ids[i]];
  await wrapErr(async () => {
    const r = await api('POST', `/courses/${state.course.course.id}/chapters/reorder`, { order: ids });
    state.course.chapters = r.chapters;
    renderValidation(r.validation); toast('已重排并重新验证知识前置关系（非仅更新总分钟数）', 'ok');
    state.tab = 'outline'; renderMain();
  });
}
async function delChapter(chid) {
  if (!guardWrite()) return;
  const ch = state.course.chapters.find(x => x.id === chid);
  if (!confirm(`删除章节《${ch.title}》？其下片段与考核点引用会一并处理，前置关系将重新验证。`)) return;
  await wrapErr(async () => {
    await api('DELETE', `/chapters/${chid}?expected_rev=${ch.head_rev}`);
    toast('章节已删除，前置关系已重验证', 'ok');
    await openCourse(state.course.course.id, true);  // renderMain 内会自动拉取最新 validation
  });
}

async function addSeg(chid, kind) {
  if (!guardWrite()) return;
  const title = prompt(kindTag(kind) + ' 片段标题'); if (!title) return;
  await wrapErr(async () => {
    await api('POST', `/chapters/${chid}/segments`, { kind, title, script: '' });
    toast('片段已新增', 'ok'); await reloadAll();
  });
}
async function saveSeg(sid) {
  if (!guardWrite()) return;
  const s = findSeg(sid);
  const title = document.getElementById('st' + sid).value;
  const script = document.getElementById('ss' + sid).value;
  await wrapErr(async () => {
    await api('PUT', `/segments/${sid}`, { title, script, expected_rev: s.head_rev });
    toast('已保存', 'ok'); await reloadAll();
  });
}
async function delSeg(sid) {
  if (!guardWrite()) return;
  const s = findSeg(sid);
  if (!confirm(`删除片段《${s.title}》？引用它的考核点会变为悬空，覆盖报告会更新。`)) return;
  await wrapErr(async () => {
    const r = await api('DELETE', `/segments/${sid}?expected_rev=${s.head_rev}`);
    toast('片段已删除', 'ok');
    await openCourse(state.course.course.id, true);
    renderValidation(r.validation);
    if (state.tab === 'coverage') renderCoverage();
  });
}
function findSeg(sid) {
  for (const ch of state.course.chapters) { const f = ch.segments.find(x => x.id === sid); if (f) return f; }
  return null;
}

// ---------- 素材绑定 / 时间码 ----------
function renderAssets() {
  document.getElementById('assetList').innerHTML = state.assets.map(a =>
    `<div style="margin:6px 0"><b>🎞 ${esc(a.title)}</b>
      <div class="smalltxt">${(a.versions || []).map(v =>
        `v${v.version} #${v.id}（${v.duration_ms}ms，音轨${v.has_audio ? '有' : '缺'}）`).join(' · ') || '暂无版本'}</div></div>`).join('');
}
async function newAsset() {
  if (!guardWrite()) return;
  const title = document.getElementById('assetTitle').value.trim();
  if (!title) return toast('请输入素材名', 'err');
  const r = await api('POST', `/teams/${state.teamId}/assets`, { title });
  const a = await api('GET', `/teams/${state.teamId}/assets`); state.assets = a.assets;
  await addAssetVersion(r.id);
  document.getElementById('assetTitle').value = '';
  renderAssets();
}
async function addAssetVersion(aid) {
  const duration = parseInt(prompt('素材时长（毫秒）', '60000'), 10);
  if (!duration) return;
  const hasAudio = confirm('该版本是否包含音轨？\n确定=有音轨，取消=音轨缺失（将被标记 audio_missing）');
  await api('POST', `/assets/${aid}/versions`, {
    video_uri: `v/${aid}/${Date.now()}.mp4`, subtitle_uri: `v/${aid}/${Date.now()}.srt`,
    notes_uri: `v/${aid}/${Date.now()}.md`, duration_ms: duration, has_audio: hasAudio });
  toast('素材版本已创建（画面/字幕/说明同源）', 'ok');
  await loadAssets(); renderAssets();
}
async function openBind(sid) {
  const s = findSeg(sid);
  const versions = [];
  state.assets.forEach(a => (a.versions || []).forEach(v => versions.push({ a, v })));
  if (!versions.length) return toast('请先在左侧素材库创建素材版本', 'err');
  const opts = versions.map(({ a, v }) =>
    `<option value="${v.id}" ${s.asset_version_id === v.id ? 'selected' : ''}>
      ${esc(a.title)} v${v.version} #${v.id} · ${v.duration_ms}ms · 音轨${v.has_audio ? '有' : '缺'}</option>`).join('');
  const tcRows = (s.timecodes || []).map((t, i) =>
    `<tr><td><input id="tcl${sid}_${i}" value="${esc(t.label)}"/></td>
     <td><input id="tct${sid}_${i}" type="number" value="${t.at_ms}"/></td>
     <td><button class="small danger" onclick="this.closest('tr').remove()">删</button></td></tr>`).join('');
  showModal(`<h2 style="margin-top:0">绑定素材版本与时间码 — ${esc(s.title)}</h2>
    <p class="muted">画面、字幕、说明必须来自<b>同一素材版本</b>。替换为更短版本后，超界时间码会自动进入“待修复”。</p>
    <select id="avSel" style="width:100%">${opts}</select>
    <h3>时间码（超出素材时长即待修复）</h3>
    <table id="tcTable"><tr><th>标注</th><th>at_ms</th><th></th></tr>${tcRows}</table>
    <button class="small ghost" onclick="addTcRow()">＋ 时间码</button>
    <div class="row" style="margin-top:14px;justify-content:flex-end">
      <button class="ghost" onclick="closeModal()">取消</button>
      <button onclick="submitBind(${sid})">保存绑定</button>
    </div>`);
  window.__sid = sid;
}
function addTcRow() {
  document.getElementById('tcTable').insertAdjacentHTML('beforeend',
    '<tr><td><input class="tcl2" placeholder="标注"/></td><td><input class="tct2" type="number" value="0"/></td>'
    + '<td><button class="small danger" onclick="this.closest(\'tr\').remove()">删</button></td></tr>');
}
async function submitBind(sid) {
  if (!guardWrite()) return;
  const s = findSeg(sid);
  const asset_version_id = Number(document.getElementById('avSel').value);
  let rev = s.head_rev;
  await wrapErr(async () => {
    if (asset_version_id !== (s.asset_version_id || null)) {
      const r = await api('POST', `/segments/${sid}/bind_asset`, { asset_version_id, expected_rev: rev });
      rev = r.segment.head_rev; s.status = r.segment.status;
      flashValidation(r.validation);
    }
    const tcs = [];
    document.querySelectorAll('#tcTable tr').forEach((tr, i) => {
      if (i === 0) return;
      const ins = tr.querySelectorAll('input');
      if (ins.length === 2) tcs.push({ label: ins[0].value, at_ms: Number(ins[1].value) });
    });
    const r2 = await api('POST', `/segments/${sid}/timecodes`, { timecodes: tcs, expected_rev: rev });
    closeModal();
    if (r2.segment.status === 'pending_repair') toast('片段已进入待修复：存在超界时间码', 'err');
    else toast('绑定已保存', 'ok');
    await reloadAll();
  });
}

// ---------- 校验 ----------
async function runValidation() {
  try {
    const r = await api('GET', `/courses/${state.course.course.id}/validation`);
    renderValidation(r.issues);
  } catch (e) { /* ignore */ }
}
function flashValidation(issues) { renderValidation(issues); }
const ISSUE_INFO = {
  prerequisite_missing: ['error', '前置章节已删除（知识前置悬空）'],
  prerequisite_order: ['error', '章节排在其前置章节之前'],
  assessment_orphan: ['error', '考核点引用的教学片段已删除'],
  objective_missing: ['error', '考核点引用的目标不存在'],
  segment_pending_repair: ['error', '片段处于待修复状态'],
  demo_asset_missing: ['error', '操作演示未绑定素材版本'],
  video_missing: ['error', '缺少画面'], subtitle_missing: ['error', '缺少字幕'],
  notes_missing: ['error', '缺少说明'],
  timecode_out_of_range: ['error', '时间码超出素材时长'],
  audio_missing: ['warn', '素材音轨缺失（可导出的告警）'],
};
function renderValidation(issues) {
  const box = document.getElementById('validationBox');
  if (!box) return;
  if (!issues || !issues.length) {
    box.innerHTML = '<div class="issue ok">✓ 前置关系、素材版本与时间码全部通过</div>'; return;
  }
  box.innerHTML = issues.map(i => {
    const [sev, label] = ISSUE_INFO[i.code] || ['error', i.code];
    return `<div class="issue ${sev}"><b>${esc(label)}</b><span class="smalltxt">${esc(i.message || '')}</span></div>`;
  }).join('');
}

// ---------- 覆盖报告 ----------
async function renderCoverage() {
  const r = await api('GET', `/courses/${state.course.course.id}/coverage`);
  const cov = r.coverage;
  const objName = id => { const o = state.course.objectives.find(x => x.id === id); return o ? `${o.code} ${o.title}` : '#' + id; };
  document.getElementById('tabBody').innerHTML = `
   <div class="grid2">
     <div class="card"><h2>未覆盖的训练目标（当前版本）</h2>
       ${cov.uncovered.length ? '<table><tr><th>编号</th><th>目标</th></tr>' +
          cov.uncovered.map(o => `<tr><td>${esc(o.code)}</td><td>${esc(o.title)}</td></tr>`).join('') + '</table>'
        : '<div class="issue ok">✓ 所有训练目标均被考核点覆盖</div>'}
       <div class="muted" style="margin-top:8px">训练目标改版、删除支撑演示后，本报告即时反映当前版本的覆盖缺口。</div>
     </div>
     <div class="card">
       <h2>考核点 → 支撑教学片段</h2>
       ${state.course.assessment_points.length ? '<table><tr><th>目标</th><th>支撑片段</th><th>备注</th></tr>' +
         state.course.assessment_points.map(ap => {
           const seg = findSeg(ap.segment_id);
           return `<tr><td>${esc(objName(ap.objective_id))}</td>
             <td>${seg ? esc(seg.title) : `<span class="tag bad">片段#${ap.segment_id} 已删除</span>`}</td>
             <td>${esc(ap.note)}</td></tr>`;
         }).join('') + '</table>' : '<div class="muted">暂无考核点</div>'}
       <div class="row" style="margin-top:10px">
         <select id="apObj">${state.course.objectives.map(o => `<option value="${o.id}">${esc(o.code)}</option>`).join('')}</select>
         <select id="apSeg">${state.course.chapters.flatMap(c => c.segments).map(s => `<option value="${s.id}">${esc(s.title)}</option>`).join('')}</select>
         <input id="apNote" placeholder="考核备注（如：跟做一遍）"/>
         <button class="small" onclick="addAssessment()">＋ 建立考核引用</button>
       </div>
       ${cov.broken_assessments.length ? `<div class="issue error" style="margin-top:8px">有 ${cov.broken_assessments.length} 个考核点的支撑片段已删除（悬空）</div>` : ''}
     </div>
   </div>`;
}
async function addAssessment() {
  if (!guardWrite()) return;
  const objective_id = Number(document.getElementById('apObj').value);
  const segment_id = Number(document.getElementById('apSeg').value);
  const note = document.getElementById('apNote').value;
  await wrapErr(async () => {
    await api('POST', `/courses/${state.course.course.id}/assessments`, { objective_id, segment_id, note });
    toast('考核引用已建立', 'ok'); await reloadAll();
  });
}

// ---------- 冻结 / 开班 ----------
function renderFreeze() {
  const c = state.course;
  const latest = state._packages ? state._packages[0] : null;
  document.getElementById('tabBody').innerHTML = `
   <div class="card">
     <h2>已开班冻结规则</h2>
     <p class="muted">比较两种复用方式：<br>
       <span class="tag pinned">pinned 固定课件版本复用</span> 开班时锁定具体不可变课件包，
       之后模板升级、章节修改、素材替换都<b>不会改动学员正在使用的内容</b>；<br>
       <span class="tag follow">follow 模块跟随更新</span> 班级解析到最新课件包，模板升级后自动浮动（仅建议未开班/可滚动更新场景）。</p>
     <div class="row" style="margin:10px 0">
       <input id="className" placeholder="班级名称，如：2026年10月班" style="flex:1"/>
       <button onclick="openClass('pinned')">以 pinned 开班（锁定最新包）</button>
       <button class="ghost" onclick="openClass('follow')">以 follow 开班（跟随更新）</button>
     </div>
   </div>
   <div class="card"><h2>现有班级实际生效版本</h2><div id="classList" class="muted">加载中…</div></div>`;
  renderClasses();
}
async function openClass(mode) {
  if (!guardWrite()) return;
  const name = document.getElementById('className').value.trim();
  if (!name) return toast('请填写班级名', 'err');
  await wrapErr(async () => {
    if (mode === 'pinned' && !(state._packages && state._packages.length)) {
      await ensurePackage();
    }
    await api('POST', `/courses/${state.course.course.id}/classes`, { name, mode });
    toast(mode === 'pinned' ? '已开班并冻结当前课件包' : '已开班，将跟随最新课件包', 'ok');
    renderFreeze();
  });
}
async function renderClasses() {
  await loadPackages();
  const r = await api('GET', `/courses/${state.course.course.id}/impact`);
  const box = document.getElementById('classList');
  if (!box) return;
  if (!r.classes.length) { box.innerHTML = '<div class="muted">还没有班级</div>'; return; }
  box.innerHTML = r.classes.map(cl => `
    <div class="issue ${cl.mode === 'pinned' ? 'ok' : 'warn'}">
      <div style="flex:1">
        <b>${esc(cl.name)}</b>
        <span class="tag ${cl.mode}">${cl.mode === 'pinned' ? '固定复用' : '跟随更新'}</span>
        <div class="smalltxt">${esc(cl.effect)}</div>
      </div>
      <button class="small" onclick="previewClass(${cl.id})">预览生效课件包</button>
    </div>`).join('');
}
async function previewClass(clid) {
  const r = await api('GET', `/classes/${clid}/package`);
  showPackageModal(r.package);
}

// ---------- 导出 / 课件包 ----------
async function loadPackages() {
  const r = await api('GET', `/courses/${state.course.course.id}/packages`);
  state._packages = r.packages;
  return r.packages;
}
async function renderExport() {
  const pkgs = await loadPackages();
  document.getElementById('tabBody').innerHTML = `
   <div class="card">
     <h2>生成可预览课件包（后台工作器）</h2>
     <div class="row">
       <input id="idemKey" placeholder="幂等键（同一业务动作请复用）" value="exp-${Date.now()}" style="flex:1;min-width:200px"/>
       <button onclick="doExport()">导出课件包</button>
       <span class="muted">同一幂等键重复提交只生成一个作业、只扣一次额度；可安全重试。</span>
     </div>
     <div id="jobBox" class="muted" style="margin-top:8px"></div>
   </div>
   <div class="card"><h2>课件包版本（不可变快照）</h2>
     ${pkgs.length ? '<table><tr><th>版本</th><th>SHA256</th><th>总分钟</th><th>问题数</th><th>生成时间</th><th></th></tr>' +
       pkgs.map(p => `<tr><td>v${p.version}</td><td><code>${esc(p.sha256.slice(0, 16))}…</code></td>
         <td>${p.snapshot.total_minutes}</td><td>${p.issues.length}</td>
         <td>${esc(p.created_at)}</td>
         <td><button class="small" onclick='previewPkg(${p.id})'>预览</button></td></tr>`).join('') + '</table>'
       : '<div class="muted">尚未导出</div>'}
   </div>`;
}
async function ensurePackage() { await loadPackages(); if (!state._packages.length) await doExport(); }
async function doExport() {
  if (!guardWrite()) return;
  const idempotency_key = document.getElementById('idemKey').value.trim();
  if (!idempotency_key) return toast('需要幂等键', 'err');
  const box = document.getElementById('jobBox');
  try {
    const r = await api('POST', `/courses/${state.course.course.id}/export`, { idempotency_key });
    if (r.deduplicated) toast('重复请求已去重：返回既有作业，未重复扣额度', 'info');
    if (box) box.innerHTML = `作业 #${r.job.id} 已排队（状态 ${r.job.status}）…`;
    await loadQuota(); renderMain();
    const job = await pollJob(r.job.id);
    await showReceipt(job.id, true);
    await loadQuota();
    state.tab = 'export'; renderMain();
    if (job.status === 'succeeded') toast('课件包已生成（含音轨缺失等告警）', 'ok');
  } catch (e) { toast(e.message, 'err'); if (box) box.innerHTML = '<div class="issue error">' + esc(e.message) + '</div>'; }
}
async function pollJob(jid) {
  for (let i = 0; i < 100; i++) {
    const r = await api('GET', `/jobs/${jid}`);
    if (['succeeded', 'failed'].includes(r.job.status)) return r.job;
    await new Promise(res => setTimeout(res, 200));
  }
  throw new Error('作业超时');
}
async function showReceipt(jid, auto) {
  try {
    const r = await api('GET', `/jobs/${jid}/receipt`);
    const rc = r.receipt;
    showModal(`<h2 style="margin-top:0">导出回执 ${auto ? '（已持久化，丢失可重取）' : ''}</h2>
      <div class="kv">
        <div class="muted">回执号</div><div><code>${esc(rc.receipt_id)}</code></div>
        <div class="muted">课件包</div><div>v${rc.version} #${rc.package_id}</div>
        <div class="muted">SHA256</div><div><code>${esc(rc.sha256)}</code></div>
        <div class="muted">签发时间</div><div>${esc(rc.issued_at)}</div>
      </div>
      <div class="row" style="justify-content:flex-end;margin-top:12px">
        <button class="ghost" onclick="closeModal()">关闭</button>
        <button onclick="previewPkg(${rc.package_id})">预览课件包</button></div>`);
  } catch (e) { toast('回执尚未生成：' + e.message, 'err'); }
}
async function previewPkg(pid) {
  await loadPackages();
  const p = state._packages.find(x => x.id === pid);
  showPackageModal(p);
}
function showPackageModal(p) {
  const chs = p.snapshot.chapters.map((c, i) =>
    `<div style="border:1px solid #e2e8f0;border-radius:10px;padding:8px 10px;margin:6px 0">
      <b>第${i + 1}章 ${esc(c.title)}</b> <span class="smalltxt">前置 ${(c.requires || []).join(',') || '无'}</span>
      ${c.segments.map(s => `<div class="seg ${s.kind}" style="margin:6px 0">
        <span class="tag ${s.kind}">${kindTag(s.kind)}</span> <b>${esc(s.title)}</b>
        ${s.asset_version ? `<span class="smalltxt">· 素材v${s.asset_version.version} · ${s.asset_version.duration_ms}ms${s.asset_version.has_audio ? '' : ' · 音轨缺失'}</span>` : ''}
        <div class="smalltxt">${esc(s.script)}</div></div>`).join('')}
     </div>`).join('');
  showModal(`<h2 style="margin-top:0">课件包 v${p.version} 预览（不可变快照）</h2>
    <div class="smalltxt">SHA256 <code>${esc(p.sha256)}</code> · 总时长 ${p.snapshot.total_minutes} 分钟 · ${esc(p.created_at)}</div>
    ${p.issues.length ? '<div style="margin-top:8px">' + p.issues.map(i =>
      `<div class="issue ${i.code === 'audio_missing' ? 'warn' : 'error'}">${esc(i.code)}：${esc(i.message || '')}</div>`).join('') + '</div>'
      : '<div class="issue ok" style="margin-top:8px">快照无结构问题</div>'}
    <div style="margin-top:10px">${chs}</div>
    <div class="row" style="justify-content:flex-end;margin-top:10px"><button onclick="closeModal()">关闭</button></div>`);
}

// ---------- 历史与撤销 ----------
async function renderHistory() {
  const r = await api('GET', `/courses/${state.course.course.id}/history`);
  document.getElementById('tabBody').innerHTML = `
   <div class="card"><h2>修订历史（版本条件 / 字段级撤销）</h2>
     <p class="muted">撤销采用字段级补偿：只回退该修订引入、且此后未被同事再改动的字段；
     已被同事后续修订覆盖的字段保持现值。离职成员仍可在此只读查看历史。</p>
     <table><tr><th>#rev</th><th>实体</th><th>动作</th><th>操作人</th><th>时间</th><th></th></tr>
     ${r.revisions.map(x => `<tr>
       <td>#${x.rev}</td><td>${esc(x.entity)}#${x.entity_id}</td>
       <td><span class="tag ${x.action === 'delete' ? 'bad' : x.action === 'undo' ? 'warn' : ''}">${esc(x.action)}</span></td>
       <td>${esc(x.actor_name || ('用户#' + x.actor_id))}</td><td class="smalltxt">${esc(x.ts)}</td>
       <td>${canWrite() ? `<button class="small ghost" onclick="doUndo(${x.id})">撤销</button>` : '<span class="muted">只读</span>'}</td>
     </tr>`).join('')}</table></div>`;
}
async function doUndo(rid) {
  if (!guardWrite()) return;
  await wrapErr(async () => {
    const r = await api('POST', `/revisions/${rid}/undo`, {});
    if (r.status === 'skipped') toast('已跳过：这些字段被同事后续修订覆盖，未回退他人工作（' + (r.skipped || []).join(',') + '）', 'info');
    else toast('撤销完成：' + (r.effect || ('已回退字段 ' + (r.applied || []).join(','))), 'ok');
    await reloadAll();
  });
}

// ---------- 离职（演示） ----------
async function leaveTeam() {
  const u = activeUser();
  if (!confirm(`让 ${u.name} 离开团队？离开后写权限关闭，历史仍可只读查看。`)) return;
  await wrapErr(async () => {
    await api('POST', `/users/${u.id}/leave`, {});
    const all = await api('GET', `/teams/${state.teamId}/users`);
    state.users = all.users;
    const sel = document.getElementById('userSel');
    sel.innerHTML = state.users.map(x => `<option value="${x.id}">${esc(x.name)}（${x.role}${x.active ? '' : '·已离职'}）</option>`).join('');
    toast('已离开团队', 'info'); await reloadAll();
  });
}

// ---------- modal / 通用 ----------
function showModal(html) {
  document.getElementById('modalRoot').innerHTML =
    `<div class="modal-bg" onclick="if(event.target===this)closeModal()"><div class="modal">${html}</div></div>`;
}
function closeModal() { document.getElementById('modalRoot').innerHTML = ''; }
async function wrapErr(fn) {
  try { await fn(); }
  catch (e) {
    if (e.status === 409) toast('版本冲突：内容已被他人修改，请刷新后重试（' + e.message + '）', 'err');
    else toast(e.message, 'err');
    await openCourse(state.course.course.id, true).catch(() => {});
  }
}

// 暴露给控制台 / 顶部按钮
window.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });
boot().catch(e => toast('启动失败：' + e.message, 'err'));
