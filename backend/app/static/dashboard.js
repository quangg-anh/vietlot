"use strict";

const gameCode = document.body.dataset.game;
const element = id => document.getElementById(id);
const numberFormat = new Intl.NumberFormat("vi-VN");
const percentFormat = new Intl.NumberFormat("vi-VN", {style: "percent", maximumFractionDigits: 2});
const dateFormat = new Intl.DateTimeFormat("vi-VN", {day: "2-digit", month: "2-digit", year: "numeric", timeZone: "Asia/Ho_Chi_Minh"});
const timeFormat = new Intl.DateTimeFormat("vi-VN", {hour: "2-digit", minute: "2-digit", day: "2-digit", month: "2-digit", timeZone: "Asia/Ho_Chi_Minh"});
let apiKey = "";
let workspace = null;
let cursor = null;
let previousPages = [];
let refreshing = false;
let syncing = false;
let timer = null;

function node(tag, className, text) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== undefined) result.textContent = text;
  return result;
}

function showMessage(text, error = false) {
  const message = element("status-message");
  message.textContent = text;
  message.classList.toggle("error", error);
  message.hidden = !text;
}

async function request(path, options = {}) {
  const response = await fetch(path, {cache: "no-store", ...options});
  let body;
  try { body = await response.json(); }
  catch { throw new Error("Máy chủ chưa phản hồi đúng định dạng. Thử lại sau."); }
  if (!response.ok) {
    if (response.status === 401) {
      apiKey = "";
      throw new Error("API key chưa đúng. Kiểm tra lại key của ứng dụng.");
    }
    if (response.status === 503) throw new Error("Chưa cấu hình API_KEY trên máy chủ. Tự động sync vẫn cần worker đang chạy.");
    if (response.status === 429) throw new Error("Quá nhiều yêu cầu. Chờ một phút rồi thử lại.");
    throw new Error(typeof body.detail === "string" ? body.detail : "Không thể thực hiện yêu cầu. Thử lại sau.");
  }
  return body;
}

function balls(ticket) {
  const group = node("div", "balls");
  for (const value of ticket.main || []) group.append(node("span", "ball", String(value).padStart(2, "0")));
  for (const value of Array.isArray(ticket.digits) ? ticket.digits : []) group.append(node("span", "ball digit-ball", value));
  if (ticket.special !== null && ticket.special !== undefined) {
    const special = node("span", "ball special", String(ticket.special).padStart(2, "0"));
    special.title = "Số đặc biệt tham khảo; không phải số chính chọn trên vé";
    group.append(node("span", "ball-divider"), special);
  }
  return group;
}

function empty(container, title, description, small = false) {
  const state = node("div", `empty-state${small ? " small" : ""}`);
  if (!small) state.append(node("span", "empty-symbol", "∅"));
  state.append(node("h3", "", title), node("p", "", description));
  container.replaceChildren(state);
}

function emptyTable(body, columns, text) {
  const row = node("tr");
  const cell = node("td", "table-empty", text);
  cell.colSpan = columns;
  row.append(cell);
  body.replaceChildren(row);
}

function prizeLabel(group) {
  return ({first: "Giải nhất", second: "Giải nhì", third: "Giải ba", consolation: "Khuyến khích", special: "Đặc biệt"})[group] || group;
}

function renderLearning(data) {
  const container = element("model-learning");
  container.replaceChildren();
  const status = data.training?.status;
  if (status === "failed") container.append(node("p", "training-error", `Train lỗi: ${data.training.last_error}. Worker sẽ thử lại; model cũ giữ nguyên.`));
  if (status === "training" || status === "pending") container.append(node("p", "", "Đang học lại từ dữ liệu mới. Không thay đổi dự đoán đã lưu."));
  if (data.retraining_due && status !== "training" && status !== "failed") container.append(node("p", "", "Có dữ liệu hoặc cấu hình mới; chờ chu kỳ tự học lại tiếp theo."));
  if (!data.runner_enabled) container.append(node("p", "training-error", "Worker đang tắt: model không tự học lại cho đến khi bật worker."));
  if (!data.model) return;
  const model = data.model;
  const config = model.config?.name || "bayesian";
  container.append(node("p", "", `Model v${model.id} · ${model.training_count} kỳ · cửa sổ ${model.window} kỳ · ${config}. Tự học lại khi dữ liệu đủ điều kiện thay đổi; kiểm tra mỗi phút.`));
  if (data.kind === "digits") container.append(node("p", "", "Tách model từng nhóm giải; chọn lại giữa ngẫu nhiên, tần suất 60/300 kỳ và vị trí chữ số. Chỉ chọn bằng dữ liệu quá khứ; có thể giữ model ngẫu nhiên nếu model khác kém hơn."));
  if (data.model_versions?.length) container.append(node("p", "version-trail", `Phiên bản gần đây: ${data.model_versions.slice(0, 5).map(version => `v${version.id} (${version.training_count} kỳ)`).join(" · ")}`));
}

