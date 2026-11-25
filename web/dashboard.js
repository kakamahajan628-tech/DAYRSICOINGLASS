"use strict";
const $=id=>document.getElementById(id);
const number=(v,n=2)=>v==null?"—":v!==0&&Math.abs(v)<1e-6?Number(v).toExponential(3):Number(v).toLocaleString("en-IN",{maximumFractionDigits:n});
const when=v=>v?new Date(v).toLocaleString("en-IN",{timeZone:"Asia/Kolkata",hour12:false}):"Unavailable";
let snapshot={},history=[];
async function api(path){const r=await fetch(path,{credentials:"same-origin",cache:"no-store"});if(r.status===401){location.href="/login";throw Error("Session expired");}if(!r.ok)throw Error("Data unavailable. Try refresh.");return r.json();}
function render(){
 const tbody=$("coins");tbody.replaceChildren();
 const coins=(snapshot.coins||[]).filter(c=>!$("priority").checked||c.priority.includes("BELOW 70"));
 $("empty").hidden=coins.length>0;
 for(const c of coins){
  const row=document.createElement("tr");row.tabIndex=0;
  const values=[c.symbol,c.priority,c.exchange+" / "+c.market,number(c.price,10),c.day_gain==null?"—":number(c.day_gain*100)+"%",...['1h','4h','1d'].map(tf=>number(c.metrics?.[tf]?.rsi,1)),c.coverage?c.coverage.valid+" / "+c.coverage.expected:"—"];
  values.forEach((v,i)=>{const cell=document.createElement("td");cell.textContent=v;if(i>2)cell.className="num";if(i===1&&c.priority.includes("BELOW 70"))cell.className="priority";row.append(cell);});
  const select=()=>{$("coin").value=c.symbol;loadHistory();};row.onclick=select;row.onkeydown=e=>{if(e.key==="Enter")select();};tbody.append(row);
 }
}
function flatten(value,prefix=""){if(value&&typeof value==='object'&&!Array.isArray(value))return Object.entries(value).flatMap(([k,v])=>flatten(v,prefix?prefix+" / "+k:k));return [prefix+": "+(Array.isArray(value)?value.join(", "):value??"Unavailable")];}
async function refresh(){try{
 snapshot=await api("/api/watchlist");$("status").textContent=snapshot.status||"No snapshot";$("asof").textContent="Scheduled "+when(snapshot.scheduled_ist)+" IST · Scan finished "+when(snapshot.scan_finished_at)+" IST";
 const selected=$("coin").value||(snapshot.coins||[])[0]?.symbol;$("coin").replaceChildren(new Option("Select a coin",""));for(const c of snapshot.coins||[])$("coin").add(new Option(c.symbol,c.symbol));$("coin").value=selected||"";
 render();$("sources").replaceChildren();for(const [name,data]of Object.entries(snapshot.sources||{})){const el=document.createElement("div");el.className="source";const title=document.createElement("strong");title.textContent=name;const desc=document.createElement("span");desc.textContent=typeof data==='object'?flatten(data).join("; "):data;el.append(title,desc);$("sources").append(el);}
 const health=await api("/api/health");const scanner=health.scanner?.epoch;const age=scanner?Math.round(Date.now()/1000-scanner):null;$("health").textContent="Scanner heartbeat: "+(age==null?"missing":age+" seconds ago")+". Telegram: "+health.telegram+". Daily delivered: "+(health.daily_health?health.daily_health.delivered+"/48":"not recorded yet");$("error").textContent="";
 if(selected)await loadHistory();
}catch(e){$("error").textContent=e.message;}}
async function loadHistory(){try{const coin=$("coin").value;history=coin?await api("/api/history?coin="+encodeURIComponent(coin)):[];draw();}catch(e){$("error").textContent=e.message;}}
function draw(){
 const canvas=$("chart"),rect=canvas.getBoundingClientRect(),dpr=window.devicePixelRatio||1;canvas.width=rect.width*dpr;canvas.height=rect.height*dpr;const ctx=canvas.getContext("2d");ctx.scale(dpr,dpr);const w=rect.width,h=rect.height;ctx.clearRect(0,0,w,h);
 const series=$("series").value,rows=history.map(r=>({...r,value:series==='price'?r.price:r.metrics?.[series]?.rsi})),valid=rows.filter(r=>r.value!=null&&Number.isFinite(r.value));ctx.fillStyle="#93a9be";ctx.font="12px Arial";
 if(!valid.length){ctx.fillText("No valid stored observations for this series.",20,45);return;}
 const minX=rows[0].epoch,maxX=rows[rows.length-1].epoch||minX+1;let minY=series==='price'?Math.min(...valid.map(r=>r.value)):0,maxY=series==='price'?Math.max(...valid.map(r=>r.value)):100;
 if(minY===maxY){minY*=.99;maxY=maxY?maxY*1.01:1;}
 const pad={l:80,r:20,t:16,b:40},x=t=>pad.l+(t-minX)/(maxX-minX||1)*(w-pad.l-pad.r),y=v=>pad.t+(maxY-v)/(maxY-minY)*(h-pad.t-pad.b);
 for(let i=0;i<5;i++){const v=minY+(maxY-minY)*i/4,yy=y(v);ctx.strokeStyle="#273e52";ctx.beginPath();ctx.moveTo(pad.l,yy);ctx.lineTo(w-pad.r,yy);ctx.stroke();ctx.fillText(number(v,series==='price'?8:0),2,yy+4);}
 ctx.strokeStyle="#73b3f4";ctx.lineWidth=2;ctx.beginPath();let previous=null;
 for(const r of rows){if(r.value==null){previous=null;continue;}if(!previous||r.epoch-previous.epoch>2100||r.exchange!==previous.exchange||r.market!==previous.market)ctx.moveTo(x(r.epoch),y(r.value));else ctx.lineTo(x(r.epoch),y(r.value));previous=r;}ctx.stroke();
 if(valid.length===1){ctx.fillStyle="#73b3f4";ctx.beginPath();ctx.arc(x(valid[0].epoch),y(valid[0].value),4,0,Math.PI*2);ctx.fill();}
 const time=t=>new Date(t*1000).toLocaleString("en-IN",{timeZone:"Asia/Kolkata",day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit",hour12:false});ctx.fillText(time(minX),pad.l,h-12);ctx.textAlign="right";ctx.fillText(time(maxX)+" IST",w-pad.r,h-12);ctx.textAlign="left";
}
$("refresh").onclick=refresh;$("priority").onchange=render;$("coin").onchange=loadHistory;$("series").onchange=draw;window.addEventListener("resize",draw);refresh();setInterval(refresh,60000);
