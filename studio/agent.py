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
from .jobs import Job, JobFailed, WaitForUser

BEGINNER_RULES = (
    "The user is a beginner at design and 3D printing; she prints PLA on a Bambu Lab P2S (0.4 mm nozzle, "
    "256 mm bed, up to 4 colours with one AMS). Make choices for her so the result prints first time: it must "
    "stand on a flat bottom (add a base if it would tip over), no part thinner than 1.5 mm, text at least 5 mm "
    "tall, avoid long overhangs and floating pieces, keep it small enough to print in a few hours (40-100 mm "
    "unless she asks for a size), and prefer 1-3 colours.")

TALK_RULES = """
How to talk to her: she is Aslı, Serkan's mother, a warm and curious beginner. Every Turkish text you write is
warm, simple and kind, like a friend who is truly happy to help her; speak to her with "sen", as the app does; no
technical words. "praise_tr" is one sincere compliment that names something specific in her idea, drawing or notes
(never generic flattery, never about yourself).
Questions: ask ONLY when the answer would change the object a lot and no sensible default exists (e.g. you cannot
tell what the drawing is, or she wants two things that conflict). Never ask about print settings, sizes you can
choose, colours you can choose or anything technical: decide those yourself. Never ask again what "dialog"
already answers; follow her answers there. When you ask, give 2-4 short answer choices.
Always also return: "praise_tr", "question_tr": null or one short question, "options_tr": [answer choices] or [],
"plan_tr": 1-2 sentences in plain Turkish saying what you will make (shape, size in cm, colours)."""

MODEL_PLAN_SYSTEM = BEGINNER_RULES + TALK_RULES + """
You are her design assistant. She left numbered notes on a 3D model; pinned notes are drawn on the 4 renders
(FRONT, RIGHT, BACK, TOP, 0..100 grid) as numbered magenta circles. LOOK at the renders first: what is at each
pin, and does the model already stand on a base/plinth?
Then turn her notes into ONE clear editing request for a Blender operator who sees the same renders and pins.
Keep her intent and words; do not invent unrelated changes. Never ask for a base when the model already has one,
and never ask to scale, rotate or move the whole model unless she asked for that.
What the operator CAN do: erase material at a pinned spot ("erase at pin 2, radius about 4 mm"; radius = about
half the size of the thing to remove), raised or engraved text and simple symbols, holes, add or cut simple shapes
(cube, cylinder, sphere, cone, torus), a base with a name, smoothing, removing floating bits.
What it CANNOT do: reshape or resize one body part (e.g. make one foot smaller), change the printed colours or
picture on the surface (e.g. redraw the flag's printed crescent), redraw a face. Do not fake those with other
operations. List them in "cant_do_tr" in kind, simple Turkish and say what would help instead (for example a new
photo of the original object); never mention screens or tools the app does not have (she only has notes, the
eraser and sending to you).
Return JSON {"seen": short English description of what is at each pin and whether there is a base,
"directive": English editing request that refers to notes by number ("at pin 2 ...") or "" if nothing is
possible, "cant_do_tr": [..], "colors": 1..4, "explanation_tr": 1-2 simple Turkish sentences telling her what you
will do, "tips_tr": [2-4 short Turkish print tips for this object]}."""

