# Mom_Design — 3D Generative & CAD Modification Engine on aicp

Implementation plan · 2026-11-04 · status: **draft, waiting for Serkan's decisions (§9)**

This plan places the spec "Autonomous 3D Generative & CAD Modification Engine v1.0.0" (MOD-01…MOD-04) on top of the
current aicp system, with two accounts: **Serkan** (unlimited) and **Aslı** (5 USD fal.ai budget).

---

## 1. What exists today (observed 2026-11-04)

| Area | Fact | Source |
|---|---|---|
| Server | Contabo VPS, Ubuntu 24.04, 8 cores, 24 GB RAM (~8 GB free), 258 GB disk free, **no GPU** | `aicp_system host` |
| Runtimes | Python 3.12, Node 20, Docker 29.9, Claude Code 2.1.312 | `aicp_system runtime` |
| aicp | Python service + SQLite, healthy. Agents run as Claude Code turns in systemd scopes (1.5 GB limit, **max 2 turns at once**). Web UI `aicp.serkan.work`, MCP connector, File Bridge, vault, Onay Kuyruğu (asks), schedules, mini apps, root repair harness | `aicp_health`, vault `1_Projects/AI_Kontrol_Duzlemi.md` |
| Auth | **Single user.** Tokens (`read/send/admin`, kind `human`/`connector`) → HttpOnly cookie. No users table, no roles, no per-user data | vault `AI_Kontrol_Duzlemi.md` |
| Models | `synthetic` provider: `syn:large:text`, `syn:large:vision` (vision on uploaded images). Subscription is paid again (Serkan, 4 Nov); the 402 errors in the 2–4 Nov agent logs should stop | `aicp_models`, agent logs |
| fal.ai | `fal` MCP (OAuth) is on Serkan's account, status `ready`. Agent tool set `fal` + custom skill `fal-media` (cheapest model first, ≤0.05 $/job without approval). Top-up was 10 $ on 9 Oct; **current balance unknown** | `check_account_status`, `aicp_skills show fal-media` |
| Module precedent | **Devreler** (3 Nov): an aicp-native module (SQLite tables + `/v1/circuits*` API + `#/devreler` page + agent tool `cp_devre` + MCP tool `aicp_devreler`) with a two-agent design/review loop and approval in Onay Kuyruğu. It is the template for this project | vault `1_Projects/Devreler_Plani.md` |
| Blender | Used on the **MacBook** (Blender 5.2.2, PREKİT render pipeline). **Not installed on the server.** The vault records 4 CVEs in 2026 for the community Blender MCP: use safe mode or keep it away from unattended runs | vault `PREKIT_Render_Hatti.md` |
| Bambu | Serkan prints on a **Bambu P2S**. A reference project exists: `~/Nilda` on the MacBook (luggage tag, 3 colour variants) | vault `MacBook_Pro_Envanteri.md` |
| Lessons | Mini-app iframes have no same-origin, so fetch/localStorage fail. Agent turns can't write `/srv/ai-files/projects` (Devreler moved to `outgoing`). Don't stop processes with `pkill -f`. Deploys kill running turns | agent memories, Pano |

This repository (`SerkanEvin/Mom_Design`) was empty. It will hold the **studio worker** (pipelines, fal client, mesh
tools, Blender runner, 3MF exporter, tests). The aicp integration (users, ledger, API, UI page) goes into
`~/workspace/ai-control-plane` through `aicp-gelistirici`, the same way Devreler did.

---

## 2. Architecture

```
 Aslı / Serkan (browser)                     Claude (claude.ai MCP)
        │  #/studio (aicp web UI, own login)          │ aicp_studio tool
        ▼                                             ▼
 ┌─────────────────────── aicp (existing service) ───────────────────────┐
 │ users + roles (NEW)   studio_* tables (NEW)   credit ledger (NEW)     │
 │ /v1/studio/* API      state machine per job   Onay Kuyruğu, notify    │
 └────────┬──────────────────────┬───────────────────────────┬───────────┘
          │ job queue            │ agent turns (only where   │ files
          ▼                      │ tool-use is needed)       ▼
 ┌──── studio-worker (THIS REPO, systemd, Seko-Claude) ────┐  /srv/ai-files/outgoing/studio/<user>/<asset>/<ver>/
 │ llm.py      → synthetic API (syn:large:text / vision)   │
 │ fal.py      → fal REST queue, cost reserved in ledger   │
 │ mesh/       → trimesh + manifold3d (audit, repair)      │
 │ blender/    → blender -b, whitelisted ops only          │
 │ export3mf/  → Bambu-compatible 3MF                      │
 └─────────────────────────────────────────────────────────┘
```

