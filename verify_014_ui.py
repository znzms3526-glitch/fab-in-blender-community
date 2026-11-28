"""Drive the real gallery HTML in WebView2 using a read-only synthetic Fab library."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from zipfile import ZipFile

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
from fab_shelf import catalog, gallery

UI_SCRIPT = r"""
async function waitFor(test, message) {
  const limit = performance.now() + 7000;
  while (performance.now() < limit) {
    if (test()) return;
    await new Promise(resolve => setTimeout(resolve, 25));
  }
  throw Error(message);
}
async function verifyIdleCategories(search, scroller) {
  const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
  const baseRpc = rpc;
  const requested = [];
  const childRows = Array.from({length: 7}, (_, index) => ({
    slug: "fixture-child-" + index, name: "Fixture child " + index, children: []
  }));
  const extraKinds = Object.fromEntries(Array.from({length: 7}, (_, index) =>
    ["fixture-kind-" + index, "Fixture kind " + index]));
  let taxonomyRequests = 0, rootRequests = 0, active = 0, maxActive = 0;
  let releaseStaleChildren;
  rpc = async (op, data, preview) => {
    if (op !== "taxonomy" && op !== "presence") return baseRpc(op, data, preview);
    requested.push({op, ...data});
    maxActive = Math.max(maxActive, ++active);
    try {
      await delay(55);
      if (op === "taxonomy") {
        if (++taxonomyRequests === 1) return {deferred: true};
        return {tree: {"3d-model": childRows}, types: extraKinds};
      }
      if (!data.kind && ++rootRequests === 1) return {deferred: true};
      if (data.kind === "3d-model" && !releaseStaleChildren) {
        return await new Promise(resolve => { releaseStaleChildren = () => resolve({found: []}); });
      }
      return {found: data.values.filter(slug => slug !== "decal")};
    } finally { --active; }
  };
  try {
    await waitFor(() => !sidebarBusy, "Previous sidebar request did not finish");
    taxonomyLoaded = false;
    tree = {};
    typeLabels = {};
    document.querySelector("#categoryGroup").open = true;
    invalidatePresence("owned");
    search("fresh");
    for (let frame = 0; frame < 10; frame++) {
      scroller.dispatchEvent(new Event("scroll"));
      await delay(80);
    }
    if (requested.length) throw Error("Classification started during recent browsing");
    const modelRoot = () => [...document.querySelectorAll("#categories .category-root")].find(
      el => el.dataset.categoryFocus === JSON.stringify(["3d-model", ""]));
    await waitFor(() => !!modelRoot() &&
      [...document.querySelectorAll("#categories .category-root")].some(el => el.textContent === "Fixture kind 6"),
      "Idle classification did not retry deferred work and finish the root categories");
    modelRoot().click();
    await waitFor(() => !!releaseStaleChildren, "Expanded category request did not start");
    const oldGeneration = stream.generation;
    search("fresh");
    await waitFor(() => stream?.generation > oldGeneration && !stream.initial,
      "Foreground browse request waited for a classification result");
    releaseStaleChildren();
    await waitFor(() => document.querySelectorAll("#categories .subcategory").length === childRows.length,
      "Stale category absence was applied after the view changed");
    const presence = requested.filter(row => row.op === "presence");
    if (maxActive !== 1 || presence.length < 4 || !requested.every(row => row.idle === true)
       || !presence.every(row => row.values.length >= 1 && row.values.length <= 3
          && Number.isInteger(row.libraryRevision) && Number.isInteger(row.libraryContentRevision))) {
      throw Error("Invalid background classification batch or overlapping queries");
    }
    return {taxonomyRequests, presenceRequests: presence.length, maxActive};
  } finally { rpc = baseRpc; }
}
async function verifyFirstPageRefresh(cards, scroller, toBottom) {
  while (cards().length < 108) {
    const previous = cards().length;
    toBottom();
    await waitFor(() => cards().length > previous, "Initial library did not load three batches");
  }
  const retained = cards().find(tile => tile.dataset.uid === "fixture-0");
  const oldTail = cards().find(tile => tile.dataset.uid === "fixture-75");
  if (!oldTail.querySelector(".batch-pick").checked) oldTail.querySelector(".batch-pick").click();
  if (!retained.querySelector(".batch-pick").checked) retained.querySelector(".batch-pick").click();
  scroller.scrollTo({top:700,behavior:"instant"});
  await waitFor(() => !stream.loading && !watchingLibrary, "Feed did not settle before head refresh");
  const generation = stream.generation;
  await rpc("verifyLibraryContentChange",{reorder:true});
  await watchLibrarySession();
  await waitFor(() => stream.stale, "Deep feed did not defer the refresh");

  const baseRpc = rpc;
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const continuations = [];
  rpc = (op,data,preview) => {
    if (op === "page" && data.generation > generation && data.cursor) {
      continuations.push(data.cursor);
      return gate.then(() => baseRpc(op,data,preview));
    }
    return baseRpc(op,data,preview);
  };
  try {
    scroller.scrollTo({top:0,behavior:"instant"});
    scroller.dispatchEvent(new Event("scroll"));
    if (stream.generation <= generation) throw Error("Returning to the top waited for the session poll");
    if (!retained.isConnected) throw Error("Head refresh cleared the displayed page while waiting");
    await waitFor(() => !stream.initial, "Refreshed head did not render");
    const expected = Array.from({length:36},(_,i) => "fixture-" + (i === 1 ? 108 : 35-i));
    if (JSON.stringify(cards().map(tile => tile.dataset.uid)) !== JSON.stringify(expected)) {
      throw Error("Refreshed head retained old tail batches or ignored server order");
    }
    if (tiles.get("fixture-0") !== retained || !retained.querySelector(".batch-pick").checked ||
        !batchAssets.has("fixture-75") || oldTail.isConnected || scroller.scrollTop > 5) {
      throw Error("Head refresh lost card identity, cross-batch selection or the top position");
    }
    release();
    await waitFor(() => cards().length >= 72, "First continuation after head refresh did not append cards");
    toBottom();
    await waitFor(() => cards().length >= 108, "Second continuation after head refresh stalled");
    if (continuations[0] !== "36" || continuations[1] !== "72" ||
        !tiles.get("fixture-75")?.querySelector(".batch-pick").checked) {
      throw Error("Page cursors skipped a batch or the selected tail card lost its check");
    }
  } finally {
    release();
    rpc = baseRpc;
  }
}
async function verifyAccountSwitchWait(search) {
  const baseRpc = rpc;
  let rejected = false, observed = false, release;
  const gate = new Promise(resolve => { release = resolve; });
  await waitFor(() => !watchingLibrary && !stream.loading, "Previous library check did not settle");
  rpc = async (op,data,preview) => {
    if (op === "librarySession" && rejected) {
      observed = true;
      await gate;
    }
    try { return await baseRpc(op,data,preview); }
    catch(error) {
      if (op === "page" && data.query === "switch-account" && error.message === "Fab 账号正在切换") rejected = true;
      throw error;
    }
  };
  try {
    search("switch-account");
    await waitFor(() => rejected && observed, "Switch error did not schedule session verification");
    if (!feedRetry.hidden || document.querySelector("#empty button") ||
        !document.querySelector("#grid .skeleton") ||
        document.querySelector("#grid").getAttribute("aria-busy") !== "true") {
      throw Error("Transient account switch showed error actions instead of waiting for verification");
    }
    release();
    await waitFor(() => tiles.has("fixture-switched") && !stream.initial, "New account did not load");
  } finally {
    release();
    rpc = baseRpc;
  }
}
async function verifyQueueUi() {
  const statusText = row => row.querySelector("small").textContent;
  const findJob = id => document.querySelector('[data-job="' + id + '"]');
  const labelButton = (target, label) => [...target.querySelectorAll("button")].find(b => b.textContent === tr(label));
  await rpc("verifyPrepareJobs");
  document.querySelector("#transfer").click();
  await poll();
  await waitFor(() => findJob("fixture-failed") && findJob("fixture-conflict"), "Queue rows did not appear");
  const bad = findJob("fixture-failed");
  if (statusText(bad).split("Fixture transfer failure").length !== 2) throw Error("Queue echoed the same error and notice");
  const retry = labelButton(bad,"继续");
  retry.focus();
  await rpc("verifyUpdateJobs");
  await poll();
  await waitFor(() => findJob("fixture-progress").querySelector("progress").value === 8, "Progress update did not render");
  if (document.activeElement !== labelButton(findJob("fixture-failed"),"继续")) {
    throw Error("Queue refresh lost the current keyboard action");
  }
  labelButton(findJob("fixture-conflict"),"去详情选择").click();
  const details = document.querySelector("#inspect");
  await waitFor(() => details.open && details.querySelectorAll(".conflict-candidate input").length === 2,
                "Queue selection did not reach the manual file controls");
  if (document.querySelector("#variantConfirm").open || !details.querySelector(".asset-options").open) {
    throw Error("Conflict started another confirmation loop");
  }
  const candidate = details.querySelector(".conflict-candidate input");
  candidate.click();
  const submit = labelButton(details.querySelector(".asset-options"),"按所选文件导入");
  submit.click();
  submit.click();
  await waitFor(() => !details.open && statusText(findJob("fixture-conflict")).startsWith(tr("等待导入")),
                "Manual choice did not keep the queued Blender import receipt");
  const result = await rpc("verifyQueueResult");
  if (result.submitted !== 1) throw Error("Double-click sent more than one manual import");
  document.querySelector('#scope [data-scope="owned"]').click();
  document.querySelector("#query").value="switch-account";
  document.querySelector("#query").dispatchEvent(new Event("input"));
  await waitFor(() => tiles.has("fixture-switched"), "Returning from queue lost the signed-in view");
}
async function verifyRawAndKeyboard() {
  const modelCard = tiles.get("fixture-switched");
  const other = card({uid:"fixture-hovered",title:"Other card",kind:"3d-model",local:[]});
  const details = document.querySelector("#inspect");
  const querySelector = document.querySelector;
  document.querySelector = function(selector) {
    return selector === ".card:hover" ? other : querySelector.call(this,selector);
  };
  try {
    modelCard.focus();
    modelCard.dispatchEvent(new KeyboardEvent("keydown",{key:" ",code:"Space",bubbles:true,cancelable:true}));
    if (!details.open || details.dataset.uid !== modelCard.dataset.uid) {
      throw Error("Space opened the hovered card instead of the keyboard-focused card");
    }
  } finally {
    document.querySelector=querySelector;
    if(details.open)details.close();
  }
  modelCard.querySelector(".options").click();
  await waitFor(() => details.querySelector('option[value="fixture-raw"]'),"Raw package did not appear in details");
  const file = details.querySelector('option[value="fixture-raw"]').parentElement;
  const size = details.querySelector(".remote-files .field select");
  file.value="fixture-raw";
  file.dispatchEvent(new Event("change"));
  if (size.value !== "8") throw Error("Locking Raw kept the previous unrelated 4K value");
  details.querySelector(".remote-files .primary").click();
  await waitFor(() => !details.open,"Raw detail action did not queue the selected file");
  if(!(await rpc("verifyQueueResult")).raw)throw Error("Raw selection was not sent as an 8K manual lock");
  const localHash="c".repeat(64);
  modelCard.asset.local=[{hash:localHash,fmt:"FBX",nominalK:8,packageTier:"raw",dimensions:[[8192,8192]],name:"Fixture_raw.zip"}];
  modelCard.querySelector(".options").click();
  const cachedFile=details.querySelector('option[value="' + localHash + '"]');
  if(!cachedFile?.textContent.includes("Raw · 8192×8192"))throw Error("Cached Raw lost its explicit file label");
  cachedFile.parentElement.value=localHash;
  cachedFile.parentElement.dispatchEvent(new Event("change"));
  details.querySelector(".local-actions .primary").click();
  await waitFor(() => details.querySelector(".asset-feedback")?.textContent === "Fixture used cached Raw",
                "Cached Raw did not import at the locked 8K specification");
  details.close();
  modelCard.asset.local=[{hash:"b".repeat(64),fmt:"附件",dimensions:[[4096,4096]],name:"Fixture_Textures.zip"}];
  modelCard.refreshActions();
  if(modelCard.querySelector(".actions .primary").textContent === tr("导入")){
    throw Error("A local texture sidecar was offered as an importable mesh");
  }
  modelCard.querySelector(".options").click();
  await waitFor(() => details.querySelector('option[value="fixture-attachment"]'), "Attachment did not appear in details");
  if(details.querySelector(".local-actions"))throw Error("A local sidecar was listed as a cached mesh");
  const attachedFiles=details.querySelector('option[value="fixture-attachment"]').parentElement;
  attachedFiles.value="fixture-attachment";
  attachedFiles.dispatchEvent(new Event("change"));
  if(!details.querySelector(".remote-files .primary").hidden)throw Error("Attachment showed Download and import");
  [...details.querySelectorAll(".remote-files button")].find(b=>b.textContent===tr("仅下载")).click();
  await waitFor(() => !details.open, "Attachment download-only did not queue");
  if(!(await rpc("verifyQueueResult")).attachment)throw Error("Attachment sent a mixed import/download action");
}
async function verifyNarrow() {
  await rpc("verifyNarrow");
  await waitFor(() => innerWidth <= 740 && matchMedia("(max-width:880px)").matches,
                "Real WebView2 window did not reflow to narrow mode");
  const width = innerWidth;
  const buttons = [...document.querySelectorAll("#scope button:not([hidden]), #tools button:not([hidden])")];
  if (getComputedStyle(document.querySelector("#scope")).display !== "flex" ||
      document.documentElement.scrollWidth > width + 1 ||
      buttons.some(button => {
        const r = button.getBoundingClientRect();
        return r.right > width + 1 && !button.closest("#scope");
      })) throw Error("Narrow layout made toolbar actions overflow the viewport");
  return width;
}
async function changeLanguage(value,phase) {
  await openPreferences("界面");
  document.querySelector("#language").value=value;
  sessionStorage.setItem("verify-phase",phase);
  await document.querySelector("#prefSave").onclick();
}
async function runFixture() {
  const cards = () => [...document.querySelectorAll("#grid .card")];
  const scroller = document.querySelector("#scroll");
  const toBottom = () => scroller.scrollTo({top: scroller.scrollHeight, behavior: "instant"});
  await waitFor(() => cards()[0]?.dataset.preview === "1", "Verified disk preview did not render");
  const cachedTile = cards()[0];
  if (cachedTile.querySelector(".batch-pick")) throw Error("Preview must be read-only");
  await waitFor(() => cards().length >= 36 && !cards()[0].dataset.preview, "Initial live batch was not rendered");
  let first = cards()[0];
  if (cachedTile !== first) throw Error("Live head replaced the cached first card");
  document.querySelector("#multi").click();
  first.querySelector(".batch-pick").click();
  await waitFor(() => cards().length >= 72, "Next live batch was not prepared before scrolling");
  if (scroller.scrollTop !== 0) throw Error("Preparing the next batch moved the viewport");
  toBottom();
  const retry = document.querySelector("#feedRetry");
  await waitFor(() => !retry.hidden, "Second batch failure did not show Retry");
  retry.click();
  await waitFor(() => cards().length >= 108, "Retry did not append items");
  if (first !== cards()[0] || !first.querySelector(".batch-pick").checked) {
    throw Error("Retry replaced the first card or its selection");
  }
  scroller.scrollTo({top:700, behavior:"instant"});
  await waitFor(() => !stream.loading && !watchingLibrary, "Feed did not settle before library revision test");
  const position = scroller.scrollTop;
  const generation = stream.generation;
  await rpc("verifyLibraryContentChange");
  await watchLibrarySession();
  await waitFor(() => stream.stale && !retry.hidden && !feedFooter.hidden, "An invalidated continuation had no refresh action");
  if (document.querySelector("#feedNotice,#feedUpdate") || retry.textContent !== tr("刷新资产库")) {
    throw Error("A changed head was shown as a new acquisition");
  }
  if (scroller.scrollTop !== position || cards()[0] !== first || stream.generation !== generation) {
    throw Error("A changed head interrupted browsing");
  }
  retry.click();
  await waitFor(() => stream.generation !== generation && !stream.stale && cards().length >= 36,
                "The footer refresh did not recover the list");
  await verifyFirstPageRefresh(cards,scroller,toBottom);
  first = cards()[0];
  while (cards().length < 1010) {
    const previous = cards().length;
    toBottom();
    await waitFor(() => cards().length > previous, "Feed stalled at " + previous);
  }
  await waitFor(() => document.querySelector("#feedStatus").textContent.includes("1010"), "Unknown end of feed");
  const uids = cards().map(card => card.dataset.uid);
  if (new Set(uids).size !== 1010 || cards()[0] !== first) throw Error("Duplicate or rebuilt feed cards");
  if (cards().some(tile => tile.textContent.includes("贴图尺寸待确认"))) throw Error("The feed repeated unverified texture hints");
  toBottom();
  const top = document.querySelector("#backToTop");
  await waitFor(() => !top.hidden, "Back-to-top control did not appear");
  top.click();
  await waitFor(() => scroller.scrollTop < 5, "Scrolling back to the top failed");
  const field = document.querySelector("#query");
  const search = term => { field.value = term; field.dispatchEvent(new Event("input")); };
  search("slow");
  await new Promise(resolve => setTimeout(resolve, 370));
  search("fresh");
  await waitFor(() => cards().length === 1 && cards()[0].dataset.uid === "fixture-fresh",
                "New search did not replace the old feed");
  await new Promise(resolve => setTimeout(resolve, 1000));
  if (cards().length !== 1 || cards()[0].dataset.uid !== "fixture-fresh") throw Error("Stale search appeared");
  const classification = await verifyIdleCategories(search, scroller);
  await verifyAccountSwitchWait(search);
  document.querySelector("#cache").click();
  await waitFor(() => document.querySelector("#menu").matches(":popover-open"), "Cover cache popover did not open");
  if (!document.querySelector("#menu").textContent.includes("/ 128 MiB")) {
    throw Error("Cover cache popover did not show the active 128 MiB limit");
  }
  document.querySelector("#menu").hidePopover();
  const modelCard = cards()[0];
  const labels = card => [...card.querySelectorAll(".levels button")].map(button => button.textContent);
  const target = card => card.querySelector(".texture-info").textContent.split("\n")[0];
  if (modelCard.querySelector(".actions select") || labels(modelCard).join() !== ["低", "中", "高"].map(tr).join()) {
    throw Error("The model card must show only the three combined presets");
  }
  const materialCard = card({uid:"fixture-material",title:"Fixture material",kind:"material",local:[]});
  const decalCard = card({uid:"fixture-decal",title:"Fixture decal",kind:"decal",local:[]});
  if ([materialCard,decalCard].some(tile => labels(tile).join() !== "1K,2K,4K,8K" || target(tile) !== "目标：2K")) {
    throw Error("Material/decal texture size controls or initial target are wrong");
  }
  if (target(modelCard) !== "目标：高模 · 4K" || modelCard.querySelector('.levels .chosen').dataset.variant !== "high") {
    throw Error("Default high preset was not selected and labeled");
  }
  const sequence = [["low","低模","1"],["medium","中模","2"],["high","高模","4"],["low","低模","1"]];
  for (const [tier,mesh,k] of sequence) {
    modelCard.querySelector('[data-variant="' + tier + '"]').click();
    if (target(modelCard) !== "目标：" + mesh + " · " + k + "K" ||
        modelCard.querySelector('.levels [aria-pressed="true"]').dataset.variant !== tier) {
      throw Error("Rapid preset change did not update the label and pressed state");
    }
  }
  const primary = modelCard.querySelector(".actions .primary");
  if (primary.textContent !== tr("获取并导入")) throw Error("An unverified model should show Get and import");
  modelCard.asset.local = [{fmt:"FBX",lod:0,dimensions:[[4096,4096]]}];
  modelCard.querySelector('[data-variant="high"]').click();
  if (!modelCard.querySelector(".texture-info").textContent.includes("本地：LOD0 · 实测 4096×4096")) {
    throw Error("Verified local LOD and dimensions were not shown separately from the target");
  }
  modelCard.asset.local = [{fmt:"FBX",dimensions:[[2048,2048],[4096,4096]]}];
  modelCard.querySelector('[data-variant="high"]').click();
  if (primary.textContent !== tr("导入")) throw Error("Mixed-resolution cached models should show Import");
  primary.click();
  await waitFor(() => document.querySelector("#toast").textContent === "Fixture imported mixed ZIP",
                "Model card did not request import from the mixed ZIP cache");
  const toolbar = document.querySelector("#importSettings");
  if (toolbar.classList.contains("primary") || toolbar.textContent.includes("+")) {
    throw Error("Import settings must be neutral and unadorned at defaults");
  }
  toolbar.click();
  const panel = document.querySelector("#importSettingsPanel");
  const channel = (view, name) => view.querySelector('.channel-picker input[data-channel="' + name + '"]');
  await waitFor(() => panel.querySelectorAll(".channel-picker input").length === Object.keys(importChannels).length,
                "Shared import preferences did not appear");
  if (existingChannels.some(name => !channel(panel, name).checked) ||
      extraChannels.some(name => channel(panel, name).checked) || !panel.querySelector(".import-height-options").hidden ||
      !panel.querySelector(".import-node-settings input").checked ||
      panel.querySelector(".import-preset-options input:checked").value !== "high") {
    throw Error("The shared import defaults do not match the Bridge-style form");
  }
  const height = channel(panel, "Height");
  height.click();
  const heightOptions = panel.querySelector(".import-height-options");
  if (heightOptions.hidden || heightOptions.querySelector("select").value !== "bump") {
    throw Error("Height opt-in did not reveal the default bump mode");
  }
  if ([...heightOptions.querySelectorAll("option")].map(option => option.textContent).join() !== "仅凹凸,真置换") {
    throw Error("Height controls lost either Bump or Displacement");
  }
  height.click();
  channel(panel, "AO").click();
  panel.querySelector("button").click();
  await waitFor(() => !panel.matches(":popover-open") && importPreferences.channels.includes("AO") &&
                toolbar.textContent.endsWith("+1"),
                "Import toolbar did not persist an explicit AO opt-in");
  await openPreferences("导入");
  const shared = document.querySelector("#importSettingsPage");
  if (!channel(shared, "AO")?.checked) throw Error("The full settings view did not reflect the shared toolbar opt-in");
  channel(shared, "Specular").click();
  await document.querySelector("#prefSave").onclick();
  document.querySelector("#prefClose").click();
  toolbar.click();
  await waitFor(() => channel(panel, "Specular")?.checked && toolbar.textContent.endsWith("+2"),
                "Toolbar settings did not reflect opt-in saved in full settings");
  panel.hidePopover();
  await verifyQueueUi();
  await verifyRawAndKeyboard();
  const narrowWidth=await verifyNarrow();
  await openPreferences("存储");
  document.querySelector("#coverMiB").value="256";
  await document.querySelector("#prefSave").onclick();
  document.querySelector("#prefClose").click();
  document.querySelector("#cache").click();
  await waitFor(() => document.querySelector("#menu").textContent.includes("/ 256 MiB"),
                "Cover cache popover did not adopt 256 MiB settings");
  document.querySelector("#menu").hidePopover();
  return {cards: uids.length, cachePreview: true, earlyBatch: true, retry: true, silentHeadChange: true,
          selection: true, staleSearch: true, refreshedCursor:true, accountWait:true, coverLimit:true,
          autoFileWithSharedImportSettings:true, classification, narrowWidth, rapidPresets:true,
          noUnverifiedSpam:true, rawLock:true, cachedRawLock:true, attachmentDownloadOnly:true, queueManualConflict:true,
          keyboard:true, changedCoverLimit:256};
}
async function roundTripLanguage() {
  const phase = sessionStorage.getItem("verify-phase");
  if (!phase) {
    sessionStorage.setItem("verify-report",JSON.stringify(await runFixture()));
    return changeLanguage("en","english");
  }
  await waitFor(() => tiles.has("fixture-switched"),"Language reload lost the user's library");
  const cardText = tiles.get("fixture-switched").querySelector(".texture-info").textContent;
  const closer = document.querySelector("#inspect header button").getAttribute("aria-label");
  if (phase === "english") {
    if (document.documentElement.lang !== "en" || !cardText.startsWith("Target: High · 4K") || closer !== "Close") {
      throw Error("English reload did not translate target and Close consistently: " + cardText + " / " + closer);
    }
    if ((await verifyNarrow()) !== 720 || (await rpc("settings")).values.coverMiB !== 256) {
      throw Error("English narrow layout or saved cache limit did not survive reload");
    }
    return changeLanguage("zh-CN","chinese");
  }
  if (document.documentElement.lang !== "zh-CN" || !cardText.startsWith("目标：高模 · 4K") || closer !== "关闭") {
    throw Error("Chinese reload kept translated UI or incorrect model target");
  }
  const report=JSON.parse(sessionStorage.getItem("verify-report"));
  chrome.webview.postMessage({op:"verifyFeedUi",report:{...report,languages:["zh-CN","en","zh-CN"]}});
}
roundTripLanguage().catch(error => chrome.webview.postMessage({op:"verifyFeedUi",error:error.message}));
"""


def resize_gallery(pid, client_width):
    user = ctypes.windll.user32
    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback, wintypes.LPARAM]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    windows = []

    @callback
    def collect(window, _):
        owner = wintypes.DWORD()
        user.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value == pid and user.IsWindowVisible(window):
            windows.append(window)
        return True

    user.EnumWindows(collect, 0)
    window, = windows
    outer, client = wintypes.RECT(), wintypes.RECT()
    for operation in (user.GetWindowRect, user.GetClientRect):
        operation.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    assert user.GetWindowRect(window, ctypes.byref(outer))
    assert user.GetClientRect(window, ctypes.byref(client))
    user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    border = outer.right - outer.left - client.right
    assert user.SetWindowPos(window, None, 0, 0, client_width + border, outer.bottom - outer.top, 0x16)


def verify_feed_ui(root):
    runtime = REPO / "fab_shelf" / "runtime"
    host = root / "host"
    host_files = ("Gallery091.exe", "Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll", "WebView2Loader.dll")
    archive_file = os.environ.get("FAB_TEST_ARCHIVE")
    if archive_file:
        (host / "gallery").mkdir(parents=True)
        with ZipFile(archive_file) as archive:
            for relative in (*map(lambda p: f"gallery/{p.name}", sorted((runtime / "gallery").iterdir())), *host_files):
                (host / relative).write_bytes(archive.read(f"fab_shelf/runtime/{relative}"))
    else:
        shutil.copytree(runtime / "gallery", host / "gallery")
        for name in host_files:
            shutil.copyfile(runtime / name, host / name)
    index = host / "gallery" / "index.html"
    index.write_text(index.read_text(encoding="utf8").replace("</body>", '<script src="verify.js"></script></body>'), encoding="utf8")
    (index.parent / "verify.js").write_text(UI_SCRIPT, encoding="utf8")
    box, cache, report = root / "mail", root / "cache", root / "report.json"
    box.mkdir()
    def item(uid):
        return dict(uid="fixture-"+str(uid), title="Fixture " + str(uid), kind="3d-model", thumb="")

    cached = gallery.Gallery(cache, box)
    try:
        account, revision = cached.remember_library_account("fixture-account")
        cached.write_library_snapshot(account, dict(items=[item(i) for i in range(36)]), revision=revision)
    finally:
        cached.close()
        cached.join()

    app = gallery.Gallery(cache, box, settings={"coverMiB": 128})
    handle, fail_more, first_head = app.handle, True, True
    account = "fixture-account"
    reordered = False
    job_rows = {}
    submitted = 0
    raw_locked = False
    attachment_download = False
    conflict_hash = "a" * 64
    candidates = ["BaseColor_A.png", "BaseColor_B.png"]
    conflict_choice = dict(localHash=conflict_hash, name="Conflict fixture", format="FBX",
                           channel="Base Color", message="Fixture conflicting textures", candidates=candidates)
    order = list(range(1010))
    changed_order = [108 if i == 34 else i for i in range(35, -1, -1)] + [34] + [
        i for i in range(36, 1010) if i != 108]

    def api(query, request_id=None, *, prefetch=False):
        nonlocal fail_more, first_head, account
        op = query["op"]
        if op == "account":
            return dict(uid=account, name="Fixture")
        if op == "library":
            if query["query"] == "slow":
                time.sleep(1)
                return dict(items=[item("slow")], next="")
            if query["query"] == "fresh":
                return dict(items=[item("fresh")], next="")
            if query["query"] == "switch-account":
                if account == "fixture-account":
                    account = "fixture-account-b"
                    raise ValueError("Fab 账号正在切换")
                return dict(items=[item("switched")], next="")
            offset = int(query["cursor"] or 0)
            if not offset and first_head:
                first_head = False
                time.sleep(.4)
            if offset == 72 and fail_more:
                if prefetch:
                    return None
                fail_more = False
                raise ValueError("Fixture interrupted this batch")
            current_order = changed_order if reordered else order
            rows = [item(i) for i in current_order[max(0, offset-1):offset+36]]
            return dict(items=rows, next=str(offset+36) if offset+36 < 1010 else "",
                        **({"observedAccount": account} if query.get("verifyAccount") else {}))
        if op == "taxonomy":
            return dict(tree={}, types={})
        if op == "presence":
            return dict(found=[])
        raise AssertionError(op)

    def commands(query):
        nonlocal reordered, submitted, raw_locked, attachment_download
        if query["op"] == "verifyFeedUi":
            catalog.atomic_json(report, query)
            return True
        if query["op"] == "verifyPrepareJobs":
            for id_, state in (("fixture-failed", "失败"), ("fixture-progress", "下载中"), ("fixture-conflict", "待选择")):
                job_rows[id_] = dict(id=id_, uid="fixture-switched", title=id_, state=state, done=4, total=16,
                                     active=state == "下载中", route="", speed=0, directory="")
            message = "Fixture transfer failure"
            job_rows["fixture-failed"].update(error=message, notice=message)
            job_rows["fixture-conflict"].update(confirmations=[conflict_choice])
            return True
        if query["op"] == "verifyUpdateJobs":
            job_rows["fixture-progress"]["done"] = 8
            return True
        if query["op"] == "verifyQueueResult":
            return dict(submitted=submitted, raw=raw_locked, attachment=attachment_download)
        if query["op"] == "verifyNarrow":
            resize_gallery(proc.pid, 720)
            return True
        if query["op"] == "jobs" and job_rows:
            return list(job_rows.values())
        if query["op"] == "card" and query["uid"] == "fixture-switched" and "fixture-conflict" in job_rows:
            return dict(item("switched"), local=[dict(hash=conflict_hash, fmt="FBX", name="fixture.zip", k=4)],
                        thumb="", localOnly=False)
        if query["op"] == "members" and query.get("hash") == conflict_hash:
            return dict(names=["Fixture.fbx", *candidates], files=["Fixture.fbx"], mode=0, modelCount=1)
        if query["op"] == "send" and query.get("hash") == conflict_hash:
            assert query["files"] == ["Fixture.fbx", "BaseColor_A.png"], query
            assert query["job"] == "fixture-conflict", query
            submitted += 1
            time.sleep(.2)
            job_rows["fixture-conflict"].update(state="等待", pendingImports=1, confirmations=[])
            return "Fixture imported manual selection"
        if query["op"] == "control" and query["job"] == "fixture-conflict":
            raise AssertionError("Manual conflict cannot remove the import job before Blender processes it")
        if query["op"] == "options" and query["uid"] == "fixture-switched":
            return [dict(uid="fixture-high", fmt="FBX", name="Fixture_high.zip", nominalK=4, packageTier="high"),
                    dict(uid="fixture-raw", fmt="FBX", name="Fixture_raw.zip", nominalK=8, packageTier="raw"),
                    dict(uid="fixture-attachment", fmt="附件", name="Fixture_Textures.zip", attachment=True)]
        if query["op"] == "verifyLibraryContentChange":
            reordered = reordered or query.get("reorder") is True
            with app.lock:
                app.library_content_revision += 1
                app.page_cache.clear()
                app.library_head_checked = time.monotonic()
            return True
        if query["op"] == "variant" and query["uid"] == "fixture-switched":
            if query["selection"].get("fileId") == "fixture-attachment":
                assert query["selection"]["mode"] == "locked" and query["import"] is False, query
                attachment_download = True
                return dict(state="queued", message="Fixture queued texture attachment")
            assert query["import"] is True
            if query["selection"].get("localHash") == "c" * 64:
                assert query["selection"] == dict(mode="locked", textureK=8, modelTier="raw", localHash="c" * 64)
                assert query["recipe"]["modelTier"] == "raw"
                return dict(state="cached", message="Fixture used cached Raw")
            if query["selection"].get("fileId") == "fixture-raw":
                assert query["selection"] == dict(mode="locked", textureK=8, modelTier="raw", fileId="fixture-raw")
                assert query["recipe"]["modelTier"] == "raw"
                raw_locked = True
                return dict(state="queued", message="Fixture queued Raw package")
            assert query["selection"] == dict(mode="auto",preset="high",textureK=4,modelTier="high")
            return dict(state="cached",message="Fixture imported mixed ZIP")
        return handle(query)

    app.api, app.handle = api, commands
    proc = None
    try:
        proc = subprocess.Popen([str(host / "Gallery091.exe"), str(box), str(os.getpid()), str(cache),
                                 "Synthetic Fab Library", str(root / "profile")])
        deadline = time.monotonic() + 45
        while not report.exists() and time.monotonic() < deadline and proc.poll() is None:
            time.sleep(.05)
        assert report.exists(), f"WebView2 fixture did not finish. Exit status {proc.poll()}"
        result = json.loads(report.read_text(encoding="utf8"))
        assert "error" not in result, result["error"]
        assert result["report"]["cards"] == 1010
        result["report"]["hostSource"] = "installation ZIP" if archive_file else "working tree"
        print("PASS: WebView2 feed", result["report"], flush=True)
    finally:
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=10)
        app.close()
        app.join()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="fu", ignore_cleanup_errors=True) as folder:
        verify_feed_ui(Path(folder))
