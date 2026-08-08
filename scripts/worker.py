#!/usr/bin/env python3
"""Worker batch InfiniteTalk (pod RunPod).

Boucle : réclame un job à MassContent -> génère (pipeline natif 720p verrouillée via generate.py)
-> upload S3 (URL presignée PUT) -> signale la fin. S'auto-termine après inactivité
prolongée (zéro gaspillage GPU). Heartbeat de progression vers MassContent pendant
la génération (visibilité sans SSH).

Config (env, injectée au create du pod par l'orchestrateur MassContent) :
  MASSCONTENT_BASE_URL   URL de base MassContent (ex https://masscontent.pro)
  PIPELINE_SECRET        secret d'auth des workers (header x-pipeline-secret)
  IDLE_EXIT_SECONDS      inactivité avant auto-arrêt du pod (defaut 300)
  POLL_EMPTY_SECONDS     délai entre deux réclamations à vide (defaut 10)
  HEARTBEAT_SECONDS      période du heartbeat /progress pendant la génération (defaut 45)
  IT_ATTENTION           sageattn (defaut) | sdpa (Phase 2, GPU non-Blackwell)
  IT_BLOCKSWAP           blocks à swapper (defaut 10 ; 20 conseillé sur 24 Go)
  (RUNPOD_POD_ID auto-injecté par RunPod ; l'auto-terminaison passe par l'endpoint
   MassContent /api/workers/runpod/terminate — plus de RUNPOD_API_KEY sur le pod, PV-003)
"""
import os, sys, time, json, socket, subprocess, threading, urllib.request, urllib.parse, traceback, glob

# Garde-fou global : urlretrieve (download assets) n'a pas de timeout socket par
# défaut → un GET S3/CDN qui hang gèlerait le pod indéfiniment. 180s borne tout
# appel socket bloquant sans timeout explicite (les urlopen ont déjà le leur).
socket.setdefaulttimeout(180)

MC_URL = os.environ.get("MASSCONTENT_BASE_URL", "").rstrip("/")
PIPELINE_SECRET = os.environ.get("PIPELINE_SECRET", "")
COMFY = "http://127.0.0.1:8188"
INPUT_DIR = "/workspace/ComfyUI/input"
GEN = "/workspace/generate.py"
IDLE_EXIT_S = int(os.environ.get("IDLE_EXIT_SECONDS", "300"))
POLL_EMPTY_S = int(os.environ.get("POLL_EMPTY_SECONDS", "10"))
HEARTBEAT_S = int(os.environ.get("HEARTBEAT_SECONDS", "45"))
# Détection du fournisseur GPU + POD_ID (id natif utilisé par /terminate côté
# MassContent). Chaque fournisseur expose son identité différemment :
#  - Novita : N'INJECTE AUCUN id natif dans le conteneur → l'orchestrateur lui passe
#    au create la var NOVITA_WORKER_NAME (= le nom unique du pod). Le worker la
#    rapporte comme POD_ID et MassContent résout nom→id natif pour le DELETE.
#    Testé EN PREMIER (priorité) pour qu'un éventuel CONTAINER_ID parasite n'écrase
#    pas l'identité Novita.
#  - Vast : injecte CONTAINER_ID = l'instance id réel → POD_ID = CONTAINER_ID
#    (c'est lui qui part à /terminate pour viser la bonne instance Vast).
#  - Clore : comme Novita, AUCUN id natif dans le conteneur → on passe CLORE_WORKER_NAME
#    (nom unique). Le worker s'identifie provider=clore avec ce nom (visibilité /claim
#    + /progress). L'auto-terminate Clore est géré côté orchestrateur (scale-down par
#    order id) car /my_orders n'expose pas le nom → terminate par nom est un no-op
#    gracieux (le nom n'est pas un order id).
#  - RunPod : injecte RUNPOD_POD_ID → POD_ID = RUNPOD_POD_ID (comportement
#    historique, strictement inchangé).
NOVITA = os.environ.get("NOVITA_WORKER_NAME", "")
CLORE = os.environ.get("CLORE_WORKER_NAME", "")
VAST = os.environ.get("CONTAINER_ID", "")
if NOVITA:
    PROVIDER, POD_ID = "novita", NOVITA
elif CLORE:
    PROVIDER, POD_ID = "clore", CLORE
elif VAST:
    PROVIDER, POD_ID = "vast", VAST