Design rules, carried over from PREKİT and Devreler:

1. **Logic and money live in deterministic code, not in an LLM.** The worker calls fal over REST and checks the
   ledger before each call. An LLM can't spend past the cap, because it never holds the fal key.
2. **Single-shot LLM steps call the synthetic API directly** (JSON in, JSON out). That covers intent extraction,
   per-view prompts, view QA and the printability explanation. A full Claude Code agent turn costs ~300 MB and one of
   only 2 concurrency slots, which is too much for a single JSON call.
3. **Agent turns only where tools matter.** MOD-03 is an edit loop (plan → run → render → inspect → fix), so it runs as
   an aicp agent. Every agent action goes through the worker's whitelisted Blender ops.
4. **Every step is a saved state.** The job record holds the state (`INTENT_EXTRACTION`, `PERSPECTIVE_SYNTHESIS`, …),
   inputs, outputs, cost and errors. A failed step can be retried without paying again for the steps before it.
5. **Cheapest model first, with an upgrade path** (the same rule as the `fal-media` skill).

---

## 3. Accounts, roles, credits

### 3.1 Users (new in aicp)
| user | role | fal budget | can see |
|---|---|---|---|
| `serkan` | `owner` | unlimited (`credit_limit_usd = NULL`), still logged | everything (as today) |
| `asli` | `member` | **5.00 USD** hard cap | only `#/studio` and her own assets |

- Migration: `users(id, slug, name, role, credit_limit_usd, created_at, disabled)`, plus `user_id` on web
  sessions/tokens. Existing tokens map to `serkan`, so current behaviour doesn't change.
- **The member role is a strict allow-list, enforced on the server:** `/api/v1/studio/*`, signed file links for her own
  assets, and `GET /api/v1/me`. No agents, chat, vault, files, system, repair, keys or board. The cookie for a
  `member` session is rejected on every other route. This matters because aicp can run root repairs.
- Login: a one-time link like the current `deploy/webui/token.sh` (`--user asli`). Serkan sends it to Aslı and she opens
  it once per device. Optionally serve the studio at its own hostname (e.g. `studio.serkan.work`), so her login never
  touches the panel's origin.
- The UI is in Turkish and works on mobile first (Aslı will probably use a phone or tablet).

### 3.2 Credit ledger
- `studio_ledger(id, user_id, job_id, step, endpoint_id, units, est_usd, charged_usd, status reserved|settled|refunded, fal_request_id, created_at)`.
- `balance = limit − Σ settled − Σ reserved`.
- **Before a job starts:** estimate the whole pipeline (§5) and refuse it if the estimate exceeds the balance. Show
  "this will cost ≈ $0.04, you have $4.21 left".
- **Before each fal call:** reserve that call's estimate. On success, settle it. If fal fails with an error or a
  timeout, refund it.
- Charged amount = the price from `get_pricing` × the billed units (megapixels, generations, compute seconds),
  computed from the actual request. A nightly job reconciles against fal's usage API if the key has that scope;
  otherwise the estimate is the charge.
- Alerts: Aslı sees her balance in the header. Serkan gets an aicp notification at 80 % and at 100 %. Serkan can top
  up with `aicp studio credit asli +5` (CLI, owner only), and every top-up is logged.
- The **real fal account balance** is Serkan's and is shared. Before Aslı starts, it must cover her 5 $ on top of
  Serkan's own use. An owner-only health check calls `check_account_status`.
- Synthetic LLM calls are covered by the subscription and are **not** charged against the 5 $. They are rate-limited
  per user instead (e.g. 60 calls/hour for `member`), because synthetic's 5-hour and weekly limits are shared with all
  of Serkan's agents.

---

## 4. Models

### 4.1 LLM side (synthetic, as requested)
| Role | Model | Used in |
|---|---|---|
| Text planner | `syn:large:text` | MOD-01 intent and semantic spec, MOD-03 action plan (JSON ops), MOD-04 report wording |
| Vision | `syn:large:vision` | MOD-01 source-image analysis and per-view prompts, QA of generated views, MOD-03 render inspection, MOD-04 colour-region naming |

