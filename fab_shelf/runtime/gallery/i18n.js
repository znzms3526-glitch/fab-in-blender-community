"use strict";
let uiLang=localStorage.getItem("fab-language")||"zh-CN";
const phrases={
"打开下载目录":"Open downloads folder",
"探索 Fab":"Explore Fab","我的 Fab 库":"My Fab Library","粘贴 Fab 链接":"Paste Fab link","Fab 商品链接":"Fab product link","定位资产":"Find asset",
"导入 Bridge 资产":"Import Bridge assets","管理 Bridge 素材库":"Manage Bridge library","Bridge 资产":"Bridge assets",
"素材来源":"Asset source","全部":"All","Fab 下载":"Fab downloads","本地文件":"Local files",
"选择 Quixel Bridge 已下载的素材库文件夹。可在 Bridge 设置中查看素材库位置。登记时使用原位置的文件。":"Select the Quixel Bridge library folder. Find it in Bridge settings. The library uses your files in place.",
"导入后可在这里查找 Bridge 素材。":"Find imported Bridge assets here.",
"选择素材库":"Choose library folder","请选择 Bridge 素材库文件夹":"Choose the Quixel Bridge library folder",
"重新扫描":"Rescan","重新定位":"Locate folder","移除登记":"Unregister","移除插件中的 Bridge 素材库登记？":"Unregister the Bridge library from the plugin?",
"确认移除登记":"Unregister library","查看 Bridge 资产":"View Bridge assets","取消扫描":"Cancel scan","正在取消扫描…":"Cancelling scan…",
"已取消扫描，保留上次登记":"Scan cancelled. The last index is available.","Bridge 扫描进度":"Bridge scan progress",
"正在扫描 Bridge 素材库":"Scanning Bridge library","正在查找 Bridge 素材…":"Finding Bridge assets…",
"已检查":"Checked","已识别":"Recognized","已登记 Bridge 资产":"Bridge assets registered",
"缺少文件":"Missing files","格式暂不支持":"Unsupported format","无法读取":"Unreadable",
"Bridge 素材库未连接":"Bridge library is disconnected","未找到可导入的 Bridge 资产":"No importable Bridge assets found. Choose the Bridge library folder.",
"此目录与原 Bridge 素材库不匹配":"This folder does not match the registered Bridge library.",
"另一个 Blender 窗口正在更新 Bridge 素材库":"Another Blender window is updating the Bridge library.",
"Bridge 素材库已在另一个窗口更新，请重试":"Another Blender window updated the Bridge library. Try again.",
"此 Bridge 规格已变化，请重新打开详情":"This Bridge variant changed. Reopen the asset details.",
"Bridge 源文件已变化，请重新扫描":"Bridge source files changed. Rescan the library.",
"模型格式与 LOD":"Model format and LOD","贴图分辨率":"Texture resolution","查看规格":"View variants",
"导入 Blender":"Import into Blender","正在准备 Bridge 文件…":"Preparing Bridge files…",
"导入的贴图尺寸与所选 Bridge 规格不符":"The imported textures do not match the selected Bridge size.",
"导入的贴图尺寸与所选规格不符":"The imported textures do not match the selected size.",
"无法读取 Bridge 素材库索引":"Could not read the Bridge library index.",
"已校验的缓存文件已变化，请重试":"The verified cached file changed. Try again.",
"包内贴图 ":"Textures in package: ",
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
"共享下载目录":"Shared download folder","浏览封面缓存上限":"Preview cache limit","清理浏览封面":"Clear browsing previews","复制脱敏诊断":"Copy diagnostics","本页默认值":"Reset this tab","保存设置":"Save settings",
"Fab 下载库":"Fab download library","待切换目录 · ":"Pending folder · ","现有任务完成后自动切换下载库":"The download library will switch after current tasks finish","目录切换后，已有文件和暂停任务保留在原库。":"Existing files and paused tasks remain in their original library.",
"正在切换下载库，请待当前任务完成后重试":"The download library is switching. Try again after current tasks finish.","正在切换 Fab 下载库…":"Switching the Fab download library…","Fab 下载库已切换 · ":"Fab download library switched · ","无法切换 Fab 下载库 · ":"Couldn't switch the Fab download library · ","无法使用 Fab 下载库 · ":"Couldn't use the Fab download library · ","请选择绝对路径作为共享下载库":"Enter an absolute path for the shared library.","无法保存 Blender 首选项":"Couldn't save Blender preferences","新 Fab 窗口启动失败":"The new Fab window did not start","等待新 Fab 窗口启动超时":"The new Fab window took too long to start","原窗口重开失败":"Couldn't reopen the original Fab window",
"跟随系统":"System","指定代理":"Custom proxy","直连":"Direct","未检测到":"None detected","重试":"Retry","正在读取子分类…":"Loading categories…","正在读取…":"Loading…","正在测量…":"Testing…","暂无预览":"Preview unavailable","没有匹配的资产":"No matching assets","按加入顺序下载":"Downloads run in queue order","队列为空":"No downloads queued",
"回到顶部":"Back to top","正在加载更多…":"Loading more…","正在更新缓存预览…":"Refreshing cached previews…","已显示全部 {count} 项":"All {count} assets shown","资产列表游标无效":"Invalid asset list cursor",
"⚙ 导入设置":"⚙ Import","核心 PBR 贴图":"Core PBR Maps","扩展贴图通道":"Additional Maps","Blender 材质节点":"Blender Material Nodes","默认档位":"Default Preset","材质与贴花尺寸":"Material and decal size","模型预设":"Model preset","低":"Low","中":"Med","高":"High","获取并导入":"Get and import","仅下载":"Download only","此贴图通道未在导入设置中启用":"This channel is disabled in Import settings","在导入设置中启用":"Enable this channel in Import settings",
"真置换按 BOTH 写入材质，Cycles 与 Blender 5.3 EEVEE 均可产生几何位移，请按需添加细分。":"Displacement uses Blender's Displacement and Bump mode. Cycles and Blender 5.3 EEVEE can displace the mesh. Subdivide it as needed.",
"贴图勾选决定本地解包与自动连线，不改变 Fab 整包 ZIP 下载体积。":"Texture choices control extraction and material links. Fab downloads the full ZIP.",
"自动":"Auto","可用文件":"Available files","首选模型精度":"Preferred mesh detail","默认模型精度":"Default mesh detail","低模":"Low","中模":"Med","高模":"High","原版":"Original",
"本次导入设置":"Import settings for this asset","恢复默认":"Restore defaults","导入贴图通道":"Texture channels","模型贴图通道":"Model texture channels","材质与贴花通道":"Material and decal channels","自动添加纹理坐标":"Add texture coordinates",
"底色":"Base color","法线":"Normal","粗糙度":"Roughness","金属度":"Metallic","环境光遮蔽":"Ambient occlusion","高光":"Specular","透明度":"Opacity","凹凸 / 置换":"Bump / displacement","高度贴图模式":"Height map mode","仅凹凸":"Bump only","真置换":"Displacement",
"贴图勾选控制本地解包与自动连线，Fab 仍下载整包。":"Texture choices control extraction and material links. Fab downloads the full package.",
"请选择有效的资产规格":"Choose a valid asset specification","请选择有效的模型精度":"Choose a valid mesh detail level","请确认可用的资产规格":"Confirm an available specification","需确认组合":"Needs confirmation","跳过":"Skip","文件标称":"Filename label","导入失败":"Import failed",
"Fab 转码":"Fab conversion",
"当前材质使用 GLB 内嵌贴图，如需更换规格请在详情中选择对应规格文件包重新导入":"This GLB uses embedded textures. To change resolution, choose the desired package in details and import it again.",
"取消收藏":"Remove favorite","规格与导入选项":"Formats and import options","原始文件":"Original file","文件未标明贴图分辨率":"The file has no resolution label","文件格式":"File format","导入":"Import","下载并导入":"Download and import","下载":"Download","按所选文件导入":"Import selected files","正在读取 Fab 可用规格…":"Loading available files…","Fab 可用文件":"Files available on Fab","该条目没有可用的 Blender 文件":"No supported Blender files available",
"暂停":"Pause","继续":"Resume","取消":"Cancel","完成":"Completed","已取消":"Canceled","失败":"Failed","等待":"Queued","下载中":"Downloading","正在读取账号与进度…":"Loading account and progress…","本轮扫描完成。":"Scan complete.","连续 24 项无法核实，扫描已暂停。请在 Fab 官网检查登录和资产状态后继续。":"Paused after 24 unresolved items. Check your sign-in and assets on Fab before resuming.","所有权未确认的资产可在官网核对。点击“仅重试异常项”可重新检查，已提交的条目只核验结果。":"Check unresolved assets on Fab. Retry exceptions to check again. Possible previous submissions are only verified.","正在后台处理，关闭面板后继续。":"Processing in the background. You may close this panel.",
"代理地址已变，请重新测速":"Proxy changed. Run the test again.","已选推荐线路，点击保存生效":"Recommendation selected. Save to apply.","设置已保存":"Settings saved","点击保存应用默认值":"Save to apply the defaults","已复制":"Copied","可选中诊断文字后复制":"Select the diagnostic text and copy it.","无有效样本":"No valid sample","暂无":"None","已连接":"Connected","尚未核验":"Not verified","运行中":"Running","空闲":"Idle","需重新验证":"Sign in again","切换后待核验":"Connection changed; verification pending",
"已清理浏览封面，已下载资产及封面保留":"Browsing previews cleared. Downloaded assets and their covers are retained.","已发送到 Blender":"Sent to Blender","已加入下载队列":"Queued for download","队列已更新":"Queue updated","该规格已缓存":"Already cached","该规格已在队列":"Already queued","登录完成后自动加载我的 Fab 库":"My Fab Library loads automatically after sign-in","Fab 账号正在切换":"Switching Fab accounts",
"该文件已有任务，请打开下载队列查看":"This file is already queued. Open the download queue to see its status.","Fab 账号已变更，请从当前账号重新提交":"Fab account changed. Submit again from the current account.","Fab 账号或任务状态已变更，请重新提交":"Fab account or job status changed. Submit again.","任务与当前 Fab 账号不一致，请从当前资产库重新选择文件":"This job belongs to a different Fab account. Choose the file again from the current library.","请核对工程后再继续上次导入":"Check the Blender project before resuming the previous import.",
"正在加载":"Loading ","匹配 ":"Match ","K 或对应 Fab 质量档":"K or the corresponding Fab quality tier","系统显式 HTTPS 代理：":"Detected HTTPS proxy: ","当前接口：":"API: ","　文件：":"   Files: ","账号连接：":"Account: ","接口：":"API: ","文件：":"Files: ","最近异常：":"Last issue: ","入库状态：":"Acquisition: ",
"浏览封面 ":"Browsing previews: ","　已下载资产封面 ":"   Downloaded asset covers: ","已下载资产封面 ":"Downloaded asset covers: ","　资产包 ":"   Packages: "," · 接口 2 次，下载每条线路最多 256 KiB":" · Two API probes; up to 256 KiB per download route","　接口 ":"   API ","　下载 ":"   Download "," KiB/s · 首字节 ":" KiB/s · First byte ",
"当前账号 · ":"Account · ","新增 ":"Added: ","　已拥有 ":"   Already owned: ","　已核验拥有 ":"   Verified: ","　待核验 ":"   To verify: ","　待重试 ":"   To retry: ","　需到 Fab 手动处理 ":"   Complete on Fab: ","　已发现，待核对 ":"   Found, pending check: ",
"需处理合集":"Complete this bundle on Fab","需确认适用条款":"Review the applicable terms","当前许可无可直接领取的零元报价":"No direct free offer for this license","打开 Fab 查看具体要求":"Open Fab to see what is required",
"无显式 HTTPS 代理":"No explicit HTTPS proxy","直连（应用层）":"Direct (application layer)","Fab 未提供该档位与格式组合。点齿轮查看此资产的实际文件。":"This format and resolution are unavailable. Open file options to see available files.",
"自动选路":"Automatic routing","自动选路要求有效的 HTTPS 地址":"Automatic routing requires a valid HTTPS address.","自动选路比较连接耗时，可能使用系统、指定代理或直连。开始传输后保持当前线路。":"Automatic routing compares connection times and may use the system route, a custom proxy, or a direct connection. Transfers stay on their current route once started.",
"HTTP 或 SOCKS5 代理":"HTTP or SOCKS5 proxy","网络设置":"Network settings","当前浏览：":"Browsing: ","浏览：":"Browsing: ","系统文件线路":"System file route","正在检测系统文件线路…":"Checking the system file route…","直连或 TUN":"Direct or TUN","系统绕过":"System bypass","PAC 绕过":"PAC bypass","PAC 失败，使用系统设置":"PAC failed, using system proxy","PAC 失败，使用系统绕过规则":"PAC failed, using system bypass rules","系统 PAC 解析失败，请检查网络设置":"System PAC lookup failed. Check Network settings.","系统代理地址不受支持，请检查网络设置":"Unsupported system proxy address. Check Network settings.","请输入 HTTP 或 SOCKS5 代理的主机和端口，例如 127.0.0.1:7890":"Enter the HTTP or SOCKS5 proxy host and port, for example 127.0.0.1:7890.","接口 2 次，下载每条线路最多 256 KiB":"Two API requests and up to 256 KiB per route","　接口 ":"   API ","　下载 ":"   Download ","首字节 ":"First byte ",
"请先登录 Fab":"Sign in to Fab first","请先登录":"Sign in first","Fab 响应格式异常，请稍后重试":"Invalid Fab response. Try again later.","Fab 响应超时，请刷新或登录":"Fab timed out. Refresh or sign in.","Fab 窗口已打开":"Fab library opened","请先保存 .blend；保存后将继续导入":"Save the .blend file to continue importing.",
"已导入活动集合 · ":"Imported into active collection · ","材质已导入 · ":"Material imported · ","正在收集所选文件到项目目录":"Collecting selected files into the project folder","选择已改变。请在 Blender 的 Fab 面板确认导入目标。":"Selection changed. Confirm the import target in Blender's Fab panel.",
"Fab 暂时未返回有效结果，正在重试":"Fab returned an invalid response. Retrying.","账号已变更，任务已暂停":"Account changed. Task paused.","Fab 登录验证已过期":"Fab session expired","请点击「登录 Fab」，完成后刷新个人库":"Sign in to Fab, then refresh your library.",
"清空已结束":"Clear finished","移除":"Remove","已移除任务记录":"Task removed","任务已不存在":"Task no longer exists","没有可清理的记录":"No finished tasks to clear","仅可移除已完成、已取消或失败的任务":"Only completed, canceled, or failed tasks can be removed.",
"已清理 ":"Cleared "," 条记录":" items",
"删除本地文件":"Delete local file","确认删除":"Confirm delete","返回":"Back","已删除本地缓存文件":"Local cached file deleted",
"确认删除此本地缓存？仅删除本机文件，不影响 Epic 账号云端资产。":"Delete this local cached file? Only local files are removed; your Epic account library is unaffected.",
"该资产正在下载中，请稍后再删除":"This asset is currently downloading. Try again later.","本地缓存文件不存在或已删除":"Local cached file no longer exists.","无效的本地资产标识":"Invalid local asset identifier.",
"包内含 ":"Contains "," 个模型文件，已自动优先选择主模型；如需其他部件请在下方勾选切换。":" model files. The main model is pre-selected; check another below to switch parts.",
"查看跳过明细":"View skipped details","格式暂不支持（如 UE UAsset）":"Unsupported format (e.g. UE UAsset)","缺少可用模型或贴图":"Missing importable models or textures",
"已有 Quixel Bridge 本地素材库？支持原位置免拷贝登记，直接在 Blender 浏览与导入。":"Have a local Quixel Bridge library? Register it in place without copying files, and browse or import directly in Blender.",
"不再提示":"Dismiss",
"Fab 返回重复页码，已保存进度，请稍后继续":"Fab returned a repeated page. Your progress is saved. Resume later.",
"此规格未返回下载地址":"Fab did not return a download URL for this variant.",
"Fab 下载地址无效":"The Fab download URL is invalid.",
"该规格已有导入任务，请在完成后重试":"An import for this variant is already queued. Try again after it finishes.",
"无法打包超长路径的贴图":"Could not pack the texture from the long file path.",
"文件正在由另一个 Blender 窗口使用，请稍后重试":"Another Blender window is using this file. Try again.",
"另一个 Blender 窗口正在使用该任务，请稍后重试":"Another Blender window is using this task. Try again.",
"任务文件正在使用，或没有删除权限，请稍后重试":"The task files are in use or cannot be removed. Try again.",
"无法读取本地缓存索引，请稍后重试":"Could not read the local cache records. Try again.",
"本地缓存文件正在使用，或没有删除权限，请稍后重试":"The cached file is in use or cannot be removed. Try again.",
"本地缓存文件已删除，请重新下载":"The cached file was deleted. Download it again.",
" · 有 ":" · "," 条任务暂无法移除，请稍后重试":" tasks could not be removed. Try again."
};
const exactLabels=new Set(["返回","移除","Fab 下载","本地文件"]);
const fragments=Object.keys(phrases).filter(k=>k.length>1&&!exactLabels.has(k)).sort((a,b)=>b.length-a.length);
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


