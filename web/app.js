/* 职业培训分镜工作台 —— 纯原生 JS，零依赖。 */
const $ = (s, el=document) => el.querySelector(s);
const $$ = (s, el=document) => [...el.querySelectorAll(s)];

const state = {
  me: null, users: [], workspaces: [], wsId: 1, courses: [], courseId: null,
  detail: null, report: null, assets: [], bindings: [], packages: [], classes: [],
  history: [], quota: null, reorderMode: false,
};

/* ---------------- API ---------------- */
async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (state.me) opt.headers['X-User-Id'] = state.me.id;
  if (body !== undefined) {
    opt.headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(body);
  }
  const res = await fetch(path, opt);
  let data = null;
  try { data = await res.json(); } catch { data = {}; }
  if (!res.ok) {
    const err = new Error(data.message || ('HTTP ' + res.status));
    err.code = res.status; err.data = data;
    throw err;
  }
  return data;
}
const esc = s => String(s ?? '').replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function toast(msg, kind='ok') {
  const t = $('#toast'); t.textContent = msg; t.className = 'toast ' + kind;
  clearTimeout(t._t); t._t = setTimeout(() => t.classList.add('hidden'), 3200);
}
const fmt = ms => ms == null ? '-' : (ms/1000).toFixed(1) + 's';

/* ---------------- 模态框 ---------------- */
function modal(title, fieldsHtml, onOk) {
  $('#modalTitle').textContent = title;
  $('#modalBody').innerHTML = fieldsHtml;
  $('#modal').classList.remove('hidden');
  $('#modalOk').onclick = async () => {
    const vals = {};
    $$('#modalBody [data-key]').forEach(el => {
      vals[el.dataset.key] = el.type === 'checkbox' ? el.checked : el.value;
    });
    try {
      await onOk(vals);
      $('#modal').classList.add('hidden');
      await refreshAll();
    } catch (e) { toast(e.message, 'bad'); }
  };
}
$('#modalCancel').onclick = () => $('#modal').classList.add('hidden');

