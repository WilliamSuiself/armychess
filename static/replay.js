// Replay viewer — god-view playback of saved games (live games and
// head-to-head matches). Reads frames from /api/replay_file.

const ICONS = {
  "司令": "⭐", "军长": "🎖️", "师长": "🏅", "旅长": "🎗️", "团长": "🔰",
  "营长": "🪖", "连长": "🛡️", "排长": "⚔️", "工兵": "🔧", "地雷": "💣",
  "炸弹": "🧨", "军旗": "🚩", "?": "❓",
};

// Same measured point layout as game.js (769x1024 board image).
const COL_X = [88.5, 234.5, 384, 533.5, 682.5].map(x => x / 769 * 100);
const ROW_Y = [77, 145, 207, 275, 343, 414, 609, 680, 748, 816, 878, 946]
  .map(y => y / 1024 * 100);
const CELL_W = 132 / 769 * 100;
const CELL_H = 56 / 1024 * 100;

const boardEl = document.getElementById("board");
const slider = document.getElementById("rp-slider");
const labelEl = document.getElementById("rp-label");
const titleEl = document.getElementById("replay-title");
const filesEl = document.getElementById("files");

let frames = [];
let idx = 0;
let timer = null;
let winner = null;

function renderBoard(board) {
  boardEl.innerHTML = "";
  for (let r = 0; r < board.length; r++) {
    for (let c = 0; c < board[r].length; c++) {
      const cell = board[r][c];
      const div = document.createElement("div");
      div.className = "cell";
      div.style.left = `${COL_X[c]}%`;
      div.style.top = `${ROW_Y[r]}%`;
      div.style.width = `${CELL_W}%`;
      div.style.height = `${CELL_H}%`;
      if (cell) {
        if (cell.terrain === "camp") div.classList.add("terrain-camp");
        if (cell.terrain === "hq") div.classList.add("terrain-hq");
        if (cell.rail) div.classList.add("rail");
        const piece = cell.piece;
        if (piece) {
          // God-view replay has no "human side" — colour is a fixed mapping:
          // ai/蓝方 = blue, player/红方 = red. Matches the live board, where
          // "own"(blue)/"enemy"(red) styling happens to be the same colours.
          div.classList.add(piece.owner === "ai" ? "own" : "enemy");
          const badge = document.createElement("div");
          badge.className = "piece-badge " +
            (piece.owner === "ai" ? "badge-own" : "badge-enemy");
          const icon = document.createElement("div");
          icon.className = "piece-icon";
          icon.textContent = ICONS[piece.type] || "?";
          const lbl = document.createElement("div");
          lbl.className = "piece-type";
          lbl.textContent = piece.type;
          badge.appendChild(icon);
          badge.appendChild(lbl);
          div.appendChild(badge);
        }
      }
      boardEl.appendChild(div);
    }
  }
}

function show(i) {
  idx = Math.max(0, Math.min(i, frames.length - 1));
  const f = frames[idx];
  renderBoard(f.board);
  slider.value = idx;
  let tail = "";
  if (idx === frames.length - 1 && winner) {
    tail = ` — 🏁 胜者: ${winner}`;
  }
  labelEl.textContent = `${idx + 1}/${frames.length} · ${f.label}${tail}`;
}

function stopAutoplay() {
  if (timer) { clearInterval(timer); timer = null; }
  document.getElementById("rp-autoplay").textContent = "▶ 自动播放";
}

async function loadFile(name, btn) {
  stopAutoplay();
  document.querySelectorAll(".replay-item").forEach(el => el.classList.remove("active"));
  btn.classList.add("active");
  const r = await fetch(`/api/replay_file?name=${encodeURIComponent(name)}`);
  const data = await r.json();
  frames = data.frames || [];
  winner = data.winner;
  const meta = data.meta || {};
  if (meta.player_side) {
    titleEl.innerHTML =
      `<b>${meta.player_side}</b>(先手) vs <b>${meta.ai_side}</b>(后手) · ${frames.length} 帧`;
    document.getElementById("side-top").textContent = `AI 侧（上方）= ${meta.ai_side}`;
    document.getElementById("side-bottom").textContent = `玩家侧（下方，先手）= ${meta.player_side}`;
  } else {
    titleEl.innerHTML = `${name} · ${frames.length} 帧`;
    document.getElementById("side-top").textContent = "AI 侧（上方）";
    document.getElementById("side-bottom").textContent = "玩家侧（下方，先手）";
  }
  slider.max = Math.max(0, frames.length - 1);
  show(0);
}

async function listFiles() {
  const r = await fetch("/api/replays");
  const data = await r.json();
  filesEl.innerHTML = "";
  for (const f of data.files) {
    const div = document.createElement("div");
    div.className = "replay-item";
    const d = new Date(f.mtime * 1000);
    const head = f.meta && f.meta.player_side
      ? `${f.meta.player_side} vs ${f.meta.ai_side}`
      : f.name.replace(/\.json$/, "");
    div.innerHTML = `${head}<div class="sub">${d.toLocaleString()} · ${f.frames}帧` +
      (f.winner ? ` · 胜:${f.winner}` : " · 进行中") + `</div>`;
    div.addEventListener("click", () => loadFile(f.name, div));
    filesEl.appendChild(div);
  }
}

document.getElementById("rp-prev").addEventListener("click", () => { stopAutoplay(); show(idx - 1); });
document.getElementById("rp-next").addEventListener("click", () => { stopAutoplay(); show(idx + 1); });
slider.addEventListener("input", e => { stopAutoplay(); show(+e.target.value); });
document.getElementById("rp-autoplay").addEventListener("click", () => {
  if (timer) { stopAutoplay(); return; }
  if (idx >= frames.length - 1) show(0);
  document.getElementById("rp-autoplay").textContent = "⏸ 暂停";
  timer = setInterval(() => {
    if (idx >= frames.length - 1) { stopAutoplay(); return; }
    show(idx + 1);
  }, 900);
});

listFiles();
