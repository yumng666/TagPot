/* ============================================================
   Tag Pot · 前端逻辑
   ============================================================ */
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const enc = encodeURIComponent;

const SVG_FOLDER =
  '<svg class="ico" viewBox="0 0 24 24" fill="none"><path d="M3 7.6A2.6 2.6 0 0 1 5.6 5h3c.55 0 1.08.22 1.47.61L11.3 6.8h7.1A2.6 2.6 0 0 1 21 9.4v7A2.6 2.6 0 0 1 18.4 19H5.6A2.6 2.6 0 0 1 3 16.4V7.6Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>';
const SVG_CARET =
  '<svg class="caret" viewBox="0 0 16 16" fill="none"><path d="M6 3.6L10.4 8 6 12.4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const SVG_X =
  '<svg viewBox="0 0 12 12" fill="none"><path d="M3 3l6 6M9 3L3 9" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';

const S = {
  roots: [],
  nodes: new Map(),      // path -> node
  active: null,
  images: [],
  tiles: new Map(),      // path -> {el, img, item}
  folderPath: null,
  loadToken: 0,
  metaToken: 0,
  modalOpen: false,
  modalIdx: -1,
  noteOriginal: '',
  timer: null,
  selEl: null,
  chaseRaf: 0,
  chaseUntil: 0
};

/* ------------------------------------------------------------ HTTP */
async function api(url, opt = {}) {
  const init = Object.assign({}, opt);
  if (init.body) init.headers = { 'Content-Type': 'application/json' };
  const res = await fetch(url, init);
  const ct = res.headers.get('content-type') || '';
  const data = ct.includes('json') ? await res.json().catch(() => ({})) : await res.text();
  if (!res.ok) throw new Error((data && data.error) || ('HTTP ' + res.status));
  return data;
}

