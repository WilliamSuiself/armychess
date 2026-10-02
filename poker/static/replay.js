// Poker replay viewer — frames carry full hole cards (recorded post-hoc).
(async () => {
  const SUIT = {s:"♠",h:"♥",d:"♦",c:"♣"};
  const listEl = document.getElementById("list");
  const slider = document.getElementById("slider");
  const STREET_CN = {preflop:"翻牌前",flop:"翻牌",turn:"转牌",river:"河牌"};
  let frames=[], current=0, timer=null, decisions=[];

  const list = await (await fetch("/api/replays")).json();
  const files = list.files||[];
  if(!files.length){listEl.innerHTML='<div class="empty">暂无回放文件</div>';}

  function cardEl(card){
    const d=document.createElement("div");d.className="card card-sm";
    if(card==="back"){d.classList.add("back");return d;}
    if(card[1]==="h"||card[1]==="d")d.classList.add("red");
    const rk=card[0]==="T"?"10":card[0];
    d.innerHTML=`<div class="rk${rk==="10"?" rk2":""}">${rk}</div><div class="st">${SUIT[card[1]]}</div>`;
    return d;
  }

  function draw(f){
    document.getElementById("count-bar").innerHTML=
      `第 <b>${f.hand_no}</b> 手 · ${STREET_CN[f.street]||""} · `+
      `底池 <b>${f.pot}</b> · 跟注额 ${f.current_bet} · 按钮=seat${f.button}`;
    const bc=document.getElementById("board-cards");bc.innerHTML="";
    for(const c of f.board||[])bc.appendChild(cardEl(c));
    const wrap=document.getElementById("seats");wrap.innerHTML="";
    for(const seat of ["0","1","2"]){
      const i=+seat;
      const box=document.createElement("div");
      box.style.cssText="display:flex;align-items:center;gap:10px;"+
        "padding:6px 4px;border-top:1px solid var(--border)"+
        (f.folded[i]?";opacity:.4":"");
      const name=document.createElement("div");
      name.style.cssText="width:210px;font-size:12px;flex-shrink:0";
      name.innerHTML=`座位${seat}${i===f.button?" 🔘":""} · 筹码${f.stacks[i]}`+
        (f.bets[i]?` · 已下${f.bets[i]}`:"")+
        (f.allin[i]?" · 全下":"")+(f.folded[i]?" · 弃牌":"");
      box.appendChild(name);
      const hand=document.createElement("div");
      hand.style.cssText="display:flex;gap:5px";
      for(const c of (f.holes[seat]||[]))hand.appendChild(cardEl(c));
      box.appendChild(hand);
      wrap.appendChild(box);
    }
    // action history so far
    const hist=[];
    for(const e of f.history||[]){
      const k=e.kind;
      if(k==="blinds")hist.push(`盲注: 钮${e.button} 小${e.sb} 大${e.bb}`);
      else if(k==="street")hist.push(`--- ${STREET_CN[e.street]} ---`);
      else if(k==="raise")hist.push(`s${e.seat}加到${e.to}${e.allin?"(全)":""}`);
      else if(k==="call")hist.push(`s${e.seat}跟${e.amount}${e.allin?"(全)":""}`);
      else if(k==="fold")hist.push(`s${e.seat}弃`);
      else if(k==="check")hist.push(`s${e.seat}过`);
      else if(k==="showdown")hist.push(`摊牌: ${e.winners.map(w=>`s${w}`).join("/")}赢`);
      else if(k==="win_fold")hist.push(`s${e.seat}收池${e.pot}`);
    }
    document.getElementById("hist-log").textContent = hist.join("  |  ");
  }

  function renderFrame(i){
    if(!frames.length)return;
    current=Math.max(0,Math.min(i,frames.length-1));
    draw(frames[current]);
    slider.value=current;
    document.getElementById("pos").textContent=`${current+1}/${frames.length}`;
    document.getElementById("frame-label").textContent=frames[current].label;
    const dl=document.getElementById("dec-log");
    dl.innerHTML=decisions.filter(d=>d.frame<=current)
      .map(d=>`<div${d.fallback?' class="fall"':""}>#${d.frame} seat${d.seat} `+
        `${d.backend}: ${d.label}${d.fallback?" ⚡兜底":""} `+
        `(${d.elapsed_s}s)</div>`).join("");
    dl.scrollTop=dl.scrollHeight;
  }

  function stopPlay(){clearInterval(timer);timer=null;
    document.getElementById("play").textContent="▶ 自动";}
  document.getElementById("play").addEventListener("click",()=>{
    if(timer){stopPlay();return;}
    if(current>=frames.length-1)current=-1;
    document.getElementById("play").textContent="⏸ 暂停";
    timer=setInterval(()=>{
      if(current<frames.length-1)renderFrame(current+1);else stopPlay();},900);});
  document.getElementById("prev").addEventListener("click",()=>{stopPlay();renderFrame(current-1);});
  document.getElementById("next").addEventListener("click",()=>{stopPlay();renderFrame(current+1);});
  slider.addEventListener("input",()=>{stopPlay();renderFrame(+slider.value);});

  async function load(name){
    const data=await (await fetch("/api/replay_file?name="+
      encodeURIComponent(name))).json();
    frames=data.frames||[];decisions=data.decisions||[];
    slider.max=Math.max(frames.length-1,0);
    renderFrame(0);
    document.querySelectorAll(".replay-item").forEach(el=>
      el.classList.toggle("active",el.dataset.name===name));
  }
  for(const f of files){
    const d=document.createElement("div");
    d.className="replay-item";d.dataset.name=f.name;
    d.innerHTML=`<div>${f.name.slice(0,22)}…</div>
      <div class="meta">${f.frames}帧 · 胜者座位${f.winner??"?"} · `+
      `${new Date(f.mtime*1000).toLocaleString()}</div>`;
    d.addEventListener("click",()=>load(f.name));
    listEl.appendChild(d);
  }
  const want=new URLSearchParams(location.search).get("file");
  if(want&&files.some(f=>f.name===want))load(want);
})();
