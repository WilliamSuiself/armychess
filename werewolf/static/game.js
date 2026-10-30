(() => {
  let gameId = sessionStorage.getItem("ww-game-id");
  if (!gameId) {
    gameId = crypto.randomUUID ? crypto.randomUUID() : String(Math.random());
    sessionStorage.setItem("ww-game-id", gameId);
  }

  const N = 6;
  let lastState = null;
  let winnerDismissedFor = null;
  let lastLogLen = 0;

  // speech composer local selection state
  let speechSel = {target: null, stance: "neutral", claim: "claim_none", reasoning: "voting_pattern"};

  async function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({"Content-Type": "application/json", "X-Game-Id": gameId},
                                  opts.headers || {});
    const r = await fetch(path, opts);
    return r.json();
  }

  const BACKENDS = ["jev", "human", "jev", "jev", "jev", "jev"];
  const controlsBar = document.getElementById("controls-bar");
  for (let i = 0; i < N; i++) {
    const label = document.createElement("label");
    label.innerHTML = `座位${i}
      <select id="ctl-${i}">
        <option value="jev" ${BACKENDS[i] === "jev" ? "selected" : ""}>Jev</option>
        <option value="human" ${BACKENDS[i] === "human" ? "selected" : ""}>玩家</option>
        <option value="laya" ${BACKENDS[i] === "laya" ? "selected" : ""}>Laya</option>
      </select>`;
    controlsBar.insertBefore(label, document.getElementById("start-btn"));
  }

  function newMatch() {
    const controllers = [...Array(N).keys()].map(i => document.getElementById(`ctl-${i}`).value);
    api("api/new_match", {method: "POST", body: JSON.stringify({controllers})})
      .then(s => { if (s.ok === false) { alert(s.error); return; }
                   winnerDismissedFor = null; lastLogLen = 0; render(s); hideWinner(); });
  }

  document.getElementById("start-btn").addEventListener("click", newMatch);
  document.getElementById("rematch-btn").addEventListener("click", newMatch);
  document.getElementById("close-winner-btn").addEventListener("click", () => {
    winnerDismissedFor = lastState ? lastState.replay_file : null;
    hideWinner();
  });

  function hideWinner() { document.getElementById("winner-overlay").style.display = "none"; }

  function showWinner(state) {
    if (state.replay_file && winnerDismissedFor === state.replay_file) return;
    const text = state.winner === "wolves" ? "🐺 狼人阵营获胜！" : "☀️ 好人阵营获胜！";
    const roles = state.seats.map(s => `${s.seat}号 ${ctlName(s.controller)} — ${s.role || "?"}`).join("\n");
    document.getElementById("winner-text").textContent = text + "\n\n" + roles;
    const rb = document.getElementById("view-replay-btn");
    rb.href = state.replay_file ? `replay?file=${encodeURIComponent(state.replay_file)}` : "replay";
    document.getElementById("winner-overlay").style.display = "flex";
  }

  function ctlName(v) { return {human: "玩家", jev: "Jev", laya: "Laya"}[v] || v; }

  function roleClass(roleCn) {
    if (roleCn === "狼人") return "wolf";
    if (roleCn) return "good";
    return "";
  }

  function renderRoster(state) {
    const el = document.getElementById("seat-roster");
    el.innerHTML = "";
    state.seats.forEach(s => {
      const card = document.createElement("div");
      card.className = "seat-card" + (s.alive ? "" : " dead") +
        (state.current_actor === s.seat ? " acting" : "");
      card.innerHTML =
        (s.is_you ? '<span class="you-tag">你</span>' : "") +
        `<div class="num">${s.seat}号</div>` +
        `<div class="ctl">${ctlName(s.controller)}${s.alive ? "" : " · 已出局"}</div>` +
        (s.role ? `<div class="role ${roleClass(s.role)}">${s.role}</div>` : "") +
        (s.ai_thinking ? `<div class="thinking">🧠 思考中</div>` : "");
      el.appendChild(card);
    });
  }

  function renderTranscript(state) {
    const el = document.getElementById("transcript");
    const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 20;
    if (state.log.length !== lastLogLen) {
      el.innerHTML = state.log.map(line => {
        const m = line.match(/^(\d+)号：(.*)$/);
        if (m) {
          return `<div class="line speech"><span class="who">${m[1]}号</span>${m[2]}</div>`;
        }
        return `<div class="line system">${line}</div>`;
      }).join("");
      lastLogLen = state.log.length;
      if (atBottom) el.scrollTop = el.scrollHeight;
    }
  }

  function renderYourRole(state) {
    const el = document.getElementById("your-role-box");
    if (state.human_seat === null) { el.innerHTML = "纯观战模式（无玩家座位）"; return; }
    const p = state.your_private || {};
    let extra = "";
    if (p.fellow_wolves) extra += `<div>队友狼人：${p.fellow_wolves.map(s => s + "号").join("、") || "无（独狼）"}</div>`;
    if (p.check_history) {
      extra += "<div>查验记录：</div>" + (p.check_history.length
        ? p.check_history.map(c => `<div>第${c.night}夜 → ${c.seat}号 是 ${c.verdict}</div>`).join("")
        : "<div>（暂无）</div>");
    }
    if ("antidote_used" in p) {
      extra += `<div>解药：${p.antidote_used ? "已用" : "未用"} · 毒药：${p.poison_used ? "已用" : "未用"}</div>`;
      if (p.last_night_victim !== null && p.last_night_victim !== undefined) {
        extra += `<div style="color:#ef4444">今晚被攻击：${p.last_night_victim}号</div>`;
      }
    }
    el.innerHTML = `<div>你是座位 ${state.human_seat}</div>` +
      `<div class="role-name">${state.your_role}</div>` + extra;
  }

  function optBtn(label, onClick, selected) {
    const b = document.createElement("button");
    b.className = "opt-btn" + (selected ? " selected" : "");
    b.textContent = label;
    b.addEventListener("click", onClick);
    return b;
  }

  function submitAction(body) {
    api("api/action", {method: "POST", body: JSON.stringify(body)}).then(s => {
      if (s.ok === false) { alert(s.error); return; }
      render(s);
    });
  }

  function renderAction(state) {
    const el = document.getElementById("action-box");
    el.innerHTML = "";
    const ht = state.human_turn;
    if (!ht) { el.innerHTML = "<h3>等待中...</h3><div style='color:#d0a8a8'>还没轮到你行动</div>"; return; }

    if (ht.phase === "night_wolf") {
      el.innerHTML = "<h3>🐺 选择今晚击杀目标</h3>";
      const grid = document.createElement("div"); grid.className = "opt-grid";
      ht.options.forEach(s => grid.appendChild(optBtn(`${s}号`,
        () => submitAction({phase: "night_wolf", target: s}))));
      el.appendChild(grid);
    } else if (ht.phase === "night_seer") {
      el.innerHTML = "<h3>🔮 选择今晚查验目标</h3>";
      const grid = document.createElement("div"); grid.className = "opt-grid";
      ht.options.forEach(s => grid.appendChild(optBtn(`${s}号`,
        () => submitAction({phase: "night_seer", target: s}))));
      el.appendChild(grid);
    } else if (ht.phase === "night_witch") {
      el.innerHTML = `<h3>🧪 女巫用药</h3>` +
        (ht.victim !== null ? `<div style="margin-bottom:8px">今晚 ${ht.victim}号 被袭击</div>`
                            : `<div style="margin-bottom:8px">今晚是平安夜</div>`);
      const grid = document.createElement("div"); grid.className = "opt-grid";
      if (ht.can_save) grid.appendChild(optBtn(`救 ${ht.victim}号`,
        () => submitAction({phase: "night_witch", action: "save"})));
      grid.appendChild(optBtn("不使用药水",
        () => submitAction({phase: "night_witch", action: "none"})));
      el.appendChild(grid);
      if (ht.poison_options.length) {
        el.appendChild(Object.assign(document.createElement("div"),
          {textContent: "毒药目标：", style: "font-size:11px;color:#d0a8a8;margin:6px 0 4px"}));
        const pg = document.createElement("div"); pg.className = "opt-grid";
        ht.poison_options.forEach(s => pg.appendChild(optBtn(`毒 ${s}号`,
          () => submitAction({phase: "night_witch", action: "poison", target: s}))));
        el.appendChild(pg);
      }
    } else if (ht.phase === "day_discuss") {
      renderSpeechComposer(el, ht);
    } else if (ht.phase === "day_vote") {
      el.innerHTML = "<h3>🗳️ 投票放逐</h3>";
      const grid = document.createElement("div"); grid.className = "opt-grid";
      ht.options.forEach(s => grid.appendChild(optBtn(`投 ${s}号`,
        () => submitAction({phase: "day_vote", target: s}))));
      grid.appendChild(optBtn("弃票", () => submitAction({phase: "day_vote", target: null})));
      el.appendChild(grid);
    }
  }

  function renderSpeechComposer(el, ht) {
    el.innerHTML = "<h3>💬 发表你的发言</h3>";

    const row1 = document.createElement("div"); row1.className = "form-row";
    row1.innerHTML = "<label>发言针对</label>";
    const g1 = document.createElement("div"); g1.className = "opt-grid";
    ht.options.forEach(s => g1.appendChild(optBtn(`${s}号`,
      () => { speechSel.target = s; renderSpeechComposer(el, ht); }, speechSel.target === s)));
    row1.appendChild(g1); el.appendChild(row1);

    const row2 = document.createElement("div"); row2.className = "form-row";
    row2.innerHTML = "<label>立场</label>";
    const g2 = document.createElement("div"); g2.className = "opt-grid";
    [["accuse", "指控"], ["defend", "辩护"], ["neutral", "中立"]].forEach(([k, label]) =>
      g2.appendChild(optBtn(label, () => { speechSel.stance = k; renderSpeechComposer(el, ht); },
        speechSel.stance === k)));
    row2.appendChild(g2); el.appendChild(row2);

    const row3 = document.createElement("div"); row3.className = "form-row";
    row3.innerHTML = "<label>是否跳身份</label>";
    const g3 = document.createElement("div"); g3.className = "opt-grid";
    [["claim_none", "不跳"], ["claim_seer_good", "跳预言家(好人)"],
     ["claim_seer_wolf", "跳预言家(狼人)"], ["claim_witch", "跳女巫"]].forEach(([k, label]) =>
      g3.appendChild(optBtn(label, () => { speechSel.claim = k; renderSpeechComposer(el, ht); },
        speechSel.claim === k)));
    row3.appendChild(g3); el.appendChild(row3);

    const row4 = document.createElement("div"); row4.className = "form-row";
    row4.innerHTML = "<label>理由</label>";
    const g4 = document.createElement("div"); g4.className = "opt-grid";
    [["voting_pattern", "投票站边"], ["death_timing", "死亡顺序"], ["behavior_suspicious", "言行矛盾"],
     ["trust_me", "请相信我"], ["last_night_info", "昨夜信息"]].forEach(([k, label]) =>
      g4.appendChild(optBtn(label, () => { speechSel.reasoning = k; renderSpeechComposer(el, ht); },
        speechSel.reasoning === k)));
    row4.appendChild(g4); el.appendChild(row4);

    const submit = document.createElement("button");
    submit.className = "submit-btn"; submit.textContent = "发表发言";
    submit.addEventListener("click", () => {
      submitAction({phase: "day_discuss", target: speechSel.target,
        stance: speechSel.stance, claim: speechSel.claim, reasoning: speechSel.reasoning});
      speechSel = {target: null, stance: "neutral", claim: "claim_none", reasoning: "voting_pattern"};
    });
    el.appendChild(submit);
  }

  function render(state) {
    lastState = state;
    document.getElementById("status-bar").textContent =
      state.phase === "over" ? "对局结束" :
      `第${state.night_no}夜 / 第${state.day_no}天 · 当前阶段: ${
        {night_wolf: "狼人行动", night_seer: "预言家行动", night_witch: "女巫行动",
         day_discuss: "白天发言", day_vote: "白天投票"}[state.phase] || state.phase}`;
    renderRoster(state);
    renderTranscript(state);
    renderYourRole(state);
    renderAction(state);
    document.getElementById("laya-warning").style.display = state.laya_enabled ? "none" : "inline";
    if (state.phase === "over") showWinner(state);
  }

  async function poll() {
    try { render(await api("api/state")); } catch (e) { /* transient */ }
  }
  setInterval(poll, 700);
  poll();
})();