/* ------------------------------------------------------------ 工具 */
function fmtDate(ms) {
  if (!ms) return '—';
  const d = new Date(ms);
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function fmtSize(b) {
  if (!b && b !== 0) return '—';
  if (b < 1024) return b + ' B';
  if (b < 1024 * 1024) return (b / 1024).toFixed(1) + ' KB';
  if (b < 1024 * 1024 * 1024) return (b / 1024 / 1024).toFixed(2) + ' MB';
  return (b / 1024 / 1024 / 1024).toFixed(2) + ' GB';
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function relPos(el, container) {
  let x = 0, y = 0, n = el;
  while (n && n !== container) {
    x += n.offsetLeft;
    y += n.offsetTop;
    n = n.offsetParent;
    if (!n) break;
  }
  return { x, y, w: el.offsetWidth, h: el.offsetHeight };
}

function afterHeight(el, cb) {
  let done = false;
  const finish = () => {
    if (done) return;
    done = true;
    el.removeEventListener('transitionend', onEnd);
    cb();
  };
  const onEnd = e => {
    if (e.target === el && e.propertyName === 'height') finish();
  };
  el.addEventListener('transitionend', onEnd);
  setTimeout(finish, 560);
}

let _toastEl = null, _toastTimer = 0;
function toast(msg, kind) {
  if (!_toastEl) {
    _toastEl = document.createElement('div');
    _toastEl.className = 'toast';
    document.body.appendChild(_toastEl);
  }
  _toastEl.textContent = msg;
  _toastEl.classList.toggle('bad', kind === 'bad');
  _toastEl.classList.add('on');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => _toastEl.classList.remove('on'), 2200);
}

/* ------------------------------------------------------------ 侧栏：节点 */
function createNode(o) {
  return {
    id: o.id || null,
    // 根目录与子目录可能指向同一路径，键空间必须分开
    key: (o.depth === 0 ? 'r:' : 'c:') + o.path,
    name: o.name,
    path: o.path,
    depth: o.depth,
    hasDirs: o.hasDirs !== false,
    parent: o.parent || null,
    dirs: null,
    rendered: false,
    loaded: false,
    loading: false,
    open: false,
    el: null,
    itemEl: null,
    subEl: null,
    innerEl: null
  };
}

function buildNodeEl(node) {
  const group = document.createElement('div');
  group.className = 'group' + (node.depth ? ' depth-' + Math.min(node.depth, 2) : '');
  group.dataset.path = node.path;

  const item = document.createElement('div');
  item.className = 'item';
  item.dataset.path = node.path;
  item.title = node.path;
  item.innerHTML =
    SVG_FOLDER +
    '<span class="label"></span>' +
    (node.depth === 0 ? '<button class="rm" type="button" title="从列表移除">' + SVG_X + '</button>' : '') +
    (node.hasDirs ? SVG_CARET : SVG_CARET.replace('class="caret"', 'class="caret" style="opacity:0"'));
  item.querySelector('.label').textContent = node.name;

  const sub = document.createElement('div');
  sub.className = 'sub';
  const inner = document.createElement('div');
  inner.className = 'sub-inner';
  sub.appendChild(inner);

  group.appendChild(item);
  group.appendChild(sub);

  node.el = group;
  node.itemEl = item;
  node.subEl = sub;
  node.innerEl = inner;

  item.addEventListener('click', e => {
    if (e.target.closest('.rm')) return;
    onItemClick(node);
  });

  const rm = item.querySelector('.rm');
  if (rm) {
    rm.addEventListener('click', async e => {
      e.stopPropagation();
      const okGo = await askConfirm({
        title: `从 Tag Pot 移除「${node.name}」？`,
        message: '只从左侧列表移除，不会删除磁盘上的文件。',
        okText: '移除',
        danger: true
      });
      if (!okGo) return;
      try {
        const r = await api('/api/roots/remove', {
          method: 'POST',
          body: JSON.stringify({ id: node.id })
        });
        group.style.transition = 'opacity .2s, transform .2s';
        group.style.opacity = '0';
        group.style.transform = 'translateX(-8px)';
        setTimeout(() => {
          group.remove();
          S.nodes.delete(node.key);
          if (S.active === node) {
            S.active = null;
            S.selEl.style.opacity = '0';
            renderEmpty('已移除该文件夹');
          }
        }, 200);
        S.roots = r.roots;
      } catch (err) {
        toast(err.message, 'bad');
      }
    });
  }

  return group;
}

function buildRoots(roots) {
  S.roots = roots;
  S.nodes.clear();
  S.active = null;
  const menu = $('#menu');
  menu.innerHTML = '<div class="sel" id="sel"></div>';
  S.selEl = $('#sel');
  roots.forEach(r => addRootNode(r));
}

function addRootNode(r) {
  const node = createNode({
    id: r.id,
    name: r.name,
    path: r.path,
    depth: 0,
    hasDirs: r.hasDirs !== false
  });
  S.nodes.set(node.key, node);
  $('#menu').appendChild(buildNodeEl(node));
  return node;
}

/* ------------------------------------------------------------ 弹性跟随的选中块 */
function placeSel() {
  const node = S.active;
  if (!node || !S.selEl) return;
  const p = relPos(node.itemEl, $('#menu'));
  const st = S.selEl.style;
  st.left = p.x + 'px';
  st.width = p.w + 'px';
  st.height = p.h + 'px';
  st.transform = `translateY(${p.y}px)`;
  st.opacity = '1';
}

/**
 * 折叠/展开会让后续栏目位移，选中块需要一路跟随，
 * 所以在动画期间用 rAF 持续把目标位置喂给同一个过渡元素。
 */
function chase(ms = 780) {
  S.chaseUntil = performance.now() + ms;
  if (S.chaseRaf) return;
  const step = () => {
    S.chaseRaf = 0;
    if (!S.active) return;
    placeSel();
    if (performance.now() < S.chaseUntil) S.chaseRaf = requestAnimationFrame(step);
  };
  S.chaseRaf = requestAnimationFrame(step);
}

/* ------------------------------------------------------------ 选中 / 展开 */
function selectNode(node) {
  if (S.active === node) {
    placeSel();
    return;
  }
  if (S.active) S.active.itemEl.classList.remove('active');
  S.active = node;
  node.itemEl.classList.add('active');
  placeSel();
  chase();
  node.itemEl.scrollIntoView({ block: 'nearest' });
  loadFolder(node);
  api('/api/active', { method: 'POST', body: JSON.stringify({ id: node.id }) }).catch(() => {});
}

/* ------------------------------------------------------------ 确认弹窗 */
let confirmResolve = null;

function askConfirm(opts) {
  return new Promise((resolve) => {
    confirmResolve = resolve;
    $('#cfTitle').textContent = opts.title || '确认';
    const msg = $('#cfMsg');
    msg.textContent = opts.message || '';
    msg.style.display = opts.message ? '' : 'none';
    $('#cfOk').textContent = opts.okText || '确定';
    $('#confirmCard').classList.toggle('danger', !!opts.danger);
    $('#confirmMask').classList.add('on');
    setTimeout(() => { try { $('#cfCancel').focus(); } catch (e) { } }, 130);
  });
}

function settleConfirm(ok) {
  if (!confirmResolve) return;
  const done = confirmResolve;
  confirmResolve = null;
  $('#confirmMask').classList.remove('on');
  done(ok);
}

function onItemClick(node) {
  selectNode(node);
  // 已经展开就收起；否则每次都去问一次目录，这样后来才出现的子项也能展开
  if (node.open) collapse(node);
  else expand(node);
}

function isAncestor(a, b) {
  let p = b.parent;
  while (p) {
    if (p === a) return true;
    p = p.parent;
  }
  return false;
}

function closeOthers(keep) {
  for (const n of S.nodes.values()) {
    if (n.open && n !== keep && !isAncestor(n, keep)) collapse(n);
  }
}

function normalizeAncestors(node) {
  let p = node.parent;
  while (p) {
    if (p.open && p.subEl.style.height !== 'auto') p.subEl.style.height = 'auto';
    p = p.parent;
  }
}

function setCaret(node, on) {
  const c = node.itemEl.querySelector('.caret');
  if (c) c.style.opacity = on ? '' : '0';
}

function renderChildren(node) {
  node.rendered = true;
  (node.dirs || []).forEach((d, i) => {
    const child = createNode({
      name: d.name,
      path: d.path,
      depth: node.depth + 1,
      hasDirs: d.hasDirs,
      parent: node
    });
    S.nodes.set(child.key, child);
    const el = buildNodeEl(child);
    el.style.setProperty('--i', i);
    node.innerEl.appendChild(el);
  });
}

function purgeNode(node) {
  (node.dirs || []).forEach(d => {
    const c = S.nodes.get('c:' + d.path);
    if (c) purgeNode(c);
  });
  if (node.el && node.el.parentNode) node.el.remove();
  S.nodes.delete(node.key);
}

/**
 * 子文件夹有增删时就地更新子菜单：只动变化的那几行，不重建整棵树，
 * 容器高度跟着过渡，后面的栏目位置同步平移。
 */
function sortDirsNewest(dirs) {
  /* 子文件夹按修改时间排序，最新改动的在最上面；同时间的按名字排。 */
  return (dirs || []).slice().sort((a, b) =>
    (b.mtime || 0) - (a.mtime || 0) ||
    String(a.name).localeCompare(String(b.name), 'zh'));
}

function reconcileChildren(node, dirs) {
  const oldPaths = (node.dirs || []).map(d => d.path);
  const newPaths = dirs.map(d => d.path);
  if (oldPaths.join('\u0000') === newPaths.join('\u0000')) return false;

  const before = node.open ? node.innerEl.offsetHeight : 0;
  let activeLost = false;

  oldPaths.forEach(p => {
    if (newPaths.indexOf(p) >= 0) return;
    const child = S.nodes.get('c:' + p);
    if (!child) return;
    if (!activeLost && (S.active === child || isAncestor(child, S.active))) activeLost = true;
    purgeNode(child);
  });

  dirs.forEach((d, idx) => {
    if (S.nodes.has('c:' + d.path)) return;
    const child = createNode({
      name: d.name,
      path: d.path,
      depth: node.depth + 1,
      hasDirs: d.hasDirs,
      parent: node
    });
    S.nodes.set(child.key, child);
    const el = buildNodeEl(child);
    el.classList.add('just-added');
    el.addEventListener('animationend', () => el.classList.remove('just-added'), { once: true });
    node.innerEl.insertBefore(el, node.innerEl.children[idx] || null);
  });

  // 顺序可能变了（按修改时间排序后会换位）：把已有子项挪到新位置
  dirs.forEach((d, idx) => {
    const child = S.nodes.get('c:' + d.path);
    if (!child || !child.el) return;
    if (node.innerEl.children[idx] !== child.el) {
      node.innerEl.insertBefore(child.el, node.innerEl.children[idx] || null);
    }
  });

  node.dirs = dirs;
  node.hasDirs = dirs.length > 0;
  setCaret(node, dirs.length > 0);

  // 选中的就是被删掉的那一项（或它的子孙）：退回父级
  if (activeLost) selectNode(node);

  if (!node.open) return true;

  if (!dirs.length) {
    collapse(node);
    return true;
  }

  const after = node.innerEl.offsetHeight;
  if (before && before !== after) {
    node.subEl.style.height = before + 'px';
    void node.subEl.offsetHeight;
    node.subEl.style.height = after + 'px';
    afterHeight(node.subEl, () => {
      if (node.open) node.subEl.style.height = 'auto';
    });
  } else {
    node.subEl.style.height = 'auto';
  }
  placeSel();
  chase(620);
  return true;
}

async function expand(node) {
  if (node.loading) return;
  node.loading = true;
  let dirs = null;
  try {
    const d = await api('/api/dirs?path=' + enc(node.path));
    dirs = d.dirs || [];
  } catch (e) {
    node.loading = false;
    toast(e.message, 'bad');
    return;
  }
  node.loading = false;
  node.loaded = true;

  dirs = sortDirsNewest(dirs);
  if (!node.rendered) {
    node.dirs = dirs;
    renderChildren(node);
  } else {
    reconcileChildren(node, dirs);
  }

  if (!dirs.length) {
    node.hasDirs = false;
    setCaret(node, false);
    if (node.open) collapse(node);
    else closeOthers(node);
    return;
  }

  node.hasDirs = true;
  setCaret(node, true);

  closeOthers(node);
  normalizeAncestors(node);

  node.open = true;
  node.itemEl.classList.add('open');
  node.subEl.style.height = node.innerEl.scrollHeight + 'px';
  requestAnimationFrame(() => {
    if (node.open) node.subEl.classList.add('open');
  });
  afterHeight(node.subEl, () => {
    if (node.open) node.subEl.style.height = 'auto';
  });
  chase(500);
}

function collapse(node) {
  if (!node.open) return;
  node.open = false;
  node.itemEl.classList.remove('open');
  node.subEl.classList.remove('open');
  node.subEl.style.height = node.subEl.offsetHeight + 'px';
  void node.subEl.offsetHeight;
  node.subEl.style.height = '0px';
  afterHeight(node.subEl, () => {
    if (!node.open) node.subEl.style.height = '0px';
  });
  (node.dirs || []).forEach(d => {
    const c = S.nodes.get('c:' + d.path);
    if (c && c.open) collapse(c);
  });
  chase(500);
}

/**
 * 让展开中的子菜单跟着磁盘变化刷新。dirsHint 是这一轮已经拿到的目录列表，
 * 免得同一个目录重复请求。
 */
let treeBusy = false;
async function syncTree(hintPath, dirsHint) {
  if (treeBusy) return;
  const opened = [];
  for (const n of S.nodes.values()) {
    if (n.open && n.rendered) opened.push(n);
  }
  if (!opened.length) return;

  treeBusy = true;
  try {
    for (const node of opened) {
      if (!node.open || node.loading) continue;
      let dirs = null;
      if (dirsHint && hintPath && node.path === hintPath) {
        dirs = dirsHint;
      } else {
        try {
          dirs = (await api('/api/dirs?path=' + enc(node.path))).dirs || [];
        } catch (e) {
          continue;
        }
      }
      dirs = sortDirsNewest(dirs);
      if (!node.open) continue;
      reconcileChildren(node, dirs);
    }
  } finally {
    treeBusy = false;
  }
}

/* ------------------------------------------------------------ 右侧：瀑布流 */
async function loadFolder(node) {
  const token = ++S.loadToken;
  try {
    const d = await api('/api/images?path=' + enc(node.path));
    if (token !== S.loadToken) return;
    S.folderPath = d.path;
    $('#stageName').textContent = d.name;
    $('#stageSub').textContent = d.path;
    syncImages(d.images || [], true);
    if (!(d.images || []).length && token === S.loadToken) {
      // 这个文件夹本身没有图片：看看图片是不是都在子文件夹里
      try {
        const dd = await api('/api/dirs?path=' + enc(node.path));
        if (token !== S.loadToken) return;
        if ((dd.dirs || []).length) {
          setEmptyCopy('这个文件夹没有图片', '图片都在子文件夹里，点左侧的子文件夹看看');
        } else {
          setEmptyCopy('这个文件夹没有图片', '把图片放进来后点右上角刷新，或换一个文件夹');
        }
      } catch (e) { }
    }
  } catch (e) {
    toast(e.message, 'bad');
  }
}

function thumbUrl(it) {
  return `/api/thumb?path=${enc(it.path)}&w=560&v=${it.mtime}`;
}

function createTile(it) {
  const el = document.createElement('div');
  el.className = 'tile';

  const inner = document.createElement('div');
  inner.className = 'tile-inner';

  const img = document.createElement('img');
  img.alt = it.name;
  img.decoding = 'async';
  img.loading = 'lazy';
  img.src = thumbUrl(it);
  img.addEventListener('load', () => {
    img.classList.add('ready');
    if (!it.w || !it.h) {
      it.w = img.naturalWidth;
      it.h = img.naturalHeight;
      layout();
    }
  });
  img.addEventListener('error', () => {
    inner.style.background = 'repeating-linear-gradient(45deg,#eef4fd,#eef4fd 8px,#e6eefa 8px,#e6eefa 16px)';
  });

  const cap = document.createElement('div');
  cap.className = 'tile-cap';
  cap.textContent = it.name;

  inner.appendChild(img);
  inner.appendChild(cap);
  el.appendChild(inner);
  inner.addEventListener('click', () => openModal(it.path));

  const rec = { el, img, inner, item: it };
  S.tiles.set(it.path, rec);
  $('#masonry').appendChild(el);

  requestAnimationFrame(() => requestAnimationFrame(() => el.classList.add('show')));
  return rec;
}

function syncImages(images, reset) {
  if (reset) {
    S.tiles.forEach(rec => rec.el.remove());
    S.tiles.clear();
    S.images = [];
  }
  const next = images;
  const nextPaths = new Set(next.map(i => i.path));

  for (const [p, rec] of Array.from(S.tiles)) {
    if (!nextPaths.has(p)) {
      rec.el.classList.add('out');
      S.tiles.delete(p);
      setTimeout(() => rec.el.remove(), 340);
    }
  }

  next.forEach(it => {
    const rec = S.tiles.get(it.path);
    if (rec) {
      // mtime/size 变化通常只是元数据被外部工具更新，
      // 像素没变，不重载缩略图；像素真的变了用右上角「刷新」
      rec.item = it;
    } else {
      createTile(it);
    }
  });

  S.images = next;
  layout();
  updateHeader();
}

function layout() {
  const masonry = $('#masonry');
  const W = masonry.clientWidth;
  if (!W) return;
  const gap = 16;
  let cols = Math.floor((W + gap) / (238 + gap));
  cols = Math.max(2, Math.min(6, cols));
  const colW = (W - gap * (cols - 1)) / cols;
  const hs = new Array(cols).fill(0);

  S.images.forEach(it => {
    const rec = S.tiles.get(it.path);
    if (!rec) return;
    const ratio = (it.w && it.h) ? it.h / it.w : 1.32;
    const h = Math.max(96, Math.round(colW * ratio));
    let c = 0;
    for (let i = 1; i < cols; i++) if (hs[i] < hs[c] - 0.5) c = i;
    const x = Math.round(c * (colW + gap));
    const y = Math.round(hs[c]);
    hs[c] = y + h + gap;
    const st = rec.el.style;
    st.width = colW + 'px';
    st.height = h + 'px';
    st.transform = `translate3d(${x}px, ${y}px, 0)`;
  });

  const maxH = hs.length ? Math.max.apply(null, hs) : 0;
  masonry.style.height = Math.max(0, maxH - gap) + 'px';
}

function updateHeader() {
  const n = S.images.length;
  $('#countPill').textContent = n + ' 张';
  $('#empty').classList.toggle('on', n === 0);
}

function renderEmpty(msg) {
  S.images = [];
  S.tiles.forEach(rec => rec.el.remove());
  S.tiles.clear();
  S.folderPath = null;
  $('#stageName').textContent = msg || '未选择文件夹';
  $('#stageSub').textContent = '在左侧添加或选择一个文件夹';
  $('#countPill').textContent = '0 张';
  setEmptyCopy('还没有文件夹', '点左上角「添加文件夹」，或把文件夹直接拖到那个按钮上');
  $('#empty').classList.add('on');
  layout();
}

function setEmptyCopy(title, hint) {
  $('#emptyTitle').textContent = title;
  $('#emptyHint').textContent = hint;
}

/* ------------------------------------------------------------ 轮询刷新 */
function startPoll() {
  stopPoll();
  S.timer = setInterval(async () => {
    if (document.hidden || S.modalOpen || !S.active) return;
    let d = null;
    try {
      d = await api('/api/images?path=' + enc(S.active.path));
    } catch (e) {
      // 当前目录被删或掉盘：仍然去同步树，让栏目自己发现并退回父级
      syncTree(null, null);
      return;
    }
    if (d.path !== S.folderPath) return;
    syncImages(d.images || [], false);
    syncTree(d.path, d.dirs);
  }, 2000);
}

function stopPoll() {
  if (S.timer) clearInterval(S.timer);
  S.timer = null;
}

/* ------------------------------------------------------------ 弹窗 */
/* ------------------------------------------------------------ 生成参数（模型 / Lora） */
const LORA_PLUS_SVG =
  '<svg viewBox="0 0 12 12" fill="none"><path d="M6 1.7v8.6M1.7 6h8.6" stroke="currentColor"'
  + ' stroke-width="1.6" stroke-linecap="round"/></svg>';

function makeLoraRow(item) {
  const row = document.createElement('div');
  row.className = 'lora-item';

  const name = document.createElement('input');
  name.type = 'text';
  name.className = 'gen-input lora-name';
  name.spellcheck = false;
  name.autocomplete = 'off';
  name.placeholder = 'Lora 名称';
  name.value = item.name || '';

  const str = document.createElement('input');
  str.type = 'number';
  str.step = '0.05';
  str.className = 'gen-input gen-str lora-str';
  str.autocomplete = 'off';
  str.title = 'Lora 强度';
  const v = item.strength;
  str.value = (v === null || v === undefined || v === '') ? '' : v;

  row.appendChild(name);
  row.appendChild(str);
  return row;
}

function makeLoraAdd() {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'lora-add';
  btn.title = '添加一个 Lora';
  btn.innerHTML = LORA_PLUS_SVG;
  btn.addEventListener('click', () => {
    const box = $('#loraList');
    const wrap = box.querySelector('.lora-add-row');
    const row = makeLoraRow({ name: '', strength: 1 });
    box.insertBefore(row, wrap || null);
    row.querySelector('.lora-name').focus();
    box.scrollTop = box.scrollHeight;
  });
  return btn;
}

function renderLoras(loras) {
  const box = $('#loraList');
  box.textContent = '';
  const list = Array.isArray(loras) ? loras.filter((l) => l && l.name) : [];
  if (!list.length) {
    box.appendChild(makeLoraRow({ name: '', strength: null }));
  } else {
    list.forEach((it) => box.appendChild(makeLoraRow(it)));
  }
  const wrap = document.createElement('div');
  wrap.className = 'lora-add-row';
  wrap.appendChild(makeLoraAdd());
  box.appendChild(wrap);
}

function collectLoras() {
  const out = [];
  $('#loraList').querySelectorAll('.lora-item').forEach((row) => {
    const name = row.querySelector('.lora-name').value.trim();
    if (!name) return;
    const raw = Number(row.querySelector('.lora-str').value);
    out.push({ name, strength: isFinite(raw) ? raw : 1 });
  });
  return out;
}

function readModelStrength() {
  const raw = $('#mModelStr').value.trim();
  if (raw === '') return 1;
  const n = Number(raw);
  return isFinite(n) ? n : 1;
}

function fillGen(gen) {
  const g = gen || {};
  $('#genBlock').classList.toggle('nai', g.engine === 'novelai');
  $('#mModel').value = g.model || '';
  const ms = g.modelStrength;
  $('#mModelStr').value = (ms === null || ms === undefined) ? '' : ms;
  renderLoras(g.loras || []);
  S.genSnap = { model: $('#mModel').value, strength: readModelStrength(), loras: collectLoras() };
}

function resetGen() {
  $('#genBlock').classList.remove('nai');
  $('#mModel').value = '';
  $('#mModelStr').value = '';
  $('#loraList').textContent = '';
  S.genSnap = null;
}

async function saveGen(path) {
  if (!S.genSnap) return;
  const model = $('#mModel').value.trim();
  const strength = readModelStrength();
  const loras = collectLoras();
  const snap = S.genSnap;
  const patch = { path };
  let dirty = false;
  if (model !== snap.model || strength !== snap.strength) {
    // 名字留空 = 没有自定义，撤销覆盖（有检测值就回到检测值）
    patch.model = model ? { name: model, strength } : null;
    dirty = true;
  }
  if (JSON.stringify(loras) !== JSON.stringify(snap.loras)) {
    patch.loras = loras;
    dirty = true;
  }
  if (!dirty) return;
  const token = S.metaToken;
  try {
    await api('/api/annotation', { method: 'POST', body: JSON.stringify(patch) });
    if (token === S.metaToken) S.genSnap = { model, strength, loras };
  } catch (e) {
    toast('模型 / Lora 保存失败：' + e.message, 'bad');
  }
}

async function openModal(path) {
  const idx = S.images.findIndex(i => i.path === path);
  if (idx < 0) return;
  S.modalOpen = true;
  $('#mask').classList.add('on');
  await showModal(idx);
}

async function showModal(i) {
  const it = S.images[i];
  if (!it) return;
  S.modalIdx = i;
  const img = $('#mImg');
  img.classList.remove('ready');
  img.src = `/api/full?path=${enc(it.path)}`;

  $('#mName').textContent = it.name;
  $('#mDate').textContent = fmtDate(it.ctime);
  $('#mRes').textContent = it.w && it.h ? `${it.w} × ${it.h}` : '—';
  $('#mSize').textContent = fmtSize(it.size);
  $('#mFmt').textContent = (it.name.split('.').pop() || '').toUpperCase();

  const note = $('#mNote');
  note.value = '';
  note.placeholder = '读取中…';
  S.noteOriginal = '';
  $('#mNoteSrc').textContent = '图片属性 · 关闭弹窗自动保存';
  $('#mState').classList.remove('on');
  resetGen();

  const token = ++S.metaToken;
  try {
    const m = await api('/api/meta?path=' + enc(it.path));
    if (token !== S.metaToken) return;
    note.value = m.comment || '';
    note.placeholder = '记录这张图的提示词…';
    S.noteOriginal = m.comment || '';
    $('#mDate').textContent = fmtDate(m.ctime);
    if (m.w && m.h) $('#mRes').textContent = `${m.w} × ${m.h}`;
    $('#mSize').textContent = fmtSize(m.size);
    $('#mFmt').textContent = m.format || '';
    const srcText = {
      property: '图片属性 · 关闭弹窗自动保存',
      exif: '图片属性（EXIF）· 关闭弹窗自动保存',
      generated: '自动读取自生成参数 · 关闭弹窗自动保存',
      sidecar: '该格式无法写入文件属性，提示词只存在应用内'
    };
    $('#mNoteSrc').textContent = srcText[m.commentSource] || srcText.property;
    fillGen(m.gen);
  } catch (e) {
    if (token === S.metaToken) note.placeholder = '提示词读取失败';
  }
}

async function closeModal() {
  if (!S.modalOpen) return;
  S.modalOpen = false;
  $('#mask').classList.remove('on');
  await persistModal();
}

/** 保存当前弹窗里的提示词与模型/Lora，切图或关闭时调用 */
async function persistModal() {
  const it = S.images[S.modalIdx];
  if (!it) return;
  const path = it.path;
  const note = $('#mNote').value;

  if (note !== S.noteOriginal) {
    try {
      await api('/api/comment', {
        method: 'POST',
        body: JSON.stringify({ path, comment: note })
      });
      it.comment = note;
      if (S.images[S.modalIdx] && S.images[S.modalIdx].path === path) S.noteOriginal = note;
      toast('提示词已保存');
    } catch (e) {
      toast('提示词保存失败：' + e.message, 'bad');
    }
  }
  await saveGen(path);
}

function step(delta) {
  if (!S.images.length) return;
  const n = S.images.length;
  persistModal();
  showModal((S.modalIdx + delta + n) % n);
}

/* ------------------------------------------------------------ 添加文件夹 */
async function addByPath(path) {
  const p = (path || '').trim().replace(/^"|"$/g, '');
  if (!p) return;
  try {
    const r = await api('/api/roots', { method: 'POST', body: JSON.stringify({ path: p }) });
    const existing = new Set(S.roots.map(x => x.path));
    (r.roots || []).forEach(root => {
      if (!existing.has(root.path) && !S.nodes.has('r:' + root.path)) addRootNode(root);
    });
    S.roots = r.roots || S.roots;
    if (r.added) {
      const node = Array.from(S.nodes.values()).find(n => n.id === r.added);
      if (node) {
        selectNode(node);
        setTimeout(() => { if (node.hasDirs) expand(node); }, 60);
      }
      toast('已添加文件夹');
    } else {
      toast('该文件夹已在列表中');
    }
  } catch (e) {
    toast(e.message, 'bad');
  }
}

/* ------------------------------------------------------------ 内置文件夹选择器 */
const PK = { path: '', parent: '', sel: null, open: false, drives: [], places: [] };

function parentOf(p) {
  if (!p) return '';
  const m = p.replace(/[\\/]+$/, '');
  const i = Math.max(m.lastIndexOf('\\'), m.lastIndexOf('/'));
  if (i < 0) return '';
  const head = m.slice(0, i);
  if (/^[A-Za-z]:$/.test(head)) return head + '\\';
  return head;
}

async function openPicker(startAt) {
  PK.open = true;
  PK.sel = null;
  $('#pkNote').textContent = '';
  $('#pkNote').classList.remove('bad');
  $('#pickMask').classList.add('on');
  try {
    if (!PK.drives.length) {
      const d = await api('/api/fs/places');
      PK.drives = d.drives || [];
      PK.places = d.places || [];
    }
  } catch (e) { /* 侧栏拿不到不影响浏览 */ }
  const start = startAt !== undefined
    ? startAt
    : (S.active ? parentOf(S.active.path) : '');
  await pkLoad(start);
  setTimeout(() => {
    const inp = $('#pkInput');
    inp.focus();
    inp.select();
  }, 240);
}

function closePicker() {
  PK.open = false;
  $('#pickMask').classList.remove('on');
}

async function pkLoad(path) {
  const note = $('#pkNote');
  note.classList.remove('bad');
  note.textContent = '读取中…';
  try {
    const d = await api('/api/fs/list?path=' + enc(path || ''));
    if (!PK.open) return;
    PK.path = d.path || '';
    PK.parent = d.parent || '';
    PK.sel = null;
    $('#pkInput').value = PK.path;
    renderCrumbs(d);
    renderPkQuick();
    renderPkSide();
    renderPkList(d);
    if (d.ok === false) {
      note.textContent = d.error || '无法访问该文件夹';
      note.classList.add('bad');
    } else if (d.path && !(d.dirs || []).length) {
      note.textContent = '这个文件夹没有子文件夹，可以直接添加当前文件夹';
    } else {
      note.textContent = '';
    }
    updatePkSel();
    $('#pkList').scrollTop = 0;
  } catch (e) {
    note.textContent = e.message;
    note.classList.add('bad');
  }
}

function renderCrumbs(d) {
  const box = $('#pkCrumbs');
  box.textContent = '';
  const crumb = (label, path, cur) => {
    const el = document.createElement('span');
    el.className = 'pk-crumb' + (cur ? ' cur' : '');
    el.textContent = label;
    el.title = path || '此电脑';
    el.addEventListener('click', () => pkLoad(path));
    box.appendChild(el);
  };
  const sep = () => {
    const el = document.createElement('span');
    el.className = 'pk-crumb-sep';
    el.textContent = '›';
    box.appendChild(el);
  };
  if (!d.path) {
    crumb('此电脑', '', true);
    return;
  }
  crumb('此电脑', '', false);
  const segs = d.path.replace(/[\\/]+$/, '').split(/[\\/]/);
  let acc = '';
  segs.forEach((seg, i) => {
    acc = i === 0 ? seg + '\\' : acc.replace(/[\\/]+$/, '') + '\\' + seg;
    sep();
    crumb(seg, acc, i === segs.length - 1);
  });
}

const SVG_DRIVE =
  '<svg viewBox="0 0 24 24" fill="none"><rect x="3" y="6" width="18" height="12" rx="2.5" stroke="currentColor" stroke-width="1.6"/><path d="M6.5 14.5h4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>';
const SVG_STAR =
  '<svg viewBox="0 0 24 24" fill="none"><path d="M3 7.6A2.6 2.6 0 0 1 5.6 5h3c.55 0 1.08.22 1.47.61L11.3 6.8h7.1A2.6 2.6 0 0 1 21 9.4v7A2.6 2.6 0 0 1 18.4 19H5.6A2.6 2.6 0 0 1 3 16.4V7.6Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>';
const SVG_UP =
  '<svg viewBox="0 0 24 24" fill="none"><path d="M12 19V6M6 11.5 12 5.5l6 6" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';

function renderPkSide() {
  const box = $('#pkSide');
  box.textContent = '';
  const sec = (t) => {
    const el = document.createElement('div');
    el.className = 'pk-sec';
    el.textContent = t;
    box.appendChild(el);
  };
  const item = (name, path, icon, tag) => {
    const el = document.createElement('div');
    el.className = 'pk-side-item' + (PK.path === path ? ' on' : '');
    el.innerHTML = icon + '<span class="nm"></span>' + (tag ? '<span class="tag"></span>' : '');
    el.querySelector('.nm').textContent = name;
    if (tag) el.querySelector('.tag').textContent = tag;
    el.title = path || '此电脑';
    el.addEventListener('click', () => pkLoad(path));
    box.appendChild(el);
  };

  sec('位置');
  item('此电脑', '', SVG_DRIVE);

  if (PK.drives.length) {
    sec('磁盘');
    PK.drives.forEach((d) => item(d.name, d.path, SVG_DRIVE, d.label));
  }

}

function renderPkQuick() {
  const box = $('#pkQuick');
  box.textContent = '';
  PK.places.forEach((p) => {
    const el = document.createElement('div');
    el.className = 'pk-chip' + (PK.path === p.path ? ' on' : '');
    el.innerHTML = SVG_STAR + '<span></span>';
    el.querySelector('span').textContent = p.name;
    el.title = p.path;
    el.addEventListener('click', () => pkLoad(p.path));
    box.appendChild(el);
  });
}

function renderPkList(d) {
  const box = $('#pkList');
  box.textContent = '';
  if (d.path && d.parent !== null && d.parent !== undefined) {
    const up = document.createElement('div');
    up.className = 'pk-row up';
    up.innerHTML = SVG_UP + '<span class="nm">返回上一级</span>';
    up.addEventListener('click', () => pkLoad(d.parent));
    box.appendChild(up);
  }
  (d.dirs || []).forEach((it) => {
    const row = document.createElement('div');
    row.className = 'pk-row';
    row.dataset.path = it.path;
    row.innerHTML = SVG_FOLDER + '<span class="nm"></span>';
    row.querySelector('.nm').textContent = it.name;
    row.title = it.path;
    row.addEventListener('click', () => {
      box.querySelectorAll('.pk-row.on').forEach((e) => e.classList.remove('on'));
      row.classList.add('on');
      PK.sel = it.path;
      updatePkSel();
    });
    row.addEventListener('dblclick', () => pkLoad(it.path));
    box.appendChild(row);
  });
}

function updatePkSel() {
  const target = PK.sel || PK.path;
  $('#pkSelPath').textContent = target || '—';
  $('#pkSelPath').title = target || '';
  $('#pkOk').disabled = !target;
}

function pkConfirm() {
  const target = PK.sel || PK.path;
  if (!target) return;
  closePicker();
  addByPath(target);
}

/* ------------------------------------------------------------ 拖文件夹进「添加」 */
async function handleDropped(dt) {
  const files = dt && dt.files;
  if (!files || !files.length) {
    toast('没识别到文件夹，换个位置再试试', 'bad');
    return;
  }
  const wv = window.chrome && window.chrome.webview;
  if (wv && wv.postMessageWithAdditionalObjects) {
    try {
      wv.postMessageWithAdditionalObjects('TagPotDrop', files);
    } catch (e) { /* 继续走回退 */ }
    for (let i = 0; i < 16; i++) {
      await sleep(130);
      try {
        const r = await api('/api/drop/take');
        if (r.paths && r.paths.length) {
          await addByPath(r.paths[0]);
          return;
        }
      } catch (e) { /* 继续等 */ }
    }
  }
  const guess = files[0] && files[0].path;
  if (guess) return addByPath(guess);
  toast('读不到拖入文件夹的路径，请改用「添加文件夹」', 'bad');
}

function initDropTarget() {
  const btn = $('#addBtn');
  const label = btn.querySelector('.add-label');
  const base = label.textContent;
  let depth = 0;

  btn.addEventListener('dragenter', (e) => {
    e.preventDefault();
    e.stopPropagation();
    depth++;
    btn.classList.add('drop-on');
    label.textContent = '松手即可添加';
  });
  btn.addEventListener('dragover', (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
  });
  btn.addEventListener('dragleave', (e) => {
    e.preventDefault();
    depth--;
    if (depth <= 0) {
      depth = 0;
      btn.classList.remove('drop-on');
      label.textContent = base;
    }
  });
  btn.addEventListener('drop', async (e) => {
    e.preventDefault();
    e.stopPropagation();
    depth = 0;
    btn.classList.remove('drop-on');
    label.textContent = base;
    await handleDropped(e.dataTransfer);
  });
}

/* ------------------------------------------------------------ 绑定 */
function bindUI() {
  $('#cfOk').addEventListener('click', () => settleConfirm(true));
  $('#cfCancel').addEventListener('click', () => settleConfirm(false));
  $('#confirmMask').addEventListener('click', (e) => {
    if (e.target === $('#confirmMask')) settleConfirm(false);
  });

  $('#addBtn').addEventListener('click', () => openPicker());
  initDropTarget();

  // 文件夹选择器
  $('#pkClose').addEventListener('click', closePicker);
  $('#pkCancel').addEventListener('click', closePicker);
  $('#pkOk').addEventListener('click', pkConfirm);
  $('#pickMask').addEventListener('click', (e) => {
    if (e.target === $('#pickMask')) closePicker();
  });
  const goInput = () => {
    const v = $('#pkInput').value.trim().replace(/^"|"$/g, '');
    pkLoad(v);
  };
  $('#pkGo').addEventListener('click', goInput);
  $('#pkInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      goInput();
    }
    if (e.key === 'Escape') {
      e.preventDefault();
      closePicker();
    }
  });
  $('#pkList').addEventListener('dblclick', (e) => {
    const row = e.target.closest('.pk-row');
    if (row && row.dataset.path) pkLoad(row.dataset.path);
  });

  $('#refreshBtn').addEventListener('click', async () => {
    const b = $('#refreshBtn');
    b.classList.add('spin');
    setTimeout(() => b.classList.remove('spin'), 460);
    if (S.active) await loadFolder(S.active);
  });

  // 弹窗
  $('#mask').addEventListener('click', e => {
    if (e.target === $('#mask')) closeModal();
  });
  $('#mClose').addEventListener('click', closeModal);
  $('#mPrev').addEventListener('click', e => { e.stopPropagation(); step(-1); });
  $('#mNext').addEventListener('click', e => { e.stopPropagation(); step(1); });
  $('#mImg').addEventListener('load', () => $('#mImg').classList.add('ready'));
  $('#mImg').addEventListener('click', e => e.stopPropagation());

  document.addEventListener('keydown', e => {
    if (confirmResolve) {
      if (e.key === 'Escape') { e.preventDefault(); settleConfirm(false); }
      if (e.key === 'Enter') { e.preventDefault(); settleConfirm(true); }
      return;
    }
    if (e.key === 'Escape') {
      if (PK.open) { closePicker(); return; }
      if (S.modalOpen) closeModal();
      return;
    }
    if (!S.modalOpen) return;
    if (e.key === 'ArrowLeft') step(-1);
    if (e.key === 'ArrowRight') step(1);
  });

  let rt = 0;
  window.addEventListener('resize', () => {
    clearTimeout(rt);
    rt = setTimeout(() => {
      placeSel();
      layout();
    }, 120);
  });
}

