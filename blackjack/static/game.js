// Blackjack frontend — poll /api/state, DOM cards.
(() => {
  let gameId = sessionStorage.getItem("bj-game-id");
  if (!gameId) {
    gameId = crypto.randomUUID ? crypto.randomUUID() : String(Math.random());
    sessionStorage.setItem("bj-game-id", gameId);
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
    const w = s.winner;
    document.getElementById("winner-text").textContent =
      w===null||w===undefined ? "对局结束" :
      `🏆 座位${w} (${CTL[s.seats[w].controller]}) 筹码最多，获胜！`;
    document.getElementById("view-replay-btn").href =
      s.replay_file ? `/replay?file=${encodeURIComponent(s.replay_file)}` : "/replay";
    document.getElementById("winner-overlay").style.display="flex";
  }

  function cardEl(card, small){
    const d=document.createElement("div");
    d.className="card"+(small?" card-sm":"");
    if(card==="back"){d.classList.add("back");return d;}
    if(card[0]==="h"||card[0]==="d") d.classList.add("red");
    const rk=card[1]==="T"?"10":card[1];
    d.innerHTML=`<div class="rk${rk==="10"?" rk2":""}">${rk}</div><div class="st">${SUIT[card[0]]}</div>`;
    return d;
  }
  function renderCards(el, hand, small){
    const key=hand.join(",");
    if(el.dataset.key===key) return;
    el.dataset.key=key; el.innerHTML="";
    for(const c of hand) el.appendChild(cardEl(c,small));
  }

  function slotMap(s){
    const bottom = s.human_seat!==null ? s.human_seat : 0;
    return {bottom, left:(bottom+1)%3, right:(bottom+2)%3};
  }

  function render(s){
    lastState=s;
    const slots=slotMap(s);
    for(const slot of ["bottom","left","right"]){
      const seat=slots[slot], sv=s.seats[seat];
      document.getElementById(`name-${slot}`).textContent=
        `座位${seat} · ${CTL[sv.controller]}`;
      document.getElementById(`badge-${slot}`).textContent=
        sv.controller==="human"?"":(sv.ai_thinking?"🧠思考中":"AI");
      const tags=[`筹码 ${sv.chips}`];
      if(sv.bet) tags.push(`押 ${sv.bet}`);
      if(sv.total!==null&&sv.total!==undefined&&sv.hand_count)
        tags.push(`${sv.total}点${sv.soft?"(软)":""}`);
      if(sv.natural) tags.push("天牌!");
      if(sv.busted) tags.push("爆了");
      if(sv.stood&&!sv.busted) tags.push("停牌");
      document.getElementById(`info-${slot}`).textContent=tags.join(" · ");
      const handEl=document.getElementById(`hand-${slot}`);
      if(sv.hand) renderCards(handEl,sv.hand,slot!=="bottom");
      else { // hidden -> backs
        const key="backs:"+sv.hand_count;
        if(handEl.dataset.key!==key){handEl.dataset.key=key;handEl.innerHTML="";
          for(let i=0;i<sv.hand_count;i++)handEl.appendChild(cardEl("back",true));}
      }
      const ai=document.getElementById(`ai-${slot}`);
      if(sv.controller==="human"){ai.style.display="none";}
      else{ai.style.display="block";
        const d=sv.last_decision;
        ai.innerHTML=(sv.ai_thinking?'<div class="ai-thinking">🧠 思考中…</div>':"")+
          (d&&d.label?`<div class="ai-decision">决策: ${d.label}`+
            (d.reason?` · ${d.reason}`:"")+"</div>":"");
      }
    }
    // dealer
    const dh=document.getElementById("dealer-hand");
    const dkey=(s.dealer_hand||[]).join(",")+(s.dealer_hidden?"+h":"");
    if(dh.dataset.key!==dkey){dh.dataset.key=dkey;dh.innerHTML="";
      for(const c of s.dealer_hand||[]) dh.appendChild(cardEl(c));
      if(s.dealer_hidden) dh.appendChild(cardEl("back"));}
    document.getElementById("dealer-total").textContent =
      s.dealer_total!==null&&s.dealer_total!==undefined?`${s.dealer_total}点`:"";

    // count bar
    const tc=(s.running_count/Math.max(s.decks_remaining,0.25)).toFixed(1);
    document.getElementById("count-bar").innerHTML=
      `🧮 真数 <b>${tc}</b> · 流水 <b>${s.running_count}</b> · `+
      `剩余 <b>${s.decks_remaining}</b> 副 · 第 <b>${s.round}</b> 轮`+
      `<span class="count-note">（真数为正 = 大牌多 = 玩家有利）</span>`;

    // turn hint + action bars
    const my=s.human_seat!==null&&s.turn===s.human_seat;
    const hint=document.getElementById("turn-hint");
    if(s.phase==="over") hint.textContent="对局结束";
    else if(s.phase==="settle") hint.textContent=`第${s.round}轮结算中…`;
    else if(s.turn!==null) hint.textContent=
      `${s.phase==="betting"?"下注":"行动"} — 轮到 座位${s.turn}(${CTL[s.seats[s.turn].controller]})`;
    const betBar=document.getElementById("bet-bar");
    const actBar=document.getElementById("act-bar");
    betBar.style.display=(my&&s.phase==="betting")?"flex":"none";
    actBar.style.display=(my&&s.phase==="acting")?"flex":"none";
    if(my&&s.phase==="betting"&&betBar.dataset.built!==String(s.legal_bets)){
      betBar.dataset.built=String(s.legal_bets);betBar.innerHTML="";
      for(const b of s.legal_bets){
        const btn=document.createElement("button");
        btn.className="bid-btn";btn.textContent=`押 ${b}`;
        btn.addEventListener("click",()=>{
          api("/api/action",{method:"POST",body:JSON.stringify(
            {type:"bet",seat:s.human_seat,value:b})})
            .then(r=>{if(r.ok===false)alert(r.error);else render(r);});});
        betBar.appendChild(btn);
      }
    }
    if(my&&s.phase==="acting"){
      document.querySelectorAll(".act-btn").forEach(b=>{
        b.disabled=!s.legal_actions.includes(b.dataset.act);});
    }

    if(!s.laya_enabled)document.getElementById("laya-warning").style.display="inline";
    if(s.phase==="over")showWinner(s);
  }

  document.querySelectorAll(".act-btn").forEach(b=>{
    b.addEventListener("click",()=>{
      api("/api/action",{method:"POST",body:JSON.stringify(
        {type:"action",seat:lastState.human_seat,action:b.dataset.act})})
        .then(r=>{if(r.ok===false)alert(r.error);else render(r);});});
  });

  async function poll(){try{render(await api("/api/state"));}catch(e){}}
  setInterval(poll,200); poll();
})();
