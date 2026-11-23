# Fab in Blender

Community edition · Windows x64 · [GPL-3.0-or-later](LICENSE) · [许可适用范围与第三方组件](COPYING.md)

在 Blender 的副屏窗口浏览 Fab、手动领取免费资产，或粘贴商品链接直达。下载的模型和材质可以导入工程，也可以加入 Blender 原生资产库供其他工程拖用。领取和插件内下载需要 Epic 账号。Windows 版无需安装 Epic Games Launcher 或 Quixel Bridge。

Browse Fab from a separate Blender companion window, claim free assets on Fab, or paste a product link. Downloaded files can be imported at the 3D cursor or added to Blender's Asset Browser for reuse across projects. This is a community-developed add-on for Windows x64.

**[下载 Windows 插件 / Download for Windows](https://github.com/znzms3526-glitch/fab-in-blender-community/releases/latest)** · [使用说明](使用说明.md) · [报告问题 / Report an issue](https://github.com/znzms3526-glitch/fab-in-blender-community/issues)

## 第一次导入

1. 在发布页的 **Assets** 下下载 **Fab-in-Blender-Windows.zip**。保留 ZIP 压缩格式。
2. 打开 Blender 首选项，在插件菜单选择 **从磁盘安装 / Install from Disk**，选中 ZIP 并启用 **Fab in Blender**。更新已有版本后重启 Blender。
3. 先保存当前 `.blend`。在 3D 视图按 **N → Fab → 打开我的 Fab 库**，从资产库的「账号」菜单登录。首次打开时，缺少 WebView2 的 Windows 电脑需要联网安装这个组件。
4. 在「我的库」选择一件资产。选好档位和格式，点「下载」。已有对应文件时，直接点卡片上的「导入」。模型会出现在活动集合的 3D 游标位置。

**Quick start in English.** Download **Fab-in-Blender-Windows.zip** under Releases → Assets, install it from disk in Blender Preferences, and enable the add-on. Save your scene, then open the library from the 3D Viewport sidebar under **Fab**. Sign in, choose a file format and resolution, download, and click **Import**. Change the gallery language under **设置 → 界面 → 界面语言 → English**.

## 搭场景时可以怎样用

| 你想做什么 | 操作 |
|---|---|
| 找素材，暂时不占下载空间 | 搜索或展开分类，点星标存到本地收藏。鼠标移到卡片上按空格可看大图和文件规格。 |
| 逛 Fab 或打开别人发来的商品链接 | 在「探索 Fab」用官网筛选和查看详情，也可以点「粘贴 Fab 链接」。免费商品在官网页面手动领取，完成后点「下载到插件」。 |
| 在下一个工程复用已下载素材 | 在素材详情点「加入 Blender 资产库」。在 Blender 资产浏览器选择 Fab 库即可拖用。 |
| 一次准备多件资产 | 点「多选」后统一选规格。提交前可以看到几件已缓存、几件需要下载、几件没有所选规格。 |
| 把同一个道具摆在多个位置 | 再点「导入」。支持复用的静态单网格模型共享网格和材质，每个物体都能单独移动。齿轮菜单可以创建独立副本。 |
| 渲染前提高贴图分辨率 | 选中物体，在 **N → Fab → Fab 来源** 切换贴图规格。默认只更新当前物体，也可选择共用材质的物体一起更新。 |
| 移动工程或找回贴图 | 项目文件保存在 `.blend` 同级的 `Assets/Fab`。移动时带上这个目录；遇到缺图可在 Fab 面板点「检查并修复缺失贴图」。 |

## 兼容范围

- **Windows x64**。插件声明的最低 Blender 版本为 4.1.1；0.10 系列的工程功能已在 4.1.1、5.2 和 5.3 上做过专项测试。macOS Apple Silicon 移植仍处于实验阶段。[Windows 测试记录](Windows兼容性实测.md)
- 主要处理 **FBX、GLB 和常见 PBR 贴图**。可下载的格式、分辨率和许可由各个 Fab 条目决定。部分 Megascans 资产按质量档位分包，选择 4K 档时应以实际文件为准。
- 贴图升级适用于能明确对应的外部贴图。完整浏览个人库、领取和新下载需要联网。在线功能依赖 Fab 网页接口，网站变更可能需要更新插件。
- 本项目独立开发，与 Epic Games、Fab、Quixel 和 Blender Foundation 均无官方隶属关系。插件安装包只提供工具，资产通过你自己的账号获取并遵循各自许可。

## 遇到问题

提交 Issue 时可附上 **插件版本、Blender 版本、Windows 版本、操作步骤和实际提示**。网络问题请注明是在登录、读取个人库还是下载文件时发生；截图前请遮住邮箱、登录验证码和带签名的下载地址。
