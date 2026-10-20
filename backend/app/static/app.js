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

async function busy(button, action) {
  button.disabled = true;
  show("Đang xử lý…");
  try { await action(); } catch (error) { show(error.message, true); }
  finally { button.disabled = false; }
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
