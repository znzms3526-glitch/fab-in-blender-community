const vm=require("node:vm"),fs=require("node:fs"),path=require("node:path");
const src=fs.readFileSync(path.join(__dirname,"fab_shelf/runtime/owned.js"),"utf8");
const assert=require("node:assert/strict");
const uid="sample";
async function probe(raw){
 const request=vm.runInNewContext(src+";fabRequest",{location:{origin:"https://www.fab.com"},URLSearchParams,fetch:async url=>{
 const body=url.endsWith("users/me")?'{"uid":"account"}':raw;
 return {ok:true,json:async()=>JSON.parse(body),text:async()=>body};
 }});
 return request({op:"ownedBatch",account:"account",uids:[uid]});
}
(async()=>{
 const bad='[{"uid":"sample","x":"1234567\n许可","acquired":true}]';
 const result=await probe(bad);
 assert.equal(result.owned[0],uid);
 const uncertain=await probe('[{"uid":"sample","acquired":"true"},{"uid":"sample","acquired":true}]');
 assert.equal(uncertain.owned.length,0);
 for(const raw of ['[]','[{"uid":"other","acquired":false}]','[{"uid":"sample","acquired":"false"}]',
   '[{"uid":"sample","acquired":false},{"uid":"sample","acquired":true}]']){
   const ambiguous=await probe(raw);
   assert.equal(ambiguous.available.length,0);
   assert.equal(ambiguous.owned.length,0);
 }
 await assert.rejects(probe('{"ownership":['));
 const calls=[];
 async function boundLibrary(before, after, libraryStatus=200){
   calls.length=0;
   const identities=[before,after];
   const request=vm.runInNewContext(src+";fabRequest",{location:{origin:"https://www.fab.com"},URLSearchParams,
    fetch:async (url, options)=>{
      assert.equal(options.cache,"no-store");
      const isAccount=url.endsWith("users/me");
      calls.push(isAccount?"account":"library");
      const uid=isAccount?identities.shift():null;
      const status=isAccount?(uid?200:401):libraryStatus;
      const body=isAccount?{uid}:{results:[{listingUid:"asset",title:"Fixture"}]};
      return {ok:status===200,status,text:async()=>JSON.stringify(body)};
    }});
   return request({op:"library",account:"a",cursor:"bound",verifyAccount:true});
 }
 assert.equal((await boundLibrary("a","a")).items[0].uid,"asset");
 assert.deepEqual(calls,["account","library","account"]);
 const replaced=await boundLibrary("a","b");
 assert.equal(replaced.observedAccount,"b");
 assert.equal(replaced.items,undefined);
 const before=await boundLibrary("b","a");
 assert.equal(before.items,undefined);
 assert.deepEqual(calls,["account"]);
 const signedOut=await boundLibrary("a","",401);
 assert.equal(signedOut.observedAccount,"");
 assert.equal(signedOut.items,undefined);
 const rid="a".repeat(32);
 let started, release;
 const reading=new Promise(resolve=>{started=resolve});
 const context=vm.createContext({location:{origin:"https://www.fab.com"},URLSearchParams,AbortController,setTimeout,clearTimeout,
   fetch:async (url,{signal})=>{
     if(url.endsWith("users/me"))return {ok:true,text:async()=>'{"uid":"a"}'};
     return new Promise((resolve,reject)=>{
       release=()=>reject(new Error("category read canceled"));
       signal.addEventListener("abort",release,{once:true});
       started();
     });
   }});
 const classify=vm.runInContext(src+";fabRequest",context);
 const idle=classify({op:"presence",account:"a",values:["material"],idleMetadata:true,rid});
 await reading;
 context["__fabShelfMetadata_"+rid].abort();
 assert.equal((await idle).deferred,true);
 assert.equal(context["__fabShelfMetadata_"+rid],undefined);
 console.log("CONTROL_CHAR_REGRESSION_PASS");
})().catch(e=>{console.error(e.message);process.exitCode=1});


