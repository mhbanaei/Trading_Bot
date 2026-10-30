/* تبدیل دارایی‌های خرد — مقصدآگاه: کاربر تومان یا تتر را انتخاب
   می‌کند؛ در «بازار معاملات» تک‌بازاری فقط همان مقصد فعال است */
let dustTarget="IRT";
function dustUnitFa(t){return t==="USDT"?"تتر (USDT)":"تومان (IRT)";}
function dustMoney(v,t){ if(v==null||isNaN(v))return "—";
  return t==="USDT" ? "$"+fmt(Number(v),2) : fmtTom(v)+" تومان"; }
function dustRowHtml(x,unitT){
  return `<label style="display:flex;gap:8px;align-items:center;padding:5px 6px;border-bottom:1px solid var(--border);cursor:pointer">
    <input type="checkbox" class="dustChk" data-coin="${esc(x.coin)}" checked>
    <span style="min-width:72px;font-weight:700">${esc(x.coin)}</span>
    <span class="mut" style="font-size:11px;direction:ltr">${x.amount>=1?fmtTom(x.amount):x.amount}</span>
    <span style="flex:1"></span>
    <span style="font-weight:700;color:var(--green)">${dustMoney(x.value,unitT)}${unitT==="USDT"&&x.value_irt!=null?` <span class="mut" style="font-size:10.5px">(≈ ${fmtTom(x.value_irt)} ت)</span>`:""}</span>
  </label>`;}