/* ------------------------------------------------------------ 桌面外壳适配 */
const DESKTOP = new URLSearchParams(location.search).get('shell') === 'desktop';

function shellTweaks() {
  // 桌面窗口里不需要浏览器那套默认右键菜单
  if (DESKTOP) document.addEventListener('contextmenu', e => e.preventDefault());
  // 拖文件进来时不要让内核直接跳转到该文件；具体落点由「添加」按钮接管
  ['dragover', 'drop'].forEach(t =>
    window.addEventListener(t, e => {
      if (e.target.closest && e.target.closest('#addBtn')) return;
      e.preventDefault();
      const files = t === 'drop' && e.dataTransfer && e.dataTransfer.files;
      if (files && files.length) toast('把文件夹拖到左侧「添加文件夹」按钮上');
    })
  );
}

/* ------------------------------------------------------------ 启动 */
async function init() {
  shellTweaks();
  bindUI();
  try {
    const st = await api('/api/state');
    buildRoots(st.roots || []);
    const target =
      S.nodes.get('r:' + (st.roots.find(r => r.id === st.active) || {}).path) ||
      Array.from(S.nodes.values())[0];
    if (target) {
      selectNode(target);
      if (target.hasDirs) setTimeout(() => expand(target), 140);
    } else {
      renderEmpty();
    }
  } catch (e) {
    toast('无法连接服务：' + e.message, 'bad');
  }
  startPoll();

  // 首屏数字动画收尾时校正一次选中块
  setTimeout(() => { placeSel(); layout(); }, 500);

  // 初始化完成标记（便于自查：控制台执行 document.documentElement.dataset.appReady）
  document.documentElement.dataset.appReady = 'true';
}

init();
