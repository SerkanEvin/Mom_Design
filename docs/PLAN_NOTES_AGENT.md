# Notes + design agent — rollout plan

Plan · 2026-11-04 · status: **R0 + R1 done — deployed 2026-11-04 (commit `2ccec68`), self-test passed in the container; next: R2 smoke test**

This plan covers getting the notes, drawing, design-agent, usage-gauge and effort features (see `PLAN.md` §10,
"Added 2026-11-04 (later)") from a pushed branch to Aslı printing her first object on her own.

---

## 1. Where it stands

| Part | State | Proof so far |
|---|---|---|
| Notes on models and drawings, `/api/notes*` | built | API tests: access rules, validation, 40-note cap |
| Drawing page `#/draw`, `/api/drawings*` | built | headless browser at 390 px and 1280 px: draw, pin, save, no console errors, no sideways scroll |
| Agent, drawing → flat relief | built | test: drawing → keychain → print check passes (watertight, 4.2 mm, 2 colours, $0) |
| Agent, drawing → 3D figure | built | test with fake fal/LLM: 1 sketch photo + MOD-01 calls, cost estimate matches |
| Agent, model notes → Blender | built | test with fake Blender: pins reach the planner with u/v, rounds follow effort |
| Usage gauge, effort levels | built | tests + screenshots |
| **Real synthetic planner reading pins** | **not tried** | — |
| **Real Blender run of an agent job** | **not tried** | the projection maths matches the runner's camera, unit-tested only |
| **Real print of a relief** | **not tried** | — |

Nothing new needs a fal key, a new secret or a new container setting. The database changes only add tables
(`CREATE TABLE IF NOT EXISTS notes / drawings`), so the existing data stays as it is.

---

## 2. Phases

### R0 · Before deploying (Claude, ~1 h, $0) — done
1. **Extend the offline self-test** (`python -m studio.selftest`, runs in the container with real Blender):
   - *Pin check:* put a pin on the test model's red ball, run Blender's first render, project the pin with
     `project_pins`, and check that the render pixel at that u/v is red and that the pin is hidden in the BACK view.
     This is the one link between the agent's maths and Blender that the tests could not cover.
   - *Relief check:* build a keychain from a generated drawing, run the print check (watertight, printable,
     2 colours) and the painted 3MF export.
2. Merge the branch into `main` (Serkan approves the merge).

**Done when:** `pytest` is green and the self-test passes locally, apart from the Blender-only steps, which R1 runs.

### R1 · Deploy (infra agent through aicp, ~15 min) — done 2026-11-04
Result: backup in `/Serkan/MomDesign/backup-20261104/` (database integrity ok + old `src`); image rebuilt in ~4 min;
`/healthz` ok; tables `notes` and `drawings` created; self-test all pass: the pin landed on the red ball in Blender's
FRONT render (u 50, v 42.6, pixel 210/84/73) and was hidden from the back; the relief keychain was printable; base text and
eraser passed. Note: the live `src` is now a plain copy (no `.git`); the old one with `.git` is in the backup.
1. Back up `/Serkan/MomDesign/data/studio.sqlite`, plus a copy of the current `src`, to
   `/Serkan/MomDesign/backup-2026-11-xx/`.
2. Upload the repo through the File Bridge to `/projects/mom-design/src` and run `deploy/install.sh` as before.
   The deploy restarts the container. Running jobs continue after the restart from their saved state, so pick a
   time when Aslı isn't using it anyway.
3. Inside the container: `python -m studio.selftest` (now including the R0 checks).

**Done when:** `/healthz` is OK, the self-test passes and the existing library still opens.
**Rollback:** put the backup `src` back and run `docker compose up -d --build`. The new tables can stay, because the old
code ignores them.

### R2 · Serkan's smoke test (Serkan's account, ≈ 0.15 $ fal, ~30 min)
Use Serkan's unlimited account, so Aslı's 5 $ starts untouched.

| # | Test | Pass when |
|---|---|---|
| 1 | Draw a simple shape (heart + name) → note "anahtarlık olsun" → agent, effort **Orta** | relief route, $0, ring hole at the top, print check "Basılabilir", 2 colours |
| 2 | Photo of a paper sketch of an animal → note "oyuncak, 6 cm" → agent, **Yüksek** | figure route, cost ≤ estimate, model looks like the sketch, print check queued and finished |
| 3 | Existing figure → pin on the chest "buraya ASLI yaz" + general note "taban ekle" → agent, **Yüksek** | text lands at the pin (not elsewhere), base added, one new version, old one kept |
| 4 | Same as 3 at **Ekstra** | 5 rounds max, takes longer, result at least as good |
| 5 | Pin a note, then make the job fail (cancel it) | note shows as open again and can be re-sent |
| 6 | Usage gauge after tests 1–4 | today's spend and AI calls match the Yönetim ledger |

