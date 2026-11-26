"""Drive the real gallery HTML in WebView2 using a read-only synthetic Fab library."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
from fab_shelf import catalog, gallery

UI_SCRIPT = """
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
  return {cards: uids.length, cachePreview: true, earlyBatch: true, retry: true, silentHeadChange: true,
          selection: true, staleSearch: true, refreshedCursor:true, accountWait:true, coverLimit:true,
          classification};
}
runFixture().then(result => chrome.webview.postMessage({op:"verifyFeedUi",report:result}),
                  error => chrome.webview.postMessage({op:"verifyFeedUi",error:error.message}));
"""


def verify_feed_ui(root):
    runtime = REPO / "fab_shelf" / "runtime"
    host = root / "host"
    shutil.copytree(runtime / "gallery", host / "gallery")
    for name in ("Gallery091.exe", "Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll", "WebView2Loader.dll"):
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
        nonlocal reordered
        if query["op"] == "verifyFeedUi":
            catalog.atomic_json(report, query)
            return True
        if query["op"] == "verifyLibraryContentChange":
            reordered = reordered or query.get("reorder") is True
            with app.lock:
                app.library_content_revision += 1
                app.page_cache.clear()
                app.library_head_checked = time.monotonic()
            return True
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
