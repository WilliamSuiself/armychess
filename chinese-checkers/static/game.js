/* Chinese Checkers front-end: renders the 121-hole star board from axial
   coords, animates steps / jump chains, polls /api/state. */

const HOLES = SERVER_HOLES;   // injected list of [q, r]
const SQRT3 = Math.sqrt(3);
const R = 18;                 // hole radius px
const DX = SQRT3 * 34, DY = 51;

function axialToXY(q, r) {
  const x = 380 + SQRT3 * 34 * (q + r / 2);
  const y = 430 + 51 * r;
  return [x, y];
}

// Each browser tab gets its own match on the server (per-tab sessionStorage
// id, survives reloads) — mirrors the armychess/doudizhu convention, so two
// visitors never share the 'default' game.
let gameId = sessionStorage.getItem('cc_game_id');
if (!gameId) {
  gameId = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
  sessionStorage.setItem('cc_game_id', gameId);
}
let state = null;
let selected = null;           // selected peg coord
let animating = false;

const board = document.getElementById('board');
const svgNS = 'http://www.w3.org/2000/svg';

function el(tag, attrs, parent) {
  const e = document.createElementNS(svgNS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}

// --- static board layer ---
const layerZones = el('g', {}, board);
const layerLinks = el('g', {}, board);
const layerPegs = el('g', {}, board);
const layerAnims = el('g', {}, board);

const ZONE_COLORS = {
  'red-start': 'rgba(239,68,68,.25)', 'blue-start': 'rgba(59,130,246,.25)',
};

function key(c) { return c[0] + ',' + c[1]; }

function initBoard() {
  // zone shading: color by distance to each arm apex (approx visual only)
  for (const [q, r] of HOLES) {
    const [x, y] = axialToXY(q, r);
    const c = el('circle', { cx: x, cy: y, r: R * 0.55,
      class: 'hole', 'data-hole': key([q, r]) }, layerZones);
    c.addEventListener('click', () => onHoleClick([q, r]));
  }
}

function holeEl(c) {
  return board.querySelector(`[data-hole="${key(c)}"]`);
}

function drawPegs() {
  [...layerPegs.children].forEach(e => e.remove());
  if (!state) return;
  for (const seat of state.seats) {
    for (const c of seat.pegs) {
      const [x, y] = axialToXY(c[0], c[1]);
      const g = el('g', { class: 'peg ' + seat.player,
        'data-peg': key(c) }, layerPegs);
      el('circle', { cx: x, cy: y, r: R }, g);
      el('circle', { cx: x - 5, cy: y - 6, r: R * 0.35, class: 'glint' }, g);
      if (seat.controller === 'human' && state.turn === seat.seat) {
        g.addEventListener('click', () => selectPeg(c));
        g.classList.add('own');
      }
    }
  }
}

function drawHints() {
  [...layerAnims.children].forEach(e => e.remove());
  if (!state || selected == null) return;
  for (const mv of state.legal_moves) {
    if (key(mv[0]) !== key(selected)) continue;
    const end = mv[mv.length - 1];
    const [x, y] = axialToXY(end[0], end[1]);
    const c = el('circle', { cx: x, cy: y, r: R * 0.7, class: 'hint',
      'data-target': key(end) }, layerAnims);
    c.addEventListener('click', () => playMove(mv));
    // draw chain polyline
    const pts = mv.map(p => axialToXY(p[0], p[1]).join(',')).join(' ');
    el('polyline', { points: pts, class: 'hint-path' }, layerAnims);
  }
}

function selectPeg(c) {
  if (animating) return;
  selected = (selected && key(selected) === key(c)) ? null : c;
  drawHints();
}

function onHoleClick(c) {
  // click on a hint target triggers the move (hints are drawn above holes)
  drawHints();
}

async function playMove(mv) {
  if (animating) return;
  animating = true;
  clearHints();
  // animate along the path
  const pegG = board.querySelector(`[data-peg="${key(mv[0])}"]`);
  if (pegG) {
    for (const dest of mv.slice(1)) {
      const [x, y] = axialToXY(dest[0], dest[1]);
      pegG.querySelector('circle').setAttribute('cx', x);
      pegG.querySelector('circle').setAttribute('cy', y);
      await sleep(140);
    }
  }
  const res = await fetch('api/move', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Game-Id': gameId },
    body: JSON.stringify({ seat: state.human_seat, path: mv }),
  });
  const data = await res.json();
  if (data.error) alert(data.error);
  selected = null;
  animating = false;
  update(data);
}

