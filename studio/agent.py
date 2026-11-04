"""The design agent: turns the user's notes (pinned on a 3D model or on a 2D drawing) into a print-ready object.

Aslı is new to design and to 3D printing, so the agent decides the technical parts for her (size, base,
wall thickness, colours) and explains what it did in plain Turkish. Every result is a new asset version, and a
print check (MOD-04) is queued for it right away.

  model target:   notes -> plan (syn:large:text) -> Blender edit loop with the pins drawn on its renders
  drawing target: drawing + notes -> plan (syn:large:vision) -> either
                  "relief" (flat keychain / plate / cookie cutter, built locally, free) or
                  "figure" (sketch -> product photo with the image model -> MOD-01 -> 3D)
"""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw

from . import db, falclient, jobs, llm, meshio, relief
from .config import effort, get_settings
from .jobs import Job, JobFailed

BEGINNER_RULES = (
    "The user is a beginner at design and 3D printing; she prints PLA on a Bambu Lab P2S (0.4 mm nozzle, "
    "256 mm bed, up to 4 colours with one AMS). Make choices for her so the result prints first time: it must "
    "stand on a flat bottom (add a base if it would tip over), no part thinner than 1.5 mm, text at least 5 mm "
    "tall, avoid long overhangs and floating pieces, keep it small enough to print in a few hours (40-100 mm "
    "unless she asks for a size), and prefer 1-3 colours.")

MODEL_PLAN_SYSTEM = BEGINNER_RULES + """
You are her design assistant. She left numbered notes on a 3D model (some are pinned to a spot on it). Turn them
into ONE clear editing request for a Blender operator who sees the model and the numbered pins. Keep her intent
and words; add only what makes it printable (e.g. a base). Do not invent unrelated changes.
Return JSON {"directive": English editing request that refers to notes by number ("at pin 2 ..."),
"height_mm": final model height in mm, "colors": 1..4, "explanation_tr": 1-2 simple Turkish sentences telling
her what you will do, "tips_tr": [2-4 short Turkish print tips for this object]}."""

DRAWING_PLAN_SYSTEM = BEGINNER_RULES + """
You are her design assistant. She made a 2D drawing (or photographed one) and left numbered notes; pins are
drawn on the image as numbered magenta circles. Decide how to make it a printable object:
- "relief": the drawing itself becomes a flat object: a keychain (with a ring hole), a plate/sign/coaster/magnet,
  or a cookie cutter. Best for logos, names, outlines, doodles, flat shapes. Free and always printable.
- "figure": a real 3D object (a toy, animal, figurine, vase...) drawn as a sketch. The sketch is first turned
  into a product photo, then into 3D.
Follow her notes when they say what she wants; otherwise choose what suits the drawing best.
Return JSON {"route": "relief"|"figure", "name_tr": short Turkish name (max 40 chars),
"relief": {"kind": "keychain"|"plate"|"cookie_cutter", "width_mm": 30..150, "base_mm": 2..6, "line_mm": 0.8..2,
           "hole": bool, "line_color": "#rrggbb" or null to keep the drawing's colour},
"figure": {"photo_prompt_en": English instruction for an image-editing model that receives the sketch: describe
           the real object to show (shape, colours, materials, parts, what each note asks), as ONE chunky
           printable object, "request_tr": Turkish request for the 3D step (what to make, size), "height_mm": n},
"colors": 1..4, "explanation_tr": 1-2 simple Turkish sentences telling her what you will make and why,
"tips_tr": [2-4 short Turkish print tips]}
Fill only the block of the route you choose (the other may be null)."""

PHOTO_SUFFIX = (" Show it as a real, solid, 3D-printed matte PLA object, front view, the whole object centred and "
                "fully visible, chunky proportions with no thin or fragile parts, plain light grey background, "
                "soft even lighting, no text, no hands, no other objects.")


# ------------------------------------------------------------------ helpers

