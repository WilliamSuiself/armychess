// Dou Dizhu replay viewer — frame list from /api/replays, single file from
// /api/replay_file. Each frame snapshots all three hands + the current lead.
(() => {
  const filesEl = document.getElementById("files");
  const titleEl = document.getElementById("replay-title");
  const slider = document.getElementById("rp-slider");
  const labelEl = document.getElementById("rp-label");

  const RANK_DISP = {T: "10"};
  const SUIT_SYM = {s: "♠", h: "♥", d: "♦", c: "♣"};
  const CTL_CN = {human: "玩家", jev: "Jev", laya: "Laya"};

  let frames = [];
  let meta = {};
  let winner = null;
  let idx = 0;
  let timer = null;

  function cardEl(card) {
    const div = document.createElement("div");
    div.className = "card card-sm";
    let rankTxt, suitTxt = "", red = false;
    if (card[0] === "j") {
      rankTxt = card[1] === "s" ? "小王" : "大王";
      red = card[1] === "b";
      div.classList.add("joker");
    } else {
      rankTxt = RANK_DISP[card[1]] || card[1];
      suitTxt = SUIT_SYM[card[0]];
      red = card[0] === "h" || card[0] === "d";
    }
    if (red) div.classList.add("red");
    div.innerHTML = `<div class="rk">${rankTxt}</div><div class="st">${suitTxt}</div>`;
    return div;
  }

  function stopAutoplay() {
    if (timer) { clearInterval(timer); timer = null; }
    document.getElementById("rp-autoplay").textContent = "▶ 自动播放";
  }

  function show(i) {
    if (!frames.length) return;
    idx = Math.max(0, Math.min(i, frames.length - 1));
    const f = frames[idx];
    for (let s = 0; s < 3; s++) {
      const hand = (f.hands || {})[String(s)] || [];
      const el = document.getElementById(`hand-${s}`);
      el.innerHTML = "";
      for (const c of hand) el.appendChild(cardEl(c));
      const role = document.getElementById(`role-${s}`);
      role.textContent = f.landlord === s ? "地主" :
        (f.landlord !== null && f.landlord !== undefined ? "农民" : "");
      role.className = "role-tag " +
        (f.landlord === s ? "landlord" : "peasant");
    }
    const lp = document.getElementById("rp-last");
    const lpCards = document.getElementById("rp-last-cards");
    lpCards.innerHTML = "";
    if (f.last_play) {
      lp.textContent = `当前需压过: 座位${f.last_play.seat} 的 ${f.last_play.type}`;
      for (const c of f.last_play.cards) lpCards.appendChild(cardEl(c));
    } else {
      lp.textContent = "";
    }
    slider.value = idx;
    let tail = "";
    if (idx === frames.length - 1 && winner) {
      tail = winner === "landlord" ? " — 🏠 地主胜" : " — 🌾 农民胜";
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
    winner = data.winner;
    meta = data.meta || {};
    const ctls = (meta.controllers || []).map(c => CTL_CN[c] || c);
    titleEl.innerHTML = `<b>${ctls.join(" vs ")}</b> · ${frames.length} 帧 · ${meta.started_at || ""}`;
    for (let s = 0; s < 3; s++) {
      document.getElementById(`tag-${s}`).textContent =
        `座位${s} · ${ctls[s] || "?"}`;
    }
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
      const ctls = ((f.meta || {}).controllers || []).map(c => CTL_CN[c] || c);
      const winTag = f.winner ? (f.winner === "landlord" ? "地主胜" : "农民胜") : "进行中";
      div.innerHTML = `${ctls.join(" vs ")}<div class="sub">${d.toLocaleString()} · ${f.frames}帧 · ${winTag}</div>`;
      div.addEventListener("click", () => loadFile(f.name, div));
      filesEl.appendChild(div);
      if (wantFile === f.name) loadFile(f.name, div);
    }
  }

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
    }, 800);
  });

  listFiles();
})();