### 4.2 fal.ai side — test tier (prices from `get_pricing`, 2026-11-04)
| Step | Default (cheap) | Price | Upgrade when quality is not enough |
|---|---|---|---|
| View synthesis (MOD-01 s3) | `fal-ai/hidream-o1-image/dev/edit` at **768×768** (default is 2048², which would cost 4×) | 0.005 $/MP → ~0.003 $/view | `fal-ai/qwen-image-edit-2511` 0.03 $/MP |
| Background removal (MOD-02 s1, MOD-01 s3) | `fal-ai/imageutils/rembg` | ~0.001 $/image | `pixelcut/background-removal` 0.016 $ |
| Multi-image → 3D (MOD-01 s4, MOD-02 s2) | **`fal-ai/trellis/multi`** (textured GLB, 1–N images, p50 ≈ 27 s) | **0.02 $/generation** | `fal-ai/hunyuan3d/v2/multi-view/turbo` 0.14 $ (front/back/left) |
| Single image → 3D fallback | `fal-ai/trellis/multi` with 1 image | 0.02 $ | `fal-ai/hunyuan3d/v2/mini/turbo` 0.08 $ untextured (3× textured) |
| Lighting/contrast cleanup | free, local (Pillow: auto-contrast, white balance, crop to alpha bbox) | 0 $ | `fal-ai/hidream-o1-image/dev/edit` relight |