function renderDigitProbability(data) {
  const select = element("digit-group");
  const current = select.value || data.baseline.target_group;
  select.replaceChildren();
  for (const group of Object.keys(data.baseline.digit_groups)) {
    const option = node("option", "", prizeLabel(group));
    option.value = group;
    select.append(option);
  }
  select.value = Object.hasOwn(data.baseline.digit_groups, current) ? current : data.baseline.target_group;
  const group = select.value;
  element("odds-label").textContent = "MỘT GIÁ TRỊ 000–999 / MỘT VỊ TRÍ GIẢI";
  element("jackpot-odds").textContent = "1 / 1.000";
  element("baseline-label").textContent = "Mốc ngẫu nhiên: 0,1% / giá trị";
  element("probability-source").textContent = `${prizeLabel(group)} · ${data.model ? "Ước lượng model" : "Ngẫu nhiên"}`;
  const uniform = !data.model || data.model.config?.kind === "uniform";
  element("probability-explanation").textContent = `20 giá trị trên miền 000–999 của ${prizeLabel(group)}. Xác suất cho một giá trị giải, không phải xác suất trúng vé hay cặp số.${uniform ? " Model đồng đều: thứ tự hiển thị không ngụ ý số nào tốt hơn." : " Tần suất quá khứ không chứng minh lợi thế."}`;
  const ranked = data.model?.rankings?.[group] || Array.from({length: 20}, (_, index) => ({number: String(index).padStart(3, "0"), probability: 0.001}));
  const grid = element("probability-grid");
  if (!grid) return;
  grid.replaceChildren();
  for (const item of ranked) {
    const cell = node("div", "prob-cell");
    cell.append(node("strong", "", item.number), node("span", "", percentFormat.format(item.probability)));
    grid.append(cell);
  }
  const metrics = data.model?.metrics;
  element("model-evaluation").textContent = metrics ? `Walk-forward ${metrics.evaluated_draws} kỳ · Log loss ${metrics.logloss.toFixed(4)} / ngẫu nhiên ${metrics.baseline_logloss.toFixed(4)}; thấp hơn tốt hơn. Chọn cấu hình trước đoạn đánh giá. Đây là đánh giá cuốn chiếu, không phải holdout độc lập vĩnh viễn.` : "Cần hơn 10 kỳ để đánh giá walk-forward. Chưa tuyên bố lợi thế dự đoán.";
}

function renderPredictions(data) {
  const container = element("prediction-list");
  const upcoming = data.predictions.find(item => new Date(item.target_at) > new Date());
  const tickets = upcoming ? upcoming.payload.tickets : data.model?.tickets;
  element("prediction-badge").textContent = upcoming ? `Đã lưu · kỳ #${upcoming.target_draw_id}` : data.model ? "Bản tham khảo · chưa khóa" : "Chờ dữ liệu";
  if (!data.supported) {
    empty(container, "Chưa hỗ trợ model", "Trò chơi chưa được cấu hình model.");
    return;
  }
  if (!tickets) {
    empty(container, data.usable_count ? "Đang tự cập nhật model." : "Bắt đầu từ dữ liệu thật.", data.usable_count ? `${numberFormat.format(data.usable_count)} kỳ đã sẵn sàng. Worker sẽ tự train, không cần duyệt tay.` : "Nhấn Sync để tải lịch sử. Kết quả crawl tự vào model, không cần duyệt tay.");
  } else {
    container.replaceChildren();
    tickets.forEach((ticket, index) => {
      const row = node("div", "ticket-row");
      row.append(node("span", "ticket-index", String(index + 1).padStart(2, "0")), balls(ticket), node("span", "ticket-type", upcoming ? "ĐÃ LƯU" : "THAM KHẢO"));
      container.append(row);
    });
  }
  element("prediction-description").textContent = upcoming ? `Dự đoán kỳ #${upcoming.target_draw_id} · ${timeFormat.format(new Date(upcoming.target_at))}. Đã lưu trước giờ quay, không sửa lại.` : "5 bộ số tham khảo từ model. Không phải các tổ hợp có xác suất trúng cao hơn đã được chứng minh.";
  if (data.kind === "digits") {
    element("prediction-description").textContent += ` Nhóm đối chiếu: ${prizeLabel(data.baseline.target_group)}. Số khớp không phải kết luận trúng giải hay tiền thưởng.`;
  }
  element("prediction-footnote").textContent = data.forecast?.status === "waiting_previous_result"
    ? `Chưa khóa dự đoán: thiếu kết quả kỳ ${timeFormat.format(new Date(data.forecast.required_previous_at))}. Bản tham khảo không được chấm hồi tố.`
    : "Tự lưu trước giờ quay khi đủ kết quả kỳ trước; kết quả mới vào lần học lại tiếp theo. Không sửa dự đoán cũ.";
}

