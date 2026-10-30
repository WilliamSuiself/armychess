(() => {
  let gameId = sessionStorage.getItem("uno-game-id");
  if (!gameId) {
    gameId = crypto.randomUUID ? crypto.randomUUID() : String(Math.random());
    sessionStorage.setItem("uno-game-id", gameId);
  }

  let lastState = null;
  let winnerDismissedFor = null;
  let pendingWildCard = null;  // card awaiting a color choice before we POST it

  async function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({"Content-Type": "application/json", "X-Game-Id": gameId},
                                  opts.headers || {});
    const r = await fetch(path, opts);
    return r.json();
  }

  function newMatch() {
    const controllers = [0, 1, 2, 3].map(i => document.getElementById(`ctl-${i}`).value);
    api("api/new_match", {method: "POST", body: JSON.stringify({controllers})})
      .then(s => { if (s.ok === false) { alert(s.error); return; }
                   winnerDismissedFor = null; render(s); hideWinner(); });
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
    const ctl = i => document.getElementById(`ctl-${i}`).value;
    const names = {human: "玩家", jev: "Jev", laya: "Laya"};
    document.getElementById("winner-text").textContent =
      `座位${state.winner} (${names[ctl(state.winner)] || "?"}) 获胜！总分 ${state.seats[state.winner].score}`;
    const rb = document.getElementById("view-replay-btn");
    rb.href = state.replay_file ? `replay?file=${encodeURIComponent(state.replay_file)}` : "replay";
    document.getElementById("winner-overlay").style.display = "flex";
  }

  function colorClass(code) {
    if (!code) return "";
    if (code === "W" || code === "W4") return "color-wild";
    return "color-" + code[0];
  }

  function cardSymbol(code) {
    if (!code) return "";
    if (code === "W") return "WILD";
    if (code === "W4") return "WILD\n+4";
    const sym = {"S": "⦸", "V": "⇄", "D": "+2"}[code.slice(1)];
    return sym || code.slice(1);
  }

  function makeCardEl(code, {small, playable, onClick} = {}) {
    const el = document.createElement("div");
    el.className = "card" + (small ? " card-sm" : "") +
      (code ? " " + colorClass(code) : " back") +
      (playable ? " playable" : "");
    el.textContent = cardSymbol(code);
    if (code) el.title = code;
    if (playable && onClick) el.addEventListener("click", () => onClick(code));
    return el;
  }

  function ctlName(v) { return {human: "玩家", jev: "Jev", laya: "Laya"}[v] || v; }

  function decisionHtml(d) {
    if (!d) return "";
    if (d.io && d.io.response && d.io.response.error) {
      return `<span style="color:#f87171">⚠ ${d.io.response.error}</span>`;
    }
    const pct = v => (v === null || v === undefined) ? "–" : Math.round(v * 100) + "%";
    return `<div class="lbl">${d.label || ""}</div>` +
      (d.confidence != null ? `<div class="metric">信心: ${pct(d.confidence)}</div>` : "");
  }

  function askColor(card) {
    pendingWildCard = card;
    document.getElementById("color-picker").style.display = "flex";
  }

  document.querySelectorAll(".color-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.getElementById("color-picker").style.display = "none";
      if (!pendingWildCard) return;
      api("api/action", {method: "POST",
        body: JSON.stringify({type: "play", card: pendingWildCard, color: btn.dataset.color})})
        .then(render);
      pendingWildCard = null;
    });
  });

  function playCard(code) {
    if (code === "W" || code === "W4") { askColor(code); return; }
    api("api/action", {method: "POST", body: JSON.stringify({type: "play", card: code})})
      .then(render);
  }

  document.getElementById("draw-btn").addEventListener("click", () => {
    api("api/action", {method: "POST", body: JSON.stringify({type: "draw"})}).then(render);
  });

  // Visual layout is always "you at the bottom" (slot 0), going clockwise
  // through the other DOM slots 1 (top), 2 (left), 3 (right) regardless of
  // your actual logical seat index — same convention as the mahjong table.
  function slotMap(state) {
    const bottom = state.human_seat !== null ? state.human_seat : 0;
    return [bottom, (bottom + 2) % 4, (bottom + 1) % 4, (bottom + 3) % 4];
  }

  function renderSeat(slot, seatIdx, state) {
    const seat = state.seats[seatIdx];
    const isHuman = seat.controller === "human";
    const isMe = state.human_seat === seatIdx;
    document.getElementById(`badge-${slot}`).textContent =
      `座位${seatIdx} · ${ctlName(seat.controller)}`;
    document.getElementById(`info-${slot}`).textContent =
      `${seat.hand_count}张 · 总分${seat.score}`;
    const handEl = document.getElementById(`hand-${slot}`);
    handEl.innerHTML = "";
    const myTurn = state.turn === seatIdx && isMe && !state.round_over;
    if (isMe && seat.hand_codes) {
      seat.hand_codes.forEach(code => {
        const playable = myTurn && state.legal_plays.includes(code);
        handEl.appendChild(makeCardEl(code, {playable, onClick: playCard}));
      });
    } else {
      for (let k = 0; k < seat.hand_count; k++) {
        handEl.appendChild(makeCardEl(null, {small: true}));
      }
    }
    const panel = document.getElementById(`ai-${slot}`);
    if (isHuman) {
      panel.innerHTML = "";
    } else {
      panel.innerHTML = (seat.ai_thinking ? `<div class="ai-thinking">🧠 ${ctlName(seat.controller)} 正在思考...</div>` : "") +
        decisionHtml(seat.last_decision);
    }
  }

  function render(state) {
    lastState = state;
    const slots = slotMap(state);
    slots.forEach((seatIdx, slot) => renderSeat(slot, seatIdx, state));

    document.getElementById("top-card").className = "card " + colorClass(state.top_card_code);
    document.getElementById("top-card").textContent = cardSymbol(state.top_card_code);
    document.getElementById("active-color-lbl").textContent =
      "当前颜色: " + (state.active_color_cn || "");
    document.getElementById("deck-count").textContent = `剩${state.deck_remaining}张`;
    document.getElementById("direction-indicator").textContent =
      state.direction === 1 ? "↻ 顺时针" : "↺ 逆时针";

    document.getElementById("count-bar").innerHTML =
      `<span>第 <b>${state.round}</b> 局</span>` +
      state.seats.map((s, i) => `<span>座位${i}: <b>${s.score}</b>分</span>`).join("");

    const human = state.human_seat;
    const myTurn = human !== null && state.turn === human && !state.round_over;
    const hint = document.getElementById("turn-hint");
    if (state.match_over) hint.textContent = "";
    else if (myTurn) hint.textContent = state.pending_draw
      ? `轮到你 — 必须摸${state.pending_draw}张（无法接+2/+4）`
      : "轮到你出牌";
    else hint.textContent = `等待座位${state.turn}(${ctlName(state.seats[state.turn].controller)})...`;

    document.getElementById("draw-btn").style.display =
      (myTurn && state.can_draw) ? "inline-block" : "none";

    document.getElementById("laya-warning").style.display = state.laya_enabled ? "none" : "inline";

    if (state.match_over) showWinner(state);
  }

  async function poll() {
    try { render(await api("api/state")); } catch (e) { /* transient */ }
  }
  setInterval(poll, 400);
  poll();
})();
