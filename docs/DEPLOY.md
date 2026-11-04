# Deploy (server: /Serkan/MomDesign, https://asli.serkan.work)

1. Upload this repository to File Bridge as `/projects/mom-design/src` (archive upload, which extracts it).
2. As root (aicp repair `exec.shell`): `bash /srv/ai-files/projects/mom-design/src/deploy/install.sh`
   - copies the source to `/Serkan/MomDesign/src`, builds the image (Python 3.12 + Blender LTS) and starts `mom-design` on the `coolify` network (Traefik + Let's Encrypt for `asli.serkan.work`)
   - first install: creates `serkan` (owner, unlimited) and `asli` (member, 5 $) and writes **only Serkan's** login link to File Bridge `/outgoing/mom-design/serkan_giris_baglantisi.txt`
3. Serkan opens his link once → **Yönetim**:
   - pastes the fal.ai API key (if `secrets/fal.key` isn't there)
   - presses **Yeni giriş bağlantısı** next to Aslı and sends her the link (she opens it once per device)
   - presses **Bildirim testi**
4. Updates: upload again, then run `install.sh` again (data and users are kept).

Secrets: `/Serkan/MomDesign/secrets/` (0700) for `fal.key` and `aicp_notify.token`. The synthetic key is mounted read-only from aicp's provider file.
Data: `/Serkan/MomDesign/data` (SQLite + models). CLI: `docker exec mom-design python -m studio.cli balance|grant asli 5|rekey asli`.