function renderHistory(data) {
  const body = element("results-body");
  body.replaceChildren();
  if (!data.history.length) emptyTable(body, 5, "Chưa có kết quả. Sync sẽ tải dữ liệu nguồn; không dùng kết quả mẫu.");
  for (const draw of data.history) {
    const row = node("tr");
    const result = node("td");
    if (draw.main.length) result.append(balls(draw));
    else {
      for (const [group, values] of Object.entries(draw.digits || {})) {
        result.append(node("div", "digit-prize", `${prizeLabel(group)}: ${values.join(" · ")}`));
      }
    }
    const scored = draw.predictions.filter(item => item.score);
    const hits = scored.flatMap(item => item.score.hits || []);
    const status = node("td");
    status.append(node("span", `data-label${draw.verified || draw.crawled ? "" : " pending"}`, draw.verified ? "Đã đối chiếu" : draw.crawled ? "Crawl tự động" : "CSV chưa duyệt"));
    row.append(node("td", "draw-id", `#${draw.draw_id}`), node("td", "muted", `${dateFormat.format(new Date(`${draw.draw_date}T12:00:00+07:00`))} ${draw.draw_slot}`), result, node("td", hits.length ? "" : "muted", hits.length ? `${Math.max(...hits)} / ${data.baseline?.pick_count} số` : "Chưa có điểm"), status);
    body.append(row);
  }
  element("history-count").textContent = `${data.history.length} kỳ trên trang · ${numberFormat.format(data.counts.total)} kỳ đã lưu`;
  element("newer-page").disabled = !previousPages.length;
  element("older-page").disabled = !data.next_before;
  const predictions = element("predictions-body");
  predictions.replaceChildren();
  if (!data.predictions.length) emptyTable(predictions, 5, "Chưa có dự đoán đã lưu. Bản tham khảo không được chấm điểm hồi tố.");
  for (const prediction of data.predictions) {
    prediction.payload.tickets.forEach((ticket, index) => {
      const row = node("tr");
      const numbers = node("td");
      numbers.append(balls(ticket));
      const state = node("td");
      state.append(node("span", `data-label${prediction.score ? "" : " pending"}`, prediction.score ? (prediction.score.source_verified === false ? "Đã chấm · nguồn crawl" : "Đã đối chiếu") : "Chờ kết quả"));
      row.append(node("td", "draw-id", `#${prediction.target_draw_id} · bộ ${index + 1}`), node("td", "muted", timeFormat.format(new Date(prediction.locked_at))), numbers, node("td", "", prediction.score ? `${prediction.score.hits[index]} / ${prediction.score.ticket_width || data.baseline?.pick_count}` : "—"), state);
      predictions.append(row);
    });
  }
}

