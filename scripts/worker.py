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
import os, sys, time, json, socket, subprocess, threading, urllib.request, traceback

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
POD_ID = os.environ.get("RUNPOD_POD_ID", "")  # auto-injecté par RunPod

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
GEN_ARGS = ["--blockswap", BLOCKSWAP, "--prefetch", "1", "--shift", "3", "--audio-scale", "1.0",
            "--attention", ATTENTION, "--steps", "4", "--rife", "2", "--colormatch", "mkl",
            "--scheduler", "euler",
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

def claim():
    return http("GET", f"{MC_URL}/api/workers/runpod/claim",
                headers={"x-pipeline-secret": PIPELINE_SECRET})

def complete(payload):
    return http("POST", f"{MC_URL}/api/workers/runpod/complete", body=payload,
                headers={"x-pipeline-secret": PIPELINE_SECRET})

def progress(jid, **kw):
    """Heartbeat best-effort (n'interrompt jamais la génération en cas d'échec réseau)."""
    try:
        http("POST", f"{MC_URL}/api/workers/runpod/progress",
             body={"jobId": jid, **kw},
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
            http("POST", f"{MC_URL}/api/workers/runpod/terminate",
                 body={"podId": POD_ID},
                 headers={"x-pipeline-secret": PIPELINE_SECRET}, timeout=15)
            log("pod auto-terminé (via MassContent):", POD_ID)
        except Exception as e:
            log("auto-terminaison échouée:", e)

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
    urllib.request.urlretrieve(job["imageUrl"], img_path)
    urllib.request.urlretrieve(job["audioUrl"], aud_path)
    progress(jid, note="assets téléchargés, génération en cours")
    cmd = ["python3", GEN, "--image", f"{vid}.png", "--audio", f"{vid}.mp3",
           "--width", str(w), "--height", str(h), "--prompt", prompt,
           "--prefix", f"it_{vid}"] + GEN_ARGS

    # Popen + lecture stdout en flux (logsTail) + heartbeat sur THREAD dédié :
    # le signal de vie ne dépend PAS du stdout de generate.py (une phase muette
    # >20min — tqdm en \r, VAE decode long — ferait tuer un job sain par le
    # watchdog MassContent qui considère mort tout job silencieux 20min).
    # errors="replace" : un octet non-UTF8 (lib C, tqdm corrompu) devient � au
    # lieu de lever dans la boucle (sinon le process generate.py survivrait
    # orphelin, pipe pleine, et le worker enchaînerait un 2e job en // → OOM).
    t0 = time.time()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, errors="replace", bufsize=1)
    lines = []
    done = threading.Event()

    # Le thread heartbeat porte AUSSI le kill sur durée TOTALE (HARD_KILL_S) : le
    # p.wait(timeout) plus bas ne démarre qu'à l'EOF du stdout, donc il ne protège
    # PAS d'un generate.py gelé muet (deadlock Python / kernel CUDA wedged : ni
    # sortie, ni EOF). Sans ce kill thread-side, un tel gel ne serait vu qu'au
    # watchdog zombie MassContent (180min). On tue à 7200s → la boucle stdout sort
    # sur EOF, returncode != 0, chemin d'échec standard.
    HARD_KILL_S = 7200
    def _heartbeat():
        while not done.wait(HEARTBEAT_S):
            if time.time() - t0 > HARD_KILL_S:
                log(f"HARD KILL generate.py après {HARD_KILL_S}s (gel ?)")
                p.kill()
                return
            progress(jid, logsTail="\n".join(lines[-12:]),
                     note=f"génération {int(time.time()-t0)}s")

    hb_thread = threading.Thread(target=_heartbeat, daemon=True)
    hb_thread.start()
    try:
        for line in p.stdout:
            lines.append(line.rstrip("\n"))
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
    full = "\n".join(lines)
    sys.stdout.write(full[-2000:] + "\n")
    sys.stdout.flush()
    if p.returncode != 0:
        progress(jid, note=f"generate ÉCHEC rc={p.returncode}", logsTail="\n".join(lines[-15:]))
        raise RuntimeError(f"generate rc={p.returncode}: {full[-500:]}")

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
    up = subprocess.run(
        ["curl", "-fsS", "-X", "PUT", "-H", "Content-Type: video/mp4",
         "--upload-file", path, job["uploadUrl"]],
        capture_output=True, text=True,
    )
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
    return key, url, dur_ms

def main():
    missing = [k for k, v in {"MASSCONTENT_BASE_URL": MC_URL,
                              "PIPELINE_SECRET": PIPELINE_SECRET}.items() if not v]
    if missing:
        log("config manquante:", ",".join(missing)); sys.exit(2)
    if not wait_comfy():
        log("ComfyUI introuvable après 15min, abandon"); sys.exit(1)
    log("ComfyUI up — worker démarré")
    idle = 0
    while True:
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
        try:
            key, url, dur_ms = process(job)
            complete({"jobId": job["jobId"], "videoId": job["videoId"],
                      "status": "completed", "videoS3Key": key, "videoUrl": url,
                      "durationMs": dur_ms})
            log(f"job {job['jobId']} -> completed in {dur_ms}ms")
        except Exception as e:
            log("job FAILED:", e, traceback.format_exc()[-600:])
            try:
                complete({"jobId": job["jobId"], "videoId": job["videoId"],
                          "status": "failed", "error": str(e)[:500]})
            except Exception as e2:
                log("complete(failed) err:", e2)
        finally:
            # Cleanup PII systématique (visage + voix du client) : en succès
            # process() les supprime déjà, mais en cas d'ÉCHEC ils resteraient
            # sur le pod (servis par le serveur de logs 8189 + exposition RGPD)
            # jusqu'à l'idle-exit. On efface les inputs du job quoi qu'il arrive.
            vid = job.get("videoId")
            if vid:
                for pth in (f"{INPUT_DIR}/{vid}.png", f"{INPUT_DIR}/{vid}.mp3"):
                    try:
                        os.remove(pth)
                    except OSError:
                        pass

if __name__ == "__main__":
    main()
