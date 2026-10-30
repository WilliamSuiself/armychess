// Werewolf replay viewer.
(async () => {
  const listEl = document.getElementById("list");
  const slider = document.getElementById("slider");
  let frames = [], current = 0, timer = null, fullLog = [], finalRoles = null, meta = null;

  const list = await (await fetch("api/replays")).json();
  const files = list.files || [];
  if (!files.length) listEl.innerHTML = '<div class="empty">暂无回放文件</div>';

  function roleClass(roleCn) {
    if (roleCn === "狼人") return "wolf";
    if (roleCn) return "good";
    return "";
  }

  function draw(f) {
    const phaseCn = {night_wolf: "狼人行动", night_seer: "预言家行动", night_witch: "女巫行动",
      day_discuss: "白天发言", day_vote: "白天投票", over: "对局结束"}[f.phase] || f.phase;
    document.getElementById("count-bar").textContent =
      `第${f.night_no}夜 / 第${f.day_no}天 · ${phaseCn}` + (f.winner ? ` · 🏆${f.winner === "wolves" ? "狼人" : "好人"}阵营获胜` : "");

    const roster = document.getElementById("roster-rp");
    roster.innerHTML = "";
    for (let s = 0; s < 6; s++) {
      const alive = f.alive[s];
      const role = f.roles_revealed[s];
      const card = document.createElement("div");
      card.className = "seat-card" + (alive ? "" : " dead");
      card.innerHTML = `<div class="num">${s}号</div>` +
        (role ? `<div class="role ${roleClass(role)}">${role}</div>` : "<div class=\"role\">?</div>");
      roster.appendChild(card);
    }

    const tEl = document.getElementById("transcript-rp");
    const upto = fullLog.slice(0, (f.n + 1) * 10);  // generous slice; log grows roughly with frames
    tEl.innerHTML = fullLog.slice(0, logLenForFrame(f)).map(line => {
      const m = line.match(/^(\d+)号：(.*)$/);
      if (m) return `<div class="line speech"><span class="who">${m[1]}号</span>${m[2]}</div>`;
      return `<div class="line system">${line}</div>`;
    }).join("");
    tEl.scrollTop = tEl.scrollHeight;
  }

  function logLenForFrame(f) {
    // frame.label was appended to the public log at creation time, so the
    // number of log lines "visible" at this frame equals however many of
    // fullLog's entries are <= this frame's own tail line. We approximate by
    // finding f.log_tail's last line's index in fullLog (fallback: all).
    if (!f.log_tail || !f.log_tail.length) return fullLog.length;
    const last = f.log_tail[f.log_tail.length - 1];
    const idx = fullLog.lastIndexOf(last);
    return idx >= 0 ? idx + 1 : fullLog.length;
  }

  function renderFrame(i) {
    if (!frames.length) return;
    current = Math.max(0, Math.min(i, frames.length - 1));
    draw(frames[current]);
    slider.value = current;
    document.getElementById("pos").textContent = `${current + 1}/${frames.length}`;
    document.getElementById("frame-label").textContent = frames[current].label;
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
    }, 1200);
  });
  document.getElementById("prev").addEventListener("click", () => { stopPlay(); renderFrame(current - 1); });
  document.getElementById("next").addEventListener("click", () => { stopPlay(); renderFrame(current + 1); });
  slider.addEventListener("input", () => { stopPlay(); renderFrame(+slider.value); });

  async function load(name) {
    const data = await (await fetch("api/replay_file?name=" + encodeURIComponent(name))).json();
    frames = data.frames || [];
    fullLog = data.full_log || [];
    finalRoles = data.roles || null;
    meta = data.meta || null;
    slider.max = Math.max(frames.length - 1, 0);
    renderFrame(0);
    document.querySelectorAll(".replay-item").forEach(el =>
      el.classList.toggle("active", el.dataset.name === name));
  }
  for (const f of files) {
    const d = document.createElement("div");
    d.className = "replay-item"; d.dataset.name = f.name;
    d.innerHTML = `<div>${f.name.slice(0, 22)}…</div>
      <div class="meta">${f.frames}帧 · ${f.winner ? (f.winner === "wolves" ? "狼人胜" : "好人胜") : "进行中"} · ` +
      `${new Date(f.mtime * 1000).toLocaleString()}</div>`;
    d.addEventListener("click", () => load(f.name));
    listEl.appendChild(d);
  }
  const want = new URLSearchParams(location.search).get("file");
  if (want && files.some(f => f.name === want)) load(want);
})();