function clearHints() {
  [...layerAnims.children].forEach(e => e.remove());
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// --- state sync ---
function update(s) {
  state = s;
  gameId = s.id;
  sessionStorage.setItem('cc_game_id', s.id);
  drawPegs();
  drawHints();
  // seats
  const [red, blue] = s.seats;
  setText('name-top', '蓝方 · ' + backendName(blue.controller));
  setText('name-bottom', '红方 · ' + backendName(red.controller));
  setText('info-top', `到家 ${blue.in_home}/10`);
  setText('info-bottom', `到家 ${red.in_home}/10`);
  setText('badge-top', blue.ai_thinking ? '思考中…' : '');
  setText('badge-bottom', red.ai_thinking ? '思考中…' : '');
  const humanTurn = s.human_seat !== null && s.turn === s.human_seat && !s.winner;
  setText('turn-hint', s.winner
    ? `🏁 ${s.winner === 'red' ? '红方' : '蓝方'}获胜！`
    : (humanTurn ? '轮到你（红方）— 点击棋子，再点目标' : '等待 AI …'));
  renderAI('ai-top', blue);
  renderAI('ai-bottom', red);
  if (s.winner) showWinner(s.winner);
}

function backendName(c) { return c === 'human' ? '玩家' : (c === 'jev' ? 'Jev' : 'Laya'); }

function renderAI(id, seat) {
  const box = document.getElementById(id);
  const d = seat.last_decision;
  if (!d) { box.innerHTML = '—'; return; }
  box.innerHTML = `<b>${d.label}</b>` + (d.reason ? ` · ${d.reason}` : '') +
    (d.io && d.io.request ? ' <a href="#" class="io-toggle">[查看完整请求]</a>' +
      `<pre class="io-pre" style="display:none">${
        escapeHtml(JSON.stringify(d.io.request, null, 2))}\n--- 回答 ---\n${
        escapeHtml(JSON.stringify(d.io.response, null, 2))}</pre>` : '');
  const link = box.querySelector('.io-toggle');
  if (link) link.addEventListener('click', (e) => {
    e.preventDefault();
    const pre = box.querySelector('.io-pre');
    pre.style.display = pre.style.display === 'none' ? 'block' : 'none';
  });
}

function escapeHtml(s) {
  return s.replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
}

function setText(id, t) {
  const e = document.getElementById(id);
  if (e) e.textContent = t;
}

function showWinner(winner) {
  setText('winner-text', `🏁 ${winner === 'red' ? '红方' : '蓝方'}获胜！`);
  document.getElementById('winner-overlay').style.display = 'flex';
}

// --- controls ---
async function newMatch() {
  const ctls = [document.getElementById('ctl-0').value,
                document.getElementById('ctl-1').value];
  const res = await fetch('api/new_match', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Game-Id': gameId },
    body: JSON.stringify({ controllers: ctls }),
  });
  const data = await res.json();
  if (data.error) { alert(data.error); return; }
  document.getElementById('winner-overlay').style.display = 'none';
  selected = null;
  update(data);
}

document.getElementById('start-btn').addEventListener('click', newMatch);
document.getElementById('rematch-btn').addEventListener('click', newMatch);
document.getElementById('close-winner-btn').addEventListener('click', () => {
  document.getElementById('winner-overlay').style.display = 'none';
});

async function poll() {
  try {
    const res = await fetch('api/state', { headers: { 'X-Game-Id': gameId } });
    const s = await res.json();
    if (!animating && !selected) update(s);
    else if (!animating) { /* keep selection visuals */ }
  } catch (e) { /* server restarting */ }
}

initBoard();
poll();
setInterval(poll, 1000);
