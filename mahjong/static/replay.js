// Mahjong replay viewer — frames record full concealed hands (post-hoc).
(async () => {
  const listEl = document.getElementById("list");
  const slider = document.getElementById("slider");
  const SUIT_IMG = ["Man","Pin","Sou"];
  const TILE_CN = ["万","筒","条"];
  let frames=[], current=0, timer=null, decisions=[];

  const list = await (await fetch("/api/replays")).json();
  const files = list.files||[];
  if(!files.length){listEl.innerHTML='<div class="empty">暂无回放文件</div>';}

  function tileEl(t){
    const img=document.createElement("img");
    img.className="tile tile-sm";
    img.src=`/static/tiles/${SUIT_IMG[Math.floor(t/9)]}${t%9+1}.png`;
    img.alt=TILE_CN[Math.floor(t/9)]+(t%9+1);
    img.draggable=false;
    return img;
  }

  function draw(f){
    document.getElementById("count-bar").innerHTML=
      `第 <b>${f.hand_no}</b> 局 · 庄 seat${f.dealer} · 牌墙 <b>${f.wall}</b>`+
      (f.pending?` · 待认领 ${f.pending}`:"");
    const wrap=document.getElementById("seats");wrap.innerHTML="";
    for(let i=0;i<4;i++){
      const seat=String(i);
      const row=document.createElement("div");row.className="rseat";
      const name=document.createElement("div");name.className="rseat-name";
      name.innerHTML=`座位${seat}${i===f.dealer?" 🀅":""} · ${f.points[i]}分`+
        `<br><span style="color:#7a988a">暗牌${f.concealed_count[i]}张`+
        (f.turn===i?" · ◀轮":"")+"</span>";
      row.appendChild(name);
      const tiles=document.createElement("div");tiles.className="rseat-tiles";
      for(const m of f.melds[seat]||[])for(const t of m[1])
        tiles.appendChild(tileEl(t));
      if((f.melds[seat]||[]).length){
        const sep=document.createElement("span");
        sep.style.cssText="width:8px";tiles.appendChild(sep);}
      for(const t of f.concealed[seat]||[])tiles.appendChild(tileEl(t));
      row.appendChild(tiles);
      const disc=document.createElement("div");disc.className="rseat-disc";
      for(const t of f.discards[seat]||[])disc.appendChild(tileEl(t));
      row.appendChild(disc);
      wrap.appendChild(row);
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
      if(current<frames.length-1)renderFrame(current+1);else stopPlay();},700);});
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
