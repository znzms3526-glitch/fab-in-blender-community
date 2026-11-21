const assert=require("node:assert/strict"),vm=require("node:vm"),fs=require("node:fs"),path=require("node:path");
const source=fs.readFileSync(path.join(__dirname,"fab_shelf/runtime/owned.js"),"utf8");
async function scenario({price=0,bundle=false,owns=false,accepted=true,account="a",confirmed=true,single=true,rows,accountAtSubmit=account,fast=false,readError=0}={}){
 let posts=0,owned=owns;
 let reads=0;
 const context={location:{origin:"https://www.fab.com"},document:{cookie:"fab_csrftoken=mock"},URLSearchParams,fetch:async(url,opts={})=>{
  let body;
  if(url.endsWith("/users/me"))body={uid:++reads===1?account:accountAtSubmit,hasAcceptedBuyerTos:accepted};
  else if(url.includes("listings-states/")){
    if(readError)return {ok:false,status:readError};
    body=single?{ownership:owned?["license"]:[]}:null;
  }
  else if(url.includes("listings-states?"))body=rows??[{uid:"x",acquired:owned}];
  else if(url.endsWith("prices-infos"))body={offers:[{offerId:"test",price}]};
  else if(url.endsWith("add-to-library")){assert.equal(opts.method,"POST");assert.equal(opts.body.get("offer_id"),"test");assert.equal(opts.headers["X-CsrfToken"],"mock");posts++;owned=confirmed;}
  else body={listingType:bundle?"bundle":"3d-model",licenses:[{slug:"personal",offerId:"test"}]};
  return {ok:true,status:200,text:async()=>JSON.stringify(body)};
 }};
 const fn=vm.runInNewContext(source+";fabRequest",context);
 const item={uid:"x",account:"a",tier:"personal"};
 try{
   if(fast){
     const batch=await fn({op:"ownedBatch",uids:["x"],account:"a"});
     assert.equal(batch.available?.[0],"x");
     const result=await fn({...item,op:"submitFree",recheckOwnership:true});
     return [result.message||result.status,posts];
   }
   const check=await fn({...item,op:"checkFree"});
   const result=check.status==="available"?await fn({...item,op:"submitFree"}):check;
   return [result.message||result.status,posts];
 }
 catch(e){return [e.message,posts];}
}
(async()=>{
 for(const [q,want] of [[{},["added",1]],[{price:50},["manual",0]],[{bundle:true},["manual",0]],[{owns:true},["owned",0]],[{accepted:false},["manual",0]],[{confirmed:false},["verify",1]],[{account:"other"},["账号已变更，任务已暂停",0]],[{accountAtSubmit:"other"},["账号已变更，任务已暂停",0]],[{single:false},["added",1]],[{single:false,owns:true},["owned",0]]])assert.deepEqual(await scenario(q),want);
 for(const rows of [[],{},[null],[{uid:"other",acquired:false}],[{uid:"x",acquired:"false"}],[{uid:"x",acquired:false},{uid:"x",acquired:true}]]){
   assert.deepEqual(await scenario({single:false,rows}),["unknown",0]);
 }
 const hint={fast:true,rows:[{uid:"x",acquired:false}]};
 for(const [change,want] of [[{},["added",1]],[{owns:true},["owned",0]],[{price:50},["manual",0]],
   [{accountAtSubmit:"new"},["账号已变更，任务已暂停",0]],[{readError:503},["readSeparately",0]],
   [{readError:429},["Fab HTTP 429",0]],[{confirmed:false},["verify",1]]])
   assert.deepEqual(await scenario({...hint,...change}),want);
 console.log("PASS: free-only submission and ownership verification using simulated Fab responses");
})().catch(e=>{console.error(e);process.exitCode=1});


