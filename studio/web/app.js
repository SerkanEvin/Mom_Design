import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

const $ = (s, el = document) => el.querySelector(s);
const app = $("#app");
let ME = null;
let activeViewer = null;
let pollTimer = null;

// ------------------------------------------------------------------ helpers
function h(html) { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; }
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function money(x) { return (x ?? 0).toLocaleString("tr-TR", { minimumFractionDigits: 2, maximumFractionDigits: 3 }) + " $"; }
function when(t) { return new Date(t * 1000).toLocaleString("tr-TR", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }); }
function toast(msg, ms = 3200) { const t = $("#toast"); t.textContent = msg; t.hidden = false; clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), ms); }

async function api(path, opts = {}) {
  const init = { method: opts.method || (opts.body || opts.form ? "POST" : "GET"), headers: { "X-Studio": "1" }, credentials: "same-origin" };
  if (opts.form) init.body = opts.form;
  else if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, init);
  if (r.status === 401 && !opts.noAuthRedirect) { ME = null; showLogin(); throw new Error("Giriş gerekli"); }
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.error) || `Hata ${r.status}`);
  return data;
}

function stopAll() {
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  if (activeViewer) { activeViewer.dispose(); activeViewer = null; }
}

// ------------------------------------------------------------------ 3D viewer
class Viewer {
  constructor(el) {
    this.el = el;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    el.appendChild(this.renderer.domElement);
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(35, 1, 0.01, 1000);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x8a7a6a, 1.6));
    const d = new THREE.DirectionalLight(0xffffff, 1.6); d.position.set(2, 3, 4); this.scene.add(d);
    const d2 = new THREE.DirectionalLight(0xffffff, 0.6); d2.position.set(-3, 1, -2); this.scene.add(d2);
    this.root = new THREE.Group(); this.scene.add(this.root);
    this.ro = new ResizeObserver(() => this.resize()); this.ro.observe(el);
    this.resize();
    const loop = () => { this.raf = requestAnimationFrame(loop); this.controls.update(); this.renderer.render(this.scene, this.camera); };
    loop();
  }
  resize() { const w = this.el.clientWidth, hh = this.el.clientHeight || w * 0.75; this.renderer.setSize(w, hh, false); this.camera.aspect = w / hh; this.camera.updateProjectionMatrix(); }
  clear() { while (this.root.children.length) this.root.remove(this.root.children[0]); }
  async load(url) {
    const gltf = await new GLTFLoader().loadAsync(url);
    this.clear(); this.root.add(gltf.scene); this.fit(gltf.scene); return gltf.scene;
  }
  fit(obj) {
    const box = new THREE.Box3().setFromObject(obj); const size = box.getSize(new THREE.Vector3()); const c = box.getCenter(new THREE.Vector3());
    const r = Math.max(size.x, size.y, size.z) || 1;
    obj.position.sub(c); // centre on origin
    this.camera.near = r / 100; this.camera.far = r * 100; this.camera.updateProjectionMatrix();
    this.camera.position.set(r * 1.4, r * 0.9, r * 1.8); this.controls.target.set(0, 0, 0); this.controls.update();
  }
  snapshot() { this.renderer.render(this.scene, this.camera); return this.renderer.domElement.toDataURL("image/png"); }
  dispose() { cancelAnimationFrame(this.raf); this.ro.disconnect(); this.controls.dispose(); this.renderer.dispose(); this.el.innerHTML = ""; }
}

function viewerBox(hint = "Döndür: sürükle · Yakınlaş: iki parmak / tekerlek") {
  return h(`<div class="viewer"><div class="loading">Model yükleniyor…</div><div class="hint">${esc(hint)}</div></div>`);
}

// ------------------------------------------------------------------ login
function showLogin(msg = "") {
  stopAll();
  $("#top").hidden = true;
  app.innerHTML = "";
  const el = h(`<section class="login card stack">
    <span class="logo" aria-hidden="true"></span>
    <h1>Tasarım Atölyesi</h1>
    <p class="muted">Fotoğraftan 3B model yap, düzenle ve Bambu yazıcıda renkli bas.</p>
    <form class="stack" id="lf">
      <label class="field"><span>Giriş anahtarı</span><input type="password" id="key" autocomplete="current-password" placeholder="md_…" required></label>
      <button type="submit">Giriş yap</button>
      <p class="small muted" id="lmsg">${esc(msg)}</p>
    </form></section>`);
  app.append(el);
  $("#lf").onsubmit = async e => { e.preventDefault(); await doLogin($("#key").value); };
}

async function doLogin(key) {
  try { await api("/api/login", { body: { key }, noAuthRedirect: true }); await boot(); }
  catch (e) { showLogin(e.message); }
}

async function refreshMe() {
  ME = await api("/api/me");
  const b = ME.balance;
  $("#credit").textContent = ME.user.unlimited ? "Sınırsız" : `Kredi ${money(b.available)}`;
  $("#nav-admin").hidden = ME.user.role !== "owner";
  drawUsage();
}

// header gauge: ring = the most used of credit / this hour's AI calls / today's limit
function usageRows() {
  const u = ME.usage || {}; const rows = [];
  const c = u.credit || {};
  rows.push(c.unlimited ? { label: "Kredi", text: `Sınırsız · harcanan ${money(c.used)}`, pct: null }
    : { label: "Kredi", text: `${money(c.used)} / ${money(c.granted)} kullanıldı`, pct: c.pct || 0,
        hint: "Bitince Serkan'ın hesabından otomatik +5 $ yüklenir; sen ödemezsin." });
  const a = u.ai_hour || {};
  rows.push(a.limit ? { label: "Yapay zekâ (bu saat)", text: `${a.used} / ${a.limit} çağrı`, pct: Math.min(1, a.used / a.limit),
    hint: "Saatlik sınır dolarsa biraz bekleyip tekrar dene." } : { label: "Yapay zekâ (bu saat)", text: `${a.used} çağrı · sınırsız`, pct: null });
  const t = u.today || {};
  rows.push({ label: "Bugünkü üretim", text: `${money(t.all)} / ${money(t.cap)} günlük sınır`, pct: t.cap ? Math.min(1, t.all / t.cap) : null,
    hint: ME.user.role === "owner" ? "Tüm kullanıcılar toplamı." : `Senin payın ${money(t.mine)}.` });
  return rows;
}
function level(p) { return p >= 0.9 ? "bad" : p >= 0.7 ? "warn" : "ok"; }
function drawUsage() {
  if (!ME) return;
  const rows = usageRows(); const top = Math.max(0, ...rows.map(r => r.pct ?? 0));
  const ring = $("#gring"); ring.setAttribute("stroke-dasharray", `${Math.max(2, Math.round(top * 100))} 100`);
  $("#usage").dataset.level = level(top);
  $("#usage").title = rows.map(r => `${r.label}: ${r.text}`).join("\n");
  const pop = $("#usagepop"); pop.innerHTML = `<b>Kullanım</b>`;
  rows.forEach(r => pop.append(h(`<div class="urow"><div class="row small"><span class="grow">${esc(r.label)}</span><b>${esc(r.text)}</b></div>
    ${r.pct === null ? "" : `<div class="bar ${level(r.pct)}"><i style="width:${Math.round(r.pct * 100)}%"></i></div>`}
    ${r.hint ? `<div class="small muted">${esc(r.hint)}</div>` : ""}</div>`)));
}
$("#usage").onclick = async e => {
  e.stopPropagation(); const pop = $("#usagepop"); const open = pop.hidden;
  if (open) { try { await refreshMe(); } catch {} }
  pop.hidden = !open; $("#usage").setAttribute("aria-expanded", String(open));
};
document.addEventListener("click", e => { if (!e.target.closest("#usagepop")) { $("#usagepop").hidden = true; $("#usage").setAttribute("aria-expanded", "false"); } });

// ------------------------------------------------------------------ effort (Orta / Yüksek / Ekstra)
const EFFORT_HINT = {
  mid: "Daha hızlı: daha az deneme ve kontrol turu.",
  high: "Dengeli, önerilen: her adım bir kez kontrol edilir, gerekirse düzeltilir.",
  extra: "En dikkatli: daha çok düzeltme turu, üstten görünüş ve keskin doku. Daha uzun sürer, biraz daha pahalı.",
};
function savedEffort() { try { return localStorage.getItem("effort") || "high"; } catch { return "high"; } }
function effortPicker(onChange) {
  const el = h(`<div class="field"><span class="flabel">Çaba</span><div class="seg effort" role="radiogroup" aria-label="Çaba"></div><div class="small muted ehint"></div></div>`);
  el.value = savedEffort();
  const seg = $(".seg", el);
  (ME.efforts || [{ name: "mid", label: "Orta" }, { name: "high", label: "Yüksek" }, { name: "extra", label: "Ekstra" }]).forEach(x => {
    const b = h(`<button type="button" role="radio" data-e="${x.name}">${esc(x.label)}</button>`);
    b.onclick = () => { el.value = x.name; try { localStorage.setItem("effort", x.name); } catch {} sync(); onChange && onChange(x.name); };
    seg.append(b);
  });
  const sync = () => { seg.querySelectorAll("button").forEach(b => { const on = b.dataset.e === el.value; b.classList.toggle("on", on); b.setAttribute("aria-checked", String(on)); }); $(".ehint", el).textContent = EFFORT_HINT[el.value] || ""; };
  sync();
  return el;
}

