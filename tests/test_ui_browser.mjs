/**
 * Tag Pot 前端真实浏览器验证（零第三方依赖）
 * 需要 Node 18+ 与本机 Chrome / Edge。
 *
 *   node tests/test_ui_browser.mjs http://127.0.0.1:8765
 *
 * 断言不写死目录名：先问后端要根目录/子目录，再按名字驱动界面。
 */
import { spawn } from 'node:child_process';
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, rmSync, unlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const BASE = (process.argv[2] || 'http://127.0.0.1:8765').replace(/\/$/, '');
const PORT = 9333;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const fails = [];
const shots = [];

function check(label, actual, expected) {
  const ok = typeof expected === 'function' ? expected(actual) : actual === expected;
  console.log(`${ok ? '  ok  ' : '  FAIL'} ${label}` + (ok ? ` = ${JSON.stringify(actual)}` : ` => ${JSON.stringify(actual)} (期望 ${expected})`));
  if (!ok) fails.push(label);
  return ok;
}

const jget = async (p) => (await fetch(BASE + p)).json();
const enc = encodeURIComponent;
const apiState = () => jget('/api/state');
const apiDirs = (p) => jget('/api/dirs?path=' + enc(p)).then((d) => d.dirs);
const apiImages = (p) => jget('/api/images?path=' + enc(p));

function findChrome() {
  const cands = [
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  ];
  return cands.find((p) => existsSync(p));
}

class CDP {
  constructor(ws) {
    this.ws = ws; this.nextId = 0; this.pending = new Map(); this.errors = [];
    ws.addEventListener('message', (e) => {
      const m = JSON.parse(e.data);
      if (m.method === 'Runtime.exceptionThrown') {
        this.errors.push((m.params.exceptionDetails || {}).text || 'exception');
      }
      if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
        this.errors.push('console.error');
      }
      const p = this.pending.get(m.id);
      if (!p) return;
      this.pending.delete(m.id);
      m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result);
    });
  }
  send(method, params = {}, timeoutMs = 60000) {
    const id = ++this.nextId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => { if (this.pending.delete(id)) reject(new Error('CDP 超时: ' + method)); }, timeoutMs);
    });
  }
  async eval(expression) {
    const r = await this.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error('页面内异常: ' + JSON.stringify(r.exceptionDetails).slice(0, 400));
    return r.result.value;
  }
  async shot(name) {
    const { data } = await this.send('Page.captureScreenshot', { format: 'png' });
    const dir = join(process.cwd(), 'tests', 'shots');
    mkdirSync(dir, { recursive: true });
    const p = join(dir, name + '.png');
    writeFileSync(p, Buffer.from(data, 'base64'));
    shots.push(p);
  }
  async click(x, y) {
    for (const type of ['mousePressed', 'mouseReleased']) {
      await this.send('Input.dispatchMouseEvent', { type, x, y, button: 'left', clickCount: 1 });
    }
  }
}

/* ---------------------------------------------------------------- 先摸清后端结构 */
const rootList = (await apiState()).roots;
if (!rootList.length) {
  console.log('后端没有任何根目录，无法测试');
  process.exit(2);
}
let parent = null;      // 带子文件夹的根
let child = null;
let other = null;       // 另一个根，用来测切换
for (const r of rootList) {
  const ds = await apiDirs(r.path);
  if (ds.length && !parent) { parent = r; child = ds[0]; }
}
other = rootList.find((r) => r.path !== (parent && parent.path));
if (!parent || !other) {
  console.log('演示数据不足：需要一个含子文件夹的根 + 至少一个其它根');
  process.exit(2);
}
console.log(`根目录：${parent.name}（子项 ${child.name}） / ${other.name}`);

/* ---------------------------------------------------------------- 起浏览器 */
const chromePath = findChrome();
if (!chromePath) { console.error('未找到 Chrome / Edge'); process.exit(2); }

const profile = mkdtempSync(join(tmpdir(), 'tagpot-cdp-'));
const chrome = spawn(chromePath, [
  '--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  '--no-first-run', '--no-default-browser-check', '--disable-extensions', '--disable-gpu',
  '--hide-scrollbars', '--force-device-scale-factor=1', '--window-size=1440,900', 'about:blank',
], { stdio: 'ignore' });

