// Army Chess × Jev — frontend logic

// Each browser tab gets its own game on the server. sessionStorage is
// per-tab and survives reloads, so refreshing or switching tabs keeps this
// tab's game, while other tabs / test scripts can't touch it.
const GAME_ID = (() => {
  let id = sessionStorage.getItem("armyChessGameId");
  if (!id) {
    id = (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`);
    sessionStorage.setItem("armyChessGameId", id);
  }
  return id;
})();

function apiFetch(url, opts = {}) {
  opts.headers = Object.assign({ "X-Game-Id": GAME_ID }, opts.headers || {});
  return fetch(url, opts);
}

const ICONS = {
  "司令": "⭐",
  "军长": "🎖️",
  "师长": "🏅",
  "旅长": "🎗️",
  "团长": "🔰",
  "营长": "🪖",
  "连长": "🛡️",
  "排长": "⚔️",
  "工兵": "🔧",
  "地雷": "💣",
  "炸弹": "🧨",
  "军旗": "🚩",
  "?": "❓",
};

const POOL_TYPES = ["军旗", "司令", "军长", "师长", "旅长", "团长", "营长", "连长", "排长", "工兵", "地雷", "炸弹"];

// Point centers measured in pixels on the 769x1024 board background image.
// The middle band (front line / mountain gap) is much taller than a normal
// row spacing, so a uniform CSS grid can never align — we position each cell
// absolutely at its measured point instead.
const COL_X = [88.5, 234.5, 384, 533.5, 682.5].map(x => x / 769 * 100);
const ROW_Y = [77, 145, 207, 275, 343, 414, 609, 680, 748, 816, 878, 946]
  .map(y => y / 1024 * 100);
const CELL_W = 132 / 769 * 100;   // tile ~88% of column pitch
const CELL_H = 56 / 1024 * 100;   // tile ~82% of row pitch

let selected = null;
let possibleTargets = [];
let state = null;
let busy = false;

// Setup mode state
let setupPieceType = null;  // currently selected piece from pool

// Replay: snapshots of every board state this session, so the player can
// step back through the game afterwards.
let historyFrames = [];   // [{board, label}]
let replayMode = false;
let replayIdx = 0;

function recordFrame(board, label) {
  historyFrames.push({ board: JSON.parse(JSON.stringify(board)), label });
}

function eventLabel(actor, ev) {
  const who = actor === "player" ? "玩家" : "AI";
  let t = `${who} (${ev.from[0]},${ev.from[1]})→(${ev.to[0]},${ev.to[1]})`;
  if (ev.combat) t += ` 战斗:${formatOutcome(ev.outcome)}`;
  return t;
}

const boardEl = document.getElementById("board");
const aiEl = document.getElementById("ai-thinking");
const logEl = document.getElementById("log");
const statusEl = document.getElementById("status");
const expEl = document.getElementById("exp-stats");
const resetBtn = document.getElementById("reset");
const setupPanel = document.getElementById("setup-panel");
const aiPanel = document.getElementById("ai-panel");
const poolEl = document.getElementById("pool");
const formationsEl = document.getElementById("formations");
const setupStatsEl = document.getElementById("setup-stats");
const setupStartBtn = document.getElementById("setup-start");
const saveFormationBtn = document.getElementById("save-formation-btn");
const formationNameEl = document.getElementById("formation-name");
const saveFormationStatusEl = document.getElementById("save-formation-status");
const jevIoEl = document.getElementById("jev-io");
const replayBtn = document.getElementById("replay-btn");
const replayBar = document.getElementById("replay-bar");
const replaySlider = document.getElementById("replay-slider");
const replayLabel = document.getElementById("replay-label");

resetBtn.addEventListener("click", async () => {
  // Don't reset if we're already in setup (user might just be exploring)
  if (state && state.phase === "playing") {
    if (!confirm("确定要重新开始吗？\n当前对局将被清空。")) return;
  } else if (state && state.phase === "ended") {
    if (!confirm("游戏已结束。开始新游戏？")) return;
  } else if (state && state.phase === "setup") {
    const placed = Object.keys(state.setup?.placed || {}).length;
    if (placed > 0 && !confirm(`确定要清空已摆放的 ${placed} 个棋子？`)) return;
  }
  await apiFetch("/api/reset", { method: "POST" });
  selected = null;
  possibleTargets = [];
  setupPieceType = null;
  historyFrames = [];
  exitReplay();
  jevIoEl.textContent = "（AI 行动后显示提交给 Jev 的完整 state/questions 及其返回）";
  logEl.innerHTML = "";
  await refresh();
  setStatus("布置你的棋盘（25枚棋子）");
});

setupStartBtn.addEventListener("click", async () => {
  const r = await apiFetch("/api/setup/start", { method: "POST" }).then(r => r.json());
  if (!r.ok) {
    setStatus(`开始失败: ${r.error}`);
    return;
  }
  await refresh();
  setStatus("游戏开始！点击一个我方棋子走第一步");
});

if (saveFormationBtn) {
  saveFormationBtn.addEventListener("click", async () => {
    const name = formationNameEl.value.trim();
    const r = await apiFetch("/api/setup/save_custom", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }).then(r => r.json());
    if (!r.ok) {
      saveFormationStatusEl.className = "save-formation-status err";
      saveFormationStatusEl.textContent = `保存失败: ${r.error}`;
      return;
    }
    saveFormationStatusEl.className = "save-formation-status ok";
    saveFormationStatusEl.textContent = `✓ 已保存「${r.name}」— 已加入上方阵型列表，可随时应用`;
    formationNameEl.value = "";
    await refresh();
  });
}

async function refresh() {
  const r = await apiFetch("/api/state").then(r => r.json());
  state = r;
  if (!replayMode) renderBoard(r.board);
  renderSidePanel(r);
  renderExperience();
  // First playing-phase board = the opening frame for replays.
  if (r.phase === "playing" && historyFrames.length === 0) {
    recordFrame(r.board, "开局");
  }
  if (r.winner) {
    renderWinner(r.winner);
  }
  if (r.ai_probs) {
    renderAIDecision(r.ai_probs);
    if (r.ai_probs.jev_io) renderJevIO(r.ai_probs.jev_io);
  }
  // Page was (re)loaded while an AI move was still pending — resume it so the
  // game doesn't get stuck waiting for a move that was never requested.
  if (r.awaiting_ai && !busy) {
    busy = true;
    setBoardInteractive(false);
    setStatus("AI 思考中...");
    try {
      await fetchAiMove();
    } finally {
      busy = false;
      setBoardInteractive(true);
    }
  }
}

async function renderExperience() {
  try {
    const exp = await apiFetch("/api/experience").then(r => r.json());
    const wr = exp.games_played ? ((exp.wins / exp.games_played) * 100).toFixed(0) : "-";
    expEl.innerHTML = `本地经验文件：<b>${exp.games_played}</b> 局已记录 · AI 胜率 <b>${wr}${exp.games_played ? "%" : ""}</b>`;
  } catch (e) {
    expEl.textContent = "";
  }
}

function renderSidePanel(state) {
  if (state.phase === "setup") {
    setupPanel.style.display = "block";
    aiPanel.style.display = "none";
    renderFormations(state.formations || [], state.setup?.preset);
    renderPool(state.setup?.pool || []);
    renderSetupStats(state.setup?.placed || {});
  } else {
    setupPanel.style.display = "none";
    aiPanel.style.display = "block";
  }
}

function renderFormations(formations, selectedIdx) {
  formationsEl.innerHTML = "";
  formations.forEach((f, idx) => {
    const item = document.createElement("div");
    item.className = "formation-item" + (f.custom ? " custom" : "") +
                     (selectedIdx === idx ? " selected" : "");
    const tag = f.custom ? " 💾" : "";
    item.innerHTML = `<div class="fname">${idx + 1}. ${escapeHtml(f.name)}${tag}</div><div class="fdesc">${escapeHtml(f.desc)}</div>`;
    item.addEventListener("click", () => applyFormation(idx));
    formationsEl.appendChild(item);
  });
}

async function applyFormation(idx) {
  const r = await apiFetch("/api/setup/preset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ index: idx }),
  }).then(r => r.json());
  if (!r.ok) {
    setStatus(`应用阵型失败: ${r.error}`);
    return;
  }
  setupPieceType = null;
  await refresh();
  setStatus(`已应用阵型「${r.name}」，可直接开始，或继续手动微调`);
}

function renderPool(pool) {
  // Count by type
  const counts = {};
  for (const t of pool) counts[t] = (counts[t] || 0) + 1;
  poolEl.innerHTML = "";
  for (const t of POOL_TYPES) {
    const count = counts[t] || 0;
    const item = document.createElement("div");
    item.className = "pool-item" + (count === 0 ? " empty" : "") +
                     (setupPieceType === t ? " selected" : "");
    item.innerHTML = `${ICONS[t]} ${t} ×${count}`;
    if (count > 0) {
      item.addEventListener("click", () => {
        setupPieceType = t;
        refresh();
      });
    }
    poolEl.appendChild(item);
  }
}

function renderSetupStats(placed) {
  const count = Object.keys(placed).length;
  setupStatsEl.innerHTML = `
    已放置: <b>${count}/25</b><br>
    ${count === 25
      ? '<span style="color:#4ade80">✓ 已完成，可以开始游戏</span>'
      : '继续从下方选棋子 → 点击棋盘放置'}
  `;
  setupStartBtn.disabled = count !== 25;
}

function renderBoard(board) {
  boardEl.innerHTML = "";
  for (let r = 0; r < board.length; r++) {
    for (let c = 0; c < board[r].length; c++) {
      const cell = board[r][c];
      const div = document.createElement("div");
      div.className = "cell";
      div.dataset.pos = `${r},${c}`;
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
          if (piece.owner === "player") {
            div.classList.add("own");
          } else {
            div.classList.add("enemy");
            if (piece.revealed) div.classList.add("revealed");
          }
          addPieceIcon(div, piece.type, piece.owner);
        } else if (cell.history) {
          // Not a live piece — just a small "we once saw X die/leave here"
          // memory badge, clearly distinct from an actual piece on the board.
          div.classList.add("history");
          const tag = document.createElement("div");
          tag.className = "history-tag";
          tag.textContent = `✝ ${cell.history}`;
          div.appendChild(tag);
        }
      }

      if (selected && selected[0] === r && selected[1] === c) {
        div.classList.add("selected");
      }
      if (possibleTargets.some(([tr, tc]) => tr === r && tc === c)) {
        div.classList.add("movable");
      }

      div.addEventListener("click", () => onCellClick(r, c));
      boardEl.appendChild(div);
    }
  }
}

function addPieceIcon(div, type, owner) {
  const badge = document.createElement("div");
  badge.className = "piece-badge " + (owner === "player" ? "badge-own" : "badge-enemy");

  if (type === "?") {
    // Face-down piece — a blank tile with no text. Everyone knows it's unknown.
    badge.classList.add("face-down");
  } else {
    const icon = document.createElement("div");
    icon.className = "piece-icon";
    icon.textContent = ICONS[type] || "?";
    badge.appendChild(icon);

    const label = document.createElement("div");
    label.className = "piece-type";
    label.textContent = type;
    badge.appendChild(label);
  }

  div.appendChild(badge);
}

async function onCellClick(r, c) {
  if (busy || replayMode) return;

  // Setup phase: place / remove pieces
  if (state?.phase === "setup") {
    await handleSetupClick(r, c);
    return;
  }

  if (state?.winner || state?.phase === "ended") return;
  const cell = state.board[r][c];
  const piece = cell?.piece;

  if (possibleTargets.some(([tr, tc]) => tr === r && tc === c)) {
    submitMove(selected, [r, c]);
    return;
  }
  if (piece && piece.owner === "player") {
    selectPiece([r, c]);
    return;
  }
  selected = null;
  possibleTargets = [];
  refresh();
  setStatus("已取消选择");
}

async function handleSetupClick(r, c) {
  // Click on existing own piece = remove
  const existing = state.setup?.placed?.[`${r},${c}`];
  if (existing) {
    await apiFetch("/api/setup/remove", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pos: [r, c] }),
    });
    await refresh();
    return;
  }

  if (!setupPieceType) {
    setStatus("请先从下方选择要放置的棋子");
    return;
  }

  const r2 = await apiFetch("/api/setup/place", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pos: [r, c], type: setupPieceType }),
  }).then(r => r.json());
  if (!r2.ok) {
    setStatus(`放置失败: ${r2.error}`);
    return;
  }
  // Clear selection if pool exhausted
  if (!r2.pool.includes(setupPieceType)) {
    setupPieceType = null;
  }
  await refresh();
}

async function selectPiece(pos) {
  selected = pos;
  const r = await apiFetch("/api/moves", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pos }),
  }).then(r => r.json());
  possibleTargets = r.moves || [];
  refresh();
  if (possibleTargets.length === 0) {
    setStatus("这个棋子无路可走");
    selected = null;
  } else {
    setStatus(`选中 (${pos[0]},${pos[1]}) — ${possibleTargets.length} 个可选目标，点击绿色格子移动`);
  }
}

async function submitMove(from, to) {
  busy = true;
  setStatus("移动中...");
  setBoardInteractive(false);

  try {
    // Capture the icon currently shown at `from` (before the server
    // response replaces the board) so the animation shows the right piece.
    const movingIcon = iconAt(from);

    // Player move resolves instantly — combat is computed locally, no wait.
    const r = await apiFetch("/api/move", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ from, to }),
    }).then(r => r.json());

    if (!r.ok) {
      setStatus(`错误: ${r.error}`);
      return;
    }

    if (r.event) {
      setStatus("结算中...");
      await animateMove(from, to, movingIcon, !!r.event.combat);
      appendLog(formatEvent("player", r.event));
      recordFrame(r.board, eventLabel("player", r.event));
    }

    state = r;
    selected = null;
    possibleTargets = [];
    renderBoard(r.board);
    flashCell(to);          // landing highlight stays visible after re-render
    renderExperience();

    if (r.winner) {
      renderWinner(r.winner);
      setStatus(r.winner === "player" ? "🎉 你赢了！" : "💀 AI 赢了");
      return;
    }

    // Now ask Jev for the AI's move — this is the slow part, shown as
    // "AI 思考中" while the player's result is already on the board.
    if (r.awaiting_ai) {
      setStatus("AI 思考中...");
      await fetchAiMove();
    }
  } catch (e) {
    setStatus(`请求失败: ${e.message}`);
  } finally {
    busy = false;
    setBoardInteractive(true);
  }
}

async function fetchAiMove() {
  const r = await apiFetch("/api/ai_move", { method: "POST" }).then(r => r.json());

  if (!r.ok) {
    // e.g. "no ai move pending" — never touch the board with undefined data.
    setStatus(`AI 回合异常: ${r.error || "unknown"}`);
    return;
  }

  if (r.ai_move) {
    const am = r.ai_move;
    setStatus("AI 行动中...");
    const aiIcon = iconAt(am.event.from);
    await animateMove(am.event.from, am.event.to, aiIcon, !!am.event.combat);
    appendLog(formatAIMove(am.decision, am.event));
    renderBoard(r.board);
    recordFrame(r.board, eventLabel("ai", am.event));
    flashCell(am.event.to);   // keep the AI's landing square lit so it's
                              // obvious which enemy piece just moved
    renderAIDecision(am.decision);
    renderJevIO(am.decision.jev_io);
  } else if (r.ai_unavailable) {
    appendLog(`<span class="badge-ai">AI</span> 不可用: ${escapeHtml(r.ai_unavailable)}`);
    if (r.board) renderBoard(r.board);
    setStatus("⚠️ Jev 服务暂时不可用，请稍后重试");
  } else if (r.ai_move_error) {
    appendLog(`<span class="badge-ai">AI</span> 行动失败: ${r.ai_move_error}`);
    if (r.board) renderBoard(r.board);
  }

  state = r;
  renderExperience();
  if (r.winner) {
    renderWinner(r.winner);
    setStatus(r.winner === "player" ? "🎉 你赢了！" : "💀 AI 赢了");
  } else if (!r.ai_unavailable) {
    setStatus("点击一个我方棋子继续");
  }
}

// Briefly glow a cell on the CURRENT board — used to mark where a piece
// just landed, after renderBoard has rebuilt the DOM.
function flashCell(pos, ms = 800) {
  const el = cellEl(pos);
  if (!el) return;
  el.classList.add("move-dst");
  setTimeout(() => el.classList.remove("move-dst"), ms);
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function cellEl(pos) {
  return boardEl.children[pos[0] * 5 + pos[1]];
}

function iconAt(pos) {
  const el = cellEl(pos);
  const iconEl = el && el.querySelector(".piece-icon");
  return iconEl ? iconEl.textContent : "?";
}

// Move animation, three beats so even a 1-step move is easy to follow:
//   1) the SOURCE cell glows first — "this piece is about to move";
//   2) a glowing ghost tile slides to the destination;
//   3) on combat, the destination shakes + 💥.
// A post-render landing glow is applied separately via flashCell().
async function animateMove(from, to, icon, isCombat) {
  const fromEl = cellEl(from);
  const toEl = cellEl(to);
  if (!fromEl || !toEl) return;

  // Beat 1: highlight the piece that's about to move.
  fromEl.classList.add("move-src");
  await sleep(300);

  const fromRect = fromEl.getBoundingClientRect();
  const toRect = toEl.getBoundingClientRect();

  // Beat 2: ghost tile slides over the board.
  const ghost = document.createElement("div");
  ghost.className = "move-ghost";
  ghost.textContent = icon;
  ghost.style.left = `${fromRect.left}px`;
  ghost.style.top = `${fromRect.top}px`;
  ghost.style.width = `${fromRect.width}px`;
  ghost.style.height = `${fromRect.height}px`;
  document.body.appendChild(ghost);

  // Force layout before transitioning so the browser animates from `from`.
  ghost.getBoundingClientRect();
  ghost.style.transform = `translate(${toRect.left - fromRect.left}px, ${toRect.top - fromRect.top}px)`;

  await sleep(420);
  ghost.remove();
  fromEl.classList.remove("move-src");

  // Beat 3: combat feedback on the target square.
  if (isCombat) {
    toEl.classList.add("combat-flash");
    const boom = document.createElement("div");
    boom.className = "combat-boom";
    boom.textContent = "💥";
    toEl.appendChild(boom);
    await sleep(300);
    toEl.classList.remove("combat-flash");
  }
}

function setBoardInteractive(on) {
  boardEl.style.opacity = on ? "1" : "0.6";
  boardEl.style.pointerEvents = on ? "auto" : "none";
}

function setStatus(text) {
  statusEl.textContent = text;
}

function appendLog(html) {
  const li = document.createElement("li");
  li.innerHTML = html;
  logEl.prepend(li);
}

function formatEvent(actor, ev) {
  const badgeClass = actor === "player" ? "badge-player" : "badge-ai";
  const label = actor === "player" ? "玩家" : "AI";
  let text = `<span class="${badgeClass}">${label}</span> (${ev.from[0]},${ev.from[1]}) → (${ev.to[0]},${ev.to[1]})`;
  if (ev.combat) {
    const { playerType, enemyLabel } = describeCombatForPlayer(actor, ev);
    const [attackerLabel, defenderLabel] = actor === "player"
      ? [playerType, enemyLabel]
      : [enemyLabel, playerType];
    text += ` · <span class="badge-combat">[战斗]</span> ${attackerLabel} vs ${defenderLabel} → ${formatOutcome(ev.outcome)}`;
  }
  text += formatFlagIntel(ev, actor);
  return text;
}

// Flag-intel notices: HQ deduction and the 亮旗 rule (a dead 司令 exposes
// its side's flag). `actor` is who made the move that produced the event.
function formatFlagIntel(ev, actor) {
  const me = actor === "player";
  let t = "";
  if (ev.flag_deduced) {
    t += me
      ? ` · <span class="badge-flag">🚩 该大本营无军旗 → 推断对方军旗在 (${ev.flag_deduced[0]},${ev.flag_deduced[1]})</span>`
      : ` · <span class="badge-flag">⚠️ AI 推断出我方军旗在 (${ev.flag_deduced[0]},${ev.flag_deduced[1]})</span>`;
  }
  for (const fr of ev.flags_revealed || []) {
    t += fr.owner === "ai"
      ? ` · <span class="badge-flag">🚩 AI 司令阵亡 → 其军旗亮出 (${fr.pos[0]},${fr.pos[1]})</span>`
      : ` · <span class="badge-flag">⚠️ 我方司令阵亡 → 军旗被迫亮出 (${fr.pos[0]},${fr.pos[1]})</span>`;
  }
  return t;
}

// If the player's own piece lost this exchange, don't reveal what the
// enemy piece actually was — just show "敌方未知" in the log.
function describeCombatForPlayer(actor, ev) {
  const playerType = actor === "player" ? ev.attacker_type : ev.defender_type;
  const enemyType = actor === "player" ? ev.defender_type : ev.attacker_type;

  const playerLost = actor === "player"
    ? (ev.outcome === "defender_wins" || ev.outcome === "both_die")
    : (ev.outcome === "attacker_wins" || ev.outcome === "both_die" || ev.outcome === "flag_taken_ai");

  return {
    playerType,
    enemyLabel: playerLost ? "敌方未知" : enemyType,
  };
}

// One single combined log line for the AI's move: coordinates + combat
// result (if any) + its strategic reasoning — previously this was two
// separate log lines (this summary AND a plain move-event line), which
// looked like the AI had taken an extra, unexplained second move.
function formatAIMove(decision, ev) {
  let text = `<span class="badge-ai">AI</span> (${ev.from[0]},${ev.from[1]}) → (${ev.to[0]},${ev.to[1]})`;

  if (ev.combat) {
    const { playerType, enemyLabel } = describeCombatForPlayer("ai", ev);
    text += ` · <span class="badge-combat">[战斗]</span> ${enemyLabel} vs ${playerType} → ${formatOutcome(ev.outcome)}`;
  }
  text += formatFlagIntel(ev, "ai");

  const labels = [];
  if (decision.purpose) labels.push(`目的: ${decision.purpose}`);
  if (decision.aggression != null) labels.push(`进攻度: ${(decision.aggression || 0).toFixed(1)}`);
  if (decision.win_conf != null) labels.push(`胜率: ${((decision.win_conf || 0) * 100).toFixed(0)}%`);
  if (labels.length) text += ` · ${labels.join(" · ")}`;

  return text;
}

function formatOutcome(o) {
  return ({
    "attacker_wins": "攻方胜",
    "defender_wins": "守方胜",
    "both_die": "同归于尽",
    "flag_taken_player": "玩家夺旗！",
    "flag_taken_ai": "AI夺旗",
  })[o] || o;
}

// Render the raw Jev request/response in the right panel. The rules text is
// huge and static, so it collapses to a one-line note to keep the dump readable.
function renderJevIO(io) {
  if (!io) return;
  const req = JSON.parse(JSON.stringify(io.request));
  if (req.state && req.state.rules) {
    req.state.rules = `…[规则文本 ${io.request.state.rules.length} 字符，见 game.py RULES_DESCRIPTION]`;
  }
  jevIoEl.textContent =
    ">>> JEV REQUEST\n" + JSON.stringify(req, null, 1) +
    "\n\n<<< JEV RESPONSE\n" + JSON.stringify(io.response, null, 1);
  jevIoEl.scrollTop = 0;
}

// === Replay ===

function enterReplay() {
  if (!historyFrames.length) {
    setStatus("本局还没有可回放的着法");
    return;
  }
  replayMode = true;
  replayIdx = historyFrames.length - 1;
  replayBar.style.display = "flex";
  replaySlider.max = historyFrames.length - 1;
  showReplayFrame();
  setStatus("复盘模式：拖动滑块或按上一步/下一步");
}

function showReplayFrame() {
  const f = historyFrames[replayIdx];
  if (!f) return;
  renderBoard(f.board);
  replaySlider.value = replayIdx;
  replayLabel.textContent = `${replayIdx + 1}/${historyFrames.length} · ${f.label}`;
}

function exitReplay() {
  replayMode = false;
  replayBar.style.display = "none";
  if (state) renderBoard(state.board);
}

replayBtn.addEventListener("click", () => {
  if (replayMode) exitReplay(); else enterReplay();
});
document.getElementById("replay-prev").addEventListener("click", () => {
  if (replayIdx > 0) { replayIdx--; showReplayFrame(); }
});
document.getElementById("replay-next").addEventListener("click", () => {
  if (replayIdx < historyFrames.length - 1) { replayIdx++; showReplayFrame(); }
});
replaySlider.addEventListener("input", () => {
  replayIdx = parseInt(replaySlider.value, 10);
  showReplayFrame();
});
document.getElementById("replay-exit").addEventListener("click", exitReplay);

function renderAIDecision(probs) {
  if (!probs) {
    aiEl.innerHTML = `<div class="ai-empty">等待 AI 行动...</div>`;
    return;
  }
  const html = [];
  html.push(`<div class="thinking-block">
    <h4>主行动</h4>`);
  const entries = Object.entries(probs.probabilities || {})
    .sort((a, b) => b[1] - a[1])
    .slice(0, 6);
  if (entries.length === 0) {
    html.push(`<div style="font-size:11px;color:#64748b;padding:6px 0">无概率数据（fallback random）</div>`);
  } else {
    for (const [k, v] of entries) {
      const cls = v > 0.5 ? "attack" : "move";
      html.push(`<div class="bar-row">
        <div class="bar-label">${escapeHtml(k)}</div>
        <div class="bar-track"><div class="bar-fill ${cls}" style="width:${(v*100).toFixed(1)}%"></div></div>
        <div class="bar-pct">${(v*100).toFixed(1)}%</div>
      </div>`);
    }
  }
  html.push(`</div>`);

  html.push(`<div class="thinking-block">
    <h4>策略意图</h4>
    <div class="confidence-row"><span>目的</span><span>${escapeHtml(probs.purpose || "-")}</span></div>
    <div class="confidence-row"><span>进攻度</span><span>${(probs.aggression || 0).toFixed(2)} / 4</span></div>
    <div class="confidence-row"><span>是否冒险</span><span>${((probs.take_risk || 0) * 100).toFixed(0)}%</span></div>
    <div class="confidence-row"><span>胜率信心</span><span>${((probs.win_conf || 0) * 100).toFixed(0)}%</span></div>
    <div class="confidence-row"><span>自身置信</span><span>${((probs.confidence || 0) * 100).toFixed(0)}%</span></div>
  </div>`);
  aiEl.innerHTML = html.join("");
}

function renderWinner(winner) {
  const banner = document.createElement("div");
  banner.className = `winner-banner ${winner}`;
  banner.textContent = winner === "player"
    ? "🎉 你赢了！读懂了 AI 的心思"
    : "💀 AI 赢了。再来一局？";
  const existing = aiEl.querySelector(".winner-banner");
  if (existing) existing.remove();
  aiEl.prepend(banner);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

refresh();