// ------------------------------------------------------------------ router
const routes = [
  [/^#?\/?$/, libraryPage], [/^#\/new$/, newPage], [/^#\/new\/photo$/, photoPage], [/^#\/new\/multi$/, multiPage],
  [/^#\/new\/file$/, filePage], [/^#\/job\/([\w]+)$/, jobPage], [/^#\/a\/([\w]+)$/, assetPage],
  [/^#\/a\/([\w]+)\/print$/, printPage], [/^#\/a\/([\w]+)\/edit$/, editPage], [/^#\/admin$/, adminPage],
  [/^#\/draw$/, () => drawingPage(null)], [/^#\/d\/([\w]+)$/, drawingPage],
];
async function route() {
  if (!ME) return;
  stopAll();
  const hash = location.hash || "#/";
  document.querySelectorAll(".topnav a").forEach(a => a.classList.toggle("on",
    (a.dataset.nav === "lib" && (hash === "#/" || hash.startsWith("#/a/"))) || (a.dataset.nav === "new" && hash.startsWith("#/new")) || (a.dataset.nav === "admin" && hash === "#/admin") ||
    (a.dataset.nav === "draw" && (hash === "#/draw" || hash.startsWith("#/d/")))));
  for (const [re, fn] of routes) {
    const m = hash.match(re);
    if (m) { app.innerHTML = ""; try { await fn(...m.slice(1)); } catch (e) { app.append(h(`<div class="card">Hata: ${esc(e.message)}</div>`)); } return; }
  }
  location.hash = "#/";
}
window.addEventListener("hashchange", () => { if (!takeKeyFromUrl()) route(); });
function takeKeyFromUrl() {
  const m = location.hash.match(/key=([\w\-]+)/);
  if (!m) return false;
  history.replaceState(null, "", location.pathname + "#/");
  doLogin(m[1]);
  return true;
}

// ------------------------------------------------------------------ library
async function libraryPage() {
  const all = ME.user.role === "owner" ? 1 : 0;
  const [{ assets }, { jobs }, { drawings }] = await Promise.all([api(`/api/assets?all=${all}`), api("/api/jobs?active=1"), api(`/api/drawings?all=${all}`)]);
  const el = h(`<section class="stack">
    <div class="row"><h1 class="grow">Kitaplık</h1><a class="btn ghost" href="#/draw">✏️ Çiz</a><a class="btn" href="#/new">+ Yeni model</a></div>
    <div id="running"></div><div id="draws"></div><div class="grid" id="grid"></div></section>`);
  app.append(el);
  if (drawings.length) {
    const d = h(`<div class="stack"><h2>Çizimler</h2><div class="strip"></div></div>`);
    drawings.forEach(x => $(".strip", d).append(h(`<a class="tile mini" href="#/d/${x.id}"><div class="thumb" style="background-image:url('${x.image}')"></div><div class="meta"><div class="name">${esc(x.name)}</div></div></a>`)));
    d.append(h(`<h2>Modeller</h2>`));
    $("#draws", el).append(d);
  }
  if (jobs.length) {
    const r = $("#running", el);
    r.append(h(`<div class="card stack"><b>Devam eden işler</b></div>`));
    jobs.forEach(j => r.firstChild.append(h(`<a href="#/job/${j.id}" class="row" style="text-decoration:none;color:inherit">
      <span class="dot ${j.status === "waiting" ? "ask" : "run"}"></span><span class="grow">${esc(jobTitle(j))}</span><span class="muted small">${j.status === "waiting" ? "<b>Sana bir sorusu var</b>" : esc(j.log.at(-1)?.m || "Sırada")}</span></a>`)));
  }
  const g = $("#grid", el);
  if (!assets.length) { g.replaceWith(h(`<div class="card empty"><p><b>Henüz model yok.</b></p><p class="muted">Bir fotoğraf yükle, gerisini atölye halletsin.</p><a class="btn" href="#/new/photo">Fotoğraftan başla</a></div>`)); return; }
  for (const a of assets) {
    const t = h(`<a class="tile" href="#/a/${a.id}"><div class="thumb">◆</div><div class="meta"><div class="name">${esc(a.name)}</div>
      <div class="small muted">${when(a.updated_at)}${all ? " · " + esc(a.owner) : ""}</div></div></a>`);
    const img = new Image(); img.onload = () => { const th = $(".thumb", t); th.textContent = ""; th.style.backgroundImage = `url(${img.src})`; };
    img.src = `/api/thumbs/${a.id}?t=${Math.round(a.updated_at)}`;
    g.append(t);
  }
}
function jobTitle(j) { return { mod01: "Fotoğraftan model", mod02: "Çoklu fotoğraftan model", import: "Dosya yükleme", prepare: "Baskı hazırlığı", resegment: "Renk bölgeleri", export: "Baskı dosyası", edit: "Blender düzenleme", agent: "Tasarım ajanı" }[j.module] || j.module; }

// ------------------------------------------------------------------ new
function newPage() {
  app.append(h(`<section class="stack"><h1>Yeni model</h1>
    <div class="modes">
      <a class="mode" href="#/new/photo"><span class="ic">📷</span><b>Bir fotoğraftan</b><span class="muted">Fotoğraf + kısa açıklama. Atölye diğer açıları kendisi çizer ve 3B modeli kurar.</span></a>
      <a class="mode" href="#/new/multi"><span class="ic">🖼️</span><b>Birkaç fotoğraftan</b><span class="muted">Aynı nesnenin önden, yandan, arkadan çekilmiş 2–6 fotoğrafı.</span></a>
      <a class="mode" href="#/draw"><span class="ic">✏️</span><b>Bir çizimden</b><span class="muted">Parmağınla çiz ya da kâğıttaki çizimin fotoğrafını yükle, not ekle; ajan onu basılabilir bir nesneye çevirsin.</span></a>
      <a class="mode" href="#/new/file"><span class="ic">📦</span><b>3B dosya yükle</b><span class="muted">STL, OBJ, GLB, 3MF veya STEP. Düzenleyip baskıya hazırla.</span></a>
    </div></section>`));
}

function picker(multiple, accept) {
  const el = h(`<div class="drop" tabindex="0" role="button"><b>${multiple ? "Fotoğrafları seç" : "Fotoğraf seç"}</b><div class="small muted">ya da buraya sürükle</div><div class="previews"></div>
    <input type="file" ${multiple ? "multiple" : ""} accept="${accept}" hidden></div>`);
  const input = $("input", el); el.files = [];
  const show = files => {
    el.files = [...files].slice(0, multiple ? 6 : 1); const p = $(".previews", el); p.innerHTML = "";
    el.classList.toggle("has", el.files.length > 0);
    el.files.forEach(f => { if (f.type.startsWith("image/")) { const i = new Image(); i.src = URL.createObjectURL(f); p.append(i); } else p.append(h(`<span class="chip">${esc(f.name)}</span>`)); });
    el.dispatchEvent(new Event("change"));
  };
  el.onclick = () => input.click(); el.onkeydown = e => { if (e.key === "Enter" || e.key === " ") input.click(); };
  input.onchange = () => show(input.files);
  el.ondragover = e => { e.preventDefault(); }; el.ondrop = e => { e.preventDefault(); show(e.dataTransfer.files); };
  return el;
}

async function costLine(module, extra = "") {
  const est = await api(`/api/estimate?module=${module}${extra}`);
  const bal = est.balance;
  const right = ME.user.unlimited ? "Sınırsız" : `Kalan ${money(bal.available)}`;
  return h(`<div class="cost"><span>Tahmini maliyet ≈ ${money(est.usd)}</span><span class="small">${right}</span></div>`);
}

async function photoPage() {
  const el = h(`<section class="stack" style="max-width:640px"><h1>Bir fotoğraftan model</h1>
    <div class="card stack"><div id="pick"></div>
      <label class="field"><span>Ne yapmak istiyorsun?</span><textarea id="prompt" maxlength="1000" placeholder="Örn: Bu kediyi 8 cm'lik bir biblo olarak bas, kulakları belirgin olsun."></textarea></label>
      <label class="check"><input type="checkbox" id="top"> Üstten görünüşü de üret (biraz daha pahalı)</label>
      <div id="eff"></div>
      <div id="cost"></div>
      <button id="go" disabled>Modeli oluştur</button>
      <p class="small muted">Fotoğrafta tek nesne olsun, net ve iyi ışıkta. İşlem 2–4 dakika sürer.</p></div></section>`);
  app.append(el);
  const p = picker(false, "image/*"); $("#pick", el).append(p);
  const eff = effortPicker(() => upd()); $("#eff", el).append(eff);
  const upd = async () => { $("#cost", el).replaceChildren(await costLine("mod01", `&include_top=${$("#top", el).checked ? 1 : 0}&effort=${eff.value}`)); };
  p.addEventListener("change", () => ($("#go", el).disabled = !p.files.length));
  $("#top", el).onchange = upd; upd();
  $("#go", el).onclick = async () => {
    const f = new FormData(); f.append("image", p.files[0]); f.append("prompt", $("#prompt", el).value); f.append("include_top", $("#top", el).checked ? "1" : "0"); f.append("effort", eff.value);
    $("#go", el).disabled = true; $("#go", el).textContent = "Gönderiliyor…";
    try { const j = await api("/api/jobs/mod01", { form: f }); location.hash = `#/job/${j.id}`; }
    catch (e) { toast(e.message); $("#go", el).disabled = false; $("#go", el).textContent = "Modeli oluştur"; }
  };
}

async function multiPage() {
  const el = h(`<section class="stack" style="max-width:640px"><h1>Birkaç fotoğraftan model</h1>
    <div class="card stack"><div id="pick"></div>
      <label class="field"><span>Model adı</span><input type="text" id="name" maxlength="80" placeholder="Örn: Seramik vazo"></label>
      <div id="cost"></div><button id="go" disabled>Modeli oluştur</button>
      <p class="small muted">Aynı nesneyi farklı yönlerden çek (ön, yan, arka). Arka plan otomatik silinir.</p></div></section>`);
  app.append(el);
  const p = picker(true, "image/*"); $("#pick", el).append(p);
  const upd = async () => $("#cost", el).replaceChildren(await costLine("mod02", `&count=${Math.max(1, p.files.length)}`));
  p.addEventListener("change", () => { $("#go", el).disabled = !p.files.length; upd(); }); upd();
  $("#go", el).onclick = async () => {
    const f = new FormData(); p.files.forEach(x => f.append("images", x)); f.append("name", $("#name", el).value);
    $("#go", el).disabled = true;
    try { const j = await api("/api/jobs/mod02", { form: f }); location.hash = `#/job/${j.id}`; } catch (e) { toast(e.message); $("#go", el).disabled = false; }
  };
}

async function filePage() {
  const el = h(`<section class="stack" style="max-width:640px"><h1>3B dosya yükle</h1>
    <div class="card stack"><div id="pick"></div>
      <label class="field"><span>Model adı</span><input type="text" id="name" maxlength="80"></label>
      <button id="go" disabled>Yükle</button><p class="small muted">Ücretsiz. En fazla 150 MB.</p></div></section>`);
  app.append(el);
  const p = picker(false, ".stl,.obj,.glb,.gltf,.ply,.3mf,.step,.stp"); $("b", p).textContent = "Dosya seç"; $("#pick", el).append(p);
  p.addEventListener("change", () => ($("#go", el).disabled = !p.files.length));
  $("#go", el).onclick = async () => {
    const f = new FormData(); f.append("file", p.files[0]); f.append("name", $("#name", el).value);
    $("#go", el).disabled = true; $("#go", el).textContent = "Yükleniyor…";
    try { const j = await api("/api/jobs/import", { form: f }); location.hash = `#/job/${j.id}`; } catch (e) { toast(e.message); $("#go", el).disabled = false; $("#go", el).textContent = "Yükle"; }
  };
}

// ------------------------------------------------------------------ job progress
const STEP_ORDER = {
  mod01: ["INTENT_EXTRACTION", "MULTIVIEW_PROMPT_GENERATION", "BACKGROUND_REMOVAL", "PERSPECTIVE_SYNTHESIS", "VIEW_QA", "3D_RECONSTRUCTION"],
  mod02: ["IMAGE_VALIDATION_AND_CLEANUP", "BACKGROUND_REMOVAL", "MULTI_IMAGE_3D_GENERATION"],
  agent: ["AGENT_PLAN", "SKETCH_TO_PHOTO", "RELIEF_BUILD", "INTENT_EXTRACTION", "MULTIVIEW_PROMPT_GENERATION", "BACKGROUND_REMOVAL",
          "PERSPECTIVE_SYNTHESIS", "VIEW_QA", "3D_RECONSTRUCTION", "MULTI_MODAL_ANALYSIS", "MCP_EXECUTION", "REVIEW", "VERSIONING_AND_RENDER"],
};
async function jobPage(id) {
  const el = h(`<section class="stack" style="max-width:720px"><h1 id="jt">İş</h1><div class="card stack">
    <div class="bar"><i style="width:5%"></i></div><ul class="steps" id="steps"></ul><div id="foot" class="row"></div></div>
    <div id="agentout"></div><div id="views" class="previews"></div></section>`);
  app.append(el);
  const tick = async () => {
    let j; try { j = await api(`/api/jobs/${id}`); } catch (e) { toast(e.message); return; }
    $("#jt", el).textContent = jobTitle(j);
    const order = STEP_ORDER[j.module] || [];
    const idx = Math.max(0, order.indexOf(j.state));
    const pct = j.status === "done" ? 100 : order.length ? Math.round((idx + 0.5) / order.length * 100) : 50;
    $(".bar i", el).style.width = pct + "%";
    const ul = $("#steps", el); ul.innerHTML = "";
    if (j.status === "queued") ul.append(h(`<li><span class="dot run"></span><span>Sırada${j.queue_position ? ` (önünde ${j.queue_position} iş)` : ""}</span></li>`));
    j.log.forEach((l, i) => {
      const last = i === j.log.length - 1; const bad = l.m.startsWith("Hata");
      ul.append(h(`<li><span class="dot ${bad ? "bad" : last && j.status === "running" ? "run" : "ok"}"></span><span class="grow">${esc(l.m)}</span><span class="small muted">${new Date(l.t * 1000).toLocaleTimeString("tr-TR", { hour: "2-digit", minute: "2-digit" })}</span></li>`));
    });
    const foot = $("#foot", el); foot.innerHTML = "";
    foot.append(h(`<span class="small muted grow">Harcanan ${money(j.cost_usd)}${j.est_usd ? ` · tahmin ${money(j.est_usd)}` : ""}</span>`));
    if (j.module === "mod01") showViews(id, $("#views", el), j);
    if (j.module === "agent" && j.input?.target === "drawing") showViews(id, $("#views", el), j, "", ["drawing", "sketch_photo", "view_front", "view_right", "view_back", "view_left", "view_top"]);
    if (j.module === "agent" && j.input?.target !== "drawing" && j.status === "done") showViews(id, $("#views", el), j, "final_", ["front", "right", "back", "top"]);
    if (j.module === "edit" && j.status === "done") showViews(id, $("#views", el), j, "final_", ["front", "right", "back", "top"]);
    if (j.status === "done") {
      await refreshMe();
      const aid = j.result?.asset_id || j.asset_id;
      if (aid && ["mod01", "mod02", "import"].includes(j.module)) { toast("Model hazır!"); location.hash = `#/a/${aid}`; return; }
      if (j.module === "agent") { showAgentResult($("#agentout", el), j); return; }
      if (j.module === "edit" && (j.result?.undone || (j.result?.missing_tr || []).length)) $("#agentout", el).replaceChildren(h(`<div class="card stack">${leftovers(j.result)}</div>`));
      foot.append(h(`<a class="btn" href="#/a/${aid}">Modele git</a>`)); return;
    }
    if (j.status === "waiting" && j.ask) {
      showAsk($("#agentout", el), j, () => tick());
      const c = h(`<button class="ghost small">İptal</button>`); c.onclick = async () => { await api(`/api/jobs/${id}/cancel`, { body: {} }); tick(); }; foot.append(c);
      return;   // no polling while she thinks
    }
    $("#agentout", el).querySelector(".askcard")?.remove();
    if (j.status === "failed" || j.status === "cancelled") {
      foot.append(h(`<span class="badge bad">${j.status === "failed" ? "Başarısız" : "İptal"}</span>`));
      if (j.status === "failed") { const b = h(`<button class="small">Tekrar dene</button>`); b.onclick = async () => { await api(`/api/jobs/${id}/retry`, { body: {} }); tick(); }; foot.append(b); }
      return;
    }
    const c = h(`<button class="ghost small">İptal</button>`); c.onclick = async () => { await api(`/api/jobs/${id}/cancel`, { body: {} }); tick(); }; foot.append(c);
    pollTimer = setTimeout(tick, 2000);
  };
  tick();
}
// the agent asks a question, or (from her 3rd send on) shows its plan and waits for "Tamam"
function showAsk(box, j, next) {
  if (box.querySelector(`.askcard[data-k="${j.updated_at}"]`)) return;
  const a = j.ask; const confirm = a.kind === "confirm";
  const card = h(`<div class="card stack askcard" data-k="${j.updated_at}"><div class="row"><span class="ic">🤖</span><b class="grow">${confirm ? "Şöyle yapmayı düşünüyorum" : "Sana bir şey sormak istiyorum"}</b></div>
    ${a.praise ? `<p class="praise">${esc(a.praise)}</p>` : ""}
    <p class="asktext">${esc(a.text)}</p>
    ${confirm ? `<div class="cost"><span>${a.est_usd ? `Tahmini maliyet ≈ ${money(a.est_usd)}` : "Bu iş ücretsiz"}</span></div>` : ""}
    <div class="row opts"></div>
    <label class="field"><span>${confirm ? "Değiştirmek istediğin bir şey var mı?" : "Ya da kendin yaz"}</span><textarea maxlength="600" rows="2" placeholder="${confirm ? "Örn: Biraz daha büyük olsun" : "Cevabın"}"></textarea></label>
    <div class="row"><button class="ghost small" data-a="send">Gönder</button></div></div>`);
  const post = async body => {
    card.querySelectorAll("button").forEach(b => (b.disabled = true));
    try { await api(`/api/jobs/${j.id}/answer`, { body }); card.remove(); next(); }
    catch (e) { toast(e.message); card.querySelectorAll("button").forEach(b => (b.disabled = false)); }
  };
  const opts = $(".opts", card);
  if (confirm) { const ok = h(`<button>✅ Tamam, yap</button>`); ok.onclick = () => post({ ok: true }); opts.append(ok); }
  else {
    (a.options || []).forEach(o => { const b = h(`<button class="ghost">${esc(o)}</button>`); b.onclick = () => post({ text: o }); opts.append(b); });
    const d = h(`<button class="ghost">🤷 Sen karar ver</button>`); d.onclick = () => post({ text: "" }); opts.append(d);
  }
  $("[data-a=send]", card).onclick = () => { const t = $("textarea", card).value.trim(); if (!t) return toast("Önce bir şey yaz"); post({ text: t }); };
  box.replaceChildren(card);
  $("textarea", card).onkeydown = e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("[data-a=send]", card).click(); } };
}
// what the agent could not do, honestly (undone rounds, still missing, impossible with these tools)
function leftovers(r) {
  const items = [...(r.missing_tr || []), ...(r.cant_do_tr || [])];
  return `${r.undone ? `<p class="small muted">${r.undone} deneme modeli bozduğu için geri alındı; modelde yalnızca iyi olan değişiklikler var.</p>` : ""}
    ${items.length ? `<div class="leftover"><b class="small">Henüz yapamadıklarım</b><ul class="problems small">${items.map(t => `<li>${esc(t)}</li>`).join("")}</ul></div>` : ""}`;
}
function showAgentResult(box, j) {
  const r = j.result || {};
  if (r.version_id) sessionStorage.setItem("ver:" + r.asset_id, r.version_id);
  box.replaceChildren(h(`<div class="card stack agentcard"><div class="row"><span class="ic">🤖</span><b class="grow">Ajan bitirdi</b><span class="badge ok">Baskıya hazır</span></div>
    ${r.praise_tr ? `<p class="praise">${esc(r.praise_tr)}</p>` : ""}
    ${r.explanation_tr ? `<p>${esc(r.explanation_tr)}</p>` : ""}
    ${(r.notes || []).length ? `<ul class="problems small">${r.notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
    ${leftovers(r)}
    ${(r.tips_tr || []).length ? `<div><b class="small">Baskı ipuçları</b><ul class="problems">${r.tips_tr.map(t => `<li>${esc(t)}</li>`).join("")}</ul></div>` : ""}
    <p class="small muted">Baskı kontrolü ve renk ayrımı da yapıldı. Baskı sayfasında renkleri değiştirip Bambu dosyasını indirebilirsin.</p>
    <div class="row"><a class="btn" href="#/a/${r.asset_id}/print">🖨️ Baskıya hazırla</a><a class="btn ghost" href="#/a/${r.asset_id}">Modeli gör</a></div></div>`));
}
function showViews(id, box, j, prefix = "view_", names = ["front", "right", "back", "left", "top"]) {
  names.forEach(n => {
    if (box.querySelector(`[data-v="${n}"]`)) return;
    const img = new Image(); img.dataset.v = n; img.alt = n; img.title = n;
    img.onload = () => box.append(img); img.src = `/api/jobs/${id}/files/${prefix}${n}.png?t=${Math.round(j.updated_at)}`;
  });
}

// ------------------------------------------------------------------ asset
async function assetPage(id) {
  const a = await api(`/api/assets/${id}`);
  const v = a.latest;
  const el = h(`<section class="stack">
    <div class="row"><h1 class="grow" id="nm">${esc(a.name)}</h1><button class="ghost small" id="ren">Adını değiştir</button></div>
    <div class="split"><div class="stack"><div id="vbox"></div>
        <div class="row"><div class="seg" id="tool"><button data-t="look" class="on">Döndür</button><button data-t="note">📝 Not al</button></div>
          <span class="small muted" id="toolhint"></span></div></div>
      <div class="stack">
        <div id="notes"></div>
        <div class="card stack"><b>Ne yapmak istersin?</b>
          <a class="btn" href="#/a/${id}/print">🖨️ Baskıya hazırla (Bambu)</a>
          <a class="btn ghost" href="#/a/${id}/edit">✏️ Düzenle (Blender)</a>
          ${v ? `<a class="btn ghost" href="${v.url}" download>⬇️ GLB indir</a>` : ""}
        </div>
        <div class="card stack"><b>Sürümler</b><div class="versions" id="vers"></div><div class="small muted" id="vmeta"></div></div>
        <button class="danger small" id="del">Modeli sil</button>
      </div></div></section>`);
  app.append(el);
  const vb = viewerBox(); $("#vbox", el).append(vb);
  activeViewer = new Viewer(vb);
  let cur = v; let model = null; let tool = "look";
  const notes = notesPanel({ kind: "model", assetId: id, getVersion: () => cur, viewer: () => activeViewer, getModel: () => model });
  $("#notes", el).append(notes.el);
  $("#tool", el).onclick = e => { const b = e.target.closest("button"); if (!b) return; tool = b.dataset.t;
    $("#tool", el).querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b));
    $("#toolhint", el).textContent = tool === "note" ? "Modelde not bırakmak istediğin yere dokun." : ""; };
  const canvas = activeViewer.renderer.domElement; const ray = new THREE.Raycaster(); let down = null;
  canvas.addEventListener("pointerdown", e => (down = [e.clientX, e.clientY]));
  canvas.addEventListener("pointerup", e => {
    if (tool !== "note" || !model || !down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 6) return;
    const r = canvas.getBoundingClientRect();
    ray.setFromCamera(new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1), activeViewer.camera);
    const hit = ray.intersectObjects(model.children, true).find(x => !x.object.userData.pin); if (!hit) return;
    const p = model.worldToLocal(hit.point.clone());
    notes.compose([p.x, p.y, p.z]);
  });
  const show = async ver => {
    cur = ver; $(".loading", vb).hidden = false;
    $("#vers", el).querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.id === ver.id));
    const m = ver.meta || {};
    $("#vmeta", el).textContent = [{ mod03: "Blender düzenlemesi", upload: "Yüklenen dosya", agent: "Ajanın sürümü", drawing: "Çizimden" }[ver.source] || "Üretilen model",
      m.effort ? `çaba: ${{ mid: "orta", high: "yüksek", extra: "ekstra" }[m.effort] || m.effort}` : "",
      m.prompt ? `“${m.prompt}”` : "", m.directive ? `“${m.directive}”` : "", when(ver.created_at)].filter(Boolean).join(" · ");
    try {
      model = await activeViewer.load(ver.url); $(".loading", vb).hidden = true; notes.refresh();
      if (!a.has_thumb) { a.has_thumb = true; setTimeout(() => api(`/api/assets/${id}/thumb`, { body: { png: activeViewer.snapshot() } }).catch(() => {}), 600); }
    } catch (e) { $(".loading", vb).textContent = "Model açılamadı."; }
  };
  a.versions.forEach(ver => { const b = h(`<button class="ghost small" data-id="${ver.id}">v${ver.n}</button>`); b.onclick = () => show(ver); $("#vers", el).append(b); });
  if (v) show(v); else $(".loading", vb).textContent = "Sürüm yok.";
  sessionStorage.setItem("ver:" + id, v?.id || "");
  $("#vers", el).addEventListener("click", () => sessionStorage.setItem("ver:" + id, cur?.id || ""));
  $("#ren", el).onclick = async () => { const n = prompt("Yeni ad", a.name); if (n) { await api(`/api/assets/${id}/rename`, { body: { name: n } }); $("#nm", el).textContent = n; } };
  $("#del", el).onclick = async () => { if (confirm("Bu model silinsin mi?")) { await api(`/api/assets/${id}/delete`, { body: {} }); location.hash = "#/"; } };
}
function chosenVersion(a, id) { const vid = sessionStorage.getItem("ver:" + id); return a.versions.find(x => x.id === vid) || a.latest; }

// ------------------------------------------------------------------ notes + design agent
function pinSprite(label, done) {
  const c = document.createElement("canvas"); c.width = c.height = 64; const g = c.getContext("2d");
  g.fillStyle = done ? "#2f7d4f" : "#d628a0"; g.beginPath(); g.arc(32, 32, 28, 0, Math.PI * 2); g.fill();
  g.lineWidth = 5; g.strokeStyle = "#fff"; g.stroke();
  g.fillStyle = "#fff"; g.font = "bold 30px Nunito, sans-serif"; g.textAlign = "center"; g.textBaseline = "middle"; g.fillText(label, 32, 34);
  const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(c), depthTest: false, transparent: true }));
  sp.renderOrder = 10; sp.userData.pin = true; return sp;
}
const STATUS_TR = { open: "", sent: "ajanda", done: "yapıldı" };

/* Notes on a 3D model (kind "model": pos = point in the version file's coordinates) or on a drawing
   (kind "drawing": pos = [u, v] in 0..100). Open notes are numbered 1..N in the order the agent receives them. */
function notesPanel(o) {
  const el = h(`<div class="card stack notes"><div class="row"><b class="grow">📝 Notlar</b><span class="small muted" id="ncount"></span></div>
    <p class="small muted">${o.kind === "model" ? "“Not al” ile modelde bir yere dokun ya da aşağıya genel bir not yaz." : "“Not” aracıyla çizimde bir yere dokun ya da aşağıya genel bir not yaz."} Notlar ajana gider; o da baskıya hazır bir model yapar.</p>
    <div class="composer" hidden><label class="field"><span id="cmlabel">Not</span><textarea id="cmtext" maxlength="600" placeholder="Örn: Burası daha kalın olsun / buraya ASLI yaz"></textarea></label>
      <div class="row"><button class="small" id="cmsave">Notu kaydet</button><button class="ghost small" id="cmcancel" type="button">Vazgeç</button></div></div>
    <ol class="notelist" id="nlist"></ol>
    <div class="row"><input type="text" id="general" maxlength="600" placeholder="Genel not (tüm ${o.kind === "model" ? "model" : "çizim"} için)" class="grow" style="flex:1 1 180px"><button class="ghost small" id="addgen">Ekle</button></div>
    <details class="send"><summary><b>🤖 Ajana gönder</b></summary><div class="stack">
      <div id="eff"></div>
      <label class="field"><span>Yükseklik (mm, boş bırakırsan ajan seçer)</span><input type="number" id="ahmm" min="5" max="250" placeholder="Örn: 60"></label>
      <label class="field"><span>Ajana ek bilgi (isteğe bağlı)</span><input type="text" id="extra" maxlength="600" placeholder="Örn: Annem için hediye, mavi-beyaz olsun"></label>
      <div id="acost"></div>
      <button id="send">🤖 Notları ajana gönder</button>
      <p class="small muted">Ajan yeni başlayanlar için düşünür: devrilmeyecek taban, kalın duvar, okunur yazı, uygun boyut. Sonuç yeni bir sürüm olur, eskisi korunur.</p></div></details></div>`);
  let notes = []; let pending = null; let tempPin = null; const pins = [];
  const eff = effortPicker(() => cost()); $("#eff", el).append(eff);
  async function cost() {
    if (o.kind === "model") { $("#acost", el).replaceChildren(h(`<div class="cost"><span>fal maliyeti yok</span><span class="small">yapay zekâ + Blender, 1–${eff.value === "extra" ? 10 : 5} dk</span></div>`)); return; }
    const est = await api(`/api/estimate?module=agent&target=drawing&effort=${eff.value}`);
    $("#acost", el).replaceChildren(h(`<div class="cost"><span>Düz model ücretsiz · 3B figür ≈ ${money(est.usd)}</span><span class="small">${ME.user.unlimited ? "Sınırsız" : `Kalan ${money(est.balance.available)}`}</span></div>`));
  }
  cost();
  const visible = () => notes.filter(n => o.kind !== "model" || !n.pos || n.version_id === o.getVersion()?.id);
  const numbering = () => { const m = new Map(); visible().filter(n => n.status === "open").forEach((n, i) => m.set(n.id, i + 1)); return m; };
  function drawPins() {
    pins.forEach(p => p.parent && p.parent.remove(p)); pins.length = 0;
    const nums = numbering();
    if (o.kind === "model") {
      const model = o.getModel(); if (!model) return;
      const r = new THREE.Box3().setFromObject(model).getSize(new THREE.Vector3()).length() || 1;
      for (const n of visible()) {
        if (!n.pos) continue;
        const sp = pinSprite(nums.has(n.id) ? String(nums.get(n.id)) : "✓", n.status !== "open");
        sp.position.fromArray(n.pos); const k = r * 0.07 / (model.scale.x || 1); sp.scale.set(k, k, k);
        model.add(sp); pins.push(sp);
      }
    } else o.drawPins && o.drawPins(visible().filter(n => n.pos).map(n => ({ ...n, label: nums.has(n.id) ? String(nums.get(n.id)) : "✓" })), pending);
  }
  function list() {
    const ul = $("#nlist", el); ul.innerHTML = ""; const nums = numbering(); const vs = visible();
    $("#ncount", el).textContent = vs.length ? `${nums.size} açık · ${vs.length} toplam` : "";
    if (!vs.length) ul.append(h(`<li class="muted small none">Henüz not yok.</li>`));
    vs.forEach(n => {
      const li = h(`<li class="${n.status}"><span class="num">${nums.has(n.id) ? nums.get(n.id) : "✓"}</span><span class="grow">${esc(n.text)}${n.pos ? "" : ` <span class="small muted">(genel)</span>`}
        ${STATUS_TR[n.status] ? ` <span class="badge ${n.status === "done" ? "ok" : "warn"}">${STATUS_TR[n.status]}</span>` : ""}${n.status === "sent" && n.job_id ? ` <a class="small" href="#/job/${n.job_id}">işe git</a>` : ""}</span>
        ${n.status === "done" ? `<button class="ghost small" data-a="reopen" title="Tekrar aç: yeniden ajana gönderilebilir" aria-label="Tekrar aç">↺</button>` : ""}
        <button class="ghost small" data-a="edit" title="Düzenle" aria-label="Düzenle">✎</button><button class="ghost small" data-a="del" title="Sil" aria-label="Sil">✕</button></li>`);
      $("[data-a=edit]", li).onclick = async () => { const t = prompt("Notu düzenle", n.text); if (t && t.trim()) { await api(`/api/notes/${n.id}/update`, { body: { text: t } }); refresh(); } };
      const ro = $("[data-a=reopen]", li); if (ro) ro.onclick = async () => { await api(`/api/notes/${n.id}/reopen`, { body: {} }); toast("Not yeniden açıldı", 1400); refresh(); };
      $("[data-a=del]", li).onclick = async () => { if (confirm("Not silinsin mi?")) { await api(`/api/notes/${n.id}/delete`, { body: {} }); refresh(); } };
      ul.append(li);
    });
    $("#send", el).disabled = !nums.size && !$("#extra", el).value.trim();
    if (nums.size && !el.dataset.opened) { el.dataset.opened = "1"; $("details.send", el).open = true; }
  }
  async function refresh() {
    const id = o.kind === "model" ? o.assetId : o.getDrawingId();
    if (!id) { notes = []; list(); drawPins(); return; }
    notes = (await api(`/api/notes?${o.kind === "model" ? "asset_id" : "drawing_id"}=${id}`)).notes; list(); drawPins();
  }
  async function save(pos) {
    const text = (pos === undefined ? $("#general", el).value : $("#cmtext", el).value).trim();
    if (!text) return toast("Not boş olamaz");
    const body = { kind: o.kind, text, pos: pos ?? null };
    if (o.kind === "model") body.version_id = o.getVersion().id; else body.drawing_id = await o.ensureDrawing();
    await api("/api/notes", { body }); toast("Not kaydedildi", 1400);
    if (pos === undefined) $("#general", el).value = ""; closeComposer(); refresh();
  }
  function closeComposer() { pending = null; $(".composer", el).hidden = true; if (tempPin) { tempPin.parent?.remove(tempPin); tempPin = null; } drawPins(); }
  function compose(pos) {
    pending = pos; $(".composer", el).hidden = false; $("#cmtext", el).value = ""; $("#cmlabel", el).textContent = "Bu nokta için not";
    if (o.kind === "model") { const model = o.getModel(); if (tempPin) tempPin.parent?.remove(tempPin);
      tempPin = pinSprite("+", false); tempPin.position.fromArray(pos); const r = new THREE.Box3().setFromObject(model).getSize(new THREE.Vector3()).length() || 1;
      const k = r * 0.07; tempPin.scale.set(k, k, k); model.add(tempPin); }
    else drawPins();
    $("#cmtext", el).focus({ preventScroll: true }); el.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
  $("#cmsave", el).onclick = () => save(pending);
  $("#cmcancel", el).onclick = closeComposer;
  $("#addgen", el).onclick = () => save(undefined);
  $("#general", el).onkeydown = e => { if (e.key === "Enter") save(undefined); };
  $("#extra", el).oninput = list;
  $("#send", el).onclick = async () => {
    const b = $("#send", el); b.disabled = true; b.textContent = "Gönderiliyor…";
    try {
      const body = { effort: eff.value, extra: $("#extra", el).value, height_mm: $("#ahmm", el).value || null };
      if (o.kind === "model") body.version_id = o.getVersion().id; else body.drawing_id = await o.ensureDrawing(true);
      const j = await api("/api/agent", { body }); location.hash = `#/job/${j.id}`;
    } catch (e) { toast(e.message); b.disabled = false; b.textContent = "🤖 Notları ajana gönder"; }
  };
  return { el, refresh, compose, eff };
}

// ------------------------------------------------------------------ print prep (MOD-04)
async function printPage(id) {
  const a = await api(`/api/assets/${id}`);
  const ver = chosenVersion(a, id);
  const lastPrep = a.jobs.find(j => j.module === "prepare" && j.status === "done" && j.version_id === ver.id);
  const pendingPrep = !lastPrep && a.jobs.find(j => j.module === "prepare" && ["queued", "running"].includes(j.status) && j.version_id === ver.id);
  const el = h(`<section class="stack"><div class="row"><a href="#/a/${id}" class="small">← ${esc(a.name)}</a></div>
    <h1>Baskıya hazırla</h1>
    <div class="split"><div class="stack"><div id="vbox"></div>
        <div class="row"><div class="seg" id="mode"><button data-m="color" class="on">Renkler</button><button data-m="overhang">Sarkmalar</button><button data-m="thin">İnce duvar</button></div></div></div>
      <div class="stack">
        <div class="card stack" id="setup"><b>Ayarlar</b>
          <label class="field"><span>Yükseklik (mm)</span><input type="number" id="hmm" min="1" max="250" step="0.1" value="${+(ver.meta?.height_mm || 80).toFixed(1)}"></label>
          <label class="field"><span>Renk sayısı (AMS yuvası)</span><select id="k"><option>1</option><option>2</option><option selected>3</option><option>4</option></select></label>
          <label class="check small"><input type="checkbox" id="remesh"> Yüzeyi baştan ör (bozuk modeller için)</label>
          <button id="run">Kontrol et ve renkleri ayır</button><p class="small muted">Ücretsiz. Birkaç saniye – 1 dakika.</p></div>
        <div id="out" class="stack"></div>
      </div></div></section>`);
  app.append(el);
  const vb = viewerBox("Renk boyamak için bölgeye dokun"); $("#vbox", el).append(vb); $(".loading", vb).textContent = "Kontrol başlatılınca model burada görünür.";
  activeViewer = new Viewer(vb);
  const state = { prep: null, labels: null, regions: null, overhang: null, thin: null, palette: [], slot: 0, mode: "color", mesh: null, regionFaces: null };

  $("#mode", el).onclick = e => { const b = e.target.closest("button"); if (!b) return; state.mode = b.dataset.m; $("#mode", el).querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b)); paint(); };

  const runPrep = async () => {
    $("#run", el).disabled = true; $("#out", el).innerHTML = `<div class="card">Hazırlanıyor…</div>`;
    const j = await api("/api/jobs/prepare", { body: { version_id: ver.id, height_mm: +$("#hmm", el).value, colors: +$("#k", el).value, remesh: $("#remesh", el).checked } });
    waitJob(j.id, async done => { $("#run", el).disabled = false; if (done.status === "done") await loadPrep(done); else $("#out", el).innerHTML = `<div class="card"><span class="badge bad">Hata</span> ${esc(done.error)}</div>`; },
      p => { $("#out", el).innerHTML = `<div class="card">${esc(p.log.at(-1)?.m || "Sırada…")}</div>`; });
  };
  $("#run", el).onclick = runPrep;

  async function loadPrep(job, arraysJob = null) {
    state.prep = job;
    const arr = await api(`/api/jobs/${arraysJob?.id || job.id}/files/arrays.json`);
    state.labels = Int32Array.from(arr.labels); state.regions = Int32Array.from(arr.regions);
    state.overhang = new Set(arr.overhang); state.thin = new Set(arr.thin);
    state.palette = (arraysJob?.result?.palette || job.result.palette).slice();
    state.regionFaces = new Map(); state.regions.forEach((r, i) => { if (!state.regionFaces.has(r)) state.regionFaces.set(r, []); state.regionFaces.get(r).push(i); });
    $("#hmm", el).value = job.result.height_mm;
    if (!state.mesh) {
      const scene = await activeViewer.load(`/api/jobs/${job.id}/files/preview.glb`);
      let mesh = null; scene.traverse(o => { if (o.isMesh && !mesh) mesh = o; });
      mesh.geometry = mesh.geometry.index ? mesh.geometry.toNonIndexed() : mesh.geometry;
      mesh.geometry.computeVertexNormals();
      mesh.material = new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.75, metalness: 0, flatShading: false });
      mesh.geometry.setAttribute("color", new THREE.BufferAttribute(new Float32Array(mesh.geometry.attributes.position.count * 3), 3));
      state.mesh = mesh; $(".loading", vb).hidden = true; attachPicker();
    }
    paint(); renderResults();
  }

  function paint() {
    if (!state.mesh) return;
    const col = state.mesh.geometry.attributes.color; const a = col.array; const c = new THREE.Color();
    const pal = state.palette.map(p => new THREE.Color(p));   // Color() already converts sRGB -> working space
    const grey = new THREE.Color("#c9c1b8"), red = new THREE.Color("#e5484d");
    const n = state.labels.length;
    for (let f = 0; f < n; f++) {
      if (state.mode === "color") c.copy(pal[state.labels[f]] || grey);
      else if (state.mode === "overhang") c.copy(state.overhang.has(f) ? red : grey);
      else c.copy(state.thin.has(f) ? red : grey);
      for (let k = 0; k < 3; k++) { const o = (f * 3 + k) * 3; a[o] = c.r; a[o + 1] = c.g; a[o + 2] = c.b; }
    }
    col.needsUpdate = true;
  }

  function attachPicker() {
    const canvas = activeViewer.renderer.domElement; const ray = new THREE.Raycaster(); let down = null;
    canvas.addEventListener("pointerdown", e => (down = [e.clientX, e.clientY]));
    canvas.addEventListener("pointerup", e => {
      if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 6 || state.mode !== "color") return;
      const r = canvas.getBoundingClientRect();
      ray.setFromCamera(new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1), activeViewer.camera);
      const hit = ray.intersectObject(state.mesh, false)[0]; if (!hit) return;
      const faces = state.regionFaces.get(state.regions[hit.faceIndex]) || [hit.faceIndex];
      faces.forEach(f => (state.labels[f] = state.slot)); paint();
      toast(`Bölge ${state.slot + 1}. renge boyandı`, 1400);
    });
  }

  function renderResults() {
    const r = state.prep.result.report; const adv = state.prep.result.advice;
    const out = $("#out", el); out.innerHTML = "";
    const ok = r.is_printable;
    const card = h(`<div class="card stack"><div class="row"><b class="grow">Baskı kontrolü</b><span class="badge ${ok ? "ok" : "bad"}">${ok ? "Basılabilir" : "Sorunlu"}</span></div>
      <div class="small muted">${r.size_mm.map(x => x.toFixed(1)).join(" × ")} mm · ${r.faces.toLocaleString("tr-TR")} üçgen${r.volume_cm3 ? ` · ${r.volume_cm3} cm³` : ""}</div>
      ${adv?.summary ? `<p>${esc(adv.summary)}</p>` : ""}
      ${(adv?.tips || []).length ? `<ul class="problems">${adv.tips.map(t => `<li>${esc(t)}</li>`).join("")}</ul>` : ""}
      ${r.problems.length ? `<details><summary class="small">Teknik ayrıntılar</summary><ul class="problems small">${r.problems.map(p => `<li>${esc(p)}</li>`).join("")}</ul></details>` : ""}
      ${(r.repairs || []).length ? `<p class="small muted">Yapılan onarım: ${esc(r.repairs.join("; "))}</p>` : ""}</div>`);
    out.append(card);
    const colors = h(`<div class="card stack"><b>Renkler</b><p class="small muted">Bir renk seç, sonra modelde boyamak istediğin bölgeye dokun. Renkler Bambu'daki filament yuvalarına (1–4) karşılık gelir.</p>
      <div class="slots" id="slots"></div><div class="row"><button class="ghost small" id="reseg">Renkleri yeniden ayır</button><button class="ghost small" id="addc">+ Renk ekle</button></div></div>`);
    out.append(colors);
    const drawSlots = () => {
      const s = $("#slots", colors); s.innerHTML = "";
      state.palette.forEach((p, i) => {
        const b = h(`<label class="slot ${i === state.slot ? "on" : ""}"><input type="color" value="${p}"><span>${i + 1}</span></label>`);
        b.onclick = e => { if (e.target.tagName !== "INPUT") { state.slot = i; drawSlots(); } };
        $("input", b).oninput = e => { state.palette[i] = e.target.value; paint(); };
        $("input", b).onfocus = () => { state.slot = i; s.querySelectorAll(".slot").forEach((x, k) => x.classList.toggle("on", k === i)); };
        s.append(b);
      });
    };
    drawSlots();
    $("#addc", colors).onclick = () => { if (state.palette.length >= 4) return toast("En fazla 4 renk (tek AMS)"); state.palette.push("#ffffff"); state.slot = state.palette.length - 1; drawSlots(); };
    $("#reseg", colors).onclick = async () => {
      const j = await api("/api/jobs/resegment", { body: { prep_job: state.prep.id, colors: +$("#k", el).value, seed: Math.floor(Math.random() * 1000) } });
      waitJob(j.id, async done => { if (done.status === "done") { await loadPrep(state.prep, done); } else toast(done.error || "Hata"); });
    };
    const exp = h(`<div class="card stack"><b>Baskı dosyası</b>
      <button data-mode="painted">Bambu 3MF (renkler boyalı)</button>
      <button class="ghost" data-mode="parts">Parçalı 3MF + STL'ler (her renk ayrı gövde)</button>
      <button class="ghost" data-mode="stl">Tek renk STL</button><div id="dl" class="stack"></div>
      <p class="small muted">Boyalı 3MF'i Bambu Studio'da aç; renkler hazır gelir, istersen boya kovasıyla değiştir.</p></div>`);
    out.append(exp);
    exp.querySelectorAll("button[data-mode]").forEach(b => (b.onclick = async () => {
      b.disabled = true; $("#dl", exp).innerHTML = `<span class="small muted">Hazırlanıyor…</span>`;
      const used = state.palette.slice(0, Math.max(...state.labels) + 1);
      try {
        const j = await api("/api/jobs/export", { body: { prep_job: state.prep.id, labels: Array.from(state.labels), palette: used.length ? used : state.palette, mode: b.dataset.mode, name: a.name } });
        waitJob(j.id, done => {
          b.disabled = false; const dl = $("#dl", exp); dl.innerHTML = "";
          if (done.status !== "done") { dl.append(h(`<span class="badge bad">${esc(done.error || "Hata")}</span>`)); return; }
          done.result.files.forEach(f => dl.append(h(`<a class="btn ghost small" href="/api/jobs/${done.id}/files/${encodeURIComponent(f)}" download>⬇️ ${esc(f)}</a>`)));
        });
      } catch (e) { toast(e.message); b.disabled = false; }
    }));
  }
  if (lastPrep) { await loadPrep(lastPrep); }
  else if (pendingPrep) {
    $("#run", el).disabled = true; $("#out", el).innerHTML = `<div class="card">Ajanın başlattığı kontrol sürüyor…</div>`;
    waitJob(pendingPrep.id, async done => { $("#run", el).disabled = false; if (done.status === "done") await loadPrep(done); else $("#out", el).innerHTML = ""; },
      p => { $("#out", el).innerHTML = `<div class="card">${esc(p.log.at(-1)?.m || "Sırada…")}</div>`; });
  }
}