let ws = null;
try {
  let target = null;
  for (let i = 0; i < 120 && !target; i++) {
    await sleep(250);
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      target = list.find((t) => t.type === 'page' && t.webSocketDebuggerUrl);
    } catch { }
  }
  if (!target) throw new Error('无法连接 Chrome 调试端口');

  ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
  const cdp = new CDP(ws);
  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');
  await cdp.send('DOM.enable');
  await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });

  // ---------- 静态资源 MIME（Windows 上 mimetypes 可能被注册表污染）
  console.log('\n[0] 静态资源');
  for (const [path, want] of [['/static/app.js', 'javascript'], ['/static/style.css', 'css']]) {
    const r = await fetch(BASE + path);
    const ct = (r.headers.get('content-type') || '').split(';')[0];
    check(`${path} Content-Type 含 ${want}`, ct, (v) => v.includes(want));
  }

  await cdp.send('Page.navigate', { url: BASE + '/' });
  let ready = false;
  for (let i = 0; i < 240; i++) {
    try { if (await cdp.eval("document.documentElement.dataset.appReady === 'true'")) { ready = true; break; } } catch { }
    await sleep(250);
  }
  check('应用初始化完成 appReady', ready, true);
  await sleep(900);

  console.log('\n[1] 侧栏首屏');
  check('品牌名', await cdp.eval("document.querySelector('.brand-text h1').textContent"), 'Tag Pot');
  check('菜单根项数量', await cdp.eval("document.querySelectorAll('#menu > .group').length"), rootList.length);
  const firstPath = await cdp.eval("document.getElementById('pathText').textContent");
  check('路径自动同步为某个根目录', rootList.some((r) => r.path === firstPath), true);

  // ---------- 点开带子文件夹的根
  const clickItem = (name) => cdp.eval(
    `[...document.querySelectorAll('#menu .item')].find(e => e.querySelector('.label').textContent === ${JSON.stringify(name)}).click()`
  );
  // 首屏可能已自动展开该根，先确保处于关闭态，再点一次验证「点击展开」
  const subH = `(()=>{const g=[...document.querySelectorAll('#menu > .group')].find(g=>g.dataset.path===${JSON.stringify(parent.path)}); return g ? g.querySelector('.sub').offsetHeight : 0;})()`;
  if (await cdp.eval(subH) > 30) {
    await clickItem(parent.name);
    await sleep(650);
  }
  await clickItem(parent.name);
  await sleep(850);
  check('路径同步为父级', await cdp.eval("document.getElementById('pathText').textContent"), parent.path);
  check('子菜单已就地展开', await cdp.eval(
    `(()=>{const g=[...document.querySelectorAll('#menu > .group')].find(g=>g.dataset.path===${JSON.stringify(parent.path)}); return g.querySelector('.sub').offsetHeight;})()`
  ), (v) => v > 30);
  check('子项文本与后端一致', await cdp.eval(
    `(()=>{const g=[...document.querySelectorAll('#menu > .group')].find(g=>g.dataset.path===${JSON.stringify(parent.path)}); return [...g.querySelectorAll('.sub > .sub-inner .label')].map(e=>e.textContent).join(',');})()`
  ), (await apiDirs(parent.path)).map((d) => d.name).join(','));
  check('选中块贴合当前项（位置+尺寸）', await cdp.eval(`(() => {
    const sel = document.getElementById('sel');
    const item = document.querySelector('#menu .item.active');
    if (!sel || !item) return 'no-element';
    const a = sel.getBoundingClientRect(), b = item.getBoundingClientRect();
    return Math.abs(a.top - b.top) < 2 && Math.abs(a.height - b.height) < 2 && Math.abs(a.width - b.width) < 2 ? 'match' : 'mismatch';
  })()`), 'match');
  await cdp.shot('01-initial');

  console.log('\n[2] 右侧瀑布流');
  const expectCount = (await apiImages(parent.path)).images.length;
  check(`${parent.name} 图片数与后端一致`, await cdp.eval("document.querySelectorAll('#masonry .tile').length"), expectCount);
  await cdp.eval("document.getElementById('stage').scrollTop = 99999");
  await sleep(900);
  check('图片真正解码成功', await cdp.eval("[...document.querySelectorAll('#masonry img')].filter(i=>i.naturalWidth>0).length"), expectCount);
  await cdp.eval("document.getElementById('stage').scrollTop = 0");
  await sleep(400);
  check('瀑布流多列布局', await cdp.eval(`new Set([...document.querySelectorAll('#masonry .tile')].map(t => Math.round(t.getBoundingClientRect().left))).size`), (v) => v >= 2);
  check('瀑布流无重叠', await cdp.eval(`(() => {
    const rs = [...document.querySelectorAll('#masonry .tile')].map(t=>t.getBoundingClientRect());
    for (let i=0;i<rs.length;i++) for (let j=i+1;j<rs.length;j++){
      const a=rs[i], b=rs[j];
      if (a.left < b.right-1 && b.left < a.right-1 && a.top < b.bottom-1 && b.top < a.bottom-1) return 'overlap';
    }
    return 'ok';
  })()`), 'ok');

  console.log('\n[2b] 实时刷新');
  const firstImg = (await apiImages(parent.path)).images[0];
  const ext = (firstImg.name.match(/\.[a-z]+$/i) || ['.jpg'])[0];
  const rtPath = join(parent.path, '__rt_probe__' + ext);
  copyFileSync(firstImg.path, rtPath);
  let grown = 0;
  for (let i = 0; i < 16; i++) {
    await sleep(400);
    grown = await cdp.eval("document.querySelectorAll('#masonry .tile').length");
    if (grown === expectCount + 1) break;
  }
  check('新增图片自动出现（无需手动刷新）', grown, expectCount + 1);
  unlinkSync(rtPath);
  let back = -1;
  for (let i = 0; i < 16; i++) {
    await sleep(400);
    back = await cdp.eval("document.querySelectorAll('#masonry .tile').length");
    if (back === expectCount) break;
  }
  check('删除图片自动消失', back, expectCount);
  await cdp.shot('02-realtime');

  console.log('\n[2c] 栏目实时刷新');
  const probeDir = join(parent.path, 'zz_probe_folder');
  mkdirSync(probeDir, { recursive: true });
  const hasProbe = async () => cdp.eval(
    `[...document.querySelectorAll('#menu .sub .item')].some(e => /zz_probe_folder/i.test(e.dataset.path))`
  );
  let appeared = false;
  for (let i = 0; i < 16; i++) {
    await sleep(400);
    appeared = await hasProbe();
    if (appeared) break;
  }
  check('磁盘上新增子文件夹 → 栏目里自动出现', appeared, true);

  await clickItem('zz_probe_folder');
  await sleep(800);
  check('新出现的子项可被选中', await cdp.eval("document.getElementById('pathText').textContent"), probeDir);

  rmSync(probeDir, { recursive: true, force: true });
  let vanished = false;
  for (let i = 0; i < 16; i++) {
    await sleep(400);
    vanished = !(await hasProbe());
    if (vanished) break;
  }
  check('删除子文件夹 → 栏目里自动消失', vanished, true);
  check('被删掉的选中项退回父级', await cdp.eval("document.getElementById('pathText').textContent"), parent.path);

  console.log('\n[3] 切换菜单项');
  await clickItem(other.name);
  await sleep(800);
  check('路径切换为另一个根', await cdp.eval("document.getElementById('pathText').textContent"), other.path);
  check('父级子菜单已收起（手风琴）', await cdp.eval(
    `(()=>{const g=[...document.querySelectorAll('#menu > .group')].find(g=>g.dataset.path===${JSON.stringify(parent.path)}); return g.querySelector('.sub').offsetHeight;})()`
  ), 0);
  check('选中块滑到新项', await cdp.eval(`(() => {
    const sel=document.getElementById('sel'), item=document.querySelector('#menu .item.active');
    const a=sel.getBoundingClientRect(), b=item.getBoundingClientRect();
    return Math.abs(a.top-b.top)<2 && Math.abs(a.height-b.height)<2 ? 'match' : 'mismatch';
  })()`), 'match');
  check(`${other.name} 图片数与后端一致`, await cdp.eval("document.querySelectorAll('#masonry .tile').length"), (await apiImages(other.path)).images.length);
  await cdp.shot('02-archive');

  console.log('\n[4] 快速连续切换');
  await cdp.eval(`(() => {
    const pick = (n) => [...document.querySelectorAll('#menu .item')].find(e => e.querySelector('.label').textContent === n);
    const a = pick(${JSON.stringify(other.name)}), b = pick(${JSON.stringify(parent.name)});
    a.click(); setTimeout(()=>b.click(), 60); setTimeout(()=>a.click(), 130);
  })()`);
  await sleep(1200);
  check('连续快切后仍精确落位', await cdp.eval(`(() => {
    const sel=document.getElementById('sel'), item=document.querySelector('#menu .item.active');
    const a=sel.getBoundingClientRect(), b=item.getBoundingClientRect();
    return Math.abs(a.top-b.top)<2 && Math.abs(a.height-b.height)<2 ? 'match' : 'mismatch';
  })()`), 'match');
  check('快切后路径为最后点的根', await cdp.eval("document.getElementById('pathText').textContent"), other.path);

  console.log('\n[5] 点击子文件夹');
  await clickItem(parent.name);
  await sleep(700);
  await clickItem(child.name);
  await sleep(800);
  const subSel = await cdp.eval(`(() => {
    const sel=document.getElementById('sel'), item=document.querySelector('#menu .item.active');
    const a=sel.getBoundingClientRect(), b=item.getBoundingClientRect();
    const rootW = document.querySelector('#menu > .group .item').getBoundingClientRect().width;
    return { match: Math.abs(a.top-b.top)<2 && Math.abs(a.width-b.width)<2, narrower: a.width < rootW - 4 };
  })()`);
  check('选中块跟随子项（更窄）', subSel.match && subSel.narrower, true);
  check('路径同步为子文件夹', await cdp.eval("document.getElementById('pathText').textContent"), child.path);
  check('子项图片数与后端一致', await cdp.eval("document.querySelectorAll('#masonry .tile').length"), (await apiImages(child.path)).images.length);
  check('选中子项后父级展开态保持', await cdp.eval(`(()=>{const g=[...document.querySelectorAll('#menu > .group')].find(x=>x.dataset.path===${JSON.stringify(parent.path)});
    return g.querySelector('.item').classList.contains('open') && g.querySelector('.sub').offsetHeight > 30;})()`), true);
  await cdp.shot('03-subfolder');

  console.log('\n[6] 弹窗');
  await cdp.eval("document.querySelector('#masonry .tile-inner').click()");
  await sleep(900);
  check('弹窗已打开', await cdp.eval("document.getElementById('mask').classList.contains('on')"), true);
  check('图片已加载', await cdp.eval("(()=>{const i=document.getElementById('mImg'); return !!i.src && i.naturalWidth>0;})()"), true);
  const fields = await cdp.eval(`({
    name: document.getElementById('mName').textContent,
    date: document.getElementById('mDate').textContent,
    res: document.getElementById('mRes').textContent,
    size: document.getElementById('mSize').textContent
  })`);
  check('图片名称非空', fields.name.length > 0 && fields.name !== '—', true);
  check('创建日期格式', fields.date, (v) => /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(v));
  check('分辨率', fields.res, (v) => /^\d+ × \d+$/.test(v));
  check('文件大小', fields.size, (v) => /(B|KB|MB|GB)$/.test(v));
  const metrics = await cdp.eval(`(() => {
    const t = document.getElementById('mNote');
    const side = document.querySelector('.modal-side');
    return { noteH: Math.round(t.getBoundingClientRect().height),
             tail: Math.round(side.getBoundingClientRect().bottom - t.getBoundingClientRect().bottom) };
  })()`);
  console.log('    布局度量', JSON.stringify(metrics));
  check('备注框占满剩余高度', metrics.tail, (v) => v < 90);
  await cdp.shot('04-modal');

  const original = await cdp.eval("document.getElementById('mNote').value");
  const stamp = '自动化备注 ' + Date.now();
  await cdp.eval(`(()=>{const t=document.getElementById('mNote'); t.value=${JSON.stringify(stamp)}; t.dispatchEvent(new Event('input',{bubbles:true}));})()`);
  const box = await cdp.eval("(()=>{const r=document.getElementById('mask').getBoundingClientRect(); return {x:r.left+8,y:r.top+8};})()");
  await cdp.click(box.x, box.y);
  await sleep(900);
  check('点击蒙层空白处关闭', await cdp.eval("document.getElementById('mask').classList.contains('on')"), false);
  await cdp.eval("document.querySelector('#masonry .tile-inner').click()");
  await sleep(900);
  check('备注已自动保存并回显', await cdp.eval("document.getElementById('mNote').value"), stamp);
  check('备注来源标注为文件属性', await cdp.eval("document.getElementById('mNoteSrc').textContent"), (v) => /图片属性/.test(v));
  await cdp.eval(`(()=>{const t=document.getElementById('mNote'); t.value=${JSON.stringify(original)}; t.dispatchEvent(new Event('input',{bubbles:true}));})()`);
  await cdp.eval("document.getElementById('mClose').click()");
  await sleep(800);
  check('关闭按钮可关闭', await cdp.eval("document.getElementById('mask').classList.contains('on')"), false);
  await cdp.shot('05-after-close');

  console.log('\n[7] 添加文件夹（内置选择器）');
  const before = await cdp.eval("document.querySelectorAll('#menu > .group').length");
  await cdp.eval("document.getElementById('addBtn').click()");
  await sleep(950);
  check('选择器已打开', await cdp.eval("document.getElementById('pickMask').classList.contains('on')"), true);
  check('左侧列出磁盘/常用位置', await cdp.eval("document.querySelectorAll('#pkSide .pk-side-item').length"), (v) => v >= 2);
  check('面包屑有内容', await cdp.eval("document.getElementById('pkCrumbs').textContent.trim().length"), (v) => v > 0);
  await cdp.shot('06-picker');

  // 地址栏直达目标父目录
  await cdp.eval(`(()=>{const i=document.getElementById('pkInput');
    i.value=${JSON.stringify(parent.path)};
    i.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));})()`);
  await sleep(800);
  check('地址栏跳转生效', await cdp.eval("document.getElementById('pkInput').value"), parent.path);
  check('列表出现子文件夹', await cdp.eval("document.querySelectorAll('#pkList .pk-row[data-path]').length"), (v) => v > 0);

  // 进入第一层，再返回上一级
  const firstName = await cdp.eval("document.querySelector('#pkList .pk-row[data-path] .nm').textContent");
  await cdp.eval("document.querySelector('#pkList .pk-row[data-path]').dispatchEvent(new MouseEvent('dblclick',{bubbles:true}))");
  await sleep(800);
  check('双击进入子文件夹', await cdp.eval("document.getElementById('pkInput').value"), (v) => v !== parent.path && v.endsWith(firstName));
  await cdp.eval("document.querySelector('#pkList .pk-row.up').click()");
  await sleep(800);
  check('可返回上一级', await cdp.eval("document.getElementById('pkInput').value"), parent.path);

  // 选中一个子文件夹并添加
  const addedPath = await cdp.eval("document.querySelector('#pkList .pk-row[data-path]').dataset.path");
  await cdp.eval("document.querySelector('#pkList .pk-row[data-path]').click()");
  await sleep(250);
  check('底部显示将要添加的路径', await cdp.eval("document.getElementById('pkSelPath').textContent"), addedPath);
  await cdp.eval("document.getElementById('pkOk').click()");
  await sleep(1600);
  check('选择器已关闭', await cdp.eval("document.getElementById('pickMask').classList.contains('on')"), false);
  check('菜单新增一项', await cdp.eval("document.querySelectorAll('#menu > .group').length"), before + 1);
  check('新项被选中', await cdp.eval("document.getElementById('pathText').textContent"), addedPath);
  check('新项右侧有图片', await cdp.eval("document.querySelectorAll('#masonry .tile').length"), (v) => v > 0);

  console.log('\n[7b] 拖放交互');
  const dropState = await cdp.eval(`(() => {
    const btn = document.getElementById('addBtn');
    const dt = new DataTransfer();
    btn.dispatchEvent(new DragEvent('dragenter', { bubbles: true, dataTransfer: dt }));
    const on = btn.classList.contains('drop-on');
    const label = btn.querySelector('.add-label').textContent;
    btn.dispatchEvent(new DragEvent('dragleave', { bubbles: true, dataTransfer: dt }));
    return { on, label, off: !btn.classList.contains('drop-on') };
  })()`);
  check('拖入时按钮高亮并换文案', dropState.on && /松手/.test(dropState.label), true);
  check('拖出后恢复', dropState.off, true);
  await cdp.eval(`(() => {
    const btn = document.getElementById('addBtn');
    btn.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: new DataTransfer() }));
  })()`);
  await sleep(400);
  check('空拖放给出提示', await cdp.eval("(document.querySelector('.toast') || {}).textContent || ''"), (v) => /没识别到|读不到/.test(v));

  // 清理：移除刚加的根，并把选中项还原
  const st2 = await apiState();
  const added = st2.roots.find((r) => r.path === addedPath);
  if (added) {
    await fetch(BASE + '/api/roots/remove', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: added.id }),
    });
  }
  await fetch(BASE + '/api/active', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: parent.id }),
  });

  console.log('\n[8] 控制台报错');
  check('无未捕获异常', cdp.errors.length, 0);

  console.log('\n截图：' + shots.join('\n      '));
  console.log(fails.length ? `\n❌ 失败 ${fails.length} 项：${fails.join(' / ')}` : '\n✅ 全部通过');
} catch (e) {
  console.error('\n运行出错：', e.message);
  fails.push('runtime');
} finally {
  try { if (ws) ws.close(); } catch { }
  chrome.kill();
  await sleep(800);
  try { rmSync(profile, { recursive: true, force: true }); } catch { }
  process.exit(fails.length ? 1 : 0);
}
