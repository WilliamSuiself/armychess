// UNO replay viewer.
(async () => {
  const COLOR_NAMES = {R: "红", Y: "黄", G: "绿", B: "蓝"};
  const listEl = document.getElementById("list");
  const slider = document.getElementById("slider");
  let frames = [], current = 0, timer = null, decisions = [];

  const list = await (await fetch("api/replays")).json();
  const files = list.files || [];
  if (!files.length) listEl.innerHTML = '<div class="empty">暂无回放文件</div>';

  function colorClass(code) {
    if (!code) return "";
    if (code === "W" || code === "W4") return "color-wild";
    return "color-" + code[0];
  }
  function cardSymbol(code) {
    if (code === "W") return "WILD";
    if (code === "W4") return "WILD+4";
    const sym = {"S": "⦸", "V": "⇄", "D": "+2"}[code.slice(1)];
    return sym || code.slice(1);
  }
  function cardEl(code, small) {
    const d = document.createElement("div");
    d.className = "card" + (small ? " card-sm" : "") + " " + colorClass(code);
    d.textContent = cardSymbol(code);
    d.title = code;
    return d;
  }

  function draw(f) {
    document.getElementById("count-bar").innerHTML =
      `第 <b>${f.round}</b> 局 · ${f.label}` +
      (f.match_over ? " · 🏆对局结束" : "");
    const top = document.getElementById("top-card");
    top.innerHTML = ""; top.appendChild(cardEl(f.discard_top));
    document.getElementById("active-color").textContent =
      COLOR_NAMES[f.active_color] || f.active_color || "";

    const wrap = document.getElementById("seats");
    wrap.innerHTML = "";
    for (const seat of ["0", "1", "2", "3"]) {
      const box = document.createElement("div");
      box.className = "rp-seat";
      box.style.cssText = "padding:6px 4px;border-top:1px solid var(--border)";
      const name = document.createElement("div");
      name.style.cssText = "width:150px;font-size:12px;flex-shrink:0";
      const turnMark = f.turn === +seat ? "👉 " : "";
      name.innerHTML = `${turnMark}座位${seat} · ${f.hands[seat].length}张 · 总分${(f.scores||[])[seat] ?? "?"}`;
      box.appendChild(name);
      const hand = document.createElement("div");
      hand.className = "rp-hand";
      for (const c of f.hands[seat]) hand.appendChild(cardEl(c, true));
      box.appendChild(hand);
      wrap.appendChild(box);
    }
  }

  function renderFrame(i) {
    if (!frames.length) return;
    current = Math.max(0, Math.min(i, frames.length - 1));
    draw(frames[current]);
    slider.value = current;
    document.getElementById("pos").textContent = `${current + 1}/${frames.length}`;
    document.getElementById("frame-label").textContent = frames[current].label;
    const dl = document.getElementById("dec-log");
    dl.innerHTML = decisions.filter(d => d.frame <= current)
      .map(d => `<div${d.fallback ? ' class="fall"' : ""}>#${d.frame} seat${d.seat} ` +
        `${d.backend}: ${d.label}${d.fallback ? " ⚡兜底" : ""} ` +
        `(${d.elapsed_s}s)</div>`).join("");
    dl.scrollTop = dl.scrollHeight;
  }

  function stopPlay() {
    clearInterval(timer); timer = null;
    document.getElementById("play").textContent = "▶ 自动";
  }
  document.getElementById("play").addEventListener("click", () => {
    if (timer) { stopPlay(); return; }
    if (current >= frames.length - 1) current = -1;
    document.getElementById("play").textContent = "⏸ 暂停";
    timer = setInterval(() => {
      if (current < frames.length - 1) renderFrame(current + 1); else stopPlay();
    }, 900);
  });
  document.getElementById("prev").addEventListener("click", () => { stopPlay(); renderFrame(current - 1); });
  document.getElementById("next").addEventListener("click", () => { stopPlay(); renderFrame(current + 1); });
  slider.addEventListener("input", () => { stopPlay(); renderFrame(+slider.value); });

  async function load(name) {
    const data = await (await fetch("api/replay_file?name=" + encodeURIComponent(name))).json();
    frames = data.frames || []; decisions = data.decisions || [];
    slider.max = Math.max(frames.length - 1, 0);
    renderFrame(0);
    document.querySelectorAll(".replay-item").forEach(el =>
      el.classList.toggle("active", el.dataset.name === name));
  }
  for (const f of files) {
    const d = document.createElement("div");
    d.className = "replay-item"; d.dataset.name = f.name;
    d.innerHTML = `<div>${f.name.slice(0, 22)}…</div>
      <div class="meta">${f.frames}帧 · 胜者座位${f.winner ?? "?"} · ` +
      `${new Date(f.mtime * 1000).toLocaleString()}</div>`;
    d.addEventListener("click", () => load(f.name));
    listEl.appendChild(d);
  }
  const want = new URLSearchParams(location.search).get("file");
  if (want && files.some(f => f.name === want)) load(want);
})();
