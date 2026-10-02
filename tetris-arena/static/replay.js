// Replay viewer — plays back saved matches frame-by-frame (one frame per piece
// lock). Reads the file list from /api/replays and a single file's frames from
// /api/replay_file.
(() => {
  const CELL = 28, COLS = 10, ROWS = 20;
  const filesEl = document.getElementById("files");
  const titleEl = document.getElementById("replay-title");
  const slider = document.getElementById("rp-slider");
  const labelEl = document.getElementById("rp-label");

  let frames = [];
  let colors = {};
  let meta = {};
  let winner = null;
  let idx = 0;
  let timer = null;

  function ctlName(v) {
    return {human: "玩家", jev: "Jev", laya: "Laya"}[v] || v || "?";
  }

  function drawBoard(canvas, board) {
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    for (let r = 0; r < ROWS; r++) {
      for (let c = 0; c < COLS; c++) {
        const v = board[r][c];
        if (v) {
          ctx.fillStyle = colors[v] || "#888";
          ctx.fillRect(c * CELL + 1, r * CELL + 1, CELL - 2, CELL - 2);
        }
      }
    }
    ctx.strokeStyle = "rgba(148,163,184,0.08)";
    ctx.lineWidth = 1;
    for (let c = 0; c <= COLS; c++) {
      ctx.beginPath(); ctx.moveTo(c * CELL, 0); ctx.lineTo(c * CELL, ROWS * CELL); ctx.stroke();
    }
    for (let r = 0; r <= ROWS; r++) {
      ctx.beginPath(); ctx.moveTo(0, r * CELL); ctx.lineTo(COLS * CELL, r * CELL); ctx.stroke();
    }
  }

  function stopAutoplay() {
    if (timer) { clearInterval(timer); timer = null; }
    document.getElementById("rp-autoplay").textContent = "▶ 自动播放";
  }

  function show(i) {
    if (!frames.length) return;
    idx = Math.max(0, Math.min(i, frames.length - 1));
    const f = frames[idx];
    drawBoard(document.getElementById("board-left"), f.left.board);
    drawBoard(document.getElementById("board-right"), f.right.board);
    document.getElementById("stats-left").textContent =
      `分数 ${f.left.score} · 行数 ${f.left.lines}`;
    document.getElementById("stats-right").textContent =
      `分数 ${f.right.score} · 行数 ${f.right.lines}`;
    slider.value = idx;
    let tail = "";
    if (idx === frames.length - 1 && winner) {
      tail = winner === "draw" ? " — 平局" : ` — 🏁 胜者: ${winner === "left" ? "左方" : "右方"}`;
    }
    labelEl.textContent = `${idx + 1}/${frames.length} · ${f.label}${tail}`;
  }

  async function loadFile(name, btn) {
    stopAutoplay();
    document.querySelectorAll(".replay-item").forEach(el => el.classList.remove("active"));
    if (btn) btn.classList.add("active");
    const r = await fetch(`/api/replay_file?name=${encodeURIComponent(name)}`);
    const data = await r.json();
    frames = data.frames || [];
    colors = data.colors || {};
    winner = data.winner;
    meta = data.meta || {};
    titleEl.innerHTML = `<b>左方 = ${ctlName(meta.left)}</b> vs <b>右方 = ${ctlName(meta.right)}</b>` +
      ` · ${frames.length} 帧 · ${meta.started_at || ""}`;
    document.getElementById("tag-left").textContent = `左方 · ${ctlName(meta.left)}`;
    document.getElementById("tag-right").textContent = `右方 · ${ctlName(meta.right)}`;
    slider.max = Math.max(0, frames.length - 1);
    show(0);
  }

  async function listFiles() {
    const r = await fetch("/api/replays");
    const data = await r.json();
    filesEl.innerHTML = "";
    if (!data.files.length) {
      filesEl.innerHTML = '<div style="color:#64748b;padding:6px">还没有保存的对局</div>';
      return;
    }
    for (const f of data.files) {
      const div = document.createElement("div");
      div.className = "replay-item";
      const d = new Date(f.mtime * 1000);
      const m = f.meta || {};
      const head = `${ctlName(m.left)} vs ${ctlName(m.right)}`;
      const winTag = f.winner ? (f.winner === "draw" ? "平局" :
        `胜:${f.winner === "left" ? "左方" : "右方"}`) : "进行中";
      div.innerHTML = `${head}<div class="sub">${d.toLocaleString()} · ${f.frames}帧 · ${winTag}</div>`;
      div.addEventListener("click", () => loadFile(f.name, div));
      filesEl.appendChild(div);
      if (wantFile === f.name) loadFile(f.name, div);
    }
  }

  // /replay?file=<name> jumps straight to one replay (linked from the
  // end-of-match overlay).
  const wantFile = new URLSearchParams(location.search).get("file");

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
    }, 700);
  });

  listFiles();
})();