/* ---------------- 初始化 ---------------- */
async function init() {
  state.users = await api('GET', '/api/users');
  state.workspaces = await api('GET', '/api/workspaces');
  $('#userSelect').innerHTML = state.users.map(u => `<option value="${u.id}">${esc(u.name)}</option>`).join('');
  $('#wsSelect').innerHTML = state.workspaces.map(w => `<option value="${w.id}">${esc(w.name)}</option>`).join('');
  const saved = localStorage.getItem('uid');
  state.me = state.users.find(u => u.id == saved) || state.users[1] || state.users[0];
  $('#userSelect').value = state.me.id;
  $('#wsSelect').value = state.wsId;
  $('#userSelect').onchange = e => { state.me = state.users.find(u => u.id == e.target.value); localStorage.setItem('uid', state.me.id); refreshAll(); };
  $('#wsSelect').onchange = e => { state.wsId = +e.target.value; state.courseId = null; refreshAll(); };
  $$('.tab').forEach(b => b.onclick = () => switchTab(b.dataset.tab));
  bindActions();
  await refreshAll();
}
function switchTab(name) {
  $$('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  $$('.tabpane').forEach(p => p.classList.toggle('active', p.id === 'tab-' + name));
  refreshAll();
}
function can(level) {
  if (!state.me) return false;
  const m = (state.me._memberships || []).find(x => x.workspace_id === state.wsId);
  if (!m || !m.active) return false;
  const lvl = { viewer:0, editor:1, admin:2 }[m.role] ?? -1;
  return lvl >= level;
}

/* ---------------- 数据加载 ---------------- */
async function refreshAll() {
  try {
    state.me._memberships = (await api('GET', '/api/me')).memberships;
  } catch { state.me._memberships = []; }
  const m = (state.me._memberships || []).find(x => x.workspace_id === state.wsId);
  const badge = $('#roleBadge');
  if (!m || !m.active) { badge.textContent = '非成员'; badge.className = 'badge inactive'; }
  else { badge.textContent = {admin:'管理员',editor:'编辑',viewer:'访客'}[m.role]; badge.className = 'badge ' + m.role; }

  state.courses = await api('GET', `/api/workspaces/${state.wsId}/courses`);
  if (!state.courses.find(c => c.id === state.courseId)) state.courseId = state.courses[0]?.id || null;
  renderCourseList();
  if (state.quota !== undefined || true) {
    try { state.quota = await api('GET', `/api/workspaces/${state.wsId}/quota`); } catch { state.quota = null; }
    renderQuota();
  }
  if (!state.courseId) { renderEmpty(); return; }
  state.detail = await api('GET', `/api/courses/${state.courseId}`);
  state.report = await api('GET', `/api/courses/${state.courseId}/report`);
  renderValidation();
  renderChapters();
  renderObjectives();
  if ($('#tab-assets').classList.contains('active') || state.assets.length) {
    await loadAssetsAndBindings();
  }
  await loadPackagesAndClasses();
  state.history = await api('GET', `/api/workspaces/${state.wsId}/revisions`);
  renderHistory();
}
async function loadAssetsAndBindings() {
  state.assets = await api('GET', `/api/workspaces/${state.wsId}/assets`);
  state.bindings = await api('GET', `/api/courses/${state.courseId}/bindings`);
  renderAssets(); renderBindings();
}
async function loadPackagesAndClasses() {
  state.packages = await api('GET', `/api/courses/${state.courseId}/packages`);
  state.classes = await api('GET', `/api/courses/${state.courseId}/classes`);
  renderPackages(); renderClasses();
}

function renderEmpty() {
  $('#chapters').innerHTML = '<p class="hint">该工作区暂无课程。</p>';
}
function renderCourseList() {
  $('#courseList').innerHTML = state.courses.map(c =>
    `<li data-id="${c.id}" class="${c.id===state.courseId?'active':''}">${esc(c.title)} <span class="ver">v${c.version}</span></li>`).join('');
  $$('#courseList li').forEach(li => li.onclick = () => { state.courseId = +li.dataset.id; refreshAll(); });
}
function renderQuota() {
  const q = state.quota;
  if (!q) { $('#quota').innerHTML = '-'; return; }
  const pct = Math.min(100, Math.round(q.exports_used / q.exports_limit * 100));
  $('#quota').innerHTML = `${q.exports_used} / ${q.exports_limit}（剩 ${q.remaining}）
    <div class="bar"><i style="width:${pct}%;background:${pct>85?'var(--bad)':'var(--accent2)'}"></i></div>`;
}

/* ---------------- 校验面板 ---------------- */
function renderValidation() {
  const r = state.report; const box = $('#validationBox');
  if (r.ok) {
    box.innerHTML = `<div class="ok">✅ 校验通过</div><div>总时长 ${r.total_minutes} 分钟</div>`;
    return;
  }
  const lines = [];
  r.prereq_issues.forEach(i => {
    if (i.type === 'prereq_order') lines.push(`前置顺序：${esc(i.detail)}`);
    else if (i.type === 'prereq_cycle') lines.push(`前置存在循环：${(i.path||[]).map(esc).join(' → ')}`);
    else if (i.type === 'prereq_dangling') lines.push(`前置引用悬挂（边 #${i.edge_id}）`);
  });
  r.uncovered_objectives.forEach(o => lines.push(`目标未覆盖：${esc(o.code)}（rev ${o.revision}）`));
  r.stale_coverage.forEach(o => lines.push(`覆盖待重新确认：${esc(o.code)}（目标已改版到 rev ${o.revision}）`));
  r.pending_repairs.forEach(b => lines.push(`待修复：片段「${esc(b.segment_title)}」${roleName(b.role)} - ${esc(b.repair_reason)}`));
  r.version_skew.forEach(s => lines.push(`版本错位：片段「${esc(s.title)}」(${esc(s.bindings)})`));
  box.innerHTML = `<div class="bad">⚠️ 需要处理（${lines.length}）</div>
    <div>总时长 ${r.total_minutes} 分钟（仅参考）</div><ul>${lines.map(l=>`<li>${l}</li>`).join('')}</ul>`;
}
const roleName = r => ({video:'画面',subtitle:'字幕',notes:'说明'}[r] || r);

/* ---------------- 课程 / 章节 / 片段 ---------------- */
function renderChapters() {
  const d = state.detail; const edit = can(1);
  const prereqMap = {};
  d.chapters.forEach(c => prereqMap[c.id] = c.requires || []);
  const titleById = Object.fromEntries(d.chapters.map(c => [c.id, c.title]));
  $('#chapters').innerHTML = d.chapters.map((c, idx) => `
    <div class="chapter" data-id="${c.id}" draggable="${edit && state.reorderMode}">
      <div class="chapter-head">
        <span class="pos">${c.position}</span>
        <h4>${esc(c.title)}</h4>
        ${prereqMap[c.id].length ? `<span class="prereq">前置：${prereqMap[c.id].map(id=>esc(titleById[id]||('#'+id))).join('、')}</span>`:''}
        <span class="mins">${c.minutes} 分钟 · v${c.version}</span>
        ${edit && state.reorderMode ? '<span class="tag ok">拖动重排</span>' : ''}
        ${edit ? `
          <button class="mini" data-act="edit-ch" data-id="${c.id}">编辑</button>
          <button class="mini" data-act="prereq" data-id="${c.id}">前置</button>
          <button class="mini" data-act="add-seg" data-id="${c.id}">＋片段</button>
          <button class="danger" data-act="del-ch" data-id="${c.id}">删除</button>` : ''}
      </div>
      <div class="chapter-body">
        ${c.segments.map(s => `
          <div class="seg ${s.kind}">
            <span class="kind">${({lecture:'讲授',demo:'演示',exercise:'练习'})[s.kind]}</span>
            <span class="t">${esc(s.title)}</span> <span class="ver">v${s.version} · ${s.minutes}分钟</span>
            <div class="body">${esc(s.body)}</div>
            ${edit ? `<div class="rowacts">
              <button class="mini" data-act="edit-seg" data-id="${s.id}">编辑台词/提示</button>
              ${s.kind==='demo' ? `<button class="mini" data-act="bind" data-id="${s.id}">绑定素材</button>`:''}
              <button class="danger" data-act="del-seg" data-id="${s.id}">删片段</button>
            </div>`:''}
          </div>`).join('') || '<p class="hint">暂无片段</p>'}
      </div>
    </div>`).join('');
  $$('#chapters [data-act]').forEach(b => b.onclick = () => chapterAction(b.dataset.act, +b.dataset.id));
  bindDnd();
}

function chapterAction(act, id) {
  const d = state.detail;
  const ch = d.chapters.find(c => c.id === id);
  if (act === 'add-chapter') return addChapter();
  if (act === 'edit-ch') {
    modal('编辑章节',
      field('title','标题',ch.title) + field('minutes','分钟数',ch.minutes,'number')
      + `<div class="field"><label>当前版本（乐观锁）</label><input data-key="base_version" value="${ch.version}"></div>`,
      v => api('PUT', `/api/chapters/${id}`, { title:v.title, minutes:+v.minutes, base_version:+v.base_version }));
  }
  if (act === 'add-seg') return addSegment(id);
  if (act === 'prereq') {
    const opts = d.chapters.filter(c => c.id !== id).map(c => `<option value="${c.id}">${esc(c.title)}</option>`).join('');
    modal('设置前置章节', `<div class="field"><label>学习本章前必须先完成</label><select data-key="requires_chapter_id">${opts}</select></div>`,
      v => api('POST', `/api/chapters/${id}/prereqs`, { requires_chapter_id:+v.requires_chapter_id }));
  }
  if (act === 'del-ch') {
    if (!confirm(`删除章节「${ch.title}」？将级联删除其片段与覆盖，并重新验证前置关系（可在历史中撤销）。`)) return;
    api('DELETE', `/api/chapters/${id}`).then(r => {
      toast(`已删除，校验 ok=${r.report.ok}`); refreshAll();
    }).catch(e => toast(e.message,'bad'));
  }
  if (act === 'edit-seg') {
    const seg = ch.segments.find(s => s.id === id);
    modal('编辑片段：讲师台词 / 练习提示',
      field('title','标题',seg.title)
      + `<div class="field"><label>正文（台词或练习提示）</label><textarea data-key="body">${esc(seg.body)}</textarea></div>`
      + field('minutes','分钟数',seg.minutes,'number')
      + `<div class="field"><label>当前版本</label><input data-key="base_version" value="${seg.version}"></div>`,
      v => api('PUT', `/api/segments/${id}`, { title:v.title, body:v.body, minutes:+v.minutes, base_version:+v.base_version }));
  }
  if (act === 'del-seg') {
    if (!confirm('删除该片段？引用它的目标覆盖会失效（可撤销）。')) return;
    api('DELETE', `/api/segments/${id}`).then(()=>{toast('片段已删除');refreshAll();}).catch(e=>toast(e.message,'bad'));
  }
  if (act === 'bind') return addBinding(id, ch);
}

function addChapter() {
  modal('新增章节', field('title','章节标题') + field('minutes','分钟数',0,'number'),
    v => api('POST', `/api/courses/${state.courseId}/chapters`, { title:v.title, minutes:+v.minutes||0 }));
}
function addSegment(chid) {
  modal('新增片段',
    `<div class="field"><label>类型</label><select data-key="kind">
       <option value="lecture">讲授（讲师台词）</option>
       <option value="demo">操作演示</option>
       <option value="exercise">练习提示</option></select></div>`
    + field('title','标题')
    + `<div class="field"><label>台词 / 说明 / 练习提示</label><textarea data-key="body"></textarea></div>`
    + field('minutes','分钟数',0,'number'),
    v => api('POST', `/api/chapters/${chid}/segments`, v));
}

/* 拖拽重排 */
let dragId = null;
function bindDnd() {
  if (!state.reorderMode) return;
  $$('#chapters .chapter').forEach(el => {
    el.ondragstart = () => { dragId = +el.dataset.id; el.classList.add('dragging'); };
    el.ondragend = () => { el.classList.remove('dragging'); $$('.chapter').forEach(x=>x.classList.remove('drop-target')); };
    el.ondragover = e => { e.preventDefault(); el.classList.add('drop-target'); };
    el.ondragleave = () => el.classList.remove('drop-target');
    el.ondrop = e => { e.preventDefault(); reorderAfterDrop(dragId, +el.dataset.id); };
  });
}
async function reorderAfterDrop(src, target) {
  if (src === target) return;
  let ids = state.detail.chapters.map(c => c.id);
  ids = ids.filter(x => x !== src);
  ids.splice(ids.indexOf(target), 0, src);
  try {
    const r = await api('POST', `/api/courses/${state.courseId}/chapters/reorder`,
      { order: ids, base_version: state.detail.course.version });
    state.reorderMode = false; $('#btnReorder').textContent = '↕ 进入重排';
    toast(r.report.ok ? '重排完成，前置关系校验通过' : '重排完成，但前置关系出现问题，请看左侧报告', r.report.ok?'ok':'bad');
    await refreshAll();
  } catch(e){ toast(e.message,'bad'); }
}

/* ---------------- 目标与覆盖 ---------------- */
function renderObjectives() {
  const d = state.detail; const edit = can(1);
  const allSegs = d.chapters.flatMap(c => c.segments.map(s => ({...s, chTitle:c.title})));
  $('#objectives').innerHTML = d.objectives.map(o => {
    const covs = o.coverage || [];
    return `<div class="objcard">
      <div><span class="code">${esc(o.code)}</span><strong>${esc(o.title)}</strong>
        <span class="tag rev">rev ${o.revision}</span>
        ${o.status!=='active'?`<span class="tag stale">${esc(o.status)}</span>`:''}
      </div>
      <div class="hint">${esc(o.description)}</div>
      <div>支撑片段：
        ${covs.length ? covs.map(c => {
          const seg = allSegs.find(s => s.id === c.segment_id);
          return `<span class="tag ${c.stale?'stale':'ok'}">${seg?esc(seg.title):('片段#'+c.segment_id)}${c.stale?' 待确认':''}</span>`;
        }).join('') : '<span class="tag stale">无支撑（当前版本未覆盖）</span>'}
      </div>
      ${edit ? `<div class="rowacts" style="margin-top:8px">
        <button class="mini" data-oact="rev" data-id="${o.id}">改版目标</button>
        <button class="mini" data-oact="add" data-id="${o.id}">引用教学片段</button>
        <button class="mini" data-oact="confirm" data-id="${o.id}">重新确认覆盖</button>
      </div>`:''}
    </div>`;
  }).join('');
  $$('#objectives [data-oact]').forEach(b => b.onclick = () => objectiveAction(b.dataset.oact, +b.dataset.id));
}
function field(k,label,val='',type='text'){
  return `<div class="field"><label>${label}</label><input data-key="${k}" type="${type}" value="${esc(val)}"></div>`;
}
function objectiveAction(act, id) {
  const o = state.detail.objectives.find(x => x.id === id);
  if (act === 'add') {
    const opts = state.detail.chapters.flatMap(c => c.segments.map(s =>
      `<option value="${s.id}">${esc(c.title)} / ${esc(s.title)}</option>`)).join('');
    modal('引用支撑教学片段', `<div class="field"><label>教学片段（考核点证据）</label><select data-key="segment_id">${opts}</select></div>`,
      v => api('POST', `/api/objectives/${id}/coverage`, { segment_id:+v.segment_id }));
  } else if (act === 'rev') {
    modal('训练目标改版（revision +1，旧覆盖将标记待确认）',
      field('title','新标题',o.title)
      + `<div class="field"><label>新描述</label><textarea data-key="description">${esc(o.description)}</textarea></div>`
      + `<div class="field"><label>当前版本</label><input data-key="base_version" value="${o.version}"></div>`,
      v => api('PUT', `/api/objectives/${id}`, { title:v.title, description:v.description, base_version:+v.base_version }));
  } else if (act === 'confirm') {
    api('POST', `/api/objectives/${id}/coverage/confirm`, {}).then(()=>{toast('覆盖已确认');refreshAll();});
  }
}

/* ---------------- 素材与绑定 ---------------- */
function renderAssets() {
  const edit = can(1);
  $('#assets').innerHTML = state.assets.map(a => `
    <div class="assetcard">
      <div><strong>${esc(a.name)}</strong> <span class="tag ok">当前 v${a.current_version}</span></div>
      <div class="versions">
        ${a.versions.map(v => `<div class="vrow ${v.version===a.current_version?'current':''}">
          <span>v${v.version}</span><span>时长 ${fmt(v.duration_ms)}</span>
          <span class="${v.has_audio?'':'tag repair'}">${v.has_audio?'有音轨':'⚠ 无音轨'}</span>
          <span>${esc(v.note||'')}</span></div>`).join('')}
      </div>
      ${edit ? `<div class="rowacts" style="margin-top:8px"><button class="mini" data-aact="replace" data-id="${a.id}">替换为新版本</button></div>`:''}
    </div>`).join('') || '<p class="hint">暂无素材</p>';
  $$('#assets [data-aact]').forEach(b => b.onclick = () => replaceAsset(+b.dataset.id));
}
function replaceAsset(id) {
  modal('上传 / 替换素材版本（follow 绑定将跟随；越界时间码进入待修复）',
    field('duration_ms','时长(ms)',600000,'number')
    + `<div class="field"><label>有音轨</label><select data-key="has_audio"><option value="true">有</option><option value="false">无</option></select></div>`
    + field('uri','URI','media/new.mp4') + field('note','版本说明',''),
    v => api('POST', `/api/assets/${id}/versions`,
      { duration_ms:+v.duration_ms, has_audio:v.has_audio==='true', uri:v.uri, note:v.note }));
}
function renderBindings() {
  const edit = can(1);
  $('#bindings').innerHTML = state.bindings.length ? `<table><thead><tr>
    <th>片段</th><th>角色</th><th>素材</th><th>版本</th><th>跟随</th><th>时间码</th><th>状态</th><th></th>
    </tr></thead><tbody>${state.bindings.map(b => `<tr>
      <td>${esc(b.segment_title)}</td><td>${roleName(b.role)}</td><td>${esc(b.asset_name)}</td>
      <td>v${b.asset_version}</td><td>${b.pin_mode==='follow'?'跟随':'钉住'}</td>
      <td class="tc">${fmt(b.in_ms)} ~ ${fmt(b.out_ms)}</td>
      <td>${b.status==='ok'?'<span class="tag ok">正常</span>':`<span class="tag repair">待修复:${esc(b.repair_reason)}</span>`}</td>
      <td>${edit && b.status==='pending_repair'?`<button class="mini" data-bact="repair" data-id="${b.id}">修复</button>`:''}</td>
    </tr>`).join('')}</tbody></table>` : '<p class="hint">暂无演示绑定</p>';
  $$('#bindings [data-bact]').forEach(btn => btn.onclick = () => repairBinding(+btn.dataset.id));
}
function addBinding(sid, ch) {
  const opts = state.assets.map(a => `<option value="${a.id}">${esc(a.name)}（当前 v${a.current_version}）</option>`).join('');
  modal('为演示片段绑定素材（画面/字幕/说明应同一版本）',
    `<div class="field"><label>角色</label><select data-key="role">
       <option value="video">画面</option><option value="subtitle">字幕</option><option value="notes">说明</option></select></div>
     <div class="field"><label>素材</label><select data-key="asset_id">${opts}</select></div>
     <div class="field"><label>跟随模式</label><select data-key="pin_mode"><option value="follow">follow 跟随新版本</option><option value="pinned">pinned 钉住当前版本</option></select></div>`
    + field('in_ms','入点(ms)',0,'number') + field('out_ms','出点(ms)',240000,'number'),
    v => api('POST', `/api/segments/${sid}/bindings`,
      { role:v.role, asset_id:+v.asset_id, pin_mode:v.pin_mode, in_ms:+v.in_ms, out_ms:+v.out_ms }));
}
function repairBinding(id) {
  const b = state.bindings.find(x => x.id === id);
  modal('修复待修复绑定（调整版本/时间码后自动重校验）',
    field('asset_version','素材版本',b.asset_version,'number')
    + `<div class="field"><label>跟随模式</label><select data-key="pin_mode">
        <option value="follow" ${b.pin_mode==='follow'?'selected':''}>follow</option>
        <option value="pinned" ${b.pin_mode==='pinned'?'selected':''}>pinned</option></select></div>`
    + field('in_ms','入点(ms)',b.in_ms,'number') + field('out_ms','出点(ms)',b.out_ms,'number')
    + `<div class="field"><label>当前版本</label><input data-key="base_version" value="${b.version}"></div>`,
    v => api('PUT', `/api/bindings/${id}`,
      { asset_version:+v.asset_version, pin_mode:v.pin_mode, in_ms:+v.in_ms, out_ms:+v.out_ms, base_version:+v.base_version }));
}

/* ---------------- 课件包 / 开班 ---------------- */
function renderPackages() {
  $('#packages').innerHTML = state.packages.map(p => `
    <div class="pkgcard">
      <div>包 #${p.id} · <span class="status ${p.status}">${({queued:'排队中',building:'构建中',done:'已完成',failed:'失败'})[p.status]}</span>
        <span class="tag ${p.mode==='pinned'?'frozen':'stalepkg'}">${p.mode==='pinned'?'固定 pinned':'跟随 follow'}</span>
        ${p.frozen?'<span class="tag frozen">🔒 已开班冻结</span>':''}
        ${p.stale?'<span class="tag stalepkg">内容已过期，需重建</span>':''}
        <span class="ver">课程 v${p.course_version}</span></div>
      <div class="hint">${p.receipt?`回执号：${esc(p.receipt.receipt_no)}（${esc(p.receipt.issued_at)}）`:'⚠ 尚无回执'}</div>
      <div class="rowacts">
        <button class="mini" data-pact="preview" data-id="${p.id}">预览课件包</button>
        ${can(1) && !p.frozen && p.status==='done' ? `<button class="mini" data-pact="rebuild" data-id="${p.id}">重建新版本</button>`:''}
        ${can(1) && p.status==='done' ? `<button class="mini" data-pact="class" data-id="${p.id}">用此包开班</button>`:''}
        ${p.frozen?'<span class="hint">（冻结包不可重建，保护学员内容）</span>':''}
      </div>
    </div>`).join('') || '<p class="hint">还没有导出过课件包</p>';
  $$('#packages [data-pact]').forEach(b => b.onclick = () => packageAction(b.dataset.pact, +b.dataset.id));
}
function renderClasses() {
  $('#classes').innerHTML = state.classes.map(c => `
    <div class="classcard">👥 ${esc(c.name)}
      <span class="tag frozen">冻结包 #${c.package_id} · 课程 v${c.package.course_version}</span>
      <span class="hint">${esc(c.started_at||'')}</span>
      <button class="mini" data-clact="view" data-id="${c.id}" style="margin-left:8px">查看学员所用内容</button>
    </div>`).join('') || '<p class="hint">暂无开班</p>';
  $$('#classes [data-clact]').forEach(b => b.onclick = async () => {
    const cl = await api('GET', `/api/classes/${b.dataset.id}`);
    const titles = (cl.artifact?.content?.chapters||[]).map(x=>x.title).join('、');
    modal(`班级「${esc(cl.name)}」冻结内容`, `<div class="field">课件包 #${cl.package_id}，固化于课程 v${cl.package.course_version}</div>
      <div class="field"><label>包含章节</label><div>${esc(titles)}</div></div>`, async ()=>{});
  });
}
async function exportPackage(mode) {
  const key = prompt(`导出${mode==='pinned'?'固定':'跟随'}课件包。\n请输入幂等键（重复提交同键只产生一个包、只扣一次额度）：`,
    'exp-' + Date.now());
  if (!key) return;
  try {
    const r = await api('POST', `/api/courses/${state.courseId}/exports`,
      { mode, idempotency_key: key });
    toast(r.created ? '导出作业已排队' : '命中幂等：返回既有课件包');
    setTimeout(refreshAll, 700);
  } catch(e){ toast(e.message,'bad'); }
}
async function packageAction(act, id) {
  if (act === 'preview') {
    try {
      const a = await api('GET', `/api/packages/${id}/artifact`);
      const chs = a.content.chapters.map(c =>
        `<li><strong>${esc(c.title)}</strong><ul>${c.segments.map(s=>`<li>${esc(s.title)}（${s.kind}）</li>`).join('')}</ul></li>`).join('');
      modal(`课件包 #${id} 预览（${a.mode} · 课程 v${a.course_version}）`,
        `<ul>${chs}</ul><div class="hint">生成于 ${esc(a.generated_at)}</div>`, async()=>{});
    } catch(e){ toast(e.message,'bad'); }
  } else if (act === 'rebuild') {
    try { await api('POST', `/api/packages/${id}/rebuild`, {}); toast('已提交新构建作业'); setTimeout(refreshAll,700); }
    catch(e){ toast(e.message,'bad'); }
  } else if (act === 'class') {
    const name = prompt('班级名称：'); if (!name) return;
    try { await api('POST', `/api/courses/${state.courseId}/classes`, { name, package_id:id }); toast('已开班，课件包已冻结'); refreshAll(); }
    catch(e){ toast(e.message,'bad'); }
  }
}

/* ---------------- 历史与撤销 ---------------- */
function renderHistory() {
  const edit = can(1);
  const rowsHtml = state.history.slice(0, 100).map(r => `
    <tr><td>${r.id}</td><td>${entityName(r.entity_type)} #${r.entity_id}</td>
    <td>${actionName(r.action)}</td><td class="ver">v${r.version}</td>
    <td>${esc(r.actor_name||('#'+r.actor_id))}</td><td class="ver">${esc(r.created_at)}</td>
    <td>${edit && ['create','update','delete'].includes(r.action) ? `<button class="mini" data-undo="${r.id}">撤销</button>`:''}</td>
    </tr>`).join('');
  $('#historyBody').innerHTML = rowsHtml || '<tr><td colspan="7" class="hint">暂无修订</td></tr>';
  $$('[data-undo]').forEach(b => b.onclick = async () => {
    try {
      await api('POST', `/api/revisions/${b.dataset.undo}/undo`, {});
      toast('已撤销该修订'); refreshAll();
    } catch(e) {
      if (e.code === 409) toast('撤销被拒绝：该修订之后已有同事的后续修改，不能回退他人工作', 'bad');
      else toast(e.message, 'bad');
    }
  });
}
const entityName = e => ({chapter:'章节',segment:'片段',objective:'目标',binding:'素材绑定',demo_bindings:'素材绑定',course:'课程',asset:'素材',class:'班级'}[e]||e);
const actionName = a => ({create:'新建',update:'修改',delete:'删除',reorder:'重排',undo:'撤销'}[a]||a);

/* ---------------- 动作绑定 ---------------- */
function bindActions() {
  $('#btnAddChapter').onclick = addChapter;
  $('#btnReorder').onclick = () => {
    state.reorderMode = !state.reorderMode;
    $('#btnReorder').textContent = state.reorderMode ? '✓ 完成重排' : '↕ 进入重排';
    renderChapters();
    if (state.reorderMode) toast('拖动章节卡片调整顺序，放下后自动重验证前置关系');
  };
  $('#btnAddObjective').onclick = () => modal('新增训练目标',
    field('code','目标编号（如 OBJ-4）') + field('title','目标标题')
    + `<div class="field"><label>考核说明</label><textarea data-key="description"></textarea></div>`,
    v => api('POST', `/api/courses/${state.courseId}/objectives`, v));
  $('#btnAddAsset').onclick = () => modal('新增素材',
    field('name','素材名称') + field('duration_ms','时长(ms)',300000,'number')
    + `<div class="field"><label>有音轨</label><select data-key="has_audio"><option value="true">有</option><option value="false">无</option></select></div>`
    + field('uri','URI','media/x.mp4'),
    v => api('POST', `/api/workspaces/${state.wsId}/assets`,
      { name:v.name, duration_ms:+v.duration_ms, has_audio:v.has_audio==='true', uri:v.uri }));
  $('#btnExportPinned').onclick = () => exportPackage('pinned');
  $('#btnExportFollow').onclick = () => exportPackage('follow');
  $('#btnReconcile').onclick = async () => {
    try { const r = await api('POST', `/api/workspaces/${state.wsId}/packages/reconcile`, {});
      toast(r.reissued.length ? `已补发 ${r.reissued.length} 张回执` : '对账完成，无缺失回执');
      refreshAll();
    } catch(e){ toast(e.message,'bad'); }
  };
}

init();
setInterval(() => { if (state.courseId) loadPackagesAndClasses(); }, 2500);
