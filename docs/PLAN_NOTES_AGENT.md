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
