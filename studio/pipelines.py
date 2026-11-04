"""MOD-01..MOD-04 + import, as resumable step functions. Each step checks ctx so a retry never repeats paid work."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image

from . import falclient, imaging, llm, meshio
from .config import get_settings
from .jobs import Job, JobFailed, add_version, blender_lock, create_asset, version_path

# ------------------------------------------------------------------ prompts

INTENT_SYSTEM = (
    "You turn a request for a 3D-printable object into a spec for multi-view 3D generation. The request may be "
    "Turkish. Return JSON: {\"object\": short English noun phrase, \"description\": English, 1-2 sentences of visual "
    "detail, \"parts\": [..], \"colors\": [..], \"materials\": [..], \"size_mm\": number|null, \"symmetry\": "
    "\"bilateral\"|\"radial\"|\"none\", \"topology_intent\": \"organic\"|\"hard-surface\"|\"flat-relief\", "
    "\"must_keep\": [details that must survive], \"name_tr\": short Turkish library name (max 40 chars)}.")

VIEWS_SYSTEM = (
    "You plan camera views of the SAME object shown in the reference image, for multi-view 3D reconstruction. "
    "Cameras: elevation 0°, near-orthographic, object centred. Azimuth 0° = front (as the reference shows it), "
    "90° = right side, 180° = back, 270° = left side; top = looking straight down. For each view write ONE English "
    "instruction for an image-editing model that receives the reference image: say which side is seen, and describe "
    "exactly what is visible from that side (shape, colours, parts) using the spec. Keep proportions, colours and "
    "materials identical. Return JSON {\"front\": str, \"right\": str, \"back\": str, \"left\": str, \"top\": str, "
    "\"notes\": str}.")

QA_SYSTEM = (
    "The first image is the reference (front view). The following images are generated views, in the order listed "
    "in the text. For each generated view judge: is it the same object (shape, colours, parts), seen from the stated "
    "side, fully visible, with no extra objects or text? Be strict about wrong viewpoint or a different object, "
    "lenient about small detail changes. Return JSON {\"<view>\": {\"ok\": bool, \"reason\": short English}}.")

MULTI_SYSTEM = (
    "The images should show ONE object from different sides, for 3D reconstruction. Return JSON {\"same_object\": "
    "bool, \"usable\": [bool per image, in order], \"issues\": short Turkish note for the user (empty if fine)}.")

ADVICE_SYSTEM = (
    "You explain a 3D-print check to a beginner who prints on a Bambu Lab P2S (0.4 mm nozzle, PLA). Input is a JSON "
    "report (sizes in mm). Write in simple Turkish. Return JSON {\"summary\": one sentence, \"tips\": [2-5 short "
    "practical tips: supports, orientation, scale, wall thickness, colours]}.")

VIEW_SUFFIX = (" Keep the exact same object, colours, materials and proportions as the reference. Camera: {cam}. "
               "Near-orthographic product view, object centred and fully visible, filling about 80% of the frame, "
               "plain light grey background, soft even lighting, no shadows, no text, no other objects.")

CAMERAS = {
    "right": "elevation 0°, azimuth 90°, looking at the object's right side",
    "back": "elevation 0°, azimuth 180°, looking at the object's back",
    "left": "elevation 0°, azimuth 270°, looking at the object's left side",
    "top": "looking straight down from above (elevation 90°)",
}


# ------------------------------------------------------------------ estimates (shown before start)

def estimate(module: str, inp: dict) -> float:
    s = get_settings()
    rembg = falclient.estimate(s.rembg_model)
    recon = falclient.estimate(s.recon_model)
    if module == "mod01":
        views = 4 if inp.get("include_top") else 3
        view = falclient.estimate(s.view_model, width=s.view_px, height=s.view_px)
        return round(rembg * (1 + views) + view * views + recon + (view + rembg) * 2, 4)  # + up to 2 QA redos
    if module == "mod02":
        return round(rembg * int(inp.get("count", 1)) + recon, 4)
    return 0.0


# ------------------------------------------------------------------ shared steps

def _upload_png(path: Path) -> str:
    return falclient.upload_bytes(path.read_bytes(), "image/png", path.name)


def _cutout(j: Job, key: str, src: Path, out: Path) -> Path:
    """rembg (paid, ~0.002 $) + local normalisation."""
    if out.exists():
        return out
    s = get_settings()
    url = j.ctx.setdefault("uploads", {}).get(src.name)
    if not url:
        url = _upload_png(src)
        j.ctx["uploads"][src.name] = url
        j.save()
    res = j.fal(key, s.rembg_model, {"image_url": url}, falclient.estimate(s.rembg_model))
    raw = falclient.download(res["image"]["url"], j.dir / f"{out.stem}_rembg.png")
    im = Image.open(raw)
    if imaging.alpha_fraction(im) < 0.01:
        raise JobFailed("Arka plan silinince görselde nesne kalmadı; daha net bir fotoğraf dene.")
    imaging.save_png(imaging.normalize_cutout(im, size=s.view_px), out)
    return out


def _reconstruct(j: Job, images: list[Path]) -> Path:
    s = get_settings()
    out = j.dir / "model.glb"
    if out.exists():
        return out
    urls = j.ctx.setdefault("recon_urls", [])
    if not urls:
        urls = [_upload_png(p) for p in images]
        j.ctx["recon_urls"] = urls
        j.save()
    args = {"image_urls": urls, "texture_size": 1024, "mesh_simplify": 0.95, "multiimage_algo": "stochastic"}
    res = j.fal("recon", s.recon_model, args, falclient.estimate(s.recon_model))
    mesh = res.get("model_mesh") or {}
    if not mesh.get("url"):
        raise JobFailed("3B model üretilemedi (modelden dosya gelmedi).")
    falclient.download(mesh["url"], out)
    return out


def _finish_generated(j: Job, model: Path, name: str, source: str, meta: dict, thumb_src: Path | None) -> None:
    if not j.asset_id:
        j.asset_id = create_asset(j.user_id, name, source)
        j.save()
    extra = []
    if thumb_src and thumb_src.exists():
        t = j.dir / "source.png"
        if thumb_src != t:
            Image.open(thumb_src).save(t)
        extra.append(t)
    j.version_id = add_version(j.asset_id, source, model, {"up": "y", **meta}, extra_files=extra)
    j.ctx["result"] = {"asset_id": j.asset_id, "version_id": j.version_id}
    j.save()


# ------------------------------------------------------------------ MOD-01 single image + text

def mod01(j: Job) -> None:
    s = get_settings()
    src = j.dir / "input_0.png"
    prompt = (j.input.get("prompt") or "").strip()
    views = ["right", "back", "left"] + (["top"] if j.input.get("include_top") else [])

    if "spec" not in j.ctx:
        j.state("INTENT_EXTRACTION", "İstek anlaşılıyor")
        text = prompt or "Make a 3D printable version of the object in the image."
        j.ctx["spec"] = llm.chat_json(INTENT_SYSTEM, text, user_id=j.user_id)
        j.save()
    spec = j.ctx["spec"]

    if "view_prompts" not in j.ctx:
        j.state("MULTIVIEW_PROMPT_GENERATION", "Görünüşler planlanıyor")
        content = [llm.image_block(src), {"type": "text", "text": "Spec:\n" + json.dumps(spec, ensure_ascii=False)
                                          + "\nUser request: " + prompt}]
        j.ctx["view_prompts"] = llm.chat_json(VIEWS_SYSTEM, content, vision=True, user_id=j.user_id)
        j.save()
    vp = j.ctx["view_prompts"]

    j.state("BACKGROUND_REMOVAL", "Arka plan siliniyor")
    front = _cutout(j, "rembg_front", src, j.dir / "view_front.png")

    ref = j.dir / "reference.png"
    if not ref.exists():
        imaging.square_rgb(Image.open(front), s.view_px).save(ref)
    if "ref_url" not in j.ctx:
        j.ctx["ref_url"] = _upload_png(ref)
        j.save()

    def make_view(v: str, attempt: int) -> Path:
        out = j.dir / f"view_{v}.png"
        if out.exists():
            return out
        instr = str(vp.get(v) or f"Show the {v} side of the object.")
        args = {"prompt": instr + VIEW_SUFFIX.format(cam=CAMERAS[v]), "reference_image_urls": [j.ctx["ref_url"]],
                "image_size": {"width": s.view_px, "height": s.view_px}, "num_images": 1, "output_format": "png",
                "seed": 1000 + attempt * 77 + list(CAMERAS).index(v)}
        res = j.fal(f"view_{v}_{attempt}", s.view_model, args,
                    falclient.estimate(s.view_model, width=s.view_px, height=s.view_px))
        imgs = res.get("images") or []
        if not imgs:
            raise JobFailed("Görünüş üretilemedi.")
        raw = falclient.download(imgs[0]["url"], j.dir / f"view_{v}_{attempt}_raw.png")
        return _cutout(j, f"rembg_{v}_{attempt}", raw, out)

    j.state("PERSPECTIVE_SYNTHESIS", "Diğer açılar üretiliyor")
    for v in views:
        make_view(v, j.ctx.get("attempt", {}).get(v, 0))

    if "qa" not in j.ctx:
        j.state("VIEW_QA", "Görünüşler kontrol ediliyor")
        qa = _qa(j, front, views)
        bad = [v for v in views if not qa.get(v, {}).get("ok", True)][:2]
        for v in bad:   # one redo for at most two views
            j.ctx.setdefault("attempt", {})[v] = 1
            (j.dir / f"view_{v}.png").unlink(missing_ok=True)
            j.log(f"{v} görünüşü yeniden üretiliyor: {qa[v].get('reason', '')}")
            make_view(v, 1)
        if bad:
            qa2 = _qa(j, front, bad)
            for v in bad:
                qa[v] = qa2.get(v, qa[v])
        j.ctx["qa"] = qa
        j.save()
    qa = j.ctx["qa"]
    good = [v for v in views if qa.get(v, {}).get("ok", True)]
    dropped = [v for v in views if v not in good]
    if dropped:
        j.log("Kullanılmayan görünüşler: " + ", ".join(dropped))

    j.state("3D_RECONSTRUCTION", "3B model oluşturuluyor")
    model = _reconstruct(j, [front] + [j.dir / f"view_{v}.png" for v in good])
    name = spec.get("name_tr") or prompt[:40] or "Yeni model"
    _finish_generated(j, model, name, "mod01", {"prompt": prompt, "spec": spec, "views": good}, front)


def _qa(j: Job, front: Path, views: list[str]) -> dict:
    content = [llm.image_block(front, 512)]
    for v in views:
        content.append(llm.image_block(j.dir / f"view_{v}.png", 512))
    content.append({"type": "text", "text": "Generated views in order: " + ", ".join(views)})
    try:
        out = llm.chat_json(QA_SYSTEM, content, vision=True, user_id=j.user_id)
        return out if isinstance(out, dict) else {}
    except llm.LLMError as e:
        j.log(f"Görünüş kontrolü atlandı ({e})")
        return {}


# ------------------------------------------------------------------ MOD-02 multiple images

def mod02(j: Job) -> None:
    n = int(j.input.get("count", 0))
    imgs = [j.dir / f"input_{i}.png" for i in range(n)]
    if not imgs:
        raise JobFailed("Görsel yok.")

    if "check" not in j.ctx:
        j.state("IMAGE_VALIDATION_AND_CLEANUP", "Görseller kontrol ediliyor")
        try:
            content = [llm.image_block(p, 512) for p in imgs]
            j.ctx["check"] = llm.chat_json(MULTI_SYSTEM, content, vision=True, user_id=j.user_id)
        except llm.LLMError as e:
            j.ctx["check"] = {"issues": f"kontrol yapılamadı: {e}"}
        if j.ctx["check"].get("issues"):
            j.log("Not: " + str(j.ctx["check"]["issues"]))
        j.save()
    usable = j.ctx["check"].get("usable")
    keep = [p for i, p in enumerate(imgs) if not isinstance(usable, list) or i >= len(usable) or usable[i]] or imgs

    j.state("BACKGROUND_REMOVAL", "Arka planlar siliniyor")
    cleaned = [_cutout(j, f"rembg_{i}", p, j.dir / f"clean_{i}.png") for i, p in enumerate(keep)]

    j.state("MULTI_IMAGE_3D_GENERATION", "3B model oluşturuluyor")
    model = _reconstruct(j, cleaned)
    _finish_generated(j, model, j.input.get("name") or "Çoklu görselden model", "mod02",
                      {"images": len(cleaned)}, cleaned[0])


# ------------------------------------------------------------------ import an uploaded 3D file

def import_file(j: Job) -> None:
    j.state("ASSET_INGESTION", "Dosya okunuyor")
    src = j.dir / j.input["file"]
    m = meshio.load(src)
    up = meshio.up_axis_for(src)
    out = j.dir / "model.glb"
    if src.suffix.lower() == ".glb":
        out = src
    else:
        meshio.export_glb_with_colors(m, out, up=up)
    lo, hi = m.bounds
    _finish_generated(j, out, j.input.get("name") or src.stem, "upload",
                      {"original": src.name, "faces": int(len(m.faces)),
                       "source_size": [round(float(x), 3) for x in (hi - lo)]}, None)


# ------------------------------------------------------------------ MOD-04 print preparation

def _prep_paths(j: Job) -> dict:
    return {"npz": j.dir / "print.npz", "preview": j.dir / "preview.glb", "arrays": j.dir / "arrays.json"}


def prepare(j: Job) -> None:
    from . import audit, segment
    height = float(j.input.get("height_mm", 80))
    if not 5 <= height <= 300:
        raise JobFailed("Yükseklik 5–300 mm arasında olmalı (P2S tablası 256 mm).")
    k = int(j.input.get("colors", 3))
    path, ver, meta = version_path(j.input["version_id"])
    j.asset_id, j.version_id = ver["asset_id"], ver["id"]
    p = _prep_paths(j)

    j.state("MANIFOLD_AND_PRINTABILITY_AUDIT", "Model baskı için hazırlanıyor")
    m = meshio.to_print_frame(meshio.load(path), meta.get("up", "y"), height)
    m, notes = audit.basic_repair(m)
    if not m.is_watertight or j.input.get("remesh"):
        j.log("Model kapalı değil; voksel ile yeniden örülüyor")
        m = audit.voxel_remesh(m)
        notes.append("kapalı olmayan yüzey voksel yöntemiyle yeniden örüldü (renkler korunarak)")
    report, flags = audit.audit(m)
    report["repairs"] = notes

    j.state("SURFACE_COLOR_SEGMENTATION", "Renk bölgeleri ayrılıyor")
    seg = segment.segment(m, k=k)
    meshio.save_npz(m, p["npz"])
    meshio.export_preview_glb(m, p["preview"])
    p["arrays"].write_text(json.dumps({
        "labels": seg["labels"].tolist(), "regions": seg["regions"].tolist(),
        "overhang": np.flatnonzero(flags["overhang"]).tolist(), "thin": np.flatnonzero(flags["thin"]).tolist()}))

    advice = None
    try:
        advice = llm.chat_json(ADVICE_SYSTEM, json.dumps(report, ensure_ascii=False), user_id=j.user_id,
                               max_tokens=800)
    except llm.LLMError:
        pass
    j.ctx["result"] = {"report": report, "palette": seg["palette"], "advice": advice, "height_mm": height,
                       "faces": int(len(m.faces)), "asset_id": j.asset_id, "version_id": j.version_id}
    j.save()


def resegment(j: Job) -> None:
    from . import segment
    prep = _load_prep_job(j)
    m, _ = meshio.load_npz(prep.dir / "print.npz")
    j.state("SURFACE_COLOR_SEGMENTATION", "Renk bölgeleri yeniden ayrılıyor")
    seg = segment.segment(m, k=int(j.input.get("colors", 3)), seed=int(j.input.get("seed", 0)))
    arrays = json.loads((prep.dir / "arrays.json").read_text())
    arrays.update({"labels": seg["labels"].tolist(), "regions": seg["regions"].tolist()})
    (j.dir / "arrays.json").write_text(json.dumps(arrays))
    j.ctx["result"] = {"palette": seg["palette"], "prep_job": prep.id}
    j.save()


def export(j: Job) -> None:
    from . import threemf
    from .segment import regions as _regions  # noqa: F401
    prep = _load_prep_job(j)
    m, _ = meshio.load_npz(prep.dir / "print.npz")
    labels = np.asarray(j.input.get("labels") or [], dtype=np.int32)
    if len(labels) != len(m.faces):
        raise JobFailed("Renk listesi modelle uyuşmuyor; sayfayı yenileyip tekrar dene.")
    if labels.min() < 0 or labels.max() > 15:
        raise JobFailed("Filament numarası 1–16 arasında olmalı.")
    palette = [str(c) for c in (j.input.get("palette") or [])][:16]
    while len(palette) <= labels.max():
        palette.append("#cccccc")
    name = (j.input.get("name") or "model").replace("/", "_")[:40]
    mode = j.input.get("mode", "painted")
    j.state("EXPORT_HANDOFF", "Baskı dosyası hazırlanıyor")
    if mode == "painted":
        out = j.dir / f"{name}_boyali.3mf"
        out.write_bytes(threemf.painted_3mf(m, labels, palette, name))
    elif mode == "parts":
        parts = threemf.split_parts(m, labels)
        out = j.dir / f"{name}_parcali.3mf"
        out.write_bytes(threemf.parts_3mf(parts, palette, name))
        (j.dir / f"{name}_parcalar_stl.zip").write_bytes(threemf.parts_stl_zip(parts, palette, name))
    elif mode == "stl":
        out = j.dir / f"{name}.stl"
        out.write_bytes(threemf.stl_bytes(m))
    else:
        raise JobFailed("Bilinmeyen çıktı türü.")
    files = [out.name] + ([f"{name}_parcalar_stl.zip"] if mode == "parts" else [])
    j.ctx["result"] = {"files": files, "mode": mode, "palette": palette, "prep_job": prep.id}
    j.save()


def _load_prep_job(j: Job) -> Job:
    from . import db
    r = db.row("SELECT * FROM jobs WHERE id=? AND user_id=? AND module='prepare' AND status='done'",
               j.input.get("prep_job"), j.user_id)
    if not r:
        raise JobFailed("Önce baskı hazırlığını çalıştır.")
    return Job(r)


# ------------------------------------------------------------------ MOD-03 Blender editor

def edit(j: Job) -> None:
    from . import blender
    with blender_lock():
        blender.edit_loop(j)


MODULES = {"mod01": mod01, "mod02": mod02, "import": import_file, "prepare": prepare,
           "resegment": resegment, "export": export, "edit": edit}
