"use strict";

let localSource = "all";
let bridgeState = null;
let renderedBridgeRevision = null;
let bridgeTimer = 0;
let bridgeTicket = 0;
let bridgeActionPending = false;
let bridgeStartedHere = false;
let bridgeCancelling = false;

function bridgeScanning(state) {
  return state === "discovering" || state === "scanning";
}

function bridgeAffectsView() {
  return scope === "favorites" || (scope === "local" && ["all", "bridge"].includes(localSource));
}

function selectLocalSource(source) {
  resetFilters();
  localSource = source;
  batchAssets.clear();
  selecting = false;
  for (const key of visibleBranches.keys()) {
    if (["local", "favorites"].includes(JSON.parse(key)[0])) visibleBranches.delete(key);
  }
  return load();
}

function renderBridgeBanner(inLocal) {
  const banner = $("#bridgeBanner");
  if (!banner) return;
  const dismissed = localStorage.getItem("fab-bridge-banner-dismissed") === "1";
  const show = Boolean(inLocal && bridgeState && !bridgeState.path && !bridgeScanning(bridgeState.state) && !dismissed);
  banner.hidden = !show;
}

function bridgeToolbar(inLocal) {
  $("#localSources").hidden = !inLocal;
  $("#bridgeImport").hidden = !inLocal;
  for (const entry of $("#localSources").querySelectorAll("button")) {
    entry.setAttribute("aria-pressed", String(entry.dataset.source === localSource));
  }
  const state = bridgeState?.state;
  const label = bridgeScanning(state) ? "正在扫描 Bridge 素材库" :
    bridgeState?.path ? "管理 Bridge 素材库" : "导入 Bridge 资产";
  $("#bridgeImport").textContent = tr(label);
  renderBridgeBanner(inLocal);
}

function renderBridgeLibrary() {
  const result = bridgeState || {};
  const { state, path, connected, assets, total, done, found, recognized, missing, unsupported, invalid } = result;
  const scanning = bridgeScanning(state);
  bridgeToolbar(scope === "local");
  $("#bridgeHeading").textContent = tr(path ? "管理 Bridge 素材库" : "导入 Bridge 资产");
  $("#bridgeGuide").hidden = Boolean(path) || scanning;
  const directory = scanning ? result.target || path : path;
  $("#bridgePath").hidden = !directory;
  $("#bridgePath").textContent = directory || "";
  for (const [selector, show] of [
    ["#bridgeChoose", !path && !scanning],
    ["#bridgeRescan", path && connected && !scanning],
    ["#bridgeRelocate", path && !scanning],
    ["#bridgeRemove", path && !scanning],
    ["#bridgeCancel", scanning],
    ["#bridgeView", assets && !scanning && connected],
  ]) {
    const control = $(selector);
    control.hidden = !show;
    control.disabled = bridgeActionPending || (selector === "#bridgeCancel" && bridgeCancelling);
  }
  const progress = $("#bridgeProgress");
  progress.hidden = !scanning && state !== "complete";
  if (state === "discovering") {
    progress.removeAttribute("value");
  } else {
    progress.value = state === "complete" ? 100 : total ? Math.min(99, Math.floor(done * 100 / total)) : 0;
  }

  let summary = "";
  if (bridgeCancelling && scanning) summary = tr("正在取消扫描…");
  else if (state === "discovering") summary = tr("正在查找 Bridge 素材…") + " " + found;
  else if (state === "scanning") {
    summary = tr("已检查") + " " + done + " / " + total + " · " + tr("已识别") + " " + recognized;
  } else if (state === "failed") summary = tr(result.message);
  else if (state === "cancelled") summary = tr("已取消扫描，保留上次登记");
  else if (path && !connected) summary = tr("Bridge 素材库未连接");
  else if (path) summary = tr("已登记 Bridge 资产") + " " + assets;

  const skipped = [
    [missing, "缺少文件"],
    [unsupported, "格式暂不支持"],
    [invalid, "无法读取"],
  ].filter(([count]) => count).map(([count, label]) => tr(label) + " " + count);
  $("#bridgeScan").hidden = !summary;
  if ($("#bridgeSummary").textContent !== summary) $("#bridgeSummary").textContent = summary;
  $("#bridgeSkipped").hidden = !skipped.length;
  $("#bridgeSkipped").textContent = skipped.join(" · ");
  const skippedItems = Array.isArray(result.skippedItems) ? result.skippedItems : [];
  const skippedDetails = $("#bridgeSkippedDetails");
  const skippedList = $("#bridgeSkippedList");
  const skippedSummary = $("#bridgeSkippedSummary");
  if (skippedDetails && skippedList && skippedSummary) {
    const reasonLabels = {
      unsupported: "格式暂不支持（如 UE UAsset）",
      missing: "缺少可用模型或贴图",
      invalid: "无法读取",
    };
    skippedDetails.hidden = !skippedItems.length || scanning;
    if (skippedItems.length) {
      const totalSkipped = (missing || 0) + (unsupported || 0) + (invalid || 0);
      skippedSummary.textContent = tr("查看跳过明细") + " (" + (totalSkipped > skippedItems.length ? skippedItems.length + "/" + totalSkipped : skippedItems.length) + ")";
      skippedList.replaceChildren(...skippedItems.map(entry => {
        const li = el("li");
        li.append(el("span", "name", entry.name || ""), document.createTextNode(" — " + tr(reasonLabels[entry.reason] || entry.reason || "无法读取")));
        return li;
      }));
    } else {
      skippedDetails.open = false;
      skippedList.replaceChildren();
    }
  }
}

