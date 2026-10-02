// Mahjong frontend — 4 seats, real tile images, poll /api/state.
(() => {
  let gameId = sessionStorage.getItem("mj-game-id");
  if (!gameId) {
    gameId = crypto.randomUUID ? crypto.randomUUID() : String(Math.random());
    sessionStorage.setItem("mj-game-id", gameId);
  }

  const CTL = {human:"玩家",jev:"Jev",laya:"Laya"};
  const SUIT_IMG = ["Man","Pin","Sou"];
  const TILE_CN = ["万","筒","条"];
  const NSEATS = 4;
  let lastState = null;
  let winnerDismissedFor = null;

  function tlabel(t){return TILE_CN[Math.floor(t/9)] + (t%9+1);}
  function tsrc(t){return `/static/tiles/${SUIT_IMG[Math.floor(t/9)]}${t%9+1}.png`;}

  async function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({"Content-Type":"application/json",
      "X-Game-Id": gameId}, opts.headers||{});
    return (await fetch(path, opts)).json();
  }
  function controllers(){return [0,1,2,3].map(i=>document.getElementById(`ctl-${i}`).value);}

  function newMatch(){
    api("/api/new_match",{method:"POST",
      body:JSON.stringify({controllers:controllers()})})
      .then(s=>{if(s.ok===false){alert(s.error);return;}
        winnerDismissedFor=null; render(s); hideWinner();});
  }
  document.getElementById("start-btn").addEventListener("click",newMatch);
  document.getElementById("rematch-btn").addEventListener("click",newMatch);
  document.getElementById("close-winner-btn").addEventListener("click",()=>{
    winnerDismissedFor=lastState?lastState.replay_file:null; hideWinner();});
  function hideWinner(){document.getElementById("winner-overlay").style.display="none";}
  function showWinner(s){
    if(s.replay_file && winnerDismissedFor===s.replay_file) return;
    const w=s.match_winner;
    document.getElementById("winner-text").textContent =
      w===null||w===undefined?"对局结束":
      `🏆 座位${w} (${CTL[s.seats[w].controller]}) 总分最高获胜！`;
    document.getElementById("view-replay-btn").href =
      s.replay_file?`/replay?file=${encodeURIComponent(s.replay_file)}`:"/replay";
    document.getElementById("winner-overlay").style.display="flex";
  }

  function tileEl(t, small, clickable, lastDrawn){
    const img=document.createElement("img");
    img.className="tile"+(small?" tile-sm":"");
    img.src=tsrc(t); img.alt=tlabel(t); img.title=tlabel(t);
    img.draggable=false;
    if(clickable)img.classList.add("clickable");
    if(lastDrawn)img.classList.add("last-drawn");
    return img;
  }
  function backEl(small){
    const img=document.createElement("img");
    img.className="tile"+(small?" tile-sm":"")+" tile-back";
    img.src="/static/tiles/Back.png"; img.draggable=false;
    return img;
  }

  function renderTiles(el, tiles, opts){
    opts=opts||{};
    const key=(tiles===null?"backs:"+opts.count:
      (tiles.join(",")+(opts.drawn?"+d":"")+(opts.click?"+c":"")));
    if(el.dataset.key===key)return;
    el.dataset.key=key;el.innerHTML="";
    if(tiles===null){
      for(let i=0;i<(opts.count||0);i++) el.appendChild(backEl(true));
      return;
    }
    tiles.forEach((t,i)=>{
      const lastDrawn=opts.drawn&&i===tiles.length-1;
      const e=tileEl(t,opts.small,opts.click,lastDrawn);
      if(opts.click){
        e.addEventListener("click",()=>{
          api("/api/action",{method:"POST",body:JSON.stringify(
            {seat:lastState.human_seat,action:`discard:${t}`,kind:"action"})})
            .then(r=>{if(r.ok===false)alert(r.error);else render(r);});});
      }
      el.appendChild(e);
    });
  }

  function renderMelds(el, melds){
    const key=JSON.stringify(melds);
    if(el.dataset.key===key)return;
    el.dataset.key=key;el.innerHTML="";
    for(const m of melds){
      const box=document.createElement("div");
      box.className="meld";
      for(const t of m[1])box.appendChild(tileEl(t,true));
      const tag=document.createElement("span");
      tag.className="meld-tag";
      tag.textContent={peng:"碰",gang:"杠",jia:"杠",angang:"暗"}[m[0]]||m[0];
      box.appendChild(tag);
      el.appendChild(box);
    }
  }

  // 视角固定：自己在下，右手边=下一家，对面=对家，左手边=上一家
  function slotMap(s){
    const bottom = s.human_seat!==null ? s.human_seat : 0;
    return {bottom,
            right:(bottom+1)%NSEATS,
            top:(bottom+2)%NSEATS,
            left:(bottom+3)%NSEATS};
  }

  function myTurn(s){
    return s.human_seat!==null&&s.turn===s.human_seat&&
      s.turn_phase==="action"&&s.phase==="playing";
  }

  function render(s){
    lastState=s;
    const slots=slotMap(s);
    for(const slot of ["bottom","left","right","top"]){
      const seat=slots[slot], sv=s.seats[seat];
      document.getElementById(`name-${slot}`).textContent=
        `座位${seat} · ${CTL[sv.controller]}${seat===s.dealer?" 🀅庄":""}`;
      document.getElementById(`badge-${slot}`).textContent=
        sv.controller==="human"?"":(sv.ai_thinking?"🧠思考中":"AI");
      const tags=[`${sv.points}分`];
      if(sv.shanten!==null&&sv.shanten!==undefined)
        tags.push(sv.shanten===0?"听牌!":`向听${sv.shanten}`);
      document.getElementById(`info-${slot}`).textContent=tags.join(" · ");
      renderMelds(document.getElementById(`melds-${slot}`), sv.melds||[]);
      const canDiscard = myTurn(s)&&slot==="bottom";
      renderTiles(document.getElementById(`hand-${slot}`), sv.concealed,
        {small:slot!=="bottom",count:sv.concealed_count,
         click:canDiscard,drawn:sv.last_drawn});
      const ai=document.getElementById(`ai-${slot}`);
      if(sv.controller==="human"){ai.style.display="none";}
      else{ai.style.display="block";
        const d=sv.last_decision;
        ai.innerHTML=(sv.ai_thinking?'<div class="ai-thinking">🧠 思考中…</div>':"")+
          (d&&d.label?`<div class="ai-decision">决策: ${d.label}`+
            (d.reason?` · ${d.reason}`:"")+"</div>":"");
      }
    }
    // discards for all four directions
    for(const slot of ["left","right","top","bottom"]){
      const seat=slots[slot], sv=s.seats[seat];
      const el=document.getElementById(`disc-${slot}`);
      const pkey=sv.discards.join(",")+"|"+(s.pending_from===seat?"p":"");
      if(el.dataset.key!==pkey){
        el.dataset.key=pkey;el.innerHTML="";
        sv.discards.forEach((t,i)=>{
          const e=tileEl(t,true);
          if(s.pending_tile!==null&&s.pending_tile!==undefined&&
             s.pending_from===seat&&
             sv.discards.lastIndexOf(s.pending_tile)===i)
            e.classList.add("pending");
          el.appendChild(e);});
      }
    }
    document.getElementById("wall-info").textContent=
      `牌墙 ${s.wall} · 第${s.hand_no}局`;
    const hint=document.getElementById("turn-hint");
    if(s.phase==="over")hint.textContent="对局结束";
    else if(s.phase==="handover")hint.textContent="本局结算…";
    else if(s.turn_phase==="claim")hint.textContent=
      `seat${s.pending_from}打出${tlabel(s.pending_tile)} — 等待碰/杠/胡`;
    else if(s.turn!==null&&s.turn!==undefined)hint.textContent=
      `轮到 座位${s.turn}(${CTL[s.seats[s.turn].controller]}) 打牌`;

    // live tile counts panel
    const lg=document.getElementById("live-grid");
    if(s.live_tiles){
      const key=JSON.stringify(s.live_tiles);
      if(lg.dataset.key!==key){
        lg.dataset.key=key;lg.innerHTML="";
        for(const name of Object.keys(s.live_tiles)){
          const n=s.live_tiles[name];
          const cell=document.createElement("div");
          cell.className="live-cell"+(n===0?" dead":"");
          cell.innerHTML=`${name}<b>${n}</b>`;
          lg.appendChild(cell);
        }
      }
    }

    // claim bar for human
    const cb=document.getElementById("claim-bar");
    const myClaim=(s.legal_claims||[]).length>0;
    const extras=(s.legal_actions||[]).filter(a=>!a.startsWith("discard:"));
    const showBar=myClaim||extras.length>0;
    cb.style.display=showBar?"flex":"none";
    const ckey=(s.legal_claims||[]).join("|")+"#"+extras.join("|");
    if(showBar&&cb.dataset.built!==ckey){
      cb.dataset.built=ckey;cb.innerHTML="";
      const CN={hu:"胡牌!",gang:"明杠",peng:"碰",pass:"过"};
      for(const a of s.legal_claims||[]){
        const btn=document.createElement("button");
        btn.className="act-btn"+(a==="hu"?" raise-btn":"");
        btn.textContent=CN[a]||a;
        btn.addEventListener("click",()=>{
          api("/api/action",{method:"POST",body:JSON.stringify(
            {seat:s.human_seat,action:a,kind:"claim"})})
            .then(r=>{if(r.ok===false)alert(r.error);else render(r);});});
        cb.appendChild(btn);
      }
      for(const a of extras){
        const btn=document.createElement("button");
        btn.className="act-btn raise-btn";
        btn.textContent=a==="hu:self"?"自摸!":
          a.startsWith("gang:an:")?`暗杠${tlabel(+a.split(":")[2])}`:
          `加杠${tlabel(+a.split(":")[2])}`;
        btn.addEventListener("click",()=>{
          api("/api/action",{method:"POST",body:JSON.stringify(
            {seat:s.human_seat,action:a,kind:"action"})})
            .then(r=>{if(r.ok===false)alert(r.error);else render(r);});});
        cb.appendChild(btn);
      }
    }

    if(!s.laya_enabled)document.getElementById("laya-warning").style.display="inline";
    if(s.phase==="over")showWinner(s);
  }

  async function poll(){try{render(await api("/api/state"));}catch(e){}}
  setInterval(poll,200); poll();
})();