else:
    PROVIDER, POD_ID = "runpod", os.environ.get("RUNPOD_POD_ID", "")

# GPU effectif du pod (nom BRUT nvidia-smi). Envoyé au /claim pour l'observabilité
# coût/GPU côté MassContent (qui le mappe au label court "5090"/"PRO4500"/...). Best-
# effort : si nvidia-smi échoue/absent, GPU_NAME="" et le claim n'envoie pas le param
# (rétro-compat). Le GPU ne change pas en cours de pod → détecté une fois au démarrage.
def _detect_gpu():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().splitlines()[0].strip()
    except Exception:
        pass
    return ""
GPU_NAME = _detect_gpu()

# Pipeline natif verrouillée (= réf qualité tom_FINAL). num_frames auto depuis l'audio.
# IT_ATTENTION/IT_BLOCKSWAP : overrides par pod (env) pour les GPU non-Blackwell
# (Phase 2 : sdpa sur 4090/A6000 — la sage de l'image est compilée sm_120 only)
# et les VRAM plus petites (4090 24Go → blockswap 20). Défauts = pipeline réf.
# ⚠ Les DÉFAUTS de generate.py ≠ cette pipeline (outil de test paramétrable) :
# la réf prod vit ICI (GEN_ARGS) + README (commande de réf) — garder en synchro.
# --base-model doit matcher le fichier téléchargé/vérifié par setup-prod.sh.
# `or` (pas get(k, def)) : une env définie mais VIDE (cas fréquent d'un champ env
# laissé vide par l'orchestrateur) retombe sur le défaut — aligné sur ${VAR:-def}
# de setup-prod.sh. Sinon ATTENTION="" → `--attention ""` invalide, BLOCKSWAP=""
# → int('') = crash argparse, sur CHAQUE job.
ATTENTION = os.environ.get("IT_ATTENTION") or "sageattn"
BLOCKSWAP = os.environ.get("IT_BLOCKSWAP") or "10"
# Plafond de durée vidéo (Tom 2026-06-22 : 1 min max). 60s × 25fps = 1500 frames.
# AVANT : 2250 (90s, PV-005) — mais le GPU OOM-kill au-delà de ~60s (PROUVÉ : 60s
# COMPLETE en ~35min ; 85s plante en plein sampling vers la frame ~1500, le conteneur
# manque de RAM). 2250 était donc un plafond THÉORIQUE jamais atteignable. On cale à
# 1500 = enveloppe GPU prouvée sûre : un audio > ~60s est TRONQUÉ (+ WARN generate.py)
# au lieu d'OOM-killer le pod en boucle (retry coûteux + 0 vidéo). L'alignement fenêtre
# (81+72k) arrondit 1500 → 1521 frames (60,8s) = exactement le 60s prouvé. Tunable via
# IT_MAX_FRAMES — NE PAS remonter sans re-prouver l'enveloppe RAM du conteneur GPU.
MAX_FRAMES = os.environ.get("IT_MAX_FRAMES") or "1500"
GEN_ARGS = ["--blockswap", BLOCKSWAP, "--prefetch", "1", "--shift", "3", "--audio-scale", "1.0",
            "--attention", ATTENTION, "--steps", "4", "--rife", "2", "--colormatch", "mkl",
            "--scheduler", "euler", "--max-frames", MAX_FRAMES,
            "--base-model", "Wan2_1-I2V-14B-720p_fp8_e4m3fn_scaled_KJ.safetensors"]
DEFAULT_PROMPT = ("a person calmly speaking to the camera, talking naturally to a friend, "
                  "realistic, highly detailed face, sharp")

def log(*a):
    print("[worker]", *a, flush=True)