function waitJob(id, onDone, onProgress) {
  const t = async () => {
    let j; try { j = await api(`/api/jobs/${id}`); } catch (e) { return; }
    if (["done", "failed", "cancelled"].includes(j.status)) return onDone(j);
    onProgress && onProgress(j); pollTimer = setTimeout(t, 1500);
  };
  t();
}

// ------------------------------------------------------------------ Blender edit (MOD-03)
async function editPage(id) {
  const a = await api(`/api/assets/${id}`);
  const ver = chosenVersion(a, id);
  const el = h(`<section class="stack"><div class="row"><a href="#/a/${id}" class="small">← ${esc(a.name)}</a></div>
    <h1>Düzenle</h1>
    <div class="split"><div class="stack"><div id="vbox"></div>
        <div class="row"><div class="seg" id="tool"><button data-t="look" class="on">Döndür</button><button data-t="erase">🧽 Silgi</button></div>
          <select id="brush" style="max-width:150px"><option value="2">Silgi 2 mm</option><option value="4" selected>Silgi 4 mm</option><option value="8">Silgi 8 mm</option></select>
          <button class="ghost small" id="undo">Geri al</button><span class="small muted" id="cnt"></span></div>
        <p class="small muted">Silgi: modelde istemediğin ip, çubuk ya da çıkıntıya dokun; kırmızı işaretler silinecek yerlerdir.</p></div>
      <div class="card stack">
        <p class="muted">Yazılı istekleri yapay zekâ Blender'da uygular; silgi işaretlerini doğrudan siler. Sonuç yeni sürüm olur (v${ver.n} korunur).</p>
        <div class="examples" id="ex"></div>
        <label class="field"><span>İstek (isteğe bağlı)</span><textarea id="dir" maxlength="1000" placeholder="Örn: Altına 8 mm yuvarlak taban ekle ve tabanın önüne ASLI yaz."></textarea></label>
        <label class="field"><span>Model yüksekliği (mm)</span><input type="number" id="hmm" value="${Math.round(ver.meta?.height_mm || 80)}" min="1" max="250"></label>
        <div id="eff"></div>
        <button id="go">Uygula</button><p class="small muted">Ücretsiz (yapay zekâ + Blender). 1–5 dakika.</p></div></div></section>`);
  app.append(el);
  ["Altına 8 mm yuvarlak taban ekle ve önüne ismimi yaz", "Altına düz kare taban ekle", "Anahtarlık için üstüne 4 mm delik aç", "Boşta duran küçük parçaları temizle"].forEach(t => {
    const b = h(`<button type="button">${esc(t)}</button>`); b.onclick = () => ($("#dir", el).value = t); $("#ex", el).append(b);
  });
  const eff = effortPicker(); $("#eff", el).append(eff);
  const vb = viewerBox("Silgi seçiliyken dokunduğun yer işaretlenir"); $("#vbox", el).append(vb);
  activeViewer = new Viewer(vb);
  let model = null; const marks = []; let tool = "look";
  try { model = await activeViewer.load(ver.url); $(".loading", vb).hidden = true; } catch { $(".loading", vb).textContent = "Model açılamadı."; }
  const count = () => ($("#cnt", el).textContent = marks.length ? `${marks.length} işaret` : "");
  $("#tool", el).onclick = e => { const b = e.target.closest("button"); if (!b) return; tool = b.dataset.t; $("#tool", el).querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b)); };
  $("#undo", el).onclick = () => { const m = marks.pop(); if (m) activeViewer.root.remove(m.mesh); count(); };
  const canvas = activeViewer.renderer.domElement; const ray = new THREE.Raycaster(); let down = null;
  canvas.addEventListener("pointerdown", e => (down = [e.clientX, e.clientY]));
  canvas.addEventListener("pointerup", e => {
    if (tool !== "erase" || !model || !down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 6) return;
    if (marks.length >= 30) return toast("En fazla 30 işaret");
    const r = canvas.getBoundingClientRect();
    ray.setFromCamera(new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1), activeViewer.camera);
    const hit = ray.intersectObject(model, true)[0]; if (!hit) return;
    const p = hit.point.clone().sub(model.position);          // back to the file's own coordinates
    const size = new THREE.Box3().setFromObject(model).getSize(new THREE.Vector3());
    const mmPerUnit = (+$("#hmm", el).value || 80) / (size.y || 1);
    const rmm = +$("#brush", el).value;
    const s = new THREE.Mesh(new THREE.SphereGeometry(rmm / mmPerUnit, 16, 12), new THREE.MeshBasicMaterial({ color: 0xe5484d, transparent: true, opacity: 0.55 }));
    s.position.copy(hit.point); activeViewer.root.add(s);
    marks.push({ p: [p.x, p.y, p.z], r_mm: rmm, mesh: s }); count();
  });
  $("#go", el).onclick = async () => {
    const directive = $("#dir", el).value.trim();
    if (!directive && !marks.length) return toast("Bir istek yaz ya da silinecek yerleri işaretle");
    try {
      const j = await api("/api/jobs/edit", { body: { version_id: ver.id, directive, height_mm: +$("#hmm", el).value, effort: eff.value,
        erase: marks.map(m => ({ p: m.p, r_mm: m.r_mm })) } });
      location.hash = `#/job/${j.id}`;
    } catch (e) { toast(e.message); }
  };
}

