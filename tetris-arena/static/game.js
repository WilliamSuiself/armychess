(() => {
  const CELL = 28;
  const COLS = 10, ROWS = 20;

  // Per-tab game id, so opening this page in multiple tabs gets independent matches.
  let gameId = sessionStorage.getItem("tetris-game-id");
  if (!gameId) {
    gameId = crypto.randomUUID ? crypto.randomUUID() : String(Math.random());
    sessionStorage.setItem("tetris-game-id", gameId);
  }

  let colors = {};
  let lastState = null;
  // replay_file of the match whose winner overlay the user dismissed — the poll
  // loop would otherwise re-show it every tick while state.winner stays set.
  let winnerDismissedFor = null;

  async function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({"Content-Type": "application/json", "X-Game-Id": gameId},
                                  opts.headers || {});
    const r = await fetch(path, opts);
    return r.json();
  }

  function newMatch() {
    const left = document.getElementById("ctl-left").value;
    const right = document.getElementById("ctl-right").value;
    api("/api/new_match", {method: "POST", body: JSON.stringify({left, right})})
      .then(s => { if (s.ok === false) { alert(s.error); return; }
                   winnerDismissedFor = null; render(s); hideWinner(); });
  }

  document.getElementById("start-btn").addEventListener("click", newMatch);
  document.getElementById("rematch-btn").addEventListener("click", newMatch);
  document.getElementById("close-winner-btn").addEventListener("click", () => {
    winnerDismissedFor = lastState ? lastState.replay_file : null;
    hideWinner();
  });

  function hideWinner() {
    document.getElementById("winner-overlay").style.display = "none";
  }

  function showWinner(state) {
    if (state.replay_file && winnerDismissedFor === state.replay_file) return;
    const el = document.getElementById("winner-overlay");
    const label = side => side === "left" ? document.getElementById("ctl-left").value
                                           : document.getElementById("ctl-right").value;
    let text;
    if (state.winner === "draw") text = "平局！";
    else text = `${state.winner === "left" ? "左方" : "右方"} (${label(state.winner)}) 获胜！`;
    document.getElementById("winner-text").textContent = text;
    const rb = document.getElementById("view-replay-btn");
    rb.href = state.replay_file
      ? `/replay?file=${encodeURIComponent(state.replay_file)}` : "/replay";
    el.style.display = "flex";
  }

  function drawCell(ctx, r, c, color, alphaOnly) {
    const x = c * CELL, y = r * CELL;
    if (alphaOnly) {
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.strokeRect(x + 2, y + 2, CELL - 4, CELL - 4);
    } else {
      ctx.fillStyle = color;
      ctx.fillRect(x + 1, y + 1, CELL - 2, CELL - 2);
    }
  }

  function drawGrid(ctx) {
    ctx.strokeStyle = "rgba(148,163,184,0.08)";
    ctx.lineWidth = 1;
    for (let c = 0; c <= COLS; c++) {
      ctx.beginPath(); ctx.moveTo(c * CELL, 0); ctx.lineTo(c * CELL, ROWS * CELL); ctx.stroke();
    }
    for (let r = 0; r <= ROWS; r++) {
      ctx.beginPath(); ctx.moveTo(0, r * CELL); ctx.lineTo(COLS * CELL, r * CELL); ctx.stroke();
    }
  }

  function drawBoard(canvas, view) {
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    for (let r = 0; r < ROWS; r++) {
      for (let c = 0; c < COLS; c++) {
        const v = view.board[r][c];
        if (v) drawCell(ctx, r, c, colors[v] || "#888");
      }
    }
    (view.ghost_cells || []).forEach(([r, c]) => {
      if (r >= 0) drawCell(ctx, r, c, colors[view.piece] || "#888", true);
    });
    (view.piece_cells || []).forEach(([r, c]) => {
      if (r >= 0) drawCell(ctx, r, c, colors[view.piece] || "#888");
    });
    drawGrid(ctx);
  }

  function drawMini(canvas, letter) {
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (!letter) return;
    const shapes = {
      I: [[0,0],[0,1],[0,2],[0,3]], O: [[0,0],[0,1],[1,0],[1,1]],
      T: [[0,1],[1,0],[1,1],[1,2]], S: [[0,1],[0,2],[1,0],[1,1]],
      Z: [[0,0],[0,1],[1,1],[1,2]], J: [[0,0],[1,0],[1,1],[1,2]],
      L: [[0,2],[1,0],[1,1],[1,2]],
    };
    const cells = shapes[letter] || [];
    const cs = 16;
    const offX = (canvas.width - 4 * cs) / 2;
    cells.forEach(([r, c]) => {
      ctx.fillStyle = colors[letter] || "#888";
      ctx.fillRect(offX + c * cs + 1, 6 + r * cs + 1, cs - 2, cs - 2);
    });
  }

  function drawNextQueue(canvas, letters) {
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const shapes = {
      I: [[0,0],[0,1],[0,2],[0,3]], O: [[0,0],[0,1],[1,0],[1,1]],
      T: [[0,1],[1,0],[1,1],[1,2]], S: [[0,1],[0,2],[1,0],[1,1]],
      Z: [[0,0],[0,1],[1,1],[1,2]], J: [[0,0],[1,0],[1,1],[1,2]],
      L: [[0,2],[1,0],[1,1],[1,2]],
    };
    const cs = 14;
    (letters || []).forEach((letter, i) => {
      const cells = shapes[letter] || [];
      const offY = i * 68 + 10;
      const offX = (canvas.width - 4 * cs) / 2;
      cells.forEach(([r, c]) => {
        ctx.fillStyle = colors[letter] || "#888";
        ctx.fillRect(offX + c * cs + 1, offY + r * cs + 1, cs - 2, cs - 2);
      });
    });
  }

  function decisionHtml(d) {
    if (!d) return "";
    if (d.error) return `<span style="color:#f87171">⚠ ${d.error}</span>`;
    if (d.purpose === "fallback_timeout") {
      return `<div class="lbl">${d.label || ""}</div>` +
        `<div class="metric" style="color:#fbbf24">模型响应太慢，已用本地兜底选点</div>`;
    }
    const pct = v => (v === null || v === undefined) ? "–" : Math.round(v * 100) + "%";
    return `<div class="lbl">${d.label || ""}</div>` +
      `<div class="purpose">意图: ${d.purpose || "?"} · 激进度: ${d.aggression ?? "?"}/4</div>` +
      `<div class="metric">胜率信心: ${pct(d.win_conf)} · 冒险: ${pct(d.take_risk)}</div>`;
  }

  function ctlName(v) {
    return {human: "玩家", jev: "Jev", laya: "Laya"}[v] || v;
  }

  function renderSide(side, view) {
    drawBoard(document.getElementById(`board-${side}`), view);
    drawMini(document.getElementById(`hold-${side}`), view.hold);
    drawNextQueue(document.getElementById(`next-${side}`), view.next);
    document.getElementById(`badge-${side}`).textContent = ctlName(view.controller);
    document.getElementById(`stats-${side}`).innerHTML =
      `<span>分数 <b>${view.score}</b></span><span>行数 <b>${view.lines}</b></span>` +
      `<span>连击 <b>${view.combo}</b></span>`;
    const gbar = document.getElementById(`garbage-${side}`);
    const pct = Math.min(100, view.pending_garbage * 10);
    gbar.innerHTML = `<div class="fill" style="width:${pct}%"></div>`;
    const panel = document.getElementById(`ai-panel-${side}`);
    const thinking = document.getElementById(`ai-thinking-${side}`);
    const decision = document.getElementById(`ai-decision-${side}`);
    if (view.controller === "human") {
      panel.style.display = "none";
    } else {
      panel.style.display = "block";
      thinking.textContent = view.ai_thinking ? `🧠 ${ctlName(view.controller)} 正在思考...` : "";
      decision.innerHTML = decisionHtml(view.last_decision);
    }
    if (view.game_over) {
      document.getElementById(`col-${side}`).style.opacity = 0.55;
    } else {
      document.getElementById(`col-${side}`).style.opacity = 1;
    }
  }

  function render(state) {
    lastState = state;
    colors = state.colors || colors;
    renderSide("left", state.left);
    renderSide("right", state.right);
    if (state.winner) showWinner(state);
  }

  async function poll() {
    try {
      const s = await api("/api/state");
      render(s);
    } catch (e) { /* transient network hiccup, ignore */ }
  }

  setInterval(poll, 80);
  poll();

  // ---- keyboard controls (only affect the human side, if any) ----
  const keyMap = {
    ArrowLeft: "left", ArrowRight: "right", ArrowDown: "softdrop",
    ArrowUp: "rotate_cw", x: "rotate_cw", X: "rotate_cw",
    z: "rotate_ccw", Z: "rotate_ccw",
    " ": "harddrop", c: "hold", C: "hold",
  };

  document.addEventListener("keydown", (e) => {
    const action = keyMap[e.key];
    if (!action) return;
    if (["ArrowLeft", "ArrowRight", "ArrowDown", "ArrowUp", " "].includes(e.key)) {
      e.preventDefault();
    }
    if (action === "harddrop" && e.repeat) return; // no repeated hard drops from key-repeat
    api("/api/action", {method: "POST", body: JSON.stringify({action})}).then(render);
  });
})();
