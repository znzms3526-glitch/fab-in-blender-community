async function fabRequest(q) {
  if (location.origin !== "https://www.fab.com") throw Error("请先登录 Fab");
  function httpError(r){
    let hint="";
    if(r.status===429){
      const h=r.headers?.get("Retry-After");
      if(h){const sec=/^\d+$/.test(h)?Number(h):Math.ceil((Date.parse(h)-Date.now())/1000);
            if(Number.isFinite(sec)&&sec>0)hint=" retry="+sec;}
    }
    return Error("Fab HTTP "+r.status+hint);
  }
  async function get(path,cache="default") {
    const r = await fetch(path, {credentials:"same-origin",cache,headers:{"Accept":"application/json","X-Requested-With":"XMLHttpRequest"}});
    if (!r.ok) throw httpError(r);
    const raw=await r.text();
    try{return JSON.parse(raw)}catch{
      // Normalize literal C0 characters only inside quoted JSON strings.
      let quoted=false,escaped=false;
      const fixed=Array.from(raw,ch=>{
        if(escaped){escaped=false;return ch}
        if(quoted && ch==="\\"){escaped=true;return ch}
        if(ch==='"')quoted=!quoted;
        if(quoted && ch.charCodeAt(0)<32)return JSON.stringify(ch).slice(1,-1);
        return ch;
      }).join("");
      try{return JSON.parse(fixed)}catch{throw Error("Fab 响应格式异常，请稍后重试")}
    }
  }
  const id = encodeURIComponent(q.uid || "");
  const base = "/i/listings/" + id;
  const photo = x => {
    const pictures = x?.thumbnails?.[0]?.images || x?.thumbnail?.images || x?.images || [];
    return Array.isArray(pictures) ? [...pictures].filter(i=>i.url).sort((a,b)=>Math.abs(a.width-360)-Math.abs(b.width-360))[0]?.url || "" : "";
  };
  async function account(allowSignedOut=false){
    let me;
    try{me=await get("/i/users/me","no-store");}
    catch(e){
      if(allowSignedOut && /^Fab HTTP (401|403)$/.test(e.message))return {uid:""};
      throw e;
    }
    if(!me?.uid){
      if(allowSignedOut)return {uid:""};
      throw Error("请先登录 Fab");
    }
    return {uid:me.uid,name:me.displayName||"",accepted:me.hasAcceptedBuyerTos===true};
  }
  async function checkLibraryAccount(){
    if(q.account && (await account(true)).uid!==q.account)throw Error("Fab 账号正在切换");
  }
  async function claimAccount(){
    const me=await account();
    if(me.uid!==q.account)throw Error("账号已变更，任务已暂停");
    return me;
  }
  if(q.op==="account")return account(q.allowSignedOut===true);
  if(q.op==="presence"){
    await checkLibraryAccount();
    const found=[];
    for(let i=0;i<q.values.length;i+=3){
      await Promise.all(q.values.slice(i,i+3).map(async value=>{
        const p=new URLSearchParams({listing_types:q.kind||value});
        if(q.kind)p.set("categories",value);
        const res=await fetch("/i/library/search?"+p,{credentials:"same-origin",headers:{"Accept":"application/json","X-Requested-With":"XMLHttpRequest"}});
        if(res.status===400 || res.status===404)return;
        if(!res.ok)throw Error("Fab HTTP "+res.status);
        const r=await res.json();
        if(!Array.isArray(r.results))throw Error("分类结果不可用");
        if(r.results.length)found.push(value);
      }));
    }
    await checkLibraryAccount();
    return {found};
  }
  if(q.op==="freePage"){
    const p=new URLSearchParams({is_free:"1",sort_by:"-firstPublishedAt"});
    if(q.range==="quixel")p.set("seller","Quixel Megascans");
    if(q.cursor)p.set("cursor",q.cursor);
    const r=await get("/i/listings/search?"+p);
    return {items:r.results.map(x=>({uid:x.uid,title:x.title})),next:r.cursors?.next||""};
  }
  async function batchStates(uids){
    const params=new URLSearchParams();
    for(const uid of uids)params.append("listing_ids",uid);
    const rows=await get("/i/users/me/listings-states?"+params,"no-store");
    if(!Array.isArray(rows))return [];
    return uids.flatMap(uid=>{
      const matches=rows.filter(row=>row?.uid===uid);
      if(matches.length!==1 || typeof matches[0].acquired!=="boolean")return [];
      return [{uid,acquired:matches[0].acquired}];
    });
  }
  function unavailableRead(e){
    return /响应格式异常|HTTP (400|404)\b/.test(e.message);
  }
  async function ownership(){
    try{
      const state=await get("/i/users/me/listings-states/"+id+"?fields=ownership","no-store");
      if(Array.isArray(state?.ownership) && (state.uid==null || state.uid===q.uid))
        return state.ownership.length>0;
    }catch(e){if(!unavailableRead(e))throw e;}
    try{
      const rows=await batchStates([q.uid]);
      return rows[0]?.acquired ?? null;
    }catch(e){if(!unavailableRead(e))throw e;return null;}
  }
  async function formats(){
    const data=await get(base+"/asset-formats");
    return data.flatMap(x=>(x.files||[]).map(f=>({
      uid:f.uid,name:f.name,format:x.assetFormatType?.code,bytes:f.size||f.fileSize||0
    })));
  }
  const listing=x=>({category:x.category?.slug||"",uid:q.uid,title:x.title,thumb:photo(x),kind:x.listingType?.code||x.listingType||""});
  if(q.op==="listingAccess"){
    const me=await account();
    const own=await ownership();
    let details, files;
    if(own===true){
      [details,files]=await Promise.all([get(base),formats()]);
    }
    if((await account()).uid!==me.uid)throw Error("账号已变更，请重新核对");
    if(own!==true)return {status:own===false?"available":"unknown",account:me.uid};
    return {status:"owned",account:me.uid,listing:listing(details),files};
  }
  if(q.op==="ownedBatch" || q.op==="checkFree" || q.op==="submitFree"){
    let me,current;
    try{
      me=await claimAccount();
      // Attribute the live entitlement read to the account on both sides of the request.
      if(q.op==="submitFree" && q.recheckOwnership){
        try{current=await ownership();}
        catch(e){
          if(/Failed to fetch|NetworkError|HTTP (5\d\d|408)\b/.test(e.message))
            return {status:"readSeparately"};
          throw e;
        }
        await claimAccount();
      }
    }catch(e){
      if(q.op==="submitFree")return {status:"beforeSubmitError",message:e.message};
      throw e;
    }
    if(q.op==="ownedBatch"){
      const rows=await batchStates(q.uids);
      await claimAccount();
      return {owned:rows.filter(row=>row.acquired).map(row=>row.uid),
              available:rows.filter(row=>!row.acquired).map(row=>row.uid)};
    }
    if(q.op==="checkFree"){
      const value=await ownership();
      await claimAccount();
      return {status:value===null?"unknown":value?"owned":"available"};
    }
    let offerId,csrf;
    try{
      if(q.recheckOwnership && current!==false)return {status:current===true?"owned":"retry"};
      const [d,p]=await Promise.all([get(base),get(base+"/prices-infos","no-store")]);
      if((d.listingType?.code||d.listingType)==="bundle" || (d.productType?.code||d.productType)==="bundle" || !me.accepted)return {status:"manual"};
      const licenses=Array.isArray(d.licenses)?d.licenses:[];
      const l=licenses.find(l=>l.slug===q.tier)||licenses.find(l=>l.isCc0||l.isCc);
      const offer=p.offers?.find(o=>o.offerId===l?.offerId);
      if(!offer||!l?.offerId)return {status:"manual"};
      if(typeof offer.price!=="number" || offer.price!==0)return {status:"manual"};
      if(offer.discountedPrice!=null && offer.discountedPrice!==0)return {status:"manual"};
      const token=()=>document.cookie.split(";").map(c=>c.trim()).find(c=>c.startsWith("fab_csrftoken="))?.slice(14);
      if(!token())await fetch("/i/csrf",{credentials:"same-origin"});
      if(!token())throw Error("Fab 登录验证已过期");
      await claimAccount();
      offerId=l.offerId;
      csrf=decodeURIComponent(token()||"");
      if(!csrf)throw Error("Fab 登录验证已过期");
    }catch(e){return {status:"beforeSubmitError",message:e.message};}
    const result=await fetch(base+"/add-to-library",{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/x-www-form-urlencoded","X-CsrfToken":csrf,"X-Requested-With":"XMLHttpRequest"},body:new URLSearchParams({offer_id:offerId})});
    if([401,403,429].includes(result.status))return {status:"rejected",message:httpError(result).message};
    const verified=await ownership();
    await claimAccount();
    return {status:verified===true?(result.ok?"added":"confirmed"):"verify"};
  }
  if(q.op==="taxonomy"){
    const [tree,types]=await Promise.all([get("/i/taxonomy/categories/tree"),get("/i/taxonomy/listing-types")]);
    return {tree:tree.results,types:Object.fromEntries(types.results.map(t=>[t.code,t.name]))};
  }
  if(q.op==="library" || q.op==="discover"){
    const p=new URLSearchParams();
    if(q.op==="library")p.set("sort_by","-createdAt");
    if(q.kind && q.kind!=="ALL")p.set("listing_types",q.kind);

    if(q.category)p.set("categories",q.category);
    if(q.query)p.set("q",q.query);
    if(q.cursor)p.set("cursor",q.cursor);
    if(q.op==="library")await checkLibraryAccount();
    const page=await get((q.op==="library"?"/i/library/search?":"/i/listings/search?")+p,
                         q.op==="library"?"no-store":"default");
    if(q.op==="library")await checkLibraryAccount();
    if(!Array.isArray(page.results))throw Error("Fab 个人库格式已变更");
    return {items:page.results.map(e=>{
      const x=typeof e.listing==="object"&&e.listing?e.listing:e;
      const uid=q.op==="discover"?e.uid:x===e?(e.listingUid || (typeof e.listing==="string"?e.listing:null)):x.uid;
      return {uid,discovery:q.op==="discover",category:x.category?.slug||"",title:x.title||e.title||"",thumb:photo(x)||photo(e),kind:x.listingType?.code||x.listingType||""};
    }).filter(i=>i.uid), next:page.cursors?.next || (page.next?new URL(page.next,location.origin).searchParams.get("cursor"):"")};
  }
  if(q.op==="detail"){
    return listing(await get(base));
  }
  if(q.op==="formats"){
    if(q.bindAccount){
      const initial=await account();
      const files=await formats();
      if((await account()).uid!==initial.uid)throw Error("账号已变更，请重新核对");
      return {files,account:initial.uid};
    }
    return {files:await formats()};
  }
  if(q.op==="link"){
    const checkAccount=async()=>{
      if(q.account && (await account()).uid!==q.account)throw Error("账号已变更，请切回原账号后继续");
    };
    await checkAccount();
    const d=await get(base+"/asset-formats/"+encodeURIComponent(q.format)+"/files/"+encodeURIComponent(q.file)+"/download-info");
    await checkAccount();
    const url=d.downloadInfo?.[0]?.downloadUrl;
    if(!url||new URL(url).protocol!=="https:")throw Error("此规格未返回下载地址");
    return {url};
  }
  throw Error("未知请求");
}













