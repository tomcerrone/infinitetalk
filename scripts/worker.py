#!/usr/bin/env python3
"""Worker batch InfiniteTalk (pod RunPod).

Boucle : réclame un job à MassContent -> génère (pipeline natif 720p verrouillée via generate.py)
-> upload S3 (URL presignée PUT) -> signale la fin. S'auto-termine après inactivité
prolongée (zéro gaspillage GPU). Heartbeat de progression vers MassContent pendant
la génération (visibilité sans SSH).

Config (env) :
  MASSCONTENT_BASE_URL   URL de base MassContent (ex https://masscontent.pro)
  PIPELINE_SECRET        secret d'auth des workers (header x-pipeline-secret)
  IDLE_EXIT_SECONDS      inactivité avant auto-arrêt du pod (defaut 300)
  POLL_EMPTY_SECONDS     délai entre deux réclamations à vide (defaut 10)
  RUNPOD_API_KEY         pour l'auto-terminaison du pod (RunPod fournit RUNPOD_POD_ID)
"""
import os, sys, time, json, subprocess, threading, urllib.request, traceback

MC_URL = os.environ.get("MASSCONTENT_BASE_URL", "").rstrip("/")
PIPELINE_SECRET = os.environ.get("PIPELINE_SECRET", "")
COMFY = "http://127.0.0.1:8188"
INPUT_DIR = "/workspace/ComfyUI/input"
GEN = "/workspace/generate.py"
IDLE_EXIT_S = int(os.environ.get("IDLE_EXIT_SECONDS", "300"))
POLL_EMPTY_S = int(os.environ.get("POLL_EMPTY_SECONDS", "10"))
HEARTBEAT_S = int(os.environ.get("HEARTBEAT_SECONDS", "45"))
RUNPOD_API_KEY = os.environ.get("RUNPOD_API_KEY", "")
POD_ID = os.environ.get("RUNPOD_POD_ID", "")

# Pipeline natif verrouillée (= réf qualité tom_FINAL). num_frames auto depuis l'audio.
GEN_ARGS = ["--blockswap", "10", "--prefetch", "1", "--shift", "3", "--audio-scale", "1.0",
            "--attention", "sageattn", "--steps", "4", "--rife", "2", "--colormatch", "mkl",
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
    if RUNPOD_API_KEY and POD_ID:
        try:
            http("POST", f"https://api.runpod.io/graphql?api_key={RUNPOD_API_KEY}",
                 body={"query": f'mutation {{ podTerminate(input: {{podId: "{POD_ID}"}}) }}'})
            log("pod auto-terminé:", POD_ID)
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

    # Popen + lecture stdout en flux : heartbeat périodique (visibilité sans SSH).
    t0 = time.time()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1)
    lines = []
    last_hb = time.time()
    for line in p.stdout:
        lines.append(line.rstrip("\n"))
        if len(lines) > 500:
            lines = lines[-500:]
        if time.time() - last_hb >= HEARTBEAT_S:
            progress(jid, logsTail="\n".join(lines[-12:]),
                     note=f"génération {int(time.time()-t0)}s")
            last_hb = time.time()
    # 7200s : une vidéo d'1min d'audio prend ~40min sur 5090, potentiellement
    # ~2x sur un GPU plus lent (PRO 4500). Aligné avec le watchdog MassContent
    # (zombie à 180min) : le timeout local tue toujours AVANT le watchdog.
    try:
        p.wait(timeout=7200)
    except subprocess.TimeoutExpired:
        p.kill()
        raise RuntimeError("generate.py timeout 7200s")
    full = "\n".join(lines)
    sys.stdout.write(full[-2000:] + "\n")
    sys.stdout.flush()
    if p.returncode != 0:
        progress(jid, note=f"generate ÉCHEC rc={p.returncode}", logsTail="\n".join(lines[-15:]))
        raise RuntimeError(f"generate rc={p.returncode}: {full[-500:]}")

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

if __name__ == "__main__":
    main()
