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
}

// ------------------------------------------------------------------ router
const routes = [
  [/^#?\/?$/, libraryPage], [/^#\/new$/, newPage], [/^#\/new\/photo$/, photoPage], [/^#\/new\/multi$/, multiPage],
  [/^#\/new\/file$/, filePage], [/^#\/job\/([\w]+)$/, jobPage], [/^#\/a\/([\w]+)$/, assetPage],
  [/^#\/a\/([\w]+)\/print$/, printPage], [/^#\/a\/([\w]+)\/edit$/, editPage], [/^#\/admin$/, adminPage],
];
async function route() {
  if (!ME) return;
  stopAll();
  const hash = location.hash || "#/";
  document.querySelectorAll(".topnav a").forEach(a => a.classList.toggle("on",
    (a.dataset.nav === "lib" && (hash === "#/" || hash.startsWith("#/a/"))) || (a.dataset.nav === "new" && hash.startsWith("#/new")) || (a.dataset.nav === "admin" && hash === "#/admin")));
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
  const [{ assets }, { jobs }] = await Promise.all([api(`/api/assets?all=${all}`), api("/api/jobs?active=1")]);
  const el = h(`<section class="stack">
    <div class="row"><h1 class="grow">Kitaplık</h1><a class="btn" href="#/new">+ Yeni model</a></div>
    <div id="running"></div><div class="grid" id="grid"></div></section>`);
  app.append(el);
  if (jobs.length) {
    const r = $("#running", el);
    r.append(h(`<div class="card stack"><b>Devam eden işler</b></div>`));
    jobs.forEach(j => r.firstChild.append(h(`<a href="#/job/${j.id}" class="row" style="text-decoration:none;color:inherit">
      <span class="dot run"></span><span class="grow">${esc(jobTitle(j))}</span><span class="muted small">${esc(j.log.at(-1)?.m || "Sırada")}</span></a>`)));
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
function jobTitle(j) { return { mod01: "Fotoğraftan model", mod02: "Çoklu fotoğraftan model", import: "Dosya yükleme", prepare: "Baskı hazırlığı", resegment: "Renk bölgeleri", export: "Baskı dosyası", edit: "Blender düzenleme" }[j.module] || j.module; }

// ------------------------------------------------------------------ new
function newPage() {
  app.append(h(`<section class="stack"><h1>Yeni model</h1>
    <div class="modes">
      <a class="mode" href="#/new/photo"><span class="ic">📷</span><b>Bir fotoğraftan</b><span class="muted">Fotoğraf + kısa açıklama. Atölye diğer açıları kendisi çizer ve 3B modeli kurar.</span></a>
      <a class="mode" href="#/new/multi"><span class="ic">🖼️</span><b>Birkaç fotoğraftan</b><span class="muted">Aynı nesnenin önden, yandan, arkadan çekilmiş 2–6 fotoğrafı.</span></a>
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
      <div id="cost"></div>
      <button id="go" disabled>Modeli oluştur</button>
      <p class="small muted">Fotoğrafta tek nesne olsun, net ve iyi ışıkta. İşlem 2–4 dakika sürer.</p></div></section>`);
  app.append(el);
  const p = picker(false, "image/*"); $("#pick", el).append(p);
  const upd = async () => { $("#cost", el).replaceChildren(await costLine("mod01", `&include_top=${$("#top", el).checked ? 1 : 0}`)); };
  p.addEventListener("change", () => ($("#go", el).disabled = !p.files.length));
  $("#top", el).onchange = upd; upd();
  $("#go", el).onclick = async () => {
    const f = new FormData(); f.append("image", p.files[0]); f.append("prompt", $("#prompt", el).value); f.append("include_top", $("#top", el).checked ? "1" : "0");
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
};
async function jobPage(id) {
  const el = h(`<section class="stack" style="max-width:720px"><h1 id="jt">İş</h1><div class="card stack">
    <div class="bar"><i style="width:5%"></i></div><ul class="steps" id="steps"></ul><div id="foot" class="row"></div></div>
    <div id="views" class="previews"></div></section>`);
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
    if (j.module === "edit" && j.status === "done") showViews(id, $("#views", el), j, "final_", ["front", "right", "back", "top"]);
    if (j.status === "done") {
      await refreshMe();
      const aid = j.result?.asset_id || j.asset_id;
      if (aid && ["mod01", "mod02", "import"].includes(j.module)) { toast("Model hazır!"); location.hash = `#/a/${aid}`; return; }
      foot.append(h(`<a class="btn" href="#/a/${aid}">Modele git</a>`)); return;
    }
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
    <div class="split"><div id="vbox"></div>
      <div class="stack">
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
  let cur = v;
  const show = async ver => {
    cur = ver; $(".loading", vb).hidden = false;
    $("#vers", el).querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.id === ver.id));
    const m = ver.meta || {};
    $("#vmeta", el).textContent = [ver.source === "mod03" ? "Blender düzenlemesi" : ver.source === "upload" ? "Yüklenen dosya" : "Üretilen model",
      m.prompt ? `“${m.prompt}”` : "", m.directive ? `“${m.directive}”` : "", when(ver.created_at)].filter(Boolean).join(" · ");
    try {
      await activeViewer.load(ver.url); $(".loading", vb).hidden = true;
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

// ------------------------------------------------------------------ print prep (MOD-04)
async function printPage(id) {
  const a = await api(`/api/assets/${id}`);
  const ver = chosenVersion(a, id);
  const lastPrep = a.jobs.find(j => j.module === "prepare" && j.status === "done" && j.version_id === ver.id);
  const el = h(`<section class="stack"><div class="row"><a href="#/a/${id}" class="small">← ${esc(a.name)}</a></div>
    <h1>Baskıya hazırla</h1>
    <div class="split"><div class="stack"><div id="vbox"></div>
        <div class="row"><div class="seg" id="mode"><button data-m="color" class="on">Renkler</button><button data-m="overhang">Sarkmalar</button><button data-m="thin">İnce duvar</button></div></div></div>
      <div class="stack">
        <div class="card stack" id="setup"><b>Ayarlar</b>
          <label class="field"><span>Yükseklik (mm)</span><input type="number" id="hmm" min="5" max="250" value="80"></label>
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
        <label class="field"><span>Model yüksekliği (mm)</span><input type="number" id="hmm" value="${Math.round(ver.meta?.height_mm || 80)}" min="5" max="250"></label>
        <button id="go">Uygula</button><p class="small muted">Ücretsiz (yapay zekâ + Blender). 1–5 dakika.</p></div></div></section>`);
  app.append(el);
  ["Altına 8 mm yuvarlak taban ekle ve önüne ismimi yaz", "Altına düz kare taban ekle", "Anahtarlık için üstüne 4 mm delik aç", "Boşta duran küçük parçaları temizle"].forEach(t => {
    const b = h(`<button type="button">${esc(t)}</button>`); b.onclick = () => ($("#dir", el).value = t); $("#ex", el).append(b);
  });
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
      const j = await api("/api/jobs/edit", { body: { version_id: ver.id, directive, height_mm: +$("#hmm", el).value,
        erase: marks.map(m => ({ p: m.p, r_mm: m.r_mm })) } });
      location.hash = `#/job/${j.id}`;
    } catch (e) { toast(e.message); }
  };
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