def mark_pins(src: Path, dst: Path, notes: list[dict], max_px: int = 1024) -> Path:
    """The drawing with numbered magenta circles at the note pins (u, v in 0..100)."""
    im = Image.open(src).convert("RGB")
    im.thumbnail((max_px, max_px))
    w, h = im.size
    d = ImageDraw.Draw(im, "RGBA")
    r = max(10, w // 40)
    for n in notes:
        if not n.get("p"):
            continue
        x, y = w * n["p"][0] / 100, h * n["p"][1] / 100
        d.ellipse([x - r, y - r, x + r, y + r], fill=(214, 40, 160, 220), outline=(255, 255, 255, 255), width=3)
        d.text((x - 4 * len(str(n["n"])), y - 7), str(n["n"]), fill=(255, 255, 255, 255))
    im.save(dst)
    return dst


def _notes_text(notes: list[dict], extra: str) -> str:
    lines = [f"{n['n']}. {n['text']}" + ("" if n.get("p") else " (whole object)") for n in notes]
    if extra:
        lines.append("Also: " + extra)
    return "Notes:\n" + "\n".join(lines)


def _clamp(v, lo, hi, default):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


def _queue_prepare(j: Job, height: float, colors: int) -> str:
    """MOD-04 for the new version, so she lands on a checked, colour-split model."""
    if j.ctx.get("prep_job"):
        return j.ctx["prep_job"]
    pid = jobs.create_job(j.user_id, "prepare", {"version_id": j.version_id, "height_mm": round(height, 2),
                                                  "colors": int(max(1, min(colors, 4))), "remesh": False},
                          asset_id=j.asset_id, version_id=j.version_id)
    j.ctx["prep_job"] = pid
    j.save()
    return pid


def _finish(j: Job, plan: dict, height: float, colors: int) -> None:
    pid = _queue_prepare(j, height, colors)
    ids = [n["id"] for n in j.input.get("notes", []) if n.get("id")]
    for nid in ids:
        db.execute("UPDATE notes SET status='done', updated_at=? WHERE id=? AND job_id=?", db.now(), nid, j.id)
    j.ctx["result"] = {"asset_id": j.asset_id, "version_id": j.version_id, "prep_job": pid,
                       "route": plan.get("route", "edit"), "explanation_tr": plan.get("explanation_tr"),
                       "tips_tr": [str(t) for t in (plan.get("tips_tr") or [])][:5],
                       "notes": j.ctx.get("result_notes", [])}
    j.save()


# ------------------------------------------------------------------ model target

def _run_model(j: Job, eff: dict) -> None:
    from . import blender
    from .jobs import blender_lock, version_path
    notes = j.input.get("notes", [])
    path, ver, meta = version_path(j.input["version_id"])
    if "plan" not in j.ctx:
        j.state("AGENT_PLAN", "Notların okunuyor, plan yapılıyor")
        info = {"model": meta.get("spec", {}).get("description") or meta.get("prompt") or "", "name":
                db.row("SELECT name FROM assets WHERE id=?", ver["asset_id"])["name"],
                "current_height_mm": meta.get("height_mm"), "requested_height_mm": j.input.get("height_mm")}
        plan = llm.chat_json(MODEL_PLAN_SYSTEM, _notes_text(notes, j.input.get("extra", "")) + "\n"
                             + json.dumps(info, ensure_ascii=False), user_id=j.user_id, max_tokens=1500)
        if not isinstance(plan, dict) or not str(plan.get("directive", "")).strip():
            raise JobFailed("Ajan notlardan bir plan çıkaramadı; notları biraz daha açık yaz.")
        j.ctx["plan"] = plan
        if plan.get("explanation_tr"):
            j.log(str(plan["explanation_tr"]))
        j.save()
    plan = j.ctx["plan"]
    height = _clamp(j.input.get("height_mm") or plan.get("height_mm") or meta.get("height_mm"), 5, 250, 80)
    directive = str(plan["directive"]) + "\nBeginner print rules: " + BEGINNER_RULES
    with blender_lock():
        if not j.ctx.get("edited"):
            blender.edit_loop(j, version_id=ver["id"], directive=directive, height=height, notes=notes, eff=eff,
                              source="agent")
            j.ctx["result_notes"] = (j.ctx.get("result") or {}).get("notes", [])
            j.ctx["edited"] = True
            j.save()
    _finish(j, plan, height, int(_clamp(plan.get("colors"), 1, 4, 3)))


# ------------------------------------------------------------------ drawing target

def _run_drawing(j: Job, eff: dict) -> None:
    from .pipelines import _finish_generated, _upload_png, generate_from_image
    s = get_settings()
    notes = j.input.get("notes", [])
    src = j.dir / "drawing.png"
    if not src.exists():
        raise JobFailed("Çizim bulunamadı.")
    if "plan" not in j.ctx:
        j.state("AGENT_PLAN", "Çizimin ve notların inceleniyor")
        marked = mark_pins(src, j.dir / "drawing_pins.png", notes)
        content = [llm.image_block(marked, 1024), {"type": "text", "text": _notes_text(notes, j.input.get("extra", ""))
                                                   + "\nEffort: " + eff["name"]}]
        plan = llm.chat_json(DRAWING_PLAN_SYSTEM, content, vision=True, user_id=j.user_id, max_tokens=2000)
        if not isinstance(plan, dict) or plan.get("route") not in ("relief", "figure"):
            plan = {"route": "relief", "relief": {}, "explanation_tr": "Çizimini düz, renkli bir plaka yaptım."}
        j.ctx["plan"] = plan
        if plan.get("explanation_tr"):
            j.log(str(plan["explanation_tr"]))
        j.save()
    plan = j.ctx["plan"]
    name = str(plan.get("name_tr") or j.input.get("name") or "Çizimden model")[:40]
    colors = int(_clamp(plan.get("colors"), 1, 4, 2))
    meta = {"drawing_id": j.input.get("drawing_id"), "notes": [n["text"] for n in notes], "effort": eff["name"]}

    if plan["route"] == "relief":
        r = plan.get("relief") or {}
        j.state("RELIEF_BUILD", "Çizim düz bir modele dönüştürülüyor")
        line = r.get("line_color")
        rgb = None
        if isinstance(line, str) and len(line) == 7 and line.startswith("#"):
            try:
                rgb = tuple(int(line[i:i + 2], 16) for i in (1, 3, 5))
            except ValueError:
                rgb = None
        try:
            m = relief.build(src, kind=r.get("kind", "keychain"), width_mm=_clamp(r.get("width_mm"), 20, 150, 50),
                             base_mm=_clamp(r.get("base_mm"), 1.5, 6, 3), line_mm=_clamp(r.get("line_mm"), 0.6, 2, 1.2),
                             hole=r.get("hole"), line_rgb=rgb)
        except relief.ReliefError as e:
            raise JobFailed(str(e))
        out = j.dir / "model.glb"
        meshio.export_glb_with_colors(m, out, up="z")
        info = m.metadata["relief"]
        if not j.version_id:
            _finish_generated(j, out, name, "drawing", {**meta, "height_mm": info["height_mm"], "relief": info},
                              src)
        colors = 1 if info["kind"] == "cookie_cutter" else 2
        _finish(j, plan, info["height_mm"], colors)
        return

    f = plan.get("figure") or {}
    photo = j.dir / "sketch_photo.png"
    if not photo.exists():
        j.state("SKETCH_TO_PHOTO", "Çizim gerçek bir nesneye dönüştürülüyor")
        if "drawing_url" not in j.ctx:
            sq = j.dir / "drawing_sq.png"
            from . import imaging
            imaging.square_rgb(Image.open(src).convert("RGBA"), s.view_px, bg=(255, 255, 255)).save(sq)
            j.ctx["drawing_url"] = _upload_png(sq)
            j.save()
        prompt = str(f.get("photo_prompt_en") or "Turn this sketch into the real object it shows.") + PHOTO_SUFFIX
        res = j.fal("sketch_photo", s.view_model, {"prompt": prompt, "reference_image_urls": [j.ctx["drawing_url"]],
                                                    "image_size": {"width": s.view_px, "height": s.view_px},
                                                    "num_images": 1, "output_format": "png", "seed": 4242},
                    falclient.estimate(s.view_model, width=s.view_px, height=s.view_px))
        imgs = res.get("images") or []
        if not imgs:
            raise JobFailed("Çizimden fotoğraf üretilemedi.")
        falclient.download(imgs[0]["url"], photo)
    height = _clamp(j.input.get("height_mm") or f.get("height_mm"), 10, 200, 60)
    request = (str(f.get("request_tr") or "") + f" Yükseklik yaklaşık {height:.0f} mm. " + BEGINNER_RULES).strip()
    if not j.version_id:
        generate_from_image(j, photo, request, False, eff, name=name, extra_meta={**meta, "height_mm": height})
    _finish(j, plan, height, colors)


def run(j: Job) -> None:
    eff = effort(j.input.get("effort"))
    if j.input.get("target") == "drawing":
        _run_drawing(j, eff)
    else:
        _run_model(j, eff)
