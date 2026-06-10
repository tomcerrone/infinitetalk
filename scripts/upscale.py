#!/usr/bin/env python3
"""LEGACY R&D (SeedVR2, REJETE qualite 2026-06) — NE PAS UTILISER EN PROD.
Upscale SeedVR2 (480p -> 720p, face-aware) + interpolation RIFE -> 720p 50fps.
Charge une video existante (chemin complet), passe par SeedVR2VideoUpscaler puis RIFE, recombine avec l'audio source.
API ComfyUI locale du pod (meme pattern que generate.py)."""
import argparse, json, time, urllib.request, sys, uuid

SERVER = "http://127.0.0.1:8188"

def post_json(url, data):
    req = urllib.request.Request(url, data=json.dumps(data).encode(), headers={"Content-Type":"application/json"})
    return json.load(urllib.request.urlopen(req, timeout=60))

def get_json(url):
    return json.load(urllib.request.urlopen(url, timeout=60))

def build(a):
    g = {
      "1": {"class_type":"VHS_LoadVideoPath","inputs":{"video":a.video,"force_rate":0.0,"custom_width":0,"custom_height":0,"frame_load_cap":0,"skip_first_frames":0,"select_every_nth":1}},
      "2": {"class_type":"SeedVR2LoadDiTModel","inputs":{"model":a.dit_model,"device":"cuda:0","blocks_to_swap":a.dit_blockswap,"swap_io_components":False,"offload_device":("cpu" if a.dit_blockswap>0 else "none"),"cache_model":False,"attention_mode":a.attention}},
      "3": {"class_type":"SeedVR2LoadVAEModel","inputs":{"model":"ema_vae_fp16.safetensors","device":"cuda:0","offload_device":"none"}},
      "4": {"class_type":"SeedVR2VideoUpscaler","inputs":{"image":["1",0],"dit":["2",0],"vae":["3",0],"seed":a.seed,"resolution":a.resolution,"max_resolution":0,"batch_size":a.batch_size,"uniform_batch_size":True,"color_correction":a.color,"temporal_overlap":a.temporal_overlap,"offload_device":"cpu"}},
    }
    last = ["4",0]
    if a.rife > 1:
        g["5"] = {"class_type":"RIFE VFI","inputs":{"ckpt_name":"rife49.pth","frames":["4",0],"clear_cache_after_n_frames":10,"multiplier":a.rife,"fast_mode":True,"ensemble":False,"scale_factor":1.0,"dtype":"float32","torch_compile":False,"batch_size":4}}
        last = ["5",0]
    fr = int(round(a.fps * (a.rife if a.rife > 1 else 1)))
    g["6"] = {"class_type":"VHS_VideoCombine","inputs":{"images":last,"frame_rate":fr,"loop_count":0,"filename_prefix":a.prefix,"format":"video/h264-mp4","pingpong":False,"save_output":True,"audio":["1",2]}}
    return g

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--video", required=True)          # chemin complet du mp4 source (480p)
    p.add_argument("--prefix", default="it_up720")
    p.add_argument("--dit-model", default="seedvr2_ema_3b_fp16.safetensors")
    p.add_argument("--resolution", type=int, default=720)   # bord court cible (480->720)
    p.add_argument("--batch-size", type=int, default=13)     # 4n+1 ; grand = + coherent temporellement
    p.add_argument("--color", default="lab")                 # lab|wavelet|wavelet_adaptive|hsv|adain|none
    p.add_argument("--temporal-overlap", type=int, default=2)
    p.add_argument("--attention", default="sdpa")            # sdpa (stable) | sageattn_2
    p.add_argument("--dit-blockswap", type=int, default=0)
    p.add_argument("--rife", type=int, default=2)
    p.add_argument("--fps", type=float, default=25.0)
    p.add_argument("--seed", type=int, default=42)
    a = p.parse_args()

    g = build(a)
    cid = uuid.uuid4().hex
    print(f"[up] POST /prompt video={a.video} res={a.resolution} batch={a.batch_size} color={a.color} rife={a.rife} attn={a.attention}")
    r = post_json(f"{SERVER}/prompt", {"prompt":g, "client_id":cid})
    if r.get("node_errors"):
        print("[up] NODE_ERRORS:", json.dumps(r["node_errors"], indent=2)); sys.exit(2)
    pid = r["prompt_id"]; print("[up] prompt_id", pid)

    t0 = time.time()
    while True:
        time.sleep(3)
        try: h = get_json(f"{SERVER}/history/{pid}")
        except Exception as e: print("[up] poll err", e); continue
        if pid not in h:
            el = int(time.time()-t0); print(f"[up] ... {el}s", flush=True)
            if el > 5400: print("[up] TIMEOUT"); sys.exit(3)
            continue
        entry = h[pid]; st = entry.get("status", {})
        print(f"[up] status={st.get('status_str')} ({int(time.time()-t0)}s)")
        if st.get("status_str") == "error":
            for m in st.get("messages", []): print("[up] MSG", json.dumps(m)[:600])
            sys.exit(4)
        outs = entry.get("outputs", {})
        vids = []
        for nid,o in outs.items():
            for key in ("gifs","videos","images"):
                for v in o.get(key, []): vids.append(v)
        if vids:
            for v in vids: print("[up] OUTPUT", json.dumps(v))
            v = vids[-1]; print(f"[up] DONE in {int(time.time()-t0)}s -> /workspace/ComfyUI/output/{v.get('subfolder','')}/{v['filename']}".replace("//","/"))
            break

if __name__ == "__main__":
    main()
