"use client";
import { useEffect, useState } from "react";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from "recharts";

const API = process.env.NEXT_PUBLIC_API_BASE || "/api";
export default function Home() {
  const [summary, setSummary] = useState(null);
  const [draws, setDraws] = useState([]);
  const [method, setMethod] = useState("bayesian");
  const [pred, setPred] = useState(null);
  const [error, setError] = useState("");
  async function load() {
    try {
      const [s, d] = await Promise.all([fetch(`${API}/summary`), fetch(`${API}/draws?limit=10`)]);
      if (!s.ok || !d.ok) throw new Error("Không tải được API");
      setSummary(await s.json()); setDraws(await d.json()); setError("");
    } catch (e) { setError(e.message + ". Kiểm tra API và dữ liệu đã import."); }
  }
  async function generate() {
    try {
      const r = await fetch(`${API}/predictions?method=${method}&count=5`);
      const j = await r.json(); if (!r.ok) throw new Error(j.detail || "Lỗi tạo bộ số");
      setPred(j);
    } catch (e) { setError(e.message); }
  }
  useEffect(() => { load(); }, []);
  return <main className="wrap">
    <header><div><p className="eyebrow">RESEARCH LAB · POWER 6/55</p><h1>Phân tích xác suất</h1><p className="sub">Dữ liệu, kiểm định và backtest — không phải công cụ đảm bảo trúng thưởng.</p></div><button onClick={load}>Làm mới</button></header>
    {error && <div className="error">{error}</div>}
    <section className="cards">
      <article><span>Kỳ đã lưu</span><strong>{summary?.draw_count ?? "—"}</strong></article>
      <article><span>Kỳ xác minh</span><strong>{summary?.verified_count ?? "—"}</strong></article>
      <article><span>Kỳ mới nhất</span><strong>{summary?.latest_date ?? "Chưa có dữ liệu"}</strong></article>
    </section>
    <section className="panel"><h2>Tần suất 55 số</h2>
      {summary?.frequency && <div className="chart"><ResponsiveContainer width="100%" height="100%"><BarChart data={summary.frequency}><XAxis dataKey="number" tick={{fontSize:11}}/><YAxis allowDecimals={false}/><Tooltip/><Bar dataKey="count" fill="#4f46e5" /></BarChart></ResponsiveContainer></div>}
      <p className="muted">Tần suất lịch sử mô tả quá khứ, không chứng minh số nào “sắp về”.</p>
    </section>
    <section className="panel"><h2>Bộ số tham khảo</h2><div className="controls"><select value={method} onChange={e=>setMethod(e.target.value)}><option value="uniform">Baseline ngẫu nhiên</option><option value="bayesian">Bayesian làm trơn</option><option value="xgboost">XGBoost thử nghiệm</option></select><button onClick={generate}>Tạo 5 bộ số</button></div>
      {pred && <><p className="muted">Phương pháp: {pred.method} · dữ liệu huấn luyện: {pred.training_draws} kỳ</p><div className="sets">{pred.sets.map((set,i)=><div className="set" key={i}><span>Bộ {i+1}</span>{set.map(n=><b key={n}>{String(n).padStart(2,"0")}</b>)}</div>)}</div><p className="warning">{pred.warning}</p></>}
    </section>
    <section className="panel"><h2>10 kỳ gần nhất đã lưu</h2><div className="tablewrap"><table><thead><tr><th>Kỳ</th><th>Ngày</th><th>Sáu số chính</th><th>ĐB</th><th>Nguồn</th></tr></thead><tbody>{draws.map(d=><tr key={d.draw_id}><td>{d.draw_id}</td><td>{d.draw_date}</td><td>{d.numbers.map(n=>String(n).padStart(2,"0")).join(" · ")}</td><td>{d.special}</td><td>{d.verified?"Đã đánh dấu xác minh":"Chưa xác minh"}</td></tr>)}</tbody></table></div></section>
    <footer>Chỉ phục vụ nghiên cứu thống kê. Chơi có trách nhiệm; không vay tiền để mua vé.</footer>
  </main>;
}