If test 3 puts text in the wrong place, the pin → render projection is wrong. Fix it before Aslı gets access.
The self-test in R0 should already catch this.

### R3 · Real prints on the P2S (Serkan, ~3 h machine time, $0)
1. Print test 1's keychain from the painted 3MF: the 4.2 mm plate must not warp, the ring hole must be clean (4.4 mm),
   and the raised lines must take the second filament. This also closes the "painted 3MF opens with the right colours
   in Bambu Studio" item from `PLAN.md`.
2. Print test 3's figure: the base must stand flat and the letters must be readable.
3. Note what had to be changed in Bambu Studio by hand (supports, brim, colours). Each of those becomes either an
   agent rule or a tip it gives.

**Done when:** both prints work without changes in Bambu Studio other than picking filaments.

### R4 · Aslı's first use (~30 min with Serkan beside her)
1. Before: add a small first-use card on the library page (dismissable): "1) Çiz ya da fotoğraf yükle → 2) Not
   bırak → 3) Ajana gönder → 4) Baskıya hazırla → 5) Bambu dosyasını indir". Add three starter ideas as buttons on the
   drawing page: *isimlik anahtarlık*, *kurabiye kalıbı*, *küçük hayvan figürü*.
2. Send her login link (Yönetim → "Yeni giriş bağlantısı") and have her open it once on her phone.
3. First session: she makes a keychain from her own drawing, from start to finished print. Serkan only watches and
   writes down where she gets stuck.
4. After that she uses it alone. Serkan gets the usual notifications (80 % credit, auto top-up).

### R5 · Two weeks of use, then tune (Claude, ~2 h)
Watch: how many agent jobs finish, how many results get printed, cost per object, which effort she picks, and which
notes the agent misunderstood (from the job logs). Possible adjustments:
- rules or examples in `BEGINNER_RULES`, `DRAWING_PLAN_SYSTEM` and `MODEL_PLAN_SYSTEM`;
- the relief defaults (plate 3 mm, lines 1.2 mm, width 50 mm);
- the effort table in `config.EFFORTS` (for example, if Orta is never good enough, make it the same as Yüksek
  minus the top view);
- a vault note with the results.

---

## 3. Serkan's decisions (2026-11-04)
| # | Question | Decision | Built as |
|---|---|---|---|
| 1 | When to deploy | **Today** | R0 → R1 run the same day |
| 2 | Aslı's default effort | **Yüksek** | already the default (`config.DEFAULT_EFFORT`) |
| 3 | Show the plan before spending? | **Not on her 1st or 2nd send** | the agent just does her first 2 sends; from the 3rd on it shows its plan in plain Turkish (with the cost, or "ücretsiz") and waits for "✅ Tamam, yap", or re-plans with what she writes. `STUDIO_AGENT_CONFIRM_AFTER=2` |
| 4 | Can the agent ask her? | **Yes, when necessary, and compliment her: she is Serkan's mother** | the job pauses (`waiting`) with one short question, 2–4 answer buttons, "🤷 Sen karar ver" and a free-text box; at most 2 questions per job (`STUDIO_AGENT_MAX_QUESTIONS`), never about technical settings. Every plan starts with one sincere, specific compliment about her idea or drawing, shown on the question, plan and result cards. The library shows "Sana bir sorusu var" |

## 4. Risks
| Risk | Mitigation |
|---|---|
| The planner ignores the pins or misreads u/v | R0 self-test checks the projection; R2 test 3 checks the planner; pins also go as text with their u/v |
| A pin placed before a base or scale change points to the wrong place later | Pins are only drawn while nothing has moved the model, then passed as text only (built in) |
| A messy sketch photo (shadows, lined paper) makes a speckled relief | The paper colour is measured from the brightest 10 %, specks are removed, crumbs under 2 % are dropped. R5 adds a "kâğıdı düz ve aydınlık çek" hint if needed |
| The sketch → photo step invents a different object | The figure route's photo is shown on the job page; QA of the generated views still runs; Ekstra redoes more views |
| Ekstra makes Blender jobs long (5 rounds) while other jobs wait | Only one Blender job runs at a time already; the queue position is shown. Lower Ekstra to 4 rounds if waits get long |
| AI calls: one agent job uses 3–12 synthetic calls of her 60/hour | The gauge shows it; Ekstra uses the most. Raise `STUDIO_MEMBER_LLM_PER_HOUR` if she hits it |

