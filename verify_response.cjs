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
 console.log("CONTROL_CHAR_REGRESSION_PASS");
})().catch(e=>{console.error(e.message);process.exitCode=1});