function acceptBridgeState(result) {
  bridgeState = result;
  if (!bridgeScanning(result.state)) bridgeCancelling = false;
  renderBridgeLibrary();
  if (bridgeScanning(result.state)) return;
  const startedHere = bridgeStartedHere;
  bridgeStartedHere = false;
  const completedHere = startedHere && result.state === "complete";
  if (completedHere && scope === "local" && localSource !== "bridge") selectLocalSource("bridge");
  else if (bridgeAffectsView() && renderedBridgeRevision !== null &&
           typeof result.revision === "string" && renderedBridgeRevision !== result.revision) load();
  if (completedHere && !$("#bridgeLibrary").open) toast(tr("已登记 Bridge 资产") + " " + result.assets);
}

async function pollBridgeLibrary() {
  clearTimeout(bridgeTimer);
  const ticket = ++bridgeTicket;
  try {
    const result = await rpc("bridgeStatus");
    if (ticket !== bridgeTicket) return;
    acceptBridgeState(result);
  } catch (error) {
    if (ticket === bridgeTicket) toast(error.message);
  } finally {
    if (ticket === bridgeTicket && bridgeScanning(bridgeState?.state)) {
      bridgeTimer = setTimeout(pollBridgeLibrary, 350);
    }
  }
}

async function bridgeLibraryAction(op, settings = {}) {
  if (bridgeActionPending) return;
  bridgeActionPending = true;
  ++bridgeTicket;
  clearTimeout(bridgeTimer);
  renderBridgeLibrary();
  try {
    const result = await rpc(op, settings);
    if (op === "bridgeScan" && result) {
      bridgeStartedHere = true;
      acceptBridgeState({ ...bridgeState, ...result });
    } else if (op === "bridgeRemove") {
      acceptBridgeState(result);
      if (scope === "local") selectLocalSource("bridge");
    } else if (op === "bridgeCancel") {
      acceptBridgeState(result);
    }
  } catch (error) {
    toast(error.message);
  } finally {
    bridgeActionPending = false;
    renderBridgeLibrary();
    pollBridgeLibrary();
  }
}

function openBridgeLibrary() {
  $("#bridgeRemoveConfirm").hidden = true;
  renderBridgeLibrary();
  $("#bridgeLibrary").showModal();
  layoutFab();
  if (!bridgeActionPending) pollBridgeLibrary();
}

function initBridgeLibrary() {
  for (const entry of $("#localSources").querySelectorAll("button")) {
    entry.onclick = () => selectLocalSource(entry.dataset.source);
  }
  const choose = relocate => bridgeLibraryAction("bridgeScan", {
    choose: true,
    relocate,
    directory: bridgeState?.path,
    caption: tr("请选择 Bridge 素材库文件夹"),
  });
  $("#bridgeImport").onclick = openBridgeLibrary;
  if ($("#bridgeBannerAction")) $("#bridgeBannerAction").onclick = openBridgeLibrary;
  if ($("#bridgeBannerDismiss")) {
    $("#bridgeBannerDismiss").onclick = () => {
      localStorage.setItem("fab-bridge-banner-dismissed", "1");
      renderBridgeBanner(scope === "local");
    };
  }
  $("#bridgeClose").onclick = () => $("#bridgeLibrary").close();
  $("#bridgeChoose").onclick = () => choose(false);
  $("#bridgeRescan").onclick = () => bridgeLibraryAction("bridgeScan");
  $("#bridgeRelocate").onclick = () => choose(true);
  $("#bridgeRemove").onclick = () => {
    $("#bridgeRemoveConfirm").hidden = false;
    $("#bridgeKeep").focus();
  };
  $("#bridgeKeep").onclick = () => {
    $("#bridgeRemoveConfirm").hidden = true;
    $("#bridgeRemove").focus();
  };
  $("#bridgeForget").onclick = () => {
    $("#bridgeRemoveConfirm").hidden = true;
    bridgeLibraryAction("bridgeRemove");
  };
  $("#bridgeCancel").onclick = () => {
    bridgeCancelling = true;
    bridgeLibraryAction("bridgeCancel");
  };
  $("#bridgeView").onclick = () => {
    $("#bridgeLibrary").close();
    if (scope !== "local" || localSource !== "bridge") {
      scope = "local";
      selectLocalSource("bridge");
    } else load();
  };
}