Avoid for now: Tripo (billed in "credits", so the cost isn't known in advance), Meshy multi (0.80 $), and every model
billed in "units" without a confirmed rate (`trellis-2`, Hunyuan 3.1). Phase 0 confirms `trellis/multi`'s flat 0.02 $
with one real run, because its pricing unit is returned empty.

---

## 5. Modules → concrete pipelines

Every module produces an **asset version** (`studio_assets`, `studio_versions`): files in
`/srv/ai-files/outgoing/studio/<user>/<asset>/<v>/`, a thumbnail, the job log and the cost. The viewer is three.js
`GLTFLoader` + OrbitControls on the first-party `#/studio` page (not a sandboxed mini app, which can't fetch).

### MOD-02 · Multi-image → 3D (built first: simplest end to end)
1. Validate: 1–6 images, ≤ 10 MB each, decodable, and the same object (a vision check with `syn:large:vision`, which
   warns but doesn't block).
2. Cleanup: rembg on each image (fal) → local auto-contrast, crop to the alpha bbox, pad to a square 768 px canvas,
   neutral grey background.
3. `trellis/multi` (`texture_size 1024`, `mesh_simplify 0.95`) → `model.glb`.
4. Viewer + "Send to print prep" (MOD-04).
- **Estimate:** n × 0.001 + 0.02 ≈ **0.025 $** (4 images).

### MOD-01 · Single image + text → multiview → 3D
1. `INTENT_EXTRACTION` (`syn:large:text`): user prompt → `semantic_spec {object, parts[], materials, colours[], size_mm?, symmetry, must_keep[]}` and `topology_intent` (organic / hard-surface / flat-relief).
2. `MULTIVIEW_PROMPT_GENERATION` (`syn:large:vision` + spec): one prompt per canonical camera. **Fixed camera set:**
   elevation 0°, azimuth 0/90/180/270 (front, right, back, left), orthographic-style, same scale and centring, plain
   background. `top` (elevation 90°) is optional and off by default, because trellis does better with 4 coherent
   side views than with 5 mismatched ones.
3. `PERSPECTIVE_SYNTHESIS`: hidream edit with the source image as reference, one call per view (768 px), then rembg.
   **QA gate** (`syn:large:vision`): does each view show the same object, rotated correctly? Up to 1 automatic redo per
   view, then the user picks.
4. `3D_RECONSTRUCTION`: `trellis/multi` with source + views → `model.glb`.
5. `VIEWPORT_PRESENTATION`.
- **Estimate:** 4 × 0.003 + 5 × 0.001 + 0.02 ≈ **0.04 $** (≈ 0.05 $ with one QA redo).
  **Aslı's 5 $ is roughly 100 test generations.**

### MOD-04 · Printability review & colour segmentation (built before MOD-03: printing is the goal, and the checks are deterministic)
1. `MANIFOLD_AND_PRINTABILITY_AUDIT` (trimesh + manifold3d, CPU, seconds):
   - non-manifold edges, watertight, inverted normals, degenerate faces;
   - floating islands (connected components below a volume threshold);
   - overhang: share of faces whose normal points down more than 45° from vertical, as a heat-map texture in the viewer;
   - wall thickness: inward ray casting along sampled normals, flagging walls under 0.8 mm (P2S, 0.4 mm nozzle);
   - scale: AI meshes come out unitless, so the user sets the target height in mm before any check.
   - Output: `printability_report.json` + `is_printable`. `syn:large:text` turns it into plain Turkish advice.
   - Auto-repair button: manifold3d / pymeshfix, or a Blender voxel remesh (MOD-03 op) when topology is badly broken.
2. `SURFACE_COLOR_SEGMENTATION`:
   - Sample the GLB texture per face → k-means in Lab colour space into **N ≤ 4 colours** (one AMS, set in §9) →
     smooth the labels over face adjacency so there are no speckles → the user can merge, split or recolour regions in
     the viewer (bucket fill on a region).
   - Optional: `syn:large:vision` names the regions ("body", "eyes", "base") from renders.
   - **Technical correction to the spec:** splitting one watertight mesh into separate face-group shells makes each
     shell *open*, and the slicer can't fill open shells. Two modes, then:
     - **Default — painted single body:** keep one watertight mesh and write per-triangle filament paint into the 3MF
       (Bambu Studio's paint attribute). The model opens already painted, and the user can still bucket-fill in Bambu.
     - **Parts mode:** closed solids, made by boolean splitting along region boundaries (manifold3d), saved as named
       objects with an extruder per object. Use it for functional multi-part prints.
3. `EXPORT_HANDOFF`: `model_colored.3mf` (millimetres, Z up, on the bed, centred) with `Metadata/model_settings.config`
   (object → extruder 1–4) and a filament colour list. **It is validated against a 3MF saved by Bambu Studio itself**
   (the Nilda project is the reference) and by a real import into Bambu Studio on the MacBook before it is called done.

### MOD-03 · Agentic Blender editor
- **Blender on the server:** the official Linux tarball (5.2 LTS) under `/srv/aicp-work/studio/blender/`. It needs no
  root and runs headless with `-b --factory-startup`. Previews use Cycles on the CPU at 512 px with few samples.
  EEVEE and Workbench need OpenGL, which a headless server with no GPU may not have; P0 checks this. MacBook Cycles
  is an optional extra for high-quality renders.
- **Not the community Blender MCP addon** (it needs a GUI socket, and the vault records 4 CVEs in 2026). The project has
  its own thin tool layer instead, exposed as aicp agent tools (`cp_studio_*`) and run by the worker:
  `import_mesh(path)`, `export_mesh(path, format)`, `transform(scale|rotate|translate)`, `boolean_difference(target, tool_primitive|tool_asset)`,
  `boolean_union`, `remesh_voxel(resolution_mm)`, `decimate(ratio)`, `subdivide(levels)`, `solidify(thickness_mm)`,
  `add_primitive(type, dims, pos)`, `emboss_text(text, font, depth)`, `check_manifold_status()`, `render_views(n)`.
  Each op is one JSON object, checked against a schema, then run by a fixed `runner.py` inside a `systemd-run` scope
  (3 GB RAM, 5-minute timeout, one Blender job at a time).
- `run_python_script(code)` is **owner-only**, never enabled for `member`.
- STEP import: `cadquery-ocp` in the worker venv converts STEP → STL/GLB before Blender.
- Loop (agent `studio-editor`, `syn:large:vision`, at most 4 iterations, like the Devreler design loop): directive +
  renders + `check_manifold_status` → op plan → run → re-render → VLM self-review → done, or one more pass → new asset
  version + preview. Undo = go back to the previous version.

---

## 6. Agents to create (Serkan approves before creation)
| slug | profile | model | job |
|---|---|---|---|
| `studio-editor` | developer (workdir `/srv/aicp-work/studio`) | synthetic `syn:large:vision` | MOD-03 edit loop through `cp_studio_*` tools only |
| `studio-gelistirici` *(optional)* | developer | synthetic `syn:large:text` | builds and maintains this repo. Alternative: give the work to `aicp-gelistirici` |

Single-shot LLM steps (MOD-01 s1–s3 QA, MOD-02 validation, MOD-04 wording) are **direct API calls from the worker, not
agents**.

---

## 7. Phases

| # | Phase | Output | Test spend |
|---|---|---|---|
| **P0** | Spike + decisions (1 day) | Confirm the real cost of `trellis/multi`, hidream 768 px and rembg with one run each on a test image. Check that Blender 5.2 runs headless on the server, that trimesh, manifold3d and cadquery-ocp install in a venv, and that synthetic `syn:large:vision` accepts images from the worker. Get a Bambu-saved painted 3MF from the Nilda project | ≈ 0.05 $ (Serkan) |
| **P1** | Accounts + ledger + skeleton (aicp) | `users` migration, member allow-list + tests that try every non-studio route with Aslı's cookie, ledger with reserve/settle/refund, `/v1/studio/*` + `#/studio` page (upload, library, viewer, balance), one-time login link for Aslı | 0 $ |
| **P2** | MOD-02 | rembg → trellis → GLB in the viewer. Library and versions | ≈ 0.10 $ |
| **P3** | MOD-01 | Intent → per-view prompts → views + QA gate → 3D. Aslı's first real use | ≈ 0.30 $ |
| **P4** | MOD-04 | Audit + heat maps + repair, colour regions, painted 3MF + parts mode, a real P2S test print | ≈ 0 $ (local) |
| **P5** | MOD-03 | Blender tarball, op layer, `studio-editor` agent, edit loop | ≈ 0 $ fal |
| **P6** | Hardening | Nightly ledger reconciliation, file retention (e.g. drop intermediate views after 30 days), owner dashboard (spend per user, failures), vault note per milestone | — |

All test spend is charged to `serkan` (unlimited) during development. Aslı's 5 $ starts at 0 spent on launch day.

Definition of done per phase: worker unit tests (`pytest`) + aicp tests pass, a headless-browser run of the studio page
at 390 px and 1280 px, one real end-to-end job per module with its cost shown in the ledger, and a vault note.

---

## 8. Risks
| Risk | Mitigation |
|---|---|
| Aslı's session reaches aicp's powerful routes | Server-side allow-list + a negative test per route. Optional separate hostname. `member` never gets agents, files or repair |
| fal account drained by a bug | Ledger reserves before each call, a global daily cap (e.g. 2 $/day across users), an owner alert at 80 % |
| Shared synthetic rate limits slow Serkan's other agents | Per-user call limit. Studio LLM calls are cheap single shots, not agent turns |
| Server has no GPU | All GPU work is on fal. Blender previews use the CPU renderer at low resolution. MacBook Cycles is optional |
| RAM (≈ 8 GB free, 2 agent slots) | Worker jobs run in systemd scopes with limits, one Blender job at a time, a queue with position shown in the UI |
| AI meshes are not printable (thin walls, open shells) | MOD-04 audit is required before 3MF export, with auto-repair through a voxel remesh |
| Bambu 3MF details (paint attribute, `model_settings.config`) change between Studio versions | Reverse-engineer from a file the installed Bambu Studio saved, keep a golden-file test, re-check when Bambu Studio updates |
| aicp deploys kill running turns | The worker is a separate systemd service and job state is persisted, so it resumes after a restart |

---

## 9. Decisions needed from Serkan
1. **Front door for Aslı:** a `member` role inside aicp with a strict allow-list *(recommended)*, optionally on its own
   hostname, **or** a fully separate small app that calls aicp with a service token?
2. **fal key for the worker:** the pipeline needs a server-side fal API key (stored like `providers/synthetic.key`,
   0600, never shown to agents) so cost is enforced in code. The `fal-media` skill's "no API key" rule stays in force
   for agents. OK?
3. **What the 5 $ covers:** fal spend only (recommended; synthetic is subscription), or LLM usage too?
4. **AMS:** how many filament slots does the P2S have (1 AMS = 4)? This sets the colour cap in MOD-04.
5. **When Aslı's balance runs out:** hard stop and ask Serkan *(recommended)*, or auto top-up?
6. **Agents:** create `studio-editor` (and optionally `studio-gelistirici`), or hand the build to `aicp-gelistirici`?
7. **fal balance:** what is the current balance? It must cover Aslı's 5 $ plus development (~1 $).