DRAWING_PLAN_SYSTEM = BEGINNER_RULES + TALK_RULES + """
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


def _dialog_text(j: Job) -> str:
    d = j.ctx.get("dialog") or []
    if not d:
        return ""
    out = "\nDialog with her so far (follow it): " + json.dumps(d, ensure_ascii=False)
    if sum(1 for x in d if x.get("kind") == "question") >= get_settings().agent_max_questions:
        out += "\nDo not ask anything more: decide yourself (question_tr = null)."
    return out


def _settle_plan(j: Job, make_plan, est_usd) -> dict:
    """Plan once; pause for a question when the agent really needs one, and (from the user's
    N+1th send on) show the plan and wait for "Tamam". Answers arrive through /api/jobs/<id>/answer."""
    s = get_settings()
    if "confirm" not in j.ctx:
        earlier = db.row("SELECT COUNT(*) AS n FROM jobs WHERE user_id=? AND module='agent' AND created_at<?"
                         " AND id<>?", j.user_id, db.row("SELECT created_at FROM jobs WHERE id=?", j.id)["created_at"],
                         j.id)["n"]
        j.ctx["confirm"] = earlier >= s.agent_confirm_after
        j.save()
    if "plan" not in j.ctx:
        plan = make_plan()
        asked = sum(1 for d in j.ctx.get("dialog", []) if d.get("kind") == "question")
        q = str(plan.get("question_tr") or "").strip()
        if q and asked < s.agent_max_questions:
            j.ctx["ask"] = {"kind": "question", "text": q, "praise": plan.get("praise_tr"),
                            "options": [str(o)[:60] for o in (plan.get("options_tr") or [])][:4]}
            j.log("Sana bir sorusu var: " + q)
            raise WaitForUser()
        j.ctx["plan"] = plan
        j.ctx.pop("ask", None)
        for k in ("praise_tr", "plan_tr"):
            if plan.get(k):
                j.log(str(plan[k]))
        j.save()
    plan = j.ctx["plan"]
    if j.ctx["confirm"] and not j.ctx.get("confirmed"):
        j.ctx["ask"] = {"kind": "confirm", "text": plan.get("plan_tr") or plan.get("explanation_tr") or "",
                        "praise": None if j.ctx.get("dialog") else plan.get("praise_tr"), "est_usd": est_usd(plan)}
        j.log("Plan onayını bekliyor")
        raise WaitForUser()
    j.ctx.pop("ask", None)
    return plan


def answer(j: Job, text: str, ok: bool) -> None:
    """Store her answer to ctx["ask"]; the caller re-queues the job."""
    ask = j.ctx.get("ask") or {}
    dialog = j.ctx.setdefault("dialog", [])
    if ask.get("kind") == "confirm":
        if ok and not text:
            j.ctx["confirmed"] = True
        else:   # she wants a change: re-plan with her words, then confirm again
            dialog.append({"kind": "change", "plan": ask.get("text"), "answer": text})
            j.ctx.pop("plan", None)
    else:
        dialog.append({"kind": "question", "question": ask.get("text"),
                       "answer": text or "Sen karar ver, en uygununu seç."})
    j.ctx.pop("ask", None)
    j.log("Cevabın: " + (text or ("Tamam" if ok else "Sen karar ver")))


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
                       "praise_tr": plan.get("praise_tr"),
                       "tips_tr": [str(t) for t in (plan.get("tips_tr") or [])][:5],
                       "cant_do_tr": [str(x) for x in (plan.get("cant_do_tr") or [])][:5]}
    er = j.ctx.get("edit_result") or {}
    j.ctx["result"].update({"notes": er.get("notes") or [], "missing_tr": er.get("missing_tr") or [],
                            "undone": er.get("undone") or 0})
    j.save()


# ------------------------------------------------------------------ model target

EDIT_PRINT_RULES = ("Print rules: keep every new part at least 1.5 mm thick and text at least 5 mm tall. Do not add "
                    "a base if the model already has one; do not scale, rotate or move the whole model.")


def _run_model(j: Job, eff: dict) -> None:
    from . import blender
    from .jobs import blender_lock, version_path
    notes = j.input.get("notes", [])
    path, ver, meta = version_path(j.input["version_id"])
    height = _clamp(j.input.get("height_mm") or meta.get("height_mm"), 5, 250, 80)
    with blender_lock():                      # the plan is made while LOOKING at the model and the pins
        blender.start_edit(j, path, height)

    def make_plan() -> dict:
        j.state("AGENT_PLAN", "Notların ve model inceleniyor, plan yapılıyor")
        pins, mesh = blender.note_pins(notes, path, meta.get("up", "y"), height)
        marks = blender.project_pins(pins, j.ctx["frame"], mesh) if pins and j.ctx.get("frame") else {}
        info = {"model": meta.get("spec", {}).get("description") or meta.get("prompt") or "", "name":
                db.row("SELECT name FROM assets WHERE id=?", ver["asset_id"])["name"],
                "height_mm": height, "size_mm": j.ctx.get("stats", {}).get("size_mm")}
        content = blender._views_content(j.ctx["renders"], j.dir, "plan", marks)
        content.append({"type": "text", "text": _notes_text(notes, j.input.get("extra", "")) + "\n"
                        + json.dumps(info, ensure_ascii=False) + _dialog_text(j)})
        plan = llm.chat_json(MODEL_PLAN_SYSTEM, content, vision=True, user_id=j.user_id, max_tokens=4000)
        if not isinstance(plan, dict) or not (str(plan.get("directive", "")).strip() or plan.get("question_tr")
                                              or plan.get("cant_do_tr")):
            raise JobFailed("Ajan notlardan bir plan çıkaramadı; notları biraz daha açık yaz.")
        return plan
    plan = _settle_plan(j, make_plan, lambda p: 0.0)
    cant = [str(x) for x in (plan.get("cant_do_tr") or [])][:5]
    if not str(plan.get("directive", "")).strip():
        raise JobFailed("Bu notları şu an yapamıyorum: " + " ".join(cant) if cant else
                        "Ajan notlardan bir plan çıkaramadı; notları biraz daha açık yaz.")
    directive = str(plan["directive"]) + "\n" + EDIT_PRINT_RULES
    with blender_lock():
        if not j.ctx.get("edited"):
            blender.edit_loop(j, version_id=ver["id"], directive=directive, height=height, notes=notes, eff=eff,
                              source="agent")
            r = j.ctx.get("result") or {}
            j.ctx["edit_result"] = {k: r.get(k) for k in ("notes", "missing_tr", "undone", "last_word")}
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
    def make_plan() -> dict:
        j.state("AGENT_PLAN", "Çizimin ve notların inceleniyor")
        marked = mark_pins(src, j.dir / "drawing_pins.png", notes)
        content = [llm.image_block(marked, 1024), {"type": "text", "text": _notes_text(notes, j.input.get("extra", ""))
                                                   + "\nEffort: " + eff["name"] + _dialog_text(j)}]
        plan = llm.chat_json(DRAWING_PLAN_SYSTEM, content, vision=True, user_id=j.user_id, max_tokens=2000)
        if not isinstance(plan, dict):
            plan = {}
        if plan.get("route") not in ("relief", "figure") and not plan.get("question_tr"):
            plan = {**plan, "route": "relief", "relief": plan.get("relief") or {},
                    "explanation_tr": plan.get("explanation_tr") or "Çizimini düz, renkli bir plaka yapıyorum."}
        return plan

    def cost(p: dict) -> float:
        from .pipelines import estimate
        if p.get("route") != "figure":
            return 0.0
        return estimate("agent", {"target": "drawing", "effort": eff["name"]})
    plan = _settle_plan(j, make_plan, cost)
    if plan.get("route") not in ("relief", "figure"):
        plan["route"] = "relief"
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