function bridgeSizeLabel(pixels) {
  if (!pixels) return tr("原始文件");
  const [width, height] = pixels;
  const label = width === height && width % 1024 === 0 ? width / 1024 + "K · " : "";
  return label + width + "×" + height;
}

async function bridgeAssetDetails(asset, target) {
  const details = el("div", "bridge-specs");
  const feedback = el("p", "asset-feedback", "正在读取…");
  feedback.role = "status";
  details.append(feedback);
  target.append(details);
  try {
    const result = await rpc("bridgeDetails", { uid: asset.uid });
    if (!details.isConnected) return;
    if (!result.connected || !result.models.length) {
      feedback.textContent = tr(result.connected ? "此 Bridge 规格已变化，请重新打开详情" : "Bridge 素材库未连接");
      details.append(button("管理 Bridge 素材库", () => {
        $("#inspect").close();
        openBridgeLibrary();
      }));
      return;
    }

    const modelLabel = el("label", "field", asset.kind === "3d-model" ? "模型格式与 LOD" : "本地文件");
    const model = el("select");
    const preferred = selection(asset);
    for (const choice of result.models) {
      const label = [tr(choice.fmt), choice.lod == null ? "" : "LOD " + choice.lod, choice.name].filter(Boolean);
      const option = el("option", "name", label.join(" · "));
      option.value = choice.id;
      model.append(option);
    }
    const preferredModel = result.models.find(choice=>choice.modelTiers?.includes(preferred.modelTier) &&
      choice.sizes.some(size=>size.pixels&&Math.max(...size.pixels)===preferred.k*1024)) ||
      result.models.find(choice=>choice.modelTiers?.includes(preferred.modelTier));
    if(preferredModel)model.value=preferredModel.id;
    modelLabel.append(model);
    modelLabel.hidden = asset.kind !== "3d-model" && result.models.length === 1;
    const resolutionLabel = el("label", "field", "贴图分辨率");
    const resolution = el("select");
    const override = optionalRecipe(asset);
    resolutionLabel.append(resolution);
    model.onchange = () => {
      const previous = resolution.value;
      const sizes = result.models[model.selectedIndex].sizes;
      resolution.replaceChildren(...sizes.map(({ id, pixels, label }) => {
        const option = el("option", "name", [bridgeSizeLabel(pixels), label].filter(Boolean).join(" · "));
        option.value = id;
        return option;
      }));
      if (sizes.some(size => size.id === previous)) resolution.value = previous;
      else {
        const target=sizes.find(size=>size.pixels&&Math.max(...size.pixels)===preferred.k*1024);
        if(target)resolution.value=target.id;
      }
    };
    model.onchange();

    const controls = el("div", "controls");
    const submit = async op => {
      model.disabled = resolution.disabled = true;
      for (const control of controls.children) control.disabled = true;
      feedback.textContent = tr(op === "bridgePublish" ? "正在生成 Blender 资产…" : "正在准备 Bridge 文件…");
      try {
        const response = await rpc(op, { uid: asset.uid, model: model.value, size: resolution.value,recipe:override.read() });
        if (op === "bridgePublish") {
          const state = response.created ? "已加入 Blender 资产库" :
            response.located ? "已在 Blender 中定位" : "已在 Blender 资产库";
          feedback.textContent = tr(state) + " · " + response.library + " · " + response.title;
        } else {
          feedback.textContent = tr(response);
        }
      } catch (error) {
        feedback.textContent = tr(error.message);
      } finally {
        model.disabled = resolution.disabled = false;
        for (const control of controls.children) control.disabled = false;
      }
    };
    controls.append(button("导入 Blender", () => submit("bridgeImport"), "primary"));
    if (asset.kind !== "decal") controls.append(button("加入 Blender 资产库", () => submit("bridgePublish")));
    feedback.textContent = "";
    details.prepend(modelLabel, resolutionLabel, override.node, controls);
  } catch (error) {
    feedback.textContent = tr(error.message);
  }
}