async function openDustModal(){
  closeDustModal();
  /* مقصدهای مجاز از «بازار معاملات»: در حالت تک‌بازاری فقط همان بازار */
  const qm=((cfg&&cfg.exchange&&cfg.exchange.trade_quote_markets)||"both").trim().toLowerCase();
  const irtOn=qm!=="usdt", usdtOn=qm!=="irt";
  dustTarget=!irtOn?"USDT":(!usdtOn?"IRT":(dustTarget||"IRT"));
  let curTarget=dustTarget, items=[], unitT=dustTarget;
  const ov=document.createElement("div");
  ov.style.cssText="position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:9999;display:flex;align-items:center;justify-content:center";
  ov.innerHTML=`<div style="background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px;max-width:540px;width:92%;max-height:80vh;display:flex;flex-direction:column" dir="rtl">
    <div style="font-weight:800;font-size:15px;margin-bottom:6px">تبدیل دارایی‌های خرد</div>
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
      <span class="mut" style="font-size:12px">مقصد تبدیل:</span>
      <span id="dustTargets" style="display:inline-flex;border:1px solid var(--border);border-radius:9px;overflow:hidden">
        <button type="button" data-t="IRT" class="dust-t">تومان</button>
        <button type="button" data-t="USDT" class="dust-t">تتر</button>
      </span>
      <span id="dustHint" class="mut" style="font-size:11px"></span>
    </div>
    <div class="mut" style="font-size:12px;margin-bottom:10px" id="dustSub">در حال خواندن دارایی‌ها…</div>
    <div id="dustRows" style="overflow:auto;flex:1;min-height:120px;border:1px solid var(--border);border-radius:8px;padding:6px">…</div>
    <div id="dustSum" style="font-size:13px;margin-top:10px;font-weight:700"></div>
    <div style="display:flex;gap:8px;margin-top:12px;justify-content:flex-end">
      <button id="dustCancel" style="padding:7px 14px;border-radius:8px;border:1px solid var(--border);background:var(--panel2);color:var(--text);cursor:pointer">انصراف</button>
      <button id="dustGo" disabled style="padding:7px 14px;border-radius:8px;border:none;background:var(--green);color:#04150a;font-weight:800;cursor:pointer;opacity:.5">تبدیل کن</button>
    </div>
  </div>`;
  document.body.appendChild(ov); dustModal=ov;
  ov.addEventListener("click",e=>{ if(e.target===ov) closeDustModal(); });
  ov.querySelector("#dustCancel").onclick=closeDustModal;
  const rowsEl=ov.querySelector("#dustRows"), sumEl=ov.querySelector("#dustSum"), goBtn=ov.querySelector("#dustGo"), subEl=ov.querySelector("#dustSub"), hintEl=ov.querySelector("#dustHint");
  const tBtn={IRT:ov.querySelector('.dust-t[data-t="IRT"]'),USDT:ov.querySelector('.dust-t[data-t="USDT"]')};
  if(!irtOn){tBtn.IRT.disabled=true; tBtn.IRT.title="بازار معاملات تومانی خاموش است";}
  if(!usdtOn){tBtn.USDT.disabled=true; tBtn.USDT.title="بازار معاملات تتری خاموش است";}
  const markT=()=>Object.entries(tBtn).forEach(([k,b])=>{
    b.style.cssText="padding:5px 14px;font-size:12px;cursor:pointer;border:none;background:transparent;color:var(--muted)";
    if(k===curTarget)b.style.cssText+=";background:var(--accent-soft);color:var(--accent);font-weight:800";
    if(b.disabled)b.style.cssText+=";opacity:.4;cursor:not-allowed";});
  const rowsHtml=()=>{ const conv=items.filter(x=>x.convertible), noc=items.filter(x=>!x.convertible);
    return `<div style="padding:4px 6px;display:flex;gap:8px;align-items:center;position:sticky;top:0;background:var(--panel);z-index:1">
      <label style="display:flex;gap:6px;align-items:center;cursor:pointer;font-size:12px"><input type="checkbox" id="dustAll" checked> انتخاب همه‌ی قابل‌تبدیل‌ها</label>
      <span class="mut" style="font-size:11px">ارزش به ${unitT==="USDT"?"دلار":"تومان"}</span>
    </div>`
    + conv.map(x=>dustRowHtml(x,unitT)).join("")
    + (noc.length? `<div class="mut" style="padding:6px;font-size:11px;margin-top:4px">غیرقابل تبدیل (کف صرافی / بدون قیمت / ممنوعیت بازار):</div>`
      + noc.map(x=>`<div style="display:flex;gap:8px;align-items:center;padding:4px 6px 4px 26px;opacity:.65">
          <span style="min-width:72px">${esc(x.coin)}</span><span style="flex:1"></span>
          <span class="mut" style="font-size:11px">${x.value!=null?dustMoney(x.value,unitT)+" — ":""}${esc(x.reason||"")}</span>
        </div>`).join("") : "");};
  const upd=()=>{ const sel=[...rowsEl.querySelectorAll(".dustChk:checked")];
    const tot=sel.reduce((s,c)=>{const it=items.find(x=>x.coin===c.dataset.coin); return s+(it?(it.value||0):0);},0);
    goBtn.disabled=!sel.length; goBtn.style.opacity=sel.length?1:.5;
    goBtn.textContent=sel.length?("تبدیل به "+dustUnitFa(unitT)):"تبدیل کن";
    sumEl.textContent=sel.length?(sel.length+" سکه انتخاب شد — مجموع "+dustMoney(tot,unitT)):"هیچ سکه‌ای انتخاب نشده"; };
  rowsEl.onchange=e=>{ if(e.target.id==="dustAll"){ rowsEl.querySelectorAll(".dustChk").forEach(c=>c.checked=e.target.checked); } upd(); };
  goBtn.onclick=async()=>{
    const coins=[...rowsEl.querySelectorAll(".dustChk:checked")].map(c=>c.dataset.coin);
    if(!coins.length)return;
    goBtn.disabled=true; goBtn.style.opacity=.5; goBtn.textContent="در حال تبدیل…";
    try{ const r=await jpost("/api/control/convert_selected",{coins,target:unitT}); const res=(r&&r.result)||{};
      const okN=res.converted||0, bad=res.failed||0;
      const tt=res.target||unitT;
      const det=(res.details||[]).map(x=>x.coin+": "+(x.error? ("رد شد — "+(x.message||x.error)) : ("تبدیل شد"+(x.converted_amount!=null?(" ("+dustMoney(x.converted_amount,tt)+" دریافت شد)"):"")))).join("\n");
      closeDustModal();
      toast(okN? (okN+" سکه به "+dustUnitFa(tt)+" تبدیل شد"+(bad?(" | "+bad+" ناموفق — جزئیات در ادامه"):"")) : ("تبدیلی انجام نشد"+(bad?" — "+bad+" سکه رد شد، جزئیات در ادامه":"")));
      if(det) setTimeout(()=>{ toast(det); const t=$("#toast"); t.style.whiteSpace="pre-line"; setTimeout(()=>t.style.whiteSpace="",3000); }, (okN&&bad)?2400:150);
    }catch(e){ toast("خطا: "+e); }
    poll();
  };
  /* پیش‌نمایش مقصد: هر چیپ لیست همان بازار را می‌خواند و دکمهٔ
     تبدیل همیشه با مقصدِ همان پیش‌نمایش ارسال می‌شود */
  const renderRows=async(target)=>{
    curTarget=target; dustTarget=target; markT();
    subEl.textContent="در حال خواندن دارایی‌ها…"; rowsEl.innerHTML="…"; sumEl.textContent="";
    goBtn.disabled=true; goBtn.style.opacity=.5;
    try{
      const r=await fetch("/api/dust_preview?target="+encodeURIComponent(target));
      const d=await r.json();
      if(d.error){ subEl.textContent=d.error; rowsEl.innerHTML="<div class='empty'>—</div>"; sumEl.textContent=""; return; }
      items=d.items||[]; unitT=d.target||target;
      hintEl.textContent=d.hint??"";
      if(!items.length){ subEl.textContent="دارایی خرد قابل تبدیلی در این مقصد پیدا نشد"; rowsEl.innerHTML="<div class='empty'>—</div>"; sumEl.textContent=""; return; }
      subEl.textContent="تیک بزن و تایید کن — فقط ردیف‌های تیک‌خورده واقعاً به "+dustUnitFa(unitT)+" تبدیل می‌شوند";
      rowsEl.innerHTML=rowsHtml();
      upd();
    }catch(e){ subEl.textContent="خطا در دریافت لیست: "+e; }
  };
  Object.entries(tBtn).forEach(([k,b])=>b.onclick=()=>{ if(!b.disabled&&k!==curTarget) renderRows(k); });
  await renderRows(curTarget);
}
