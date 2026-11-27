# Fab in Blender

Community edition · Windows x64 · [GPL-3.0-or-later](LICENSE) · [许可适用范围与第三方组件](COPYING.md)

在 Blender 的副屏窗口浏览 Fab、手动领取免费资产，或粘贴商品链接直达。下载的模型和材质可以导入工程，也可以加入 Blender 原生资产库供其他工程拖用。领取和插件内下载需要 Epic 账号。Windows 版无需安装 Epic Games Launcher 或 Quixel Bridge。

Browse Fab from a separate Blender companion window, claim free assets on Fab, or paste a product link. Downloaded files can be imported at the 3D cursor or added to Blender's Asset Browser for reuse across projects. This is a community-developed add-on for Windows x64.

**[下载 Windows 插件 / Download for Windows](https://github.com/znzms3526-glitch/fab-in-blender-community/releases/latest)** · [使用说明](使用说明.md) · [报告问题 / Report an issue](https://github.com/znzms3526-glitch/fab-in-blender-community/issues)

## 0.15.0 模型精度和贴图

- 3D 模型主卡提供低、中、高三档，默认高。材质和贴花沿用 1K、2K、4K、8K。主按钮会直接导入，按需先下载。详情可手动指定实际文件、LOD 和贴图尺寸，也可以仅下载。
- 在工具栏打开「导入设置」，可选 AO、高光、凹凸或置换。默认使用原有基础贴图，并开启纹理坐标与 Mapping。Fab ZIP 按整包传输，写入工程的贴图按设置和原材质依赖选择。
- Fab 工程内切换贴图规格时，只在同源、同材质集、同 LOD 的目标贴图齐全时整套替换。GLB 和 Bridge 资产可在副屏详情选择其他规格重新导入。缺图修复按每张图的实际来源和校验值恢复。

## 0.14.0 浏览性能

- 「我的 Fab 库」「已下载」和「收藏」向下滚动时自动追加下一批资产。多选可跨批次保留。
- Fab 确认当前账号后，插件可以先显示该账号最近浏览过的库，再同步最新条目。已缓存文件的贴图尺寸也会在重启后复用。
- 封面按滚动位置预取并复用连接。Windows 的下载线路可以按地址解析系统 PAC，手动代理支持 HTTP 和 SOCKS5。在「设置 → 网络」可以测试线路。

### 0.14.1 连续浏览与自动选路

- 在线续批把账号校验和列表请求交给同一次浏览宿主处理。浏览接近已准备好的批次时，插件会提前取下一批。后台查看账号和库更新时会让出繁忙的通道。
- 「文件下载与封面」可选「自动选路」。它按目标站点复查近期 HTTPS 连接表现。开启后可能采用系统线路、已填写的代理或直连。浏览与入库使用「浏览与入库」所选线路。

### 0.14.2 分类按空闲调度

- 滚动续批、切换搜索和登录请求优先。分类在空闲时逐组检查，遇到新的浏览请求时 Windows 副窗会中止当前分类检查，等待下一次空闲后继续。

### 0.14.3 列表刷新

- 移除「我的 Fab 库有新资产」浮条。浏览中的列表保留在原位，需要重新开始加载时可从列表底部刷新。

### 0.14.4 首页刷新与状态提示

- 回到顶部时，使用新首批卡片和游标继续浏览，已有多选会保留。首次建立默认首页基线时，已有筛选视图可继续浏览。
- 切号时显示加载状态。封面缓存面板显示当前设置的上限。

## 第一次导入

1. 在发布页的 **Assets** 下下载 Windows x64 安装包。保留 ZIP 压缩格式。
2. 打开 Blender 首选项，在插件菜单选择 **从磁盘安装 / Install from Disk**，选中 ZIP 并启用 **Fab in Blender**。更新已有版本后重启 Blender。
3. 先保存当前 `.blend`。在 3D 视图按 **N → Fab → 打开我的 Fab 库**，从资产库的「账号」菜单登录。首次打开时，缺少 WebView2 的 Windows 电脑需要联网安装这个组件。
4. 在「我的 Fab 库」选一件资产，设置所需的贴图尺寸和可用的模型精度后点「下载」。已有对应文件时，直接点卡片上的「导入」。模型会出现在活动集合的 3D 游标位置。

**Quick start in English.** Download the Windows x64 ZIP under Releases → Assets, install it from disk in Blender Preferences, and enable the add-on. Save your scene and open the library from the 3D Viewport sidebar under **Fab**. Sign in, choose your preferred texture size and model detail, download, and click **Import**. Change the gallery language under **设置 → 界面 → 界面语言 → English**.

## 搭场景时可以怎样用

| 你想做什么 | 操作 |
|---|---|
| 找素材，暂时不占下载空间 | 搜索或展开分类，点星标存到本地收藏。鼠标移到卡片上按空格可看大图和文件规格。 |
| 逛 Fab 或打开别人发来的商品链接 | 在「探索 Fab」用官网筛选和查看详情，也可以点「粘贴 Fab 链接」。免费商品在官网页面手动领取，完成后点「下载到插件」。 |
| 复用电脑里已有的 Quixel Bridge 素材库 | 在「已下载」点「导入 Bridge 资产」，选中 Bridge 素材库目录即可免拷贝原位置登记、按 LOD 与分辨率浏览并导入 Blender 或加入资产库。 |
| 在下一个工程复用已下载素材 | 在素材详情点「加入 Blender 资产库」。在 Blender 资产浏览器选择 Fab 库即可拖用。 |
| 一次准备多件资产 | 点「多选」后统一选规格。提交前可以看到几件已缓存、几件需要下载、几件没有所选规格。 |
| 清理下载队列或释放本地磁盘空间 | 在「下载队列」可逐条「移除」或点「清空已结束」；在资产详情「⋯」可二次确认后「删除本地文件」（不影响云端账号资产）。 |
| 把同一个道具摆在多个位置 | 再点「导入」。支持复用的静态单网格模型共享网格和材质，每个物体都能单独移动。齿轮菜单可以创建独立副本。 |
| 渲染前提高贴图分辨率 | 选中物体，在 **N → Fab → Fab 来源** 切换贴图规格。默认只更新当前物体，也可选择共用材质的物体一起更新。 |
| 更改下载和导入的位置 | **N → Fab** 显示两处路径。**Fab 下载库**存原始包。**当前工程素材目录**存导入时解包的模型和贴图，默认在 `.blend` 同级的 `Assets/Fab`，也可以指定目录。 |
| 移动工程或找回贴图 | 将 `.blend` 和当前工程素材目录一起移动。需要接回文件时，在 **N → Fab → 检查并修复 Fab 贴图** 选择**重新定位素材目录**。 |

## 兼容范围

- **Windows x64**。插件声明的最低 Blender 版本为 4.1.1；0.10 系列的工程功能已在 4.1.1、5.2 和 5.3 上做过专项测试。macOS Apple Silicon 移植仍处于实验阶段。[Windows 测试记录](Windows兼容性实测.md)
- 主要处理 **FBX、GLB 和常见 PBR 贴图**。可下载的格式、分辨率和许可由各个 Fab 条目决定。部分 Megascans 资产按质量档位分包，选择 4K 档时应以实际文件为准。
- 贴图升级适用于能明确对应的外部贴图。完整浏览个人库、领取和新下载需要联网。在线功能依赖 Fab 网页接口，网站变更可能需要更新插件。
- 本项目独立开发，与 Epic Games、Fab、Quixel 和 Blender Foundation 均无官方隶属关系。插件安装包只提供工具，资产通过你自己的账号获取并遵循各自许可。

## 遇到问题

提交 Issue 时可附上 **插件版本、Blender 版本、Windows 版本、操作步骤和实际提示**。网络问题请注明是在登录、读取个人库还是下载文件时发生；截图前请遮住邮箱、登录验证码和带签名的下载地址。