def http(method, url, body=None, headers=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")

def wait_comfy(max_s=900):
    t0 = time.time()
    while time.time() - t0 < max_s:
        try:
            urllib.request.urlopen(COMFY + "/system_stats", timeout=5)
            return True
        except Exception:
            time.sleep(3)
    return False


def comfy_alive(timeout=5):
    """ComfyUI répond-il MAINTENANT ? (ping court, sans attente)."""
    try:
        urllib.request.urlopen(COMFY + "/system_stats", timeout=timeout)
        return True
    except Exception:
        return False


def restart_comfy():
    """Relance ComfyUI sur ce pod. Renvoie True s'il répond après la relance.

    POURQUOI (07/08/2026). ComfyUI n'était lancé QU'UNE FOIS, au provisioning. Quand il
    se faisait tuer par le OOM-killer en fin de génération (cause n°1 des échecs), il ne
    redémarrait JAMAIS : le pod restait allumé et facturé, continuait à réclamer des
    jobs, et les faisait tous échouer en ~2 min sur « Connection refused ». 14 échecs de
    ce type mesurés en prod du 03 au 07/08. Relancer coûte ~1 min et récupère un pod
    déjà provisionné (dont le boot a coûté 40 min)."""
    log("ComfyUI injoignable -> relance")
    try:
        r = subprocess.run(["bash", "/workspace/setup-prod.sh", "--restart-comfy"],
                           capture_output=True, text=True, timeout=420)
        log(f"relance ComfyUI rc={r.returncode} {(r.stdout or '')[-300:]}")
    except Exception as e:
        log("relance ComfyUI a échoué:", e)
    return wait_comfy(max_s=180)

def claim():
    # provider + GPU en query → MassContent tamponne le fournisseur ET le GPU effectif
    # du pod DÈS le claim (avant le 1er heartbeat) : chaque job porte son backend +
    # son GPU tout de suite, même si le pod meurt avant de heartbeat. PROVIDER ∈
    # {runpod, vast} ; GPU_NAME = nom brut nvidia-smi (URL-encodé). Rétro-compat : un
    # MassContent antérieur ignore simplement les params.
    q = f"?provider={PROVIDER}"
    if GPU_NAME:
        q += "&gpu=" + urllib.parse.quote(GPU_NAME)
    # ── L'IDENTITE DE LA MACHINE, DES LA RECLAMATION (08/08/2026) ──────────────
    # Elle n'etait envoyee qu'au PREMIER BATTEMENT, ~45 s plus tard. Or c'est a la
    # reclamation que le serveur rattache le travail a la session de la machine :
    # sans identite a cet instant, le rattachement etait purement et simplement
    # SAUTE. Mesure du 08/08 : 13 sessions de machine enregistrees, **zero travail
    # rattache**, zero demarrage mesure, et 100 % de la facture comptee comme
    # improductive — toute l'instrumentation posee la veille etait inerte, et
    # l'alerte « machine allumee pour rien » criait en permanence.
    if POD_ID:
        q += "&podId=" + urllib.parse.quote(POD_ID)
    return http("GET", f"{MC_URL}/api/workers/runpod/claim{q}",
                headers={"x-pipeline-secret": PIPELINE_SECRET})

def complete(payload):
    return http("POST", f"{MC_URL}/api/workers/runpod/complete", body=payload,
                headers={"x-pipeline-secret": PIPELINE_SECRET})

def progress(jid, **kw):
    """Heartbeat best-effort (n'interrompt jamais la génération en cas d'échec réseau)."""
    try:
        # provider transmis sur chaque heartbeat (avec le podId déjà posté au
        # 1er progress) → MassContent persiste le bon backend sur le RunPodJob.
        http("POST", f"{MC_URL}/api/workers/runpod/progress",
             body={"jobId": jid, "provider": PROVIDER, **kw},
             headers={"x-pipeline-secret": PIPELINE_SECRET}, timeout=15)
    except Exception as e:
        log("progress err:", e)

def self_terminate():
    # PV-003 : le pod n'a plus la RUNPOD_API_KEY (clé compte-entier, dangereuse
    # sur une machine community tierce). Il demande sa terminaison à MassContent
    # (auth x-pipeline-secret) qui exécute le podTerminate avec sa propre clé.
    # Le cron orphan-killer reste le filet si cet appel échoue.
    if MC_URL and POD_ID:
        try:
            # provider → MassContent route vers le bon backend (podTerminate RunPod
            # ou DELETE /instances/{id}/ Vast). Défaut serveur "runpod" si omis.
            http("POST", f"{MC_URL}/api/workers/runpod/terminate",
                 body={"podId": POD_ID, "provider": PROVIDER},
                 headers={"x-pipeline-secret": PIPELINE_SECRET}, timeout=15)
            log(f"pod auto-terminé (via MassContent, provider={PROVIDER}):", POD_ID)
        except Exception as e:
            log("auto-terminaison échouée:", e)

def audio_seconds(path):
    """Durée (s) de l'audio source via ffprobe — best-effort (None si échec). Sert à
    NORMALISER le coût/vidéo par GPU côté MassContent (le coût/vidéo brut dépend de la
    durée audio → le coût/sec-audio est la seule métrique comparable entre GPU)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30)
        if out.returncode == 0 and out.stdout.strip():
            return round(float(out.stdout.strip()), 2)
    except Exception:
        pass
    return None

def download_asset(url, path, label):
    # Téléchargement ROBUSTE des assets (image/audio) depuis l'URL présignée S3.
    # AVANT : urllib.request.urlretrieve (0 retry, timeout socket 180s) → un blip
    # réseau pod↔S3 (connect timeout Errno 110, vécu en burst 2026-07-11 : toutes
    # les générations échouaient sur `<urlopen error [Errno 110] Connection timed
    # out>` sans rattrapage) tuait la génération entière. curl avec retries sur
    # erreurs transitoires (timeout de connexion inclus) + connect-timeout court =
    # résilience alignée sur l'upload S3 (curl plus bas). --retry couvre le connect
    # timeout ; --retry-connrefused ajoute le refus de connexion.
    r = subprocess.run(
        ["curl", "-fsS", "--location",
         "--connect-timeout", "20", "--max-time", "300",
         "--retry", "6", "--retry-delay", "4", "--retry-connrefused",
         "-o", path, url],
        capture_output=True, text=True, timeout=360,
    )
    if r.returncode != 0:
        raise RuntimeError(f"download {label} echec rc={r.returncode}: {(r.stderr or '')[-300:]}")


def run_generate(cmd, jid, t0, extra_env=None):
    """Lance generate.py, renvoie (returncode, lignes de sortie).

    Popen + lecture stdout en flux (logsTail) + heartbeat sur THREAD dédié : le signal
    de vie ne dépend PAS du stdout de generate.py (une phase muette >20min — tqdm en \\r,
    VAE decode long — ferait tuer un job sain par le watchdog MassContent qui considère
    mort tout job silencieux 20min). errors="replace" : un octet non-UTF8 (lib C, tqdm
    corrompu) devient ? au lieu de lever dans la boucle (sinon le process generate.py
    survivrait orphelin, pipe pleine, et le worker enchaînerait un 2e job en // → OOM).

    `t0` est l'horloge du JOB (pas de l'essai) : la chaîne de timeouts et l'affichage de
    durée restent bornés même quand un 2e essai (repli sans RIFE) est lancé."""
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, errors="replace", bufsize=1, env=env)
    lines = []
    done = threading.Event()

    # Le thread heartbeat porte AUSSI le kill sur durée TOTALE (HARD_KILL_S) : le
    # p.wait(timeout) plus bas ne démarre qu'à l'EOF du stdout, donc il ne protège
    # PAS d'un generate.py gelé muet (deadlock Python / kernel CUDA wedged : ni
    # sortie, ni EOF). Sans ce kill thread-side, un tel gel ne serait vu qu'au
    # watchdog zombie MassContent (180min). On tue à 7200s → la boucle stdout sort
    # sur EOF, returncode != 0, chemin d'échec standard.
    # ── L'IDENTITE DU RUN, REPETEE A CHAQUE BATTEMENT (07/08/2026) ──────────────
    # Le serveur ne garde que les 80 derniers battements, et chaque battement ne
    # porte que les 12 dernieres lignes de sortie. Les lignes de DEBUT — celles qui
    # disent le nombre d'images et si l'interpolation a ete coupee — etaient donc
    # TOUJOURS ecrasees avant la fin d'une generation de 30 minutes. Resultat :
    # impossible de savoir, devant un echec, avec quel reglage il avait tourne. On
    # les capte des la sortie et on les recolle a CHAQUE note de progression : un
    # echec redevient auto-diagnosticable.
    # On garde DEUX faits, pas un : le nombre d'images (la charge) et la decision
    # d'interpolation (le facteur x2 de memoire). L'un sans l'autre ne suffit pas a
    # expliquer un manque de memoire.
    # ── LA RAM DE LA MACHINE FAIT PARTIE DE L'IDENTITE DU RUN (08/08/2026) ──────
    # Sans elle, un echec par manque de memoire etait indistinguable de n'importe quel
    # autre : on lisait « le process est mort » sans jamais savoir avec combien de RAM il
    # travaillait. C'est precisement le chiffre qui manquait pour prouver la cause (52 %
    # d'echec mesure sur 24 h) et pour calibrer le plancher exige a la location.
    ident = {"frames": "", "rife": "", "ram": "", "txt": ""}

    def _capture_identity(line):
        if "[gen] num_frames aligne=" in line and not ident["frames"]:
            ident["frames"] = line.split("num_frames aligne=", 1)[1].split(",", 1)[0].strip()
        elif "[gen] RIFE" in line and not ident["rife"]:
            ident["rife"] = "interpolation coupee" if "DESACTIVEE" in line or "FORCEE A OFF" in line else "interpolation active"
        elif "[gen] RAM conteneur~" in line and not ident["ram"]:
            ident["ram"] = "RAM " + line.split("RAM conteneur~", 1)[1].split(",", 1)[0].strip()
        else:
            return
        bouts = [b for b in (f"{ident['frames']} images" if ident["frames"] else "",
                             ident["rife"], ident["ram"]) if b]
        ident["txt"] = " · ".join(bouts)[:120]

    HARD_KILL_S = 7200
    def _heartbeat():
        while not done.wait(HEARTBEAT_S):
            if time.time() - t0 > HARD_KILL_S:
                log(f"HARD KILL generate.py après {HARD_KILL_S}s (gel ?)")
                p.kill()
                return
            suffixe = f" · {ident['txt']}" if ident["txt"] else ""
            progress(jid, logsTail="\n".join(lines[-12:]),
                     note=f"génération {int(time.time()-t0)}s{suffixe}")

    hb_thread = threading.Thread(target=_heartbeat, daemon=True)
    hb_thread.start()
    try:
        for line in p.stdout:
            lines.append(line.rstrip("\n"))
            _capture_identity(lines[-1])
            if len(lines) > 500:
                lines = lines[-500:]
    finally:
        done.set()
    # Filet dur (cas nominal : generate.py a rendu la main, EOF reçu). generate.py
    # a son propre timeout de poll (7000s) qui mord en premier avec un message
    # propre ; ici on borne le cas où il aurait fermé stdout sans rendre la main.
    # Chaîne ordonnée : generate 7000 < worker kill 7200 < watchdog MC 180min.
    try:
        p.wait(timeout=HARD_KILL_S)
    except subprocess.TimeoutExpired:
        p.kill()
        raise RuntimeError(f"generate.py timeout {HARD_KILL_S}s")
    return p.returncode, lines


def process(job):
    vid = job["videoId"]
    jid = job["jobId"]
    w = int(job.get("width", 720))
    h = int(job.get("height", 1280))
    prompt = job.get("prompt") or DEFAULT_PROMPT
    img_path = f"{INPUT_DIR}/{vid}.png"
    aud_path = f"{INPUT_DIR}/{vid}.mp3"
    log(f"job {jid} video={vid} {w}x{h}")
    progress(jid, podId=POD_ID, status="running", note=f"start {w}x{h}")
    download_asset(job["imageUrl"], img_path, "image")
    download_asset(job["audioUrl"], aud_path, "audio")
    aud_sec = audio_seconds(aud_path)  # mesuré AVANT le cleanup → coût/GPU normalisé
    progress(jid, note="assets téléchargés, génération en cours")
    cmd = ["python3", GEN, "--image", f"{vid}.png", "--audio", f"{vid}.mp3",
           "--width", str(w), "--height", str(h), "--prompt", prompt,
           "--prefix", f"it_{vid}"] + GEN_ARGS

    t0 = time.time()
    rc, lines = run_generate(cmd, jid, t0)
    full = "\n".join(lines)

    # ── REPLI SANS RIFE (07/08/2026) ────────────────────────────────────────────
    # rc=5 = ComfyUI est devenu injoignable en cours de route, c'est-à-dire tué par
    # le OOM-killer du conteneur. Quand RIFE (interpolation 25→50 fps) était active,
    # elle est l'accumulateur de RAM n°1 : c'est elle qui fait déborder. Plutôt que de
    # jeter le pod (et de repayer un boot de 40 min ailleurs, sur un pod qui a la même
    # chance d'échouer), on relance ComfyUI et on REJOUE le MÊME job sans RIFE sur ce
    # pod déjà chaud. La vidéo sort en 25 fps — ré-échantillonnée à 30 fps au montage,
    # exactement comme toutes les vidéos longues le sont déjà aujourd'hui.
    if rc == 5 and "[gen] RIFE active" in full:
        log("crash ComfyUI avec RIFE active -> relance + 2e essai SANS RIFE (même pod)")
        progress(jid, note="ComfyUI a manqué de mémoire — nouvel essai sans interpolation",
                 logsTail="\n".join(lines[-12:]))
        if restart_comfy():
            rc2, lines2 = run_generate(cmd, jid, t0, extra_env={"IT_FORCE_NO_RIFE": "1"})
            if rc2 == 0:
                log("2e essai sans RIFE : réussi")
            rc, lines = rc2, lines2
            full = "\n".join(lines)
        else:
            log("ComfyUI n'est pas reparti -> pas de 2e essai")

    sys.stdout.write(full[-2000:] + "\n")
    sys.stdout.flush()
    if rc != 0:
        progress(jid, note=f"generate ÉCHEC rc={rc}", logsTail="\n".join(lines[-15:]))
        raise RuntimeError(f"generate rc={rc}: {full[-500:]}")

    # Contrat de sortie : generate.py imprime "[gen] DONE in <s>s -> <path>"
    # (dernière ligne utile). Ne pas changer ce format d'un côté sans l'autre.
    path = None
    for line in lines:
        if "DONE in" in line and "->" in line:
            path = line.split("->", 1)[1].strip()
    if not path or not os.path.exists(path):
        raise RuntimeError(f"sortie introuvable: {path}")

    # Upload via URL presignée PUT fournie par /claim (pas de boto3 ni de
    # credentials AWS sur le pod éphémère).
    key = job["outputKey"]
    progress(jid, note="upload S3")
    # Timeouts OBLIGATOIRES : à ce stade le thread heartbeat (qui portait le HARD_KILL
    # 7200s) est déjà mort (done.set() à l'EOF du stdout de generate.py). Un PUT S3 qui
    # stalle (presigned lente, réseau pod↔R2 dégradé) gèlerait donc le worker DANS
    # process() — jamais d'état terminal (~3h jusqu'au watchdog MC) + PII pas nettoyée
    # (le finally du main loop n'est pas atteint). --max-time côté curl + timeout
    # subprocess (double ceinture) → TimeoutExpired → chemin d'échec standard (cleanup PII).
    # Heartbeat PENDANT l'upload : le thread de génération (porteur du signal de vie) est
    # mort à l'EOF de generate.py. Un PUT lent (jusqu'à 960s) laisserait le job MUET → le
    # watchdog MassContent (20min de silence) pourrait le rescheduler sur un 2e pod = double
    # génération (conséquence rattrapée par la réconciliation S3 serveur, mais autant ne pas
    # provoquer le reschedule). Thread additif best-effort (progress() avale ses erreurs).
    up_done = threading.Event()
    def _upload_hb():
        while not up_done.wait(HEARTBEAT_S):
            progress(jid, note=f"upload S3 en cours {int(time.time() - t0)}s")
    threading.Thread(target=_upload_hb, daemon=True).start()
    try:
        up = subprocess.run(
            ["curl", "-fsS", "--connect-timeout", "30", "--max-time", "900",
             "-X", "PUT", "-H", "Content-Type: video/mp4",
             "--upload-file", path, job["uploadUrl"]],
            capture_output=True, text=True, timeout=960,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("upload S3 timeout (>960s) — PUT presigné bloqué")
    finally:
        up_done.set()
    if up.returncode != 0:
        raise RuntimeError(f"upload S3 echec rc={up.returncode}: {up.stderr[-300:]}")
    url = job.get("outputUrl") or key
    log(f"upload S3 OK -> {key}")
    for pth in (img_path, aud_path, path):
        try:
            os.remove(pth)
        except Exception:
            pass
    dur_ms = int((time.time() - t0) * 1000)  # durée génération+upload → costUsd côté MassContent
    return key, url, dur_ms, aud_sec

def main():
    missing = [k for k, v in {"MASSCONTENT_BASE_URL": MC_URL,
                              "PIPELINE_SECRET": PIPELINE_SECRET}.items() if not v]
    if missing:
        log("config manquante:", ",".join(missing)); sys.exit(2)
    # Observabilité fuite coût : tracer le fournisseur + l'id natif déduits. Un POD_ID
    # vide (env non injectée / mal nommée) rend self_terminate() inopérant → le pod ne
    # se coupe pas seul (seul le reaper/orphan-killer le rattrape, après le grace). On
    # le remonte en clair pour diagnostiquer une fuite plutôt que de la subir en silence.
    log(f"provider={PROVIDER} podId={POD_ID or '(VIDE)'} gpu={GPU_NAME or '?'}")
    if not POD_ID:
        log("WARN POD_ID vide → self_terminate no-op (coupe assurée par le reaper, pas par le pod)")
    if not wait_comfy():
        log("ComfyUI introuvable après 15min, abandon"); sys.exit(1)
    log("ComfyUI up — worker démarré")
    idle = 0
    while True:
        # GARDE « pod empoisonné » : ne JAMAIS réclamer un job si ComfyUI est mort.
        # Sans elle, un pod dont ComfyUI a été OOM-killed continuait à prendre des jobs
        # et à les faire échouer en ~2 min chacun, en cascade, tout en restant facturé.
        # On tente une relance ; si le serveur ne repart pas, le pod ne sert plus à rien
        # → on le coupe au lieu de brûler du GPU.
        if not comfy_alive():
            if not restart_comfy():
                log("ComfyUI ne repart pas -> arrêt du pod (inutile et facturé)")
                self_terminate()
                return
            log("ComfyUI relancé -> reprise des réclamations")
        try:
            r = claim()
        except Exception as e:
            log("claim err:", e); time.sleep(POLL_EMPTY_S); continue
        job = r.get("job")
        if not job:
            idle += POLL_EMPTY_S
            if idle >= IDLE_EXIT_S:
                log(f"inactif {idle}s -> arrêt du pod"); self_terminate(); return
            time.sleep(POLL_EMPTY_S); continue
        idle = 0
        # process() est SEUL dans le try : seul un échec RÉEL de génération/upload doit
        # mener à complete(failed). Si process() réussit, la vidéo est sur S3 → on ne
        # bascule JAMAIS en failed (sinon un simple blip réseau sur l'ACK /complete
        # ferait JETER le travail + RE-GÉNÉRER côté MassContent = GPU regaspillé).
        try:
            result = process(job)
        except Exception as e:
            log("job FAILED:", e, traceback.format_exc()[-600:])
            try:
                complete({"jobId": job["jobId"], "videoId": job["videoId"],
                          "status": "failed", "error": str(e)[:500]})
            except Exception as e2:
                log("complete(failed) err:", e2)
        else:
            # Succès : vidéo générée + uploadée. complete(completed) avec RETRY backoff —
            # /complete est idempotent côté serveur (accepte un completed tardif), donc on
            # insiste sans jamais émettre failed. Si tout échoue, le watchdog réconciliera.
            key, url, dur_ms, aud_sec = result
            payload = {"jobId": job["jobId"], "videoId": job["videoId"],
                       "status": "completed", "videoS3Key": key, "videoUrl": url,
                       "durationMs": dur_ms, "audioSec": aud_sec}
            backoff = [5, 15, 30, 60]
            for attempt in range(len(backoff) + 1):
                try:
                    complete(payload)
                    log(f"job {job['jobId']} -> completed in {dur_ms}ms")
                    break
                except Exception as e2:
                    log(f"complete(completed) essai {attempt + 1}/{len(backoff) + 1} échec:", e2)
                    if attempt < len(backoff):
                        time.sleep(backoff[attempt])
            else:
                log(f"job {job['jobId']} : completed NON-ACK après {len(backoff) + 1} essais "
                    f"— vidéo sur S3 ({key}), watchdog MassContent réconciliera (PAS de failed émis)")
        finally:
            # Cleanup PII systématique sur TOUS les chemins (succès ET échec) : visage
            # (<vid>.png) + voix (<vid>.mp3) + voix paddée dérivée (_pad_<vid>.mp3, créée
            # par generate.py) + vidéo de sortie (it_<vid>.mp4) si l'upload a échoué.
            # Le glob *<vid>* couvre tout dérivé présent ou futur en un seul point (le
            # videoId est unique → aucune collision inter-job).
            vid = job.get("videoId")
            if vid:
                for pth in (glob.glob(f"{INPUT_DIR}/*{vid}*")
                            + glob.glob(f"/workspace/ComfyUI/output/**/it_{vid}*", recursive=True)):
                    try:
                        os.remove(pth)
                    except OSError:
                        pass

if __name__ == "__main__":
    main()