function renderProbability(data) {
  const grid = element("probability-grid");
  if (!grid) return;
  element("digit-group-control").hidden = data.kind !== "digits";
  if (data.kind === "digits" && data.baseline) {
    renderDigitProbability(data);
    return;
  }
  element("odds-label").textContent = data.game === "lotto535" ? "5 SỐ CHÍNH VÀ SỐ ĐẶC BIỆT / MỘT BỘ" : "TRÚNG ĐỦ 6 SỐ CHÍNH / MỘT BỘ";
  if (!data.baseline) {
    empty(grid, "Chưa có mô hình xác suất", "Trò chơi này chưa hỗ trợ mô hình chọn 6 số.", true);
    element("model-evaluation").textContent = "Không hiển thị ước lượng khi cấu hình chưa được hỗ trợ.";
    return;
  }
  element("jackpot-odds").textContent = `1 / ${numberFormat.format(data.baseline.combinations)}`;
  element("baseline-label").textContent = `Mốc ngẫu nhiên: ${percentFormat.format(data.baseline.number_probability)} / số`;
  const probabilities = data.model?.probabilities || Array(data.baseline.number_count).fill(data.baseline.number_probability);
  element("probability-source").replaceChildren(node("i"), document.createTextNode(data.model ? "Ước lượng model" : "Xác suất lý thuyết"));
  element("probability-explanation").textContent = data.model ? `Mỗi ô là ước lượng một số nằm trong ${data.baseline.pick_count} số chính, không phải xác suất trúng bộ số. Sai lệch tần suất lịch sử không chứng minh lợi thế dự đoán.` : "Chưa có model được train. Các ô hiện hiển thị xác suất lý thuyết đồng đều, không phải dự đoán được huấn luyện.";
  grid.replaceChildren();
  probabilities.forEach((value, index) => {
    const cell = node("div", "prob-cell");
    const meter = document.createElement("meter");
    meter.min = 0;
    meter.max = 1;
    meter.value = value;
    meter.setAttribute("aria-label", `Số ${index + 1}: ${percentFormat.format(value)}`);
    cell.append(node("strong", "", String(index + 1).padStart(2, "0")), node("span", "", percentFormat.format(value)), meter);
    grid.append(cell);
  });
  const metrics = data.model?.metrics;
  element("model-evaluation").textContent = metrics ? `Tự đánh giá walk-forward ${metrics.evaluated_draws} kỳ · Brier model ${metrics.brier.toFixed(4)} / ngẫu nhiên ${metrics.baseline_brier.toFixed(4)} (thấp hơn tốt hơn). Đánh giá thăm dò, chưa có holdout độc lập; không tự tuyên bố vượt ngẫu nhiên.` : "Tự đánh giá walk-forward khi có hơn 10 kỳ dữ liệu. Hiện chưa đủ dữ liệu đánh giá; không hiển thị tỷ lệ thắng giả.";
}

function render(data) {
  workspace = data;
  const active = ["queued", "running"].includes(data.job?.status);
  const automatic = Boolean(data.sync?.scheduled && data.runner_enabled);
  element("auto-status").classList.toggle("paused", !automatic);
  element("auto-status").replaceChildren(node("span", "dot"), document.createTextNode(automatic ? "Tự động sync đang bật" : !data.crawl_enabled ? "Chưa hỗ trợ sync" : "Tự động sync đang tắt"));
  element("stat-total").textContent = numberFormat.format(data.counts.total);
  element("stat-verified").textContent = numberFormat.format(data.usable_count);
  element("stat-pending").textContent = "Tự nhận dữ liệu crawl · không duyệt tay";
  element("stat-model").textContent = !data.supported ? "Chưa hỗ trợ" : data.training?.status === "failed" ? "Lỗi train" : data.training?.status === "training" ? "Đang học lại" : data.model ? (data.kind === "digits" ? "Model 3D" : "Bayesian") : !data.runner_enabled ? "Worker tắt" : data.usable_count ? "Chờ train" : "Chờ dữ liệu";
  element("model-caption").textContent = !data.supported ? "Trò chơi chưa có model" : data.model ? `v${data.model.id} · ${data.model.training_count} kỳ train · ${timeFormat.format(new Date(data.model.created_at))}` : "Tự train sau khi Sync có kết quả";
  element("stat-next").textContent = data.next_at ? timeFormat.format(new Date(data.next_at)) : "Chưa hỗ trợ";
  element("next-caption").textContent = "Theo lịch cấu hình của trò chơi";
  element("last-sync").textContent = data.sync?.last_success_at ? `Sync gần nhất: ${timeFormat.format(new Date(data.sync.last_success_at))}` : "Chưa có lần sync thành công";
  const button = element("sync-button");
  button.disabled = !data.crawl_enabled || active || syncing;
  button.setAttribute("aria-busy", String(active || syncing));
  button.querySelector("span").textContent = active ? "Đang đồng bộ…" : "Sync kết quả";
  element("pipeline-sync").textContent = active ? "Đang tải kết quả nguồn" : automatic ? (data.sync.history_complete ? "Sau giờ quay 45 và 60 phút" : "Đang bổ sung lịch sử tự động") : "Chưa chạy lịch tự động";
  element("pipeline-train").textContent = !data.supported ? "Trò chơi chưa hỗ trợ train" : !data.runner_enabled ? "Bật worker để tự train" : data.model ? `Đã train đến kỳ #${data.model.trained_through}` : "Chờ kết quả từ Sync";
  element("pipeline-score").textContent = !data.supported ? "Chưa có dự đoán tự động" : "Tự lưu dự đoán · tự chấm khi có kết quả";
  element("sync-light").classList.toggle("ready", automatic);
  element("train-light").classList.toggle("ready", Boolean(data.model && data.runner_enabled));
  element("score-light").classList.toggle("ready", data.supported && data.runner_enabled);
  element("automation-note").textContent = "Kết quả crawl được dùng trực tiếp cho model, có lưu nguồn. Đây là dữ liệu một nguồn, không phải đối chiếu độc lập.";
  if (data.job?.status === "failed") showMessage(`Lần sync gần nhất chưa hoàn tất: ${data.job.error || "Lỗi nguồn dữ liệu"}. Hệ thống giữ tiến độ và thử lại theo lịch.`, true);
  else if (active) showMessage(data.runner_enabled ? "Đang đồng bộ ở nền. Có thể tiếp tục xem bảng; kết quả tự cập nhật." : "Yêu cầu đã xếp hàng nhưng worker đang tắt (DISABLE_RUNNER=1). Cần bật worker để xử lý.");
  else if (!syncing) showMessage("");
  renderLearning(data);
  renderPredictions(data);
  renderHistory(data);
  renderProbability(data);
  return active;
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  let active = false;
  try { active = render(await request(`/api/games/${gameCode}/workspace${cursor ? `?before=${cursor}` : ""}`)); }
  catch (error) { showMessage(`Không thể cập nhật bảng: ${error.message}`, true); }
  finally {
    refreshing = false;
    clearTimeout(timer);
    if (!document.hidden) timer = setTimeout(refresh, active ? 4000 : 20000);
  }
}

