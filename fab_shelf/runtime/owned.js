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
  async function get(path) {
    const r = await fetch(path, {credentials:"same-origin",headers:{"Accept":"application/json","X-Requested-With":"XMLHttpRequest"}});
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
  async function account(){
    const me=await get("/i/users/me");
    if(!me.uid)throw Error("请先登录 Fab");
    return {uid:me.uid,name:me.displayName||"",accepted:me.hasAcceptedBuyerTos===true};
  }
  if(q.op==="account")return account();
  if(q.op==="presence"){
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
    return {found};
  }
  if(q.op==="freePage"){
    const p=new URLSearchParams({is_free:"1",sort_by:"-firstPublishedAt"});
    if(q.range==="quixel")p.set("seller","Quixel Megascans");
    if(q.cursor)p.set("cursor",q.cursor);
    const r=await get("/i/listings/search?"+p);
    return {items:r.results.map(x=>({uid:x.uid,title:x.title})),next:r.cursors?.next||""};
  }
  if(q.op==="ownedBatch"){
    const me=await account();
    if(me.uid!==q.account)throw Error("账号已变更，任务已暂停");
    const params=new URLSearchParams();
    for(const uid of q.uids)params.append("listing_ids",uid);
    const states=await get("/i/users/me/listings-states?"+params);
    if(!Array.isArray(states))throw Error("Fab 批量所有权响应格式异常");
    const owned=q.uids.filter(uid=>{
      const matches=states.filter(row=>row.uid===uid);
      return matches.length===1 && matches[0].acquired===true;
    });
    return {owned};
  }
  if(q.op==="claimFree"){
    const me=await account();
    if(me.uid!==q.account)throw Error("账号已变更，任务已暂停");
    const owns=async()=>{
      const state=await get("/i/users/me/listings-states/"+id+"?fields=ownership");
      if(!Array.isArray(state.ownership))throw Error("无法核对所有权");
      return state.ownership.length>0;
    };
    if(await owns())return {status:"owned"};
    const [d,p]=await Promise.all([get(base),get(base+"/prices-infos")]);
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
    const result=await fetch(base+"/add-to-library",{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/x-www-form-urlencoded","X-CsrfToken":decodeURIComponent(token()),"X-Requested-With":"XMLHttpRequest"},body:new URLSearchParams({offer_id:l.offerId})});
    if([401,403,429].includes(result.status)||result.status>=500)throw httpError(result);
    if(await owns())return {status:"added"};
    if(result.ok)throw Error("Fab 已接收请求，所有权尚未确认，请稍后继续核验");
    return {status:"manual"};
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
    const page=await get((q.op==="library"?"/i/library/search?":"/i/listings/search?")+p);
    if(!Array.isArray(page.results))throw Error("Fab 个人库格式已变更");
    return {items:page.results.map(e=>{
      const x=typeof e.listing==="object"&&e.listing?e.listing:e;
      const uid=q.op==="discover"?e.uid:x===e?(e.listingUid || (typeof e.listing==="string"?e.listing:null)):x.uid;
      return {uid,discovery:q.op==="discover",category:x.category?.slug||"",title:x.title||e.title||"",thumb:photo(x)||photo(e),kind:x.listingType?.code||x.listingType||""};
    }).filter(i=>i.uid), next:page.cursors?.next || (page.next?new URL(page.next,location.origin).searchParams.get("cursor"):"")};
  }
  if(q.op==="detail"){
    const x=await get(base);
    return {category:x.category?.slug||"",uid:q.uid,title:x.title,thumb:photo(x),kind:x.listingType?.code||x.listingType||""};
  }
  if(q.op==="formats"){
    const data=await get(base+"/asset-formats");
    return {files:data.flatMap(x=>(x.files||[]).map(f=>({uid:f.uid,name:f.name,format:x.assetFormatType?.code,bytes:f.size||f.fileSize||0})))};
  }
  if(q.op==="link"){
    const d=await get(base+"/asset-formats/"+encodeURIComponent(q.format)+"/files/"+encodeURIComponent(q.file)+"/download-info");
    const url=d.downloadInfo?.[0]?.downloadUrl;
    if(!url||new URL(url).protocol!=="https:")throw Error("此规格未返回下载地址");
    return {url};
  }
  throw Error("未知请求");
}