// ------------------------------------------------------------------ 2D drawings
const PENS = ["#1e1e20", "#2459c8", "#d23b3b", "#2f8f4f", "#e08a1e", "#8a4fd0"];
async function drawingPage(id) {
  let d = id ? await api(`/api/drawings/${id}`) : null;
  const el = h(`<section class="stack"><div class="row"><a href="#/" class="small">← Kitaplık</a></div>
    <div class="row"><h1 class="grow">${d ? esc(d.name) : "Yeni çizim"}</h1></div>
    <div class="split"><div class="stack">
        <div class="paper" id="paper"><canvas id="cv" width="1200" height="900" aria-label="Çizim alanı"></canvas><div class="pins" id="pins"></div></div>
        <div class="row tools"><div class="seg" id="tool"><button data-t="pen" class="on">✏️ Kalem</button><button data-t="eraser">🧽 Silgi</button><button data-t="note">📝 Not</button></div>
          <div class="pens" id="pens"></div>
          <select id="size" style="max-width:120px" aria-label="Kalınlık"><option value="4">İnce</option><option value="9" selected>Orta</option><option value="18">Kalın</option><option value="34">Çok kalın</option></select></div>
        <div class="row"><button class="ghost small" id="undo">↶ Geri al</button><button class="ghost small" id="clear">Temizle</button>
          <button class="ghost small" id="photo">📷 Kâğıttaki çizimi yükle</button><input type="file" id="pf" accept="image/*" hidden>
          <span class="grow"></span><button class="small" id="save">Kaydet</button></div>
        <p class="small muted">İpucu: düz bir şey (anahtarlık, isimlik, kurabiye kalıbı) için kalın ve kapalı çizgiler çiz. Bir figür için şeklini çiz ve notlarla anlat (renk, boyut, ne olduğu).</p></div>
      <div class="stack"><div class="card stack"><label class="field"><span>Çizimin adı</span><input type="text" id="dname" maxlength="80" value="${esc(d?.name || "")}" placeholder="Örn: Kedi anahtarlık"></label></div>
        <div id="notes"></div><div id="made"></div>
        ${d ? `<button class="danger small" id="del">Çizimi sil</button>` : ""}</div></div></section>`);
  app.append(el);
  const cv = $("#cv", el); const g = cv.getContext("2d"); g.lineCap = g.lineJoin = "round";
  let tool = "pen", color = PENS[0], dirty = false; const undo = [];
  const blank = () => { g.save(); g.globalCompositeOperation = "source-over"; g.fillStyle = "#ffffff"; g.fillRect(0, 0, cv.width, cv.height); g.restore(); };
  blank();
  const snap = () => { undo.push(g.getImageData(0, 0, cv.width, cv.height)); if (undo.length > 25) undo.shift(); };
  if (d) { const im = new Image(); im.onload = () => { fitImage(im); dirty = false; }; im.src = d.image; }
  function fitImage(im) {
    const k = Math.min(cv.width / im.width, cv.height / im.height); const w = im.width * k, hh = im.height * k;
    blank(); g.drawImage(im, (cv.width - w) / 2, (cv.height - hh) / 2, w, hh); dirty = true;
  }
  PENS.forEach((c, i) => { const b = h(`<button type="button" class="pen ${i ? "" : "on"}" style="--c:${c}" aria-label="Renk ${i + 1}"></button>`);
    b.onclick = () => { color = c; $("#pens", el).querySelectorAll(".pen").forEach(x => x.classList.toggle("on", x === b)); if (tool !== "pen") setTool("pen"); }; $("#pens", el).append(b); });
  const setTool = t => { tool = t; $("#tool", el).querySelectorAll("button").forEach(x => x.classList.toggle("on", x.dataset.t === t)); $("#paper", el).dataset.tool = t; };
  $("#tool", el).onclick = e => { const b = e.target.closest("button"); if (b) setTool(b.dataset.t); };
  setTool("pen");
  const at = e => { const r = cv.getBoundingClientRect(); return [(e.clientX - r.left) / r.width, (e.clientY - r.top) / r.height]; };
  let drawing = null;
  cv.addEventListener("pointerdown", e => {
    const [u, v] = at(e);
    if (tool === "note") { notes.compose([Math.round(u * 1000) / 10, Math.round(v * 1000) / 10]); return; }
    cv.setPointerCapture(e.pointerId); snap(); drawing = [u * cv.width, v * cv.height];
    g.globalCompositeOperation = "source-over"; g.strokeStyle = tool === "eraser" ? "#ffffff" : color;
    g.lineWidth = +$("#size", el).value * (tool === "eraser" ? 2.2 : 1) * (e.pointerType === "pen" && e.pressure ? 0.6 + e.pressure : 1);
    g.beginPath(); g.moveTo(...drawing); g.lineTo(drawing[0] + 0.01, drawing[1]); g.stroke();
  });
  cv.addEventListener("pointermove", e => { if (!drawing) return; const [u, v] = at(e); const p = [u * cv.width, v * cv.height];
    g.beginPath(); g.moveTo(...drawing); g.lineTo(...p); g.stroke(); drawing = p; dirty = true; });
  const end = () => (drawing = null); cv.addEventListener("pointerup", end); cv.addEventListener("pointercancel", end);
  $("#undo", el).onclick = () => { const s = undo.pop(); if (s) { g.putImageData(s, 0, 0); dirty = true; } };
  $("#clear", el).onclick = () => { if (confirm("Çizim temizlensin mi?")) { snap(); blank(); dirty = true; } };
  $("#photo", el).onclick = () => $("#pf", el).click();
  $("#pf", el).onchange = () => { const f = $("#pf", el).files[0]; if (!f) return; const im = new Image(); im.onload = () => { snap(); fitImage(im); URL.revokeObjectURL(im.src); }; im.src = URL.createObjectURL(f); };
  const blob = () => new Promise(r => cv.toBlob(r, "image/png"));
  async function save(quiet) {
    const f = new FormData(); f.append("image", await blob(), "cizim.png"); f.append("name", $("#dname", el).value.trim() || "Çizim");
    if (!d) { d = await api("/api/drawings", { form: f }); history.replaceState(null, "", `#/d/${d.id}`); $("h1", el).textContent = d.name; }
    else {
      await api(`/api/drawings/${d.id}/image`, { form: f });
      const nm = $("#dname", el).value.trim(); if (nm && nm !== d.name) { await api(`/api/drawings/${d.id}/rename`, { body: { name: nm } }); d.name = nm; $("h1", el).textContent = nm; }
    }
    dirty = false; if (!quiet) toast("Çizim kaydedildi", 1400);
    return d.id;
  }
  $("#save", el).onclick = async () => { try { await save(); } catch (e) { toast(e.message); } };
  const notes = notesPanel({ kind: "drawing", getDrawingId: () => d?.id,
    ensureDrawing: async () => (!d || dirty) ? save(true) : d.id,
    drawPins: (list, pending) => {
      const box = $("#pins", el); box.innerHTML = "";
      list.forEach(n => box.append(h(`<span class="dpin ${n.status !== "open" ? "done" : ""}" style="left:${n.pos[0]}%;top:${n.pos[1]}%" title="${esc(n.text)}">${esc(n.label)}</span>`)));
      if (pending) box.append(h(`<span class="dpin new" style="left:${pending[0]}%;top:${pending[1]}%">+</span>`));
    } });
  $("#notes", el).append(notes.el); notes.refresh();
  if (d) {
    const jobsDone = d.jobs.filter(j => j.status !== "cancelled");
    if (jobsDone.length) {
      const m = h(`<div class="card stack"><b>Bu çizimden yapılanlar</b></div>`);
      jobsDone.forEach(j => m.append(h(`<a class="row" style="text-decoration:none;color:inherit" href="${j.status === "done" && j.result?.asset_id ? `#/a/${j.result.asset_id}` : `#/job/${j.id}`}">
        <span class="dot ${j.status === "done" ? "ok" : j.status === "failed" ? "bad" : "run"}"></span><span class="grow">${esc(j.result?.explanation_tr || (j.status === "failed" ? j.error : j.status === "waiting" ? "Sana bir sorusu var" : "Ajan çalışıyor…") || "")}</span><span class="small muted">${when(j.created_at)}</span></a>`)));
      $("#made", el).append(m);
    }
    $("#del", el).onclick = async () => { if (confirm("Çizim silinsin mi?")) { await api(`/api/drawings/${d.id}/delete`, { body: {} }); location.hash = "#/"; } };
  }
}

// ------------------------------------------------------------------ admin (owner)
async function adminPage() {
  if (ME.user.role !== "owner") { location.hash = "#/"; return; }
  const o = await api("/api/admin/overview");
  const el = h(`<section class="stack"><h1>Yönetim</h1>
    <div class="card stack"><b>Durum</b><div class="row small">
      <span class="badge ${o.fal.key_configured ? "ok" : "bad"}">fal anahtarı ${o.fal.key_configured ? "var" : "yok"}</span>
      <span class="badge ${o.fal.llm_configured ? "ok" : "bad"}">synthetic ${o.fal.llm_configured ? "var" : "yok"}</span>
      <span class="badge ${o.fal.notify_configured ? "ok" : "warn"}">aicp bildirimi ${o.fal.notify_configured ? "bağlı" : "bağlı değil"}</span></div>
      <p class="small">fal bakiyesi (senin girdiğin): <b>${money(o.fal.balance_set)}</b> · o tarihten beri tahmini harcama <b>${money(o.fal.spent_since)}</b> · günlük sınır ${money(o.fal.daily_cap)} · otomatik yükleme +${money(o.fal.auto_topup)}</p>
      <div class="row"><input type="number" id="falb" step="0.01" min="0" placeholder="Yeni fal bakiyesi ($)" style="max-width:220px"><button class="small" id="setfal">Kaydet</button><button class="ghost small" id="tn">Bildirim testi</button></div>
      <div class="row"><input type="password" id="falk" placeholder="fal.ai API anahtarı (fal.ai → Keys)" autocomplete="off" style="max-width:420px"><button class="small" id="setk">Anahtarı kaydet</button></div></div>
    <div class="card stack"><b>Kullanıcılar</b><div class="scroll"><table id="ut"><tr><th>Kullanıcı</th><th>Rol</th><th>Verilen</th><th>Harcanan</th><th>Kalan</th><th></th></tr></table></div></div>
    <div class="card stack"><b>Bildirimler</b><div class="scroll"><table id="nt"><tr><th>Zaman</th><th>Başlık</th><th>Durum</th></tr></table></div></div>
    <div class="card stack"><b>Hesap hareketleri</b><div class="scroll"><table id="lt"><tr><th>Zaman</th><th>Kullanıcı</th><th>Tür</th><th>Tutar</th><th>Adım</th><th>Durum</th></tr></table></div></div></section>`);
  app.append(el);
  o.users.forEach(u => {
    const b = u.balance; const tr = h(`<tr><td>${esc(u.name)}</td><td>${u.role === "owner" ? "sahip" : "üye"}</td><td>${u.unlimited ? "∞" : money(b.granted)}</td><td>${money(b.spent)}</td><td>${u.unlimited ? "∞" : money(b.available)}</td><td></td></tr>`);
    if (!u.unlimited) { const g = h(`<button class="small ghost">+5 $</button>`); g.onclick = async () => { await api("/api/admin/grant", { body: { slug: u.slug, amount: 5 } }); route(); }; tr.lastChild.append(g); }
    if (u.slug !== ME.user.slug) {
      const k = h(`<button class="small ghost">Yeni giriş bağlantısı</button>`);
      k.onclick = async () => {
        if (!confirm(`${u.name} için yeni bağlantı oluşturulsun mu? Eski bağlantı ve açık oturumlar kapanır.`)) return;
        const r = await api(`/api/admin/users/${u.slug}/rekey`, { body: {} });
        const box = h(`<div class="card stack"><b>${esc(u.name)} için giriş bağlantısı</b><p class="small muted">Bir kez gösterilir. Kopyalayıp ona gönder; telefonunda bir kez açması yeterli.</p><input type="text" readonly value="${esc(r.link)}"><button class="small">Kopyala</button></div>`);
        $("button", box).onclick = async () => { try { await navigator.clipboard.writeText(r.link); toast("Kopyalandı"); } catch { $("input", box).select(); } };
        $("#ut", el).closest(".card").after(box);
      };
      tr.lastChild.append(k);
    }
    $("#ut", el).append(tr);
  });
  o.notifications.forEach(n => $("#nt", el).append(h(`<tr><td>${when(n.created_at)}</td><td><b>${esc(n.title)}</b><div class="small muted">${esc(n.body)}</div></td><td>${n.delivered ? `<span class="badge ok">gönderildi</span>` : `<span class="badge warn">${esc(n.error || "bekliyor")}</span>`}</td></tr>`)));
  o.ledger.forEach(l => $("#lt", el).append(h(`<tr><td>${when(l.created_at)}</td><td>${esc(l.slug)}</td><td>${l.kind === "grant" ? "yükleme" : "harcama"}</td><td>${l.kind === "grant" ? "+" : "−"}${money(l.amount_usd)}</td><td class="small">${esc(l.step || l.note || "")}<div class="muted">${esc(l.endpoint_id || "")}</div></td><td>${esc(l.status)}</td></tr>`)));
  $("#setfal", el).onclick = async () => { const v = parseFloat($("#falb", el).value); if (isNaN(v)) return; await api("/api/admin/fal_balance", { body: { amount: v } }); toast("Kaydedildi"); route(); };
  $("#setk", el).onclick = async () => { try { await api("/api/admin/fal_key", { body: { key: $("#falk", el).value } }); toast("fal anahtarı kaydedildi"); route(); } catch (e) { toast(e.message); } };
  $("#tn", el).onclick = async () => { await api("/api/admin/test_notify", { body: {} }); toast("Test bildirimi gönderildi"); setTimeout(route, 1500); };
}

// ------------------------------------------------------------------ boot
async function boot() {
  try { await refreshMe(); } catch (e) { return; }
  $("#top").hidden = false;
  route();
}
$("#logout").onclick = async () => { await api("/api/logout", { body: {} }).catch(() => {}); ME = null; showLogin(); };

(async () => {
  if (takeKeyFromUrl()) return;
  try { ME = await api("/api/me", { noAuthRedirect: true }); } catch (e) { showLogin(); return; }
  await boot();
})();