async function sync() {
  if (syncing) return;
  syncing = true;
  element("sync-button").disabled = true;
  element("sync-button").setAttribute("aria-busy", "true");
  try {
    await request(`/api/games/${gameCode}/sync`, {method: "POST", headers: {"X-API-Key": apiKey}});
    element("auth-dialog").close();
    element("api-key").value = "";
    cursor = null;
    previousPages = [];
    showMessage("Đã xếp hàng sync. Tự động sync cũng được bật; model tự train và cập nhật dự đoán, không cần duyệt tay.");
  } finally {
    syncing = false;
    element("sync-button").disabled = !workspace?.crawl_enabled;
    element("sync-button").setAttribute("aria-busy", "false");
    await refresh();
  }
}

element("sync-button").addEventListener("click", async () => {
  if (!apiKey) {
    element("auth-error").textContent = "";
    element("auth-dialog").showModal();
    return;
  }
  try { await sync(); }
  catch (error) { showMessage(error.message, true); }
});

element("auth-form").addEventListener("submit", async event => {
  event.preventDefault();
  apiKey = element("api-key").value.trim();
  if (!apiKey) return;
  const submit = event.currentTarget.querySelector('[type="submit"]');
  submit.disabled = true;
  element("auth-error").textContent = "";
  try { await sync(); }
  catch (error) { element("auth-error").textContent = error.message; }
  finally { submit.disabled = false; }
});
element("auth-cancel").addEventListener("click", () => element("auth-dialog").close());
element("auth-dialog").addEventListener("close", () => { element("api-key").value = ""; });

const tabs = [element("results-tab"), element("predictions-tab")];
function selectTab(index) {
  tabs.forEach((tab, position) => {
    tab.setAttribute("aria-selected", String(position === index));
    tab.tabIndex = position === index ? 0 : -1;
    element(tab.getAttribute("aria-controls")).hidden = position !== index;
  });
}
tabs.forEach((tab, index) => {
  tab.addEventListener("click", () => selectTab(index));
  tab.addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? 1 : 1 - index;
    selectTab(next);
    tabs[next].focus();
  });
});
element("older-page").addEventListener("click", async () => {
  if (!workspace?.next_before || refreshing) return;
  previousPages.push(cursor);
  cursor = workspace.next_before;
  await refresh();
});
element("newer-page").addEventListener("click", async () => {
  if (!previousPages.length || refreshing) return;
  cursor = previousPages.pop();
  await refresh();
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) clearTimeout(timer);
  else refresh();
});
refresh();

element("digit-group")?.addEventListener("change", () => { if (workspace) renderProbability(workspace); });
