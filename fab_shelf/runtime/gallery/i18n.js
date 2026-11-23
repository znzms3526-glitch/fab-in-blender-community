"use strict";
let uiLang=localStorage.getItem("fab-language")||"zh-CN";
const phrases={
"探索 Fab":"Explore Fab","我的 Fab 库":"My Fab Library","粘贴 Fab 链接":"Paste Fab link","Fab 商品链接":"Fab product link","定位资产":"Find asset",
"首选贴图尺寸":"Preferred texture size","首选 ":"Preferred ","实际贴图 ":"Actual textures: ","已缓存贴图 ":"Cached textures: ","已读取 ":"Read: ",
"Fab 所选格式未提供 ":"Selected Fab format does not contain ","Fab 未列出 ":"No Fab listing for ","Fab 标称 ":"Fab labelled ","K 的包实测尺寸不符":"K package has different texture dimensions",
"批量核对已过期，请重新核对":"The batch check expired. Check again.",
"Fab 登录页面已切换，请重试":"The Fab sign-in page changed. Try again.",
"旧任务未绑定 Fab 账号，请从当前资产库重新选择文件":"Select the file again in the current library to attach this older task to an account.",
"另一个 Blender 窗口正在下载此文件，完成后再继续":"Another Blender window is downloading this file. Resume when it finishes.","本地包未包含 ":"Local package does not contain ","K 贴图":"K textures",
"已核对包内贴图，继续下载其他规格":"Checked textures. Downloading another file.","缓存贴图与首选尺寸不符或待确认":"Cached textures differ or are unconfirmed",
"Fab 文件没有可导入的模型或贴图":"This Fab file has no importable model or textures.","请选择有效的贴图尺寸":"Choose a valid texture size.","贴图尺寸待确认":"Texture size unconfirmed","Fab 未提供所选格式，请选择可用文件。":"Fab does not offer that format. Choose an available file.",
"下载到插件":"Download to plugin","在浏览器打开 ↗":"Open in browser ↗","在 Fab 查看":"View on Fab","刷新 Fab 页面":"Refresh Fab page","返回上一页":"Go back","登录 Epic":"Sign in to Epic",
"正在打开 Fab…":"Opening Fab…","Fab 页面加载失败，可刷新或在浏览器打开":"Fab could not load. Refresh or open it in your browser.",
"请粘贴有效的 Fab 商品链接":"Paste a valid Fab product link.","无法解析该 Fab 短链接":"Could not resolve this Fab short link.",
"请在系统浏览器继续此步骤":"Continue this step in your system browser.","请用「下载到插件」选择文件，或在系统浏览器下载":"Choose files with Download to plugin or use your browser.",
"正在核对 Fab 所有权…":"Checking ownership on Fab…","先在 Fab 页面添加到我的库，再重新核对":"Add the product to your library on Fab, then check again.",
"Fab 尚未确认入库，稍后重新核对":"Fab has not confirmed ownership. Check again shortly.","已核对 Fab 所有权":"Ownership confirmed on Fab","此商品无可用的 Blender 格式":"No supported Blender format is available for this product.",
"重新核对":"Check again","查看已下载":"View downloaded files","登录后返回此商品并重新核对":"Return to this product and check again after signing in.",
"账号或核对结果已变更，请重新核对":"The account or verification changed. Check again.","账号已变更，请重新核对":"The account changed. Check again.",
"账号已变更，请切回原账号后继续":"The account changed. Switch back to continue.","此规格已由另一个账号加入队列":"Another account queued this file.",
"查看其他 Fab 规格":"Show other Fab files","导入模式":"Import mode","添加本地文件":"Add local file",
"加入 Blender 资产库":"Add to Blender Asset Library","正在生成 Blender 资产…":"Preparing Blender asset…","已加入 Blender 资产库":"Added to Blender Asset Library",
"已在 Blender 资产库":"Already in Blender Asset Library","已在 Blender 中定位":"Located in Blender",
"资产库首版仅支持静态模型":"This library supports static models.","Blender 资产库支持静态模型和 PBR 材质":"The library supports static models and PBR materials.",
"无法注册 Blender 资产库":"Could not register the Blender asset library.","Blender 资产生成失败，请重试":"Could not create the Blender asset. Try again.",
"缺少可打包的贴图 · ":"Missing texture: ","无法打包贴图 · ":"Could not pack texture: ",
"等待导入":"Awaiting import","工程尚未保存。请回到 Blender 按 Ctrl+Shift+S 保存 .blend，随后自动继续导入。":"Save this project in Blender with Ctrl+Shift+S. Pending imports will continue automatically.","等待保存 .blend，保存后自动导入":"Waiting for the .blend file to be saved. Import will continue automatically.",
"多选":"Select assets","结束多选":"End selection","批量下载":"Batch download","加入下载队列":"Add to downloads","选择资产":"Select asset","移到下一项":"Download next","创建独立副本":"Create independent copy","大小未知":"Size unknown","正在核对规格…":"Checking available files…","需下载":"To download","无此规格":"Unavailable","未能核对":"Could not verify","规格":"Resolution",
"已缓存":"Cached","账号":"Account","退出登录":"Log out","确认退出":"Confirm logout","已退出登录":"Logged out","退出登录失败，请重试":"Logout failed. Try again.","退出当前线路的 Fab 会话？入库和下载将暂停，本地资产与收藏会保留。":"Log out of Fab on the current connection? Claims and downloads will pause. Local assets and favorites will be kept.",
"界面":"Interface","界面语言":"Interface language","设置":"Settings","网络":"Network","存储":"Storage","诊断":"Diagnostics","入库":"Acquisition",
"我的库":"My library","已下载":"Downloaded","收藏":"Favorites","下载队列":"Downloads","资产分类":"Categories","全部资产":"All assets","模型":"Models","材质":"Materials","贴花":"Decals","贴图":"Textures","模型包":"Model pack",
"在浏览器打开 Fab ↗":"Open Fab in browser ↗","在 Fab 查看 ↗":"View on Fab ↗","常用关键词":"Quick searches","家具":"Furniture","建筑":"Architecture","植物":"Plants","岩石":"Rocks","木材":"Wood","食物":"Food",
"展开":"Expand","收起":"Collapse","封面缓存":"Preview cache","账号登录":"Sign in","刷新资产库":"Refresh library","搜索资产名称…":"Search assets…","搜索资产名称":"Search asset names","调整侧栏宽度":"Resize sidebar",
"一键免费入库":"Claim free assets","免费资产入库":"Claim free assets","免费入库中…":"Claiming…","添加已有下载包":"Add local package","缩略图":"Preview size","上一页":"Previous page","下一页":"Next page","跳转页码":"Go to page","第":"Page","页":"","无":"None",
"范围":"Scope","许可":"License","全部免费资产":"All free assets","个人":"Personal","专业":"Professional","需在官网核对的资产（最近 12 项）":"Needs attention on Fab (latest 12)","开始入库":"Start claiming","暂停入库":"Pause","继续入库":"Resume","重新扫描":"Scan again","仅重试异常项":"Retry exceptions only","关闭":"Close",
"仅处理所选许可下可直接领取的零元报价。限免结账、合集和需确认条款的条目留待官网处理。":"Only zero-price offers available for direct acquisition under the selected license can be claimed. Complete promotional checkouts, bundles and license agreements on Fab.",
"关闭设置":"Close settings","浏览与入库":"Browsing and acquisition","文件下载与封面":"Downloads and previews","HTTP 代理":"HTTP proxy","测试可用线路":"Test connections","采用推荐":"Use recommendation","默认范围":"Default scope","默认许可":"Default license","关闭资产库窗口后继续入库":"Continue claiming when the library window closes",
"更换接口线路后可能需要重新登录 Epic。活动下载会沿原线路完成。":"Changing the API connection may require signing in again. Active downloads finish on their existing connection.",
"共享下载目录":"Shared download folder","浏览封面缓存上限":"Preview cache limit","清理浏览封面":"Clear browsing previews","复制脱敏诊断":"Copy diagnostics","本页默认值":"Reset this tab","保存设置":"Save settings","目录变更在下次启动资产库会话时生效，已有文件保留在原目录。":"The new folder takes effect next session. Existing files stay in the original folder.",
"跟随系统":"System","指定代理":"Custom proxy","直连":"Direct","未检测到":"None detected","重试":"Retry","正在读取子分类…":"Loading categories…","正在读取…":"Loading…","正在测量…":"Testing…","暂无预览":"Preview unavailable","没有匹配的资产":"No matching assets","按加入顺序下载":"Downloads run in queue order","队列为空":"No downloads queued",
"取消收藏":"Remove favorite","规格与导入选项":"Formats and import options","原始文件":"Original file","文件未标明贴图分辨率":"The file has no resolution label","文件格式":"File format","导入":"Import","下载并导入":"Download and import","下载":"Download","按所选文件导入":"Import selected files","正在读取 Fab 可用规格…":"Loading available files…","Fab 可用文件":"Files available on Fab","该条目没有可用的 Blender 文件":"No supported Blender files available",
"暂停":"Pause","继续":"Resume","取消":"Cancel","完成":"Completed","已取消":"Canceled","失败":"Failed","等待":"Queued","下载中":"Downloading","正在读取账号与进度…":"Loading account and progress…","本轮扫描完成。":"Scan complete.","连续 24 项无法核实，扫描已暂停。请在 Fab 官网检查登录和资产状态后继续。":"Paused after 24 unresolved items. Check your sign-in and assets on Fab before resuming.","所有权未确认的资产可在官网核对。点击“仅重试异常项”可重新检查，已提交的条目只核验结果。":"Check unresolved assets on Fab. Retry exceptions to check again. Possible previous submissions are only verified.","正在后台处理，关闭面板后继续。":"Processing in the background. You may close this panel.",
"代理地址已变，请重新测速":"Proxy changed. Run the test again.","已选推荐线路，点击保存生效":"Recommendation selected. Save to apply.","设置已保存":"Settings saved","点击保存应用默认值":"Save to apply the defaults","已复制":"Copied","可选中诊断文字后复制":"Select the diagnostic text and copy it.","无有效样本":"No valid sample","暂无":"None","已连接":"Connected","尚未核验":"Not verified","运行中":"Running","空闲":"Idle","需重新验证":"Sign in again","切换后待核验":"Connection changed; verification pending",
"已清理浏览封面，已下载资产及封面保留":"Browsing previews cleared. Downloaded assets and their covers are retained.","已发送到 Blender":"Sent to Blender","已加入下载队列":"Queued for download","队列已更新":"Queue updated","该规格已缓存":"Already cached","该规格已在队列":"Already queued","登录完成后自动加载我的 Fab 库":"My Fab Library loads automatically after sign-in","Fab 账号正在切换":"Switching Fab accounts",
"正在加载":"Loading ","匹配 ":"Match ","K 或对应 Fab 质量档":"K or the corresponding Fab quality tier","系统显式 HTTPS 代理：":"Detected HTTPS proxy: ","当前接口：":"API: ","　文件：":"   Files: ","账号连接：":"Account: ","接口：":"API: ","文件：":"Files: ","最近异常：":"Last issue: ","入库状态：":"Acquisition: ",
"浏览封面 ":"Browsing previews: ","　已下载资产封面 ":"   Downloaded asset covers: ","已下载资产封面 ":"Downloaded asset covers: ","　资产包 ":"   Packages: "," · 接口 2 次，下载每条线路最多 256 KiB":" · Two API probes; up to 256 KiB per download route","　接口 ":"   API ","　下载 ":"   Download "," KiB/s · 首字节 ":" KiB/s · First byte ",
"当前账号 · ":"Account · ","新增 ":"Added: ","　已拥有 ":"   Already owned: ","　已核验拥有 ":"   Verified: ","　待核验 ":"   To verify: ","　待重试 ":"   To retry: ","　待官网处理 ":"   On Fab: ","　待扫描处理 ":"   Pending: ",
"无显式 HTTPS 代理":"No explicit HTTPS proxy","直连（应用层）":"Direct (application layer)","Fab 未提供该档位与格式组合。点齿轮查看此资产的实际文件。":"This format and resolution are unavailable. Open file options to see available files.",
"请先登录 Fab":"Sign in to Fab first","请先登录":"Sign in first","Fab 响应格式异常，请稍后重试":"Invalid Fab response. Try again later.","Fab 响应超时，请刷新或登录":"Fab timed out. Refresh or sign in.","Fab 窗口已打开":"Fab library opened","请先保存 .blend；保存后将继续导入":"Save the .blend file to continue importing.",
"已导入活动集合 · ":"Imported into active collection · ","材质已导入 · ":"Material imported · ","正在收集所选文件到项目目录":"Collecting selected files into the project folder","选择已改变。请在 Blender 的 Fab 面板确认导入目标。":"Selection changed. Confirm the import target in Blender's Fab panel.",
"Fab 暂时未返回有效结果，正在重试":"Fab returned an invalid response. Retrying.","账号已变更，任务已暂停":"Account changed. Task paused.","Fab 登录验证已过期":"Fab session expired","请点击「登录 Fab」，完成后刷新个人库":"Sign in to Fab, then refresh your library."
};
const fragments=Object.keys(phrases).filter(k=>k.length>1).sort((a,b)=>b.length-a.length);
const translationPattern=new RegExp(fragments.map(k=>k.replace(/[.*+?^${}()|[\]\\]/g,"\\$&")).join("|"),"g");
function tr(value){
 if(uiLang!=="en"||typeof value!=="string")return value;
 return Object.hasOwn(phrases,value)?phrases[value]:value.replace(translationPattern,k=>phrases[k]);
}
function initLanguage(){
 document.documentElement.lang=uiLang;
 if(uiLang!=="en")return;
 const w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
 while(w.nextNode()){const n=w.currentNode,v=n.nodeValue.trim();if(Object.hasOwn(phrases,v))n.nodeValue=n.nodeValue.replace(v,tr(v))}
 for(const e of document.querySelectorAll("[aria-label],[title],[placeholder]"))
   for(const k of ["aria-label","title","placeholder"])if(e.hasAttribute(k))e.setAttribute(k,tr(e.getAttribute(k)));
}
function applyLanguage(lang){
 if(!lang||lang===uiLang)return;
 localStorage.setItem("fab-language",lang);
 if(typeof saveView==="function")saveView();
 location.reload();
}
initLanguage();