---

## 5. Incident 2026-11-04: the flag figure (R2 smoke test) and the fix

**What happened** (job `job_6aeb9609fb6861be1e`, notes: 1 "delete this", 2 "draw the Turkish flag properly, crescent
and star side by side", 3 "both feet equal"):
- The agent wrote its plan **without seeing the model** (text only), so it asked for a base. The figure already had
  one. The planner then also scaled and moved the whole model, after which the note pins were text only.
- Note 1 ("delete this") became a **12 × 14 × 12 mm box cut into the leg**: a rectangular hole.
- Round 1 embossed "☾ ★" on the flag. The reviewer saw only the AFTER renders and still listed the flag as missing.
  Round 3 then embossed "☾" and "★" **again**, next to the first ones. The repeat guard only matched identical text,
  so the result had two crescents and two stars. The reviewer noticed, but the rounds had run out, and the job still
  ended "done" with that version.
- Note 3 was impossible with the tools. The agent told her to use an "Ağ Düzenleme" screen that does not exist.
- The first try (Ekstra) failed on an empty answer from the planner.

**What was changed in the system** (the model was not fixed by hand):
1. **Each round is reviewed BEFORE vs AFTER**: same 4 views side by side, plus close-ups around the pinned notes.
   The reviewer gives `good` / `bad` / `no_change`.
2. **A bad round is rolled back.** The model, renders and frame go back to before that round, and the planner sees
   it as `rejected` with the reasons. Rolled-back ops are not saved in the version.
3. **The repeat guard knows where things are.** New text or symbols are dropped when they are made only of symbols
   already embossed and sit near an earlier emboss in the same view. A second base is still dropped. Only rounds
   that are really on the model count.
4. **`erase_note`**: the planner can erase exactly at a note's 3D pin (the eraser ball, plus removing floating
   bits) instead of cutting shapes into the figure.
5. **The agent plans while looking at the model.** The first Blender render, with the pins drawn on it, goes to the
   plan step. It must not add a base when one exists, must not scale or move the whole model unless asked, and
   lists what it **cannot** do in `cant_do_tr` in kind Turkish, without inventing app screens. If nothing is
   possible, the job ends with that explanation and her notes become open again.
6. **Honest result**: leftover items (`missing_tr`), impossible items and the number of undone attempts are shown
   on the result card ("Henüz yapamadıklarım").
7. **Empty AI answers**: the next try gets twice the token budget (up to 16 k). The planner now has 6 k.
8. **Done notes can be reopened** (↺) and sent again.

Not covered yet: resizing one part (e.g. one foot) and recolouring a printed picture would need new Blender
operations (a region scale and a texture paint).

---

## 6. Blender work moves to an aicp agent (2026-11-04, Serkan's decision)

Serkan: "Aslı is my mother and only she does the job, so no protection is needed." The fixed operation list is
replaced by the aicp agent **`studio-editor`** (Claude Code, `claude-opus-5-5`, effort medium, developer profile,
workdir `/srv/aicp-work/studio-editor`). It has its own Blender 5.2.3 LTS at `…/blender/blender`, writes free
Blender Python, reviews its own renders before and after, and keeps memory in its vault folder. A sandboxed
free-code option for other users comes later.

- **Exchange:** `/srv/aicp-work/studio-editor/jobs/<job_id>/` (2777) is mounted in the container as `/exchange`.
  The studio writes `job.json` (protocol, notes with `pin_gltf` + `pin_mm`, effort, dialog, `confirm_plan`) and
  `input.glb`. The agent writes `result.glb` + `result.json` (`done|question|confirm|failed`, praise, message,
  missing, tips, height, colours) and optionally `renders/*.png`.
- **Messages:** aicp hook `POST /hook/studio/send`, `GET /hook/studio/status` on `10.0.1.1:7812`, with a token
  that can only reach `studio-editor` (`secrets/aicp_studio.token`). Her answers go to the same session.
- **Studio** (`studio/aicp_agent.py`): agent jobs on models and the Blender edit page (request + eraser marks
  become notes) are handed to the agent. Drawings (relief/figure) and photo → 3D stay as they are. Without the token
  the built-in loop is used (tests, fallback).
