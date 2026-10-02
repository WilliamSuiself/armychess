// Poker frontend — poll /api/state.
(() => {
  let gameId = sessionStorage.getItem("pk-game-id");
  if (!gameId) {
    gameId = crypto.randomUUID ? crypto.randomUUID() : String(Math.random());
    sessionStorage.setItem("pk-game-id", gameId);
  }

  const SUIT = {s:"♠",h:"♥",d:"♦",c:"♣"};
  const CTL = {human:"玩家",jev:"Jev",laya:"Laya"};
  let lastState = null;
  let winnerDismissedFor = null;

  async function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({"Content-Type":"application/json",
      "X-Game-Id": gameId}, opts.headers||{});
    return (await fetch(path, opts)).json();
  }

  function controllers(){return [0,1,2].map(i=>document.getElementById(`ctl-${i}`).value);}

  function newMatch(){
    api("/api/new_match",{method:"POST",
      body:JSON.stringify({controllers:controllers()})})
      .then(s=>{if(s.ok===false){alert(s.error);return;}
        winnerDismissedFor=null; render(s); hideWinner();});
  }
  document.getElementById("start-btn").addEventListener("click",newMatch);
  document.getElementById("rematch-btn").addEventListener("click",newMatch);
  document.getElementById("close-winner-btn").addEventListener("click",()=>{
    winnerDismissedFor = lastState?lastState.replay_file:null; hideWinner();});
  function hideWinner(){document.getElementById("winner-overlay").style.display="none";}
  function showWinner(s){
    if(s.replay_file && winnerDismissedFor===s.replay_file) return;
    const w=s.winner;
    document.getElementById("winner-text").textContent =
      w===null||w===undefined?"对局结束":
      `🏆 座位${w} (${CTL[s.seats[w].controller]}) 筹码最多，获胜！`;
    document.getElementById("view-replay-btn").href =
      s.replay_file?`/replay?file=${encodeURIComponent(s.replay_file)}`:"/replay";
    document.getElementById("winner-overlay").style.display="flex";
  }

  function cardEl(card, small){
    const d=document.createElement("div");
    d.className="card"+(small?" card-sm":"");
    if(card==="back"){d.classList.add("back");return d;}
    if(card[1]==="h"||card[1]==="d") d.classList.add("red");
    const rk=card[0]==="T"?"10":card[0];
    d.innerHTML=`<div class="rk${rk==="10"?" rk2":""}">${rk}</div><div class="st">${SUIT[card[1]]}</div>`;
    return d;
  }
  function renderCards(el, hand, small){
    const key=hand?hand.join(","):"none";
    if(el.dataset.key===key) return;
    el.dataset.key=key; el.innerHTML="";
    if(hand===null){for(let i=0;i<2;i++)el.appendChild(cardEl("back",small));return;}
    for(const c of hand) el.appendChild(cardEl(c,small));
  }

  function slotMap(s){
    const bottom = s.human_seat!==null ? s.human_seat : 0;
    return {bottom, left:(bottom+1)%3, right:(bottom+2)%3};
  }

  function actLabel(a){
    if(a.startsWith("raise:")) return `加注到${a.split(":")[1]}`;
    return {fold:"弃牌",check:"过牌",call:"跟注"}[a]||a;
  }

  function render(s){
    lastState=s;
    const slots=slotMap(s);
    for(const slot of ["bottom","left","right"]){
      const seat=slots[slot], sv=s.seats[seat];
      document.getElementById(`name-${slot}`).textContent=
        `座位${seat} · ${CTL[sv.controller]}`+
        (seat===s.positions.button?" 🔘":"");
      document.getElementById(`badge-${slot}`).textContent=
        sv.controller==="human"?"":(sv.ai_thinking?"🧠思考中":"AI");
      const tags=[`筹码 ${sv.stack}`];
      if(sv.bet) tags.push(`已下 ${sv.bet}`);
      if(sv.folded) tags.push("已弃牌");
      else if(sv.allin) tags.push("全下!");
      else if(sv.hand_name) tags.push(sv.hand_name);
      document.getElementById(`info-${slot}`).textContent=tags.join(" · ");
      const el=document.getElementById(`hand-${slot}`);
      el.style.opacity = sv.folded ? 0.35 : 1;
      renderCards(el, sv.hole, slot!=="bottom");
      const ai=document.getElementById(`ai-${slot}`);
      if(sv.controller==="human"){ai.style.display="none";}
      else{ai.style.display="block";
        const d=sv.last_decision;
        ai.innerHTML=(sv.ai_thinking?'<div class="ai-thinking">🧠 思考中…</div>':"")+
          (d&&d.label?`<div class="ai-decision">决策: ${d.label}`+
            (d.reason?` · ${d.reason}`:"")+
            (d.range_guess?` · 读牌:${d.range_guess}`:"")+"</div>":"");
      }
    }

    // board + pot
    renderCards(document.getElementById("board-cards"), s.board, false);
    document.getElementById("street-label").textContent = s.street_cn||"";
    document.getElementById("pot").textContent = s.pot;
    document.getElementById("cur-bet").textContent =
      s.current_bet?` · 跟注额 ${s.current_bet}`:"";
    const rl=document.getElementById("result-line");
    if(s.last_result && (s.phase==="handover"||s.phase==="over")){
      const r=s.last_result;
      rl.textContent = r.type==="showdown"
        ? `摊牌：${r.winners.map(w=>`座位${w}`).join("/")} 赢 ${r.pot}`+
          (r.names?`（${Object.entries(r.names).map(([k,v])=>`座位${k}:${v}`).join(" · ")}）`:"")
        : `座位${r.winners[0]} 收池 ${r.pot}`;
    } else rl.textContent="";

    const my=s.human_seat!==null&&s.turn===s.human_seat&&s.phase==="playing";
    const hint=document.getElementById("turn-hint");
    if(s.phase==="over")hint.textContent="对局结束";
    else if(s.phase==="handover")hint.textContent="本手结算中…";
    else if(s.turn!==null&&s.turn!==undefined)hint.textContent=
      `行动 — 轮到 座位${s.turn}(${CTL[s.seats[s.turn].controller]})`;

    const bar=document.getElementById("act-bar");
    bar.style.display=my?"flex":"none";
    const key=s.legal_actions.join("|");
    if(my&&bar.dataset.built!==key){
      bar.dataset.built=key;bar.innerHTML="";
      for(const a of s.legal_actions){
        const btn=document.createElement("button");
        btn.className="act-btn"+(a==="fold"?" fold-btn":"")+
          (a.startsWith("raise:")?" raise-btn":"");
        btn.textContent=actLabel(a);
        btn.addEventListener("click",()=>{
          api("/api/action",{method:"POST",body:JSON.stringify(
            {seat:s.human_seat,action:a})})
            .then(r=>{if(r.ok===false)alert(r.error);else render(r);});});
        bar.appendChild(btn);
      }
    }

    document.getElementById("history-bar").textContent =
      (s.history||[]).slice(-8).join("  |  ");
    if(!s.laya_enabled)document.getElementById("laya-warning").style.display="inline";
    if(s.phase==="over")showWinner(s);
  }

  async function poll(){try{render(await api("/api/state"));}catch(e){}}
  setInterval(poll,200); poll();
})();
