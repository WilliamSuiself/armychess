// Dou Dizhu frontend — DOM cards (click to select), poll /api/state.
(() => {
  let gameId = sessionStorage.getItem("ddz-game-id");
  if (!gameId) {
    gameId = crypto.randomUUID ? crypto.randomUUID() : String(Math.random());
    sessionStorage.setItem("ddz-game-id", gameId);
  }

  const RANK_DISP = {T: "10"};
  const SUIT_SYM = {s: "♠", h: "♥", d: "♦", c: "♣"};
  const CTL_CN = {human: "玩家", jev: "Jev", laya: "Laya"};

  let lastState = null;
  let selected = new Set();        // card strings currently raised
  let winnerDismissedFor = null;
  let hintPlays = null;            // cached /api/hint result for this turn
  let hintIdx = -1;
  let hintTurnKey = null;

  async function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({"Content-Type": "application/json",
                                  "X-Game-Id": gameId}, opts.headers || {});
    const r = await fetch(path, opts);
    return r.json();
  }

  function controllers() {
    return [0, 1, 2].map(i => document.getElementById(`ctl-${i}`).value);
  }

  function newMatch() {
    api("/api/new_match", {method: "POST",
        body: JSON.stringify({controllers: controllers()})})
      .then(s => {
        if (s.ok === false) { alert(s.error); return; }
        winnerDismissedFor = null;
        selected.clear(); hintPlays = null;
        render(s); hideWinner();
      });
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
    const text = state.winner === "landlord" ? "🏠 地主获胜！"
               : state.winner === "peasant" ? "🌾 农民获胜！" : "游戏结束";
    document.getElementById("winner-text").textContent = text;
    const rb = document.getElementById("view-replay-btn");
    rb.href = state.replay_file
      ? `/replay?file=${encodeURIComponent(state.replay_file)}` : "/replay";
    el.style.display = "flex";
  }

  // ---- card DOM ----

  function cardEl(card, small) {
    const div = document.createElement("div");
    div.className = "card" + (small ? " card-sm" : "");
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
    div.dataset.card = card;
    return div;
  }

  function renderCards(container, hand, opts) {
    opts = opts || {};
    // Skip the DOM rebuild when nothing changed — the 200ms poll used to
    // destroy+recreate every card, which swallowed clicks that straddled a
    // poll tick (mousedown on the old node, mouseup after it was removed).
    const key = (opts.clickable ? "c:" : "") + hand.join(",");
    if (container.dataset.key === key) return;
    container.dataset.key = key;
    container.innerHTML = "";
    for (const c of hand) {
      const el = cardEl(c, opts.small);
      if (opts.clickable) {
        if (selected.has(c)) el.classList.add("sel");
        el.addEventListener("click", () => {
          if (selected.has(c)) { selected.delete(c); el.classList.remove("sel"); }
          else { selected.add(c); el.classList.add("sel"); }
        });
      }
      container.appendChild(el);
    }
  }

  function renderCardBacks(container, n) {
    const key = "backs:" + n;
    if (container.dataset.key === key) return;
    container.dataset.key = key;
    container.innerHTML = "";
    const show = Math.min(n, 17);
    for (let i = 0; i < show; i++) {
      const el = document.createElement("div");
      el.className = "card back card-sm";
      container.appendChild(el);
    }
  }

  // ---- seat layout: bottom = human seat (or seat 0 when spectating) ----

  function slotMap(state) {
    const bottom = state.human_seat !== null ? state.human_seat : 0;
    return {bottom, left: (bottom + 1) % 3, right: (bottom + 2) % 3};
  }

  function seatHeader(slot, state, seat) {
    const sv = state.seats[seat];
    document.getElementById(`name-${slot}`).textContent =
      `座位${seat} · ${CTL_CN[sv.controller]}`;
    document.getElementById(`badge-${slot}`).textContent =
      sv.controller === "human" ? "" : (sv.ai_thinking ? "🧠思考中" : "AI");
    const role = document.getElementById(`role-${slot}`);
    if (state.landlord !== null && state.landlord !== undefined) {
      role.textContent = sv.is_landlord ? "地主" : "农民";
      role.className = "role-tag " + (sv.is_landlord ? "landlord" : "peasant");
    } else {
      role.textContent = sv.bid !== null && sv.bid !== undefined
        ? (sv.bid === 0 ? "不叫" : `叫${sv.bid}分`) : "";
      role.className = "role-tag";
    }
    document.getElementById(`count-${slot}`).textContent = `剩 ${sv.hand_count} 张`;
  }

  function renderAiPanel(slot, sv) {
    const el = document.getElementById(`ai-${slot}`);
    if (sv.controller === "human") { el.style.display = "none"; return; }
    el.style.display = "block";
    const d = sv.last_decision;
    let html = sv.ai_thinking ? '<div class="ai-thinking">🧠 思考中…</div>' : "";
    if (d && d.label) {
      html += `<div class="ai-decision">决策: ${d.label}`;
      if (d.intent) html += ` · 意图:${d.intent}`;
      if (d.win_conf !== null && d.win_conf !== undefined)
        html += ` · 胜率:${(d.win_conf * 100).toFixed(0)}%`;
      html += "</div>";
    }
    el.innerHTML = html;
  }

  // per-seat latest action since the last lead_reset (for the 3 played slots)
  function lastActions(state) {
    const acts = {};
    const hist = state.history || [];
    for (let i = hist.length - 1; i >= 0; i--) {
      const h = hist[i];
      if (h.kind === "lead_reset" || h.kind === "landlord") break;
      if (h.seat === undefined || acts[h.seat]) continue;
      if (h.kind === "play") acts[h.seat] = {type: "play", cards: h.cards};
      else if (h.kind === "pass") acts[h.seat] = {type: "pass"};
    }
    return acts;
  }

  function renderPlayed(slot, seat, acts, state) {
    const slotEl = document.getElementById(`played-${slot}`);
    const lbl = slotEl.querySelector(".played-label");
    const cardsEl = slotEl.querySelector(".played-cards");
    cardsEl.innerHTML = "";
    const a = acts[seat];
    const isTurn = state.turn === seat && state.phase === "play";
    lbl.textContent = isTurn ? "👈 出牌中" : "";
    slotEl.classList.toggle("active", !!isTurn);
    if (!a) return;
    if (a.type === "pass") {
      lbl.textContent = "不出";
      return;
    }
    for (const c of a.cards) cardsEl.appendChild(cardEl(c, true));
  }

  function render(state) {
    lastState = state;
    const slots = slotMap(state);

    for (const slot of ["bottom", "left", "right"]) {
      const seat = slots[slot];
      const sv = state.seats[seat];
      seatHeader(slot, state, seat);
      renderAiPanel(slot, sv);
      const handEl = document.getElementById(`hand-${slot}`);
      if (slot === "bottom" && sv.controller === "human") {
        renderCards(handEl, sv.hand || [], {clickable: true});
      } else if (sv.hand) {
        renderCards(handEl, sv.hand, {small: slot !== "bottom"});
      } else {
        renderCardBacks(handEl, sv.hand_count);
      }
    }

    // prune selection to cards still in hand
    const mySeat = state.human_seat;
    if (mySeat !== null) {
      const mine = new Set(state.seats[mySeat].hand || []);
      for (const c of [...selected]) if (!mine.has(c)) selected.delete(c);
    }

    // bottom cards + turn hint
    const bc = document.getElementById("bottom-cards");
    bc.innerHTML = "";
    if (state.bottom && state.bottom.length) {
      const t = document.createElement("span");
      t.className = "bottom-tag";
      t.textContent = "底牌 ";
      bc.appendChild(t);
      for (const c of state.bottom) bc.appendChild(cardEl(c, true));
    }
    const hint = document.getElementById("turn-hint");
    if (state.phase === "bidding") {
      hint.textContent = `叫分中 — 轮到 座位${state.turn}(${CTL_CN[state.seats[state.turn].controller]})`;
    } else if (state.phase === "play") {
      hint.textContent = `轮到 座位${state.turn}(${CTL_CN[state.seats[state.turn].controller]})` +
        (state.lead_play ? ` · 需压过: ${state.lead_play.label}` : " · 自由出牌");
    } else {
      hint.textContent = "";
    }

    // action bars for the human seat
    const myTurn = mySeat !== null && state.turn === mySeat;
    const bidBar = document.getElementById("bid-bar");
    const playBar = document.getElementById("play-bar");
    bidBar.style.display = (myTurn && state.phase === "bidding") ? "flex" : "none";
    playBar.style.display = (myTurn && state.phase === "play") ? "flex" : "none";
    if (myTurn && state.phase === "bidding") {
      document.querySelectorAll(".bid-btn").forEach(b => {
        const v = +b.dataset.bid;
        b.disabled = !state.legal_bids.includes(v);
      });
    }
    if (myTurn && state.phase === "play") {
      document.getElementById("btn-pass").disabled = !state.can_pass;
      // invalidate cached hints if the situation changed
      const key = `${state.phase}:${state.turn}:${state.seats[mySeat].hand_count}:` +
                  (state.lead_play ? state.lead_play.label : "lead");
      if (hintTurnKey !== key) { hintPlays = null; hintIdx = -1; hintTurnKey = key; }
    }

    // per-slot latest play/pass
    const acts = lastActions(state);
    for (const slot of ["bottom", "left", "right"]) {
      renderPlayed(slot, slots[slot], acts, state);
    }

    if (!state.laya_enabled)
      document.getElementById("laya-warning").style.display = "inline";
    if (state.winner) showWinner(state);
  }

  // ---- actions ----

  document.querySelectorAll(".bid-btn").forEach(b => {
    b.addEventListener("click", () => {
      api("/api/action", {method: "POST", body: JSON.stringify(
        {type: "bid", seat: lastState.human_seat, value: +b.dataset.bid})})
        .then(s => { if (s.ok === false) alert(s.error); else render(s); });
    });
  });

  document.getElementById("btn-play").addEventListener("click", () => {
    if (!selected.size) return;
    api("/api/action", {method: "POST", body: JSON.stringify(
      {type: "play", seat: lastState.human_seat, cards: [...selected]})})
      .then(s => { if (s.ok === false) { alert(s.error); } else { selected.clear(); render(s); } });
  });

  document.getElementById("btn-pass").addEventListener("click", () => {
    api("/api/action", {method: "POST", body: JSON.stringify(
      {type: "play", seat: lastState.human_seat, cards: null})})
      .then(s => { if (s.ok === false) alert(s.error); else render(s); });
  });

  document.getElementById("btn-clear").addEventListener("click", () => {
    selected.clear();
    document.querySelectorAll("#hand-bottom .card.sel")
      .forEach(el => el.classList.remove("sel"));
  });

  document.getElementById("btn-hint").addEventListener("click", async () => {
    if (!hintPlays) {
      const r = await api("/api/hint");
      hintPlays = r.plays || [];
      hintIdx = -1;
    }
    if (!hintPlays.length) { alert("没有能压过的牌，只能不出"); return; }
    hintIdx = (hintIdx + 1) % hintPlays.length;
    selected = new Set(hintPlays[hintIdx]);
    // toggle .sel in place — a full re-render isn't needed and would rebuild
    // the exact DOM the cache-check just avoided rebuilding
    document.querySelectorAll("#hand-bottom .card").forEach(el =>
      el.classList.toggle("sel", selected.has(el.dataset.card)));
  });

  async function poll() {
    try {
      const s = await api("/api/state");
      render(s);
    } catch (e) { /* transient */ }
  }

  setInterval(poll, 200);
  poll();
})();
