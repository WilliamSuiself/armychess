// Blackjack replay viewer.
(async () => {
  const SUIT = {s:"♠",h:"♥",d:"♦",c:"♣"};
  const listEl = document.getElementById("list");
  const slider = document.getElementById("slider");
  let frames=[], current=0, timer=null, decisions=[];

  const list = await (await fetch("/api/replays")).json();
  const files = list.files||[];
  if(!files.length){listEl.innerHTML='<div class="empty">暂无回放文件</div>';}

  function cardEl(card){
    const d=document.createElement("div");d.className="card card-sm";
    if(card==="back"){d.classList.add("back");return d;}
    if(card[0]==="h"||card[0]==="d")d.classList.add("red");
    const rk=card[1]==="T"?"10":card[1];
    d.innerHTML=`<div class="rk${rk==="10"?" rk2":""}">${rk}</div><div class="st">${SUIT[card[0]]}</div>`;
    return d;
  }
  function hv(cards){
    let t=0,a=0;for(const c of cards){const r="A23456789TJQK".indexOf(c[1])+1;
      t+=r===1?11:Math.min(r,10);if(r===1)a++;}
    while(a&&t>21){t-=10;a--;}return t;
  }

  function draw(f){
    // count bar
    document.getElementById("count-bar").innerHTML=
      `🧮 流水 <b>${f.running_count}</b> · 第 <b>${f.round}</b> 轮 · `+
      `${f.label}`;
    // dealer
    const dh=document.getElementById("dealer-hand");dh.innerHTML="";
    const shown=f.dealer_hidden?f.dealer.slice(0,1):f.dealer;
    for(const c of shown)dh.appendChild(cardEl(c));
    if(f.dealer_hidden)dh.appendChild(cardEl("back"));
    document.getElementById("dealer-total").textContent=
      shown.length?hv(shown)+(f.dealer_hidden?"+?":"点"):"";
    // seats
    const wrap=document.getElementById("seats");wrap.innerHTML="";
    for(const seat of ["0","1","2"]){
      const box=document.createElement("div");
      box.style.cssText="display:flex;align-items:center;gap:10px;"+
        "padding:6px 4px;border-top:1px solid var(--border)";
      const name=document.createElement("div");
      name.style.cssText="width:190px;font-size:12px;flex-shrink:0";
      name.innerHTML=`座位${seat} · 筹码${f.chips[seat]}`+
        (f.bets[seat]?` · 押${f.bets[seat]}`:"")+
        (f.hands[seat].length?` · ${hv(f.hands[seat])}点`:"");
      box.appendChild(name);
      const hand=document.createElement("div");
      hand.style.cssText="display:flex;gap:5px;flex-wrap:wrap";
      for(const c of f.hands[seat])hand.appendChild(cardEl(c));
      box.appendChild(hand);
      wrap.appendChild(box);
    }
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
    const meta=f.meta||{};
    d.innerHTML=`<div>${f.name.slice(0,22)}…</div>
      <div class="meta">${f.frames}帧 · 胜者座位${f.winner??"?"} · `+
      `${new Date(f.mtime*1000).toLocaleString()}</div>`;
    d.addEventListener("click",()=>load(f.name));
    listEl.appendChild(d);
  }
  const want=new URLSearchParams(location.search).get("file");
  if(want&&files.some(f=>f.name===want))load(want);
})();
