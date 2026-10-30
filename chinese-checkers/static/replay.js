/* Replay viewer: loads a replay JSON, renders pegs per frame. */

const HOLES = SERVER_HOLES;
const SQRT3 = Math.sqrt(3);
const R = 18;

function axialToXY(q, r) {
  return [380 + SQRT3 * 34 * (q + r / 2), 430 + 51 * r];
}
function key(c) { return c[0] + ',' + c[1]; }

const board = document.getElementById('board');
const svgNS = 'http://www.w3.org/2000/svg';
function el(tag, attrs, parent) {
  const e = document.createElementNS(svgNS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}

for (const [q, r] of HOLES) {
  const [x, y] = axialToXY(q, r);
  el('circle', { cx: x, cy: y, r: R * 0.55, class: 'hole' }, board);
}

let data = null;
let idx = 0;
let timer = null;

function draw(idx) {
  [...board.querySelectorAll('.peg')].forEach(e => e.remove());
  if (!data) return;
  const f = data.frames[idx];
  for (const player of ['red', 'blue']) {
    for (const c of (f.pegs[player] || [])) {
      const [x, y] = axialToXY(c[0], c[1]);
      el('circle', { cx: x, cy: y, r: R, class: 'peg ' + player }, board);
    }
  }
  document.getElementById('frame-info').textContent =
    `第 ${idx + 1}/${data.frames.length} 帧`;
  document.getElementById('frame-label').textContent = f.label;
}

async function load(name) {
  const res = await fetch('api/replay_file?name=' +
    encodeURIComponent(name));
  data = await res.json();
  idx = 0;
  draw(idx);
}

async function init() {
  const res = await fetch('api/replays');
  const { files } = await res.json();
  const sel = document.getElementById('replay-select');
  for (const f of files) {
    const o = document.createElement('option');
    o.value = f.name;
    o.textContent = `${f.name.slice(0, 17)}… ${f.frames}帧` +
      (f.winner ? ` 胜=${f.winner}` : '');
    sel.appendChild(o);
  }
  const params = new URLSearchParams(location.search);
  const want = params.get('name');
  if (want && files.some(f => f.name === want)) sel.value = want;
  if (sel.value) load(sel.value);
}

document.getElementById('load-btn').onclick = () =>
  load(document.getElementById('replay-select').value);
document.getElementById('prev-btn').onclick = () =>
  { if (idx > 0) draw(--idx); };
document.getElementById('next-btn').onclick = () =>
  { if (data && idx < data.frames.length - 1) draw(++idx); };
document.getElementById('auto-btn').onclick = () => {
  if (timer) { clearInterval(timer); timer = null; return; }
  timer = setInterval(() => {
    if (data && idx < data.frames.length - 1) draw(++idx);
    else { clearInterval(timer); timer = null; }
  }, 700);
};

init();
