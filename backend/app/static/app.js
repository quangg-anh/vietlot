"use strict";
const game = document.body.dataset.game;
const output = document.getElementById("result");
const key = document.getElementById("api-key");

function show(value, error = false) {
  output.classList.toggle("error", error);
  output.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

async function api(path, body, multipart = false, method = "POST") {
  const headers = {};
  if (key.value) headers["X-API-Key"] = key.value;
  if (method !== "GET") {
    if (!key.value) throw new Error("Nhập API key trước khi thao tác.");
    headers["X-API-Key"] = key.value;
    if (!multipart) headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, {method, headers, body: method === "GET" ? undefined : multipart ? body : JSON.stringify(body)});
  let result;
  try { result = await response.json(); } catch { throw new Error(`Máy chủ trả HTTP ${response.status}.`); }
  if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : JSON.stringify(result.detail));
  return result;
}

async function waitForJob(identity, report) {
  for (let attempt = 0; attempt < 150; attempt++) {
    await new Promise(resolve => setTimeout(resolve, 2000));
    const job = await api(`/api/jobs/${identity}`, null, false, "GET");
    report(job.result || {job_id: identity, status: job.status});
    if (job.status === "failed") throw new Error(job.error);
    if (job.status === "done") return job.result;
  }
  throw new Error(`Job #${identity} còn đang chạy. Xem /api/jobs/${identity}.`);
}

async function busy(button, action) {
  button.disabled = true;
  show("Đang xử lý…");
  try { await action(); } catch (error) { show(error.message, true); }
  finally { button.disabled = false; }
}

if (document.getElementById("crawl-panel")) {
  const crawlOutput = document.getElementById("crawl-status");
  const pendingTable = document.getElementById("pending-draws");
  const report = value => { crawlOutput.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2); };
  async function crawlAction(button, action) {
    button.disabled = true;
    report("Đang xử lý…");
    try { await action(); } catch (error) { report(error.message); }
    finally { button.disabled = false; }
  }
  async function refreshPending() {
    const status = await api(`/api/games/${game}/crawl/status`, null, false, "GET");
    report(status);
    const data = await api(`/api/games/${game}/draws/pending`, null, false, "GET");
    pendingTable.replaceChildren();
    for (const draw of data.items) {
      const row = document.createElement("tr");
      const selection = document.createElement("td");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.name = "review-draw";
      checkbox.value = draw.draw_id;
      checkbox.dataset.fingerprint = draw.fingerprint;
      checkbox.setAttribute("aria-label", `Xác minh kỳ ${draw.draw_id}`);
      selection.append(checkbox);
      row.append(selection);
      for (const value of [`#${draw.draw_id} / ${draw.draw_date}`, draw.main.map(number => String(number).padStart(2, "0")).join(" ") + (draw.special === null ? "" : ` + ${draw.special}`)]) {
        const cell = document.createElement("td");
        cell.textContent = value;
        row.append(cell);
      }
      const source = document.createElement("td");
      const link = document.createElement("a");
      link.href = draw.source_url;
      link.textContent = "Đối chiếu ↗";
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      source.append(link);
      row.append(source);
      pendingTable.append(row);
    }
    if (!data.items.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 4;
      cell.textContent = "Không có kỳ crawl đang chờ xác minh.";
      row.append(cell);
      pendingTable.append(row);
    }
  }
  document.getElementById("crawl-form").addEventListener("submit", event => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    crawlAction(event.currentTarget.querySelector("button"), async () => {
      const queued = await api(`/api/games/${game}/crawl`, {mode: form.get("mode"), pages: Number(form.get("pages"))});
      report(queued);
      await waitForJob(queued.job_id, report);
      await refreshPending();
    });
  });
  for (const [identity, enabled] of [["crawl-enable", true], ["crawl-disable", false]]) {
    document.getElementById(identity).addEventListener("click", event => crawlAction(event.currentTarget, async () => {
      await api(`/api/games/${game}/crawl/schedule`, {enabled});
      await refreshPending();
    }));
  }
  document.getElementById("crawl-refresh").addEventListener("click", event => crawlAction(event.currentTarget, refreshPending));
  document.getElementById("verify-form").addEventListener("submit", event => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const draws = [...pendingTable.querySelectorAll("input[name='review-draw']:checked")].map(input => ({draw_id: Number(input.value), fingerprint: input.dataset.fingerprint}));
    crawlAction(event.currentTarget.querySelector("button"), async () => {
      if (!draws.length) throw new Error("Chọn ít nhất một kỳ đã đối chiếu.");
      await api(`/api/games/${game}/draws/verify`, {draws, checked_source_url: form.get("checked_source_url"), note: form.get("note"), acknowledged: form.has("acknowledged")});
      await refreshPending();
      show("Đã xác minh. Có thể chạy backtest; tải lại trang để cập nhật bảng lịch sử.");
    });
  });
}

document.getElementById("backtest-form").addEventListener("submit", event => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  busy(event.currentTarget.querySelector("button"), async () => {
    const queued = await api("/api/backtest", {game, method: form.get("method"), min_train: Number(form.get("min_train")), test_draws: Number(form.get("test_draws"))});
    show(`Job #${queued.job_id} đã xếp hàng.`);
    for (let attempt = 0; attempt < 120; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 2000));
      const job = await api(`/api/jobs/${queued.job_id}`, null, false, "GET");
      if (job.status === "failed") throw new Error(job.error);
      if (job.status === "done") {
        const {per_draw, ...summary} = job.result;
        show(summary);
        return;
      }
    }
    show(`Job #${queued.job_id} còn đang chạy. Xem /api/jobs/${queued.job_id}.`);
  });
});

document.getElementById("import-form").addEventListener("submit", event => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  busy(event.currentTarget.querySelector("button"), async () => {
    const result = await api("/api/import/csv", form, true);
    show({...result, message: "Đã nhập thành công. Tải lại trang để xem dữ liệu mới."});
  });
});

for (const [identity, path] of [
  ["randomness", `/api/games/${game}/randomness`],
  ["illusion", "/api/simulator/illusion"],
  ["freeze", "/api/datasets/freeze"]
]) {
  document.getElementById(identity).addEventListener("click", event => {
    busy(event.currentTarget, async () => show(await api(path, {game})));
  });
}
