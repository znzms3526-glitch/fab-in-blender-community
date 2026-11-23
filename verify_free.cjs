const assert=require("node:assert/strict"),vm=require("node:vm"),fs=require("node:fs"),path=require("node:path");
const source=fs.readFileSync(path.join(__dirname,"fab_shelf/runtime/owned.js"),"utf8");
async function scenario({price=0,bundle=false,owns=false,accepted=true,account="a",confirmed=true,single=true,rows,accountAtSubmit=account,fast=false,readError=0,postStatus=200,switchOn=""}={}){
 let posts=0,owned=owns;
 let reads=0, switched=false;
 const context={location:{origin:"https://www.fab.com"},document:{cookie:"fab_csrftoken=mock"},URLSearchParams,fetch:async(url,opts={})=>{
  let body;
  if(switchOn && url.endsWith(switchOn))switched=true;
  if(url.endsWith("/users/me"))body={uid:switched?"other":++reads===1?account:accountAtSubmit,hasAcceptedBuyerTos:accepted};
  else if(url.includes("listings-states/")){
    if(readError)return {ok:false,status:readError};
    body=single?{ownership:owned?["license"]:[]}:null;
  }
  else if(url.includes("listings-states?"))body=rows??[{uid:"x",acquired:owned}];
  else if(url.endsWith("prices-infos"))body={offers:[{offerId:"test",price}]};
  else if(url.endsWith("add-to-library")){assert.equal(opts.method,"POST");assert.equal(opts.body.get("offer_id"),"test");assert.equal(opts.headers["X-CsrfToken"],"mock");posts++;
    if(postStatus!==200)return {ok:false,status:postStatus};
    owned=confirmed;
  }
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
async function manualAccess({ownership, accounts=["a","a"], fail}){
 const requests=[];
 const responses={
   "/i/users/me":()=>({uid:accounts.shift()}),
   "/i/users/me/listings-states/x?fields=ownership":()=>ownership===null?{}:{uid:"x",ownership},
   "/i/users/me/listings-states?listing_ids=x":()=>[],
   "/i/listings/x":()=>({title:"Sample",listingType:"3d-model"}),
   "/i/listings/x/asset-formats":()=>[{assetFormatType:{code:"fbx"},files:[{uid:"f",name:"sample_2k.fbx",size:1024}]}],
   "/i/listings/x/asset-formats/fbx/files/f/download-info":()=>({downloadInfo:[{downloadUrl:"https://example.test/sample"}]})
 };
 const fn=vm.runInNewContext(source+";fabRequest",{
   location:{origin:"https://www.fab.com"},URLSearchParams,URL,
   fetch:async(url,options)=>{
     assert(!options.method||options.method==="GET","Manual flow must be read-only");
     requests.push(url);
     if(url===fail)return {ok:false,status:429};
     assert(responses[url],url);
     const content=responses[url]();
     return {ok:true,text:async()=>JSON.stringify(content)};
   }
 });
 return {fn,requests};
}
(async()=>{
 for(const [q,want] of [[{},["added",1]],[{price:50},["manual",0]],[{bundle:true},["manual",0]],[{owns:true},["owned",0]],[{accepted:false},["manual",0]],[{confirmed:false},["verify",1]],[{account:"other"},["账号已变更，任务已暂停",0]],[{accountAtSubmit:"other"},["账号已变更，任务已暂停",0]],[{single:false},["added",1]],[{single:false,owns:true},["owned",0]]])assert.deepEqual(await scenario(q),want);
 for(const rows of [[],{},[null],[{uid:"other",acquired:false}],[{uid:"x",acquired:"false"}],[{uid:"x",acquired:false},{uid:"x",acquired:true}]]){
   assert.deepEqual(await scenario({single:false,rows}),["unknown",0]);
 }
 for(const status of [401,403,429])
   assert.deepEqual(await scenario({postStatus:status}),["Fab HTTP "+status,1]);
 assert.deepEqual(await scenario({postStatus:503}),["verify",1]);
 assert.deepEqual(await scenario({switchOn:"/prices-infos"}),["账号已变更，任务已暂停",0]);
 assert.deepEqual(await scenario({switchOn:"/add-to-library"}),["账号已变更，任务已暂停",1]);
 const hint={fast:true,rows:[{uid:"x",acquired:false}]};
 for(const [change,want] of [[{},["added",1]],[{owns:true},["owned",0]],[{price:50},["manual",0]],
   [{accountAtSubmit:"new"},["账号已变更，任务已暂停",0]],[{readError:503},["readSeparately",0]],
   [{readError:429},["Fab HTTP 429",0]],[{confirmed:false},["verify",1]]])
   assert.deepEqual(await scenario({...hint,...change}),want);
 for(const [ownership,want] of [[[],"available"],[null,"unknown"],[["license"],"owned"]]){
   const {fn,requests}=await manualAccess({ownership});
   const result=await fn({op:"listingAccess",uid:"x"});
   assert.equal(result.status,want);
   assert.equal(requests.includes("/i/listings/x/asset-formats"),want==="owned");
   if(want==="owned")assert.equal(result.files[0].bytes,1024);
 }
 const changed=await manualAccess({ownership:["license"],accounts:["a","b"]});
 await assert.rejects(changed.fn({op:"listingAccess",uid:"x"}),/账号已变更/);
 const bind=await manualAccess({});
 const formats=await bind.fn({op:"formats",uid:"x",bindAccount:true});
 assert.equal(formats.account,"a");
 assert.equal(formats.files[0].uid,"f");
 const changedDuringSigning=await manualAccess({accounts:["a","b"]});
 await assert.rejects(changedDuringSigning.fn({op:"link",uid:"x",format:"fbx",file:"f",account:"a"}),/账号已变更/);
 const signed=await manualAccess({accounts:["b"]});
 await assert.rejects(signed.fn({op:"link",uid:"x",format:"fbx",file:"f",account:"a"}),/账号已变更/);
 assert(!signed.requests.some(url=>url.endsWith("/download-info")));
 const limited=await manualAccess({ownership:["license"],fail:"/i/listings/x/asset-formats"});
 await assert.rejects(limited.fn({op:"listingAccess",uid:"x"}),/Fab HTTP 429/);
 console.log("PASS: free-only submission and ownership verification using simulated Fab responses");
})().catch(e=>{console.error(e);process.exitCode=1});


