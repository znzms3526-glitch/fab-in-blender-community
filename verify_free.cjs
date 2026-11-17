const assert=require("node:assert/strict"),vm=require("node:vm"),fs=require("node:fs"),path=require("node:path");
const source=fs.readFileSync(path.join(__dirname,"fab_shelf/runtime/owned.js"),"utf8");
async function scenario({price=0,bundle=false,owns=false,accepted=true,account="a",confirmed=true}={}){
 let posts=0,owned=owns;
 const context={location:{origin:"https://www.fab.com"},document:{cookie:"fab_csrftoken=mock"},URLSearchParams,fetch:async(url,opts={})=>{
  let body;
  if(url.endsWith("/users/me"))body={uid:account,hasAcceptedBuyerTos:accepted};
  else if(url.includes("listings-states"))body={ownership:owned?["license"]:[]};
  else if(url.endsWith("prices-infos"))body={offers:[{offerId:"test",price}]};
  else if(url.endsWith("add-to-library")){assert.equal(opts.method,"POST");assert.equal(opts.body.get("offer_id"),"test");assert.equal(opts.headers["X-CsrfToken"],"mock");posts++;owned=confirmed;}
  else body={listingType:bundle?"bundle":"3d-model",licenses:[{slug:"personal",offerId:"test"}]};
  return {ok:true,status:200,text:async()=>JSON.stringify(body)};
 }};
 const fn=vm.runInNewContext(source+";fabRequest",context);
 try{return [(await fn({op:"claimFree",uid:"x",account:"a",tier:"personal"})).status,posts];}
 catch(e){return [e.message,posts];}
}
(async()=>{
 for(const [q,want] of [[{},["added",1]],[{price:50},["manual",0]],[{bundle:true},["manual",0]],[{owns:true},["owned",0]],[{accepted:false},["manual",0]],[{confirmed:false},["Fab 已接收请求，所有权尚未确认，请稍后继续核验",1]],[{account:"other"},["账号已变更，任务已暂停",0]]])assert.deepEqual(await scenario(q),want);
 console.log("PASS: free-only submission and ownership verification using simulated Fab responses");
})().catch(e=>{console.error(e);process.exitCode=1});


