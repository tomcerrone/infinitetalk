#!/usr/bin/env python3
"""Genere une video InfiniteTalk (image+audio -> 720p 9:16) via l'API ComfyUI locale du Pod.
Reutilisable : test placeholder, vrais assets FR/ES, et base du workflow prod (worker-comfyui).

NB prod : les DEFAUTS argparse ci-dessous sont des valeurs de test — la pipeline
prod verrouillee est definie par worker.py (GEN_ARGS) + README (commande de ref).
La ligne "[gen] DONE in <s>s -> <path>" est PARSEE par worker.py : ne pas changer
son format sans mettre worker.py a jour."""
import argparse, json, subprocess, time, urllib.request, sys, uuid, math

# 8188 = port lance par setup-prod.sh (start_comfyui) et expose par MassContent
# (deployWorkerPod, ports "8188/http") — meme constante dans worker.py (COMFY).
SERVER = "http://127.0.0.1:8188"

def ffprobe_duration(path):
    try:
        out = subprocess.check_output(["ffprobe","-v","error","-show_entries","format=duration","-of","csv=p=0",path])
        return float(out.decode().strip())
    except Exception as e:
        print("WARN ffprobe:", e); return None

def post_json(url, data):
    req = urllib.request.Request(url, data=json.dumps(data).encode(), headers={"Content-Type":"application/json"})
    return json.load(urllib.request.urlopen(req, timeout=60))

def get_json(url):
    return json.load(urllib.request.urlopen(url, timeout=60))

def build_graph(a):
    g = {
      "1": {"class_type":"WanVideoVAELoader","inputs":{"model_name":"Wan2_1_VAE_bf16.safetensors","precision":"bf16"}},
      "2": {"class_type":"WanVideoBlockSwap","inputs":{"blocks_to_swap":a.blockswap,"offload_img_emb":False,"offload_txt_emb":False,"use_non_blocking":True,"vace_blocks_to_swap":0,"prefetch_blocks":a.prefetch,"block_swap_debug":False}},
      "3": {"class_type":"WanVideoLoraSelect","inputs":{"lora":"lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors","strength":1.0,"low_mem_load":False,"merge_loras":True}},
      "4": {"class_type":"MultiTalkModelLoader","inputs":{"model":"InfiniteTalk/Wan2_1-InfiniteTalk-Single_fp8_e4m3fn_scaled_KJ.safetensors"}},
      "5": {"class_type":"WanVideoModelLoader","inputs":{
            "model":a.base_model,
            "base_precision":"fp16_fast","quantization":"fp8_e4m3fn_scaled","load_device":"offload_device","attention_mode":a.attention,
            "block_swap_args":["2",0],"lora":["3",0],"multitalk_model":["4",0]}},
      "6": {"class_type":"LoadImage","inputs":{"image":a.image}},
      "7": {"class_type":"ImageResizeKJv2","inputs":{"image":["6",0],"width":a.width,"height":a.height,"upscale_method":"lanczos","keep_proportion":"crop","pad_color":"0, 0, 0","crop_position":"center","divisible_by":16,"device":"cpu"}},
      "8": {"class_type":"CLIPVisionLoader","inputs":{"clip_name":"clip_vision_h.safetensors"}},
      "9": {"class_type":"WanVideoClipVisionEncode","inputs":{"clip_vision":["8",0],"image_1":["7",0],"strength_1":1.0,"strength_2":1.0,"crop":"center","combine_embeds":"average","force_offload":True}},
      "10":{"class_type":"Wav2VecModelLoader","inputs":{"model":"wav2vec2-chinese-base_fp16.safetensors","base_precision":"fp16","load_device":"main_device"}},
      "11":{"class_type":"LoadAudio","inputs":{"audio":a.audio}},
      "12":{"class_type":"MultiTalkWav2VecEmbeds","inputs":{"wav2vec_model":["10",0],"audio_1":["11",0],"normalize_loudness":True,"num_frames":a.num_frames,"fps":25.0,"audio_scale":a.audio_scale,"audio_cfg_scale":1.0,"multi_audio_type":"para"}},
      "13":{"class_type":"WanVideoTextEncodeCached","inputs":{"model_name":"umt5-xxl-enc-bf16.safetensors","precision":"bf16","positive_prompt":a.prompt,"negative_prompt":a.negative,"quantization":"disabled","use_disk_cache":True,"device":"gpu"}},
      "14":{"class_type":"WanVideoImageToVideoMultiTalk","inputs":{"vae":["1",0],"width":a.width,"height":a.height,"frame_window_size":81,"motion_frame":a.motion_frame,"force_offload":False,"colormatch":a.colormatch,"start_image":["7",0],"tiled_vae":False,"clip_embeds":["9",0],"mode":"infinitetalk"}},
      "15":{"class_type":"WanVideoSampler","inputs":{"model":["5",0],"image_embeds":["14",0],"steps":a.steps,"cfg":1.0,"shift":a.shift,"seed":a.seed,"force_offload":True,"scheduler":a.scheduler,"riflex_freq_index":0,"text_embeds":["13",0],"multitalk_embeds":["12",0]}},
      "16":{"class_type":"WanVideoDecode","inputs":{"vae":["1",0],"samples":["15",0],"enable_vae_tiling":True,"tile_x":272,"tile_y":272,"tile_stride_x":144,"tile_stride_y":128,"normalization":"default"}},
      "17":{"class_type":"VHS_VideoCombine","inputs":{"images":["16",0],"frame_rate":25,"loop_count":0,"filename_prefix":a.prefix,"format":"video/h264-mp4","pingpong":False,"save_output":True,"audio":["11",0]}},
    }
    if getattr(a, "compile", False):
        g["18"] = {"class_type":"WanVideoTorchCompileSettings","inputs":{"backend":"inductor","fullgraph":False,"mode":a.compile_mode,"dynamic":False,"dynamo_cache_size_limit":64,"compile_transformer_blocks_only":True}}
        g["5"]["inputs"]["compile_args"] = ["18",0]
    if getattr(a, "rife", 0) and a.rife > 1:
        g["20"] = {"class_type":"RIFE VFI","inputs":{"ckpt_name":"rife49.pth","frames":["16",0],"clear_cache_after_n_frames":10,"multiplier":a.rife,"fast_mode":True,"ensemble":False,"scale_factor":1.0,"dtype":"float32","torch_compile":False,"batch_size":4}}
        g["17"]["inputs"]["images"] = ["20",0]
        g["17"]["inputs"]["frame_rate"] = int(25 * a.rife)
    return g

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--image", required=True); p.add_argument("--audio", required=True)
    p.add_argument("--prompt", default="a person talking to the camera, natural facial expressions, realistic")
    p.add_argument("--negative", default="static, blurred, distorted face, deformed mouth, bad teeth, subtitles, watermark, low quality")
    p.add_argument("--prefix", default="infinitetalk_test")
    p.add_argument("--width", type=int, default=720); p.add_argument("--height", type=int, default=1280)
    p.add_argument("--steps", type=int, default=6); p.add_argument("--shift", type=float, default=11.0)
    p.add_argument("--seed", type=int, default=12345); p.add_argument("--blockswap", type=int, default=20)
    p.add_argument("--audio-scale", type=float, default=1.0)
    p.add_argument("--attention", default="sdpa")
    p.add_argument("--compile", action="store_true")
    p.add_argument("--base-model", default="Wan2_1-I2V-14B-720p_fp8_e4m3fn_scaled_KJ.safetensors")
    p.add_argument("--rife", type=int, default=0)
    p.add_argument("--colormatch", default="disabled")
    p.add_argument("--motion-frame", type=int, default=9)
    p.add_argument("--scheduler", default="dpm++_sde")
    p.add_argument("--prefetch", type=int, default=1)
    p.add_argument("--compile-mode", default="default")
    p.add_argument("--num-frames", type=int, default=0)
    p.add_argument("--fps", type=float, default=25.0); p.add_argument("--max-frames", type=int, default=1000)
    p.add_argument("--audio-path", default="/workspace/ComfyUI/input")
    a = p.parse_args()

    if a.num_frames <= 0:
        dur = ffprobe_duration(f"{a.audio_path}/{a.audio}")
        a.num_frames = min(int(round((dur or 16) * a.fps)), a.max_frames)
        print(f"[gen] audio dur={dur}s -> num_frames={a.num_frames}")

    # Aligner num_frames sur une fin de fenetre (81 + stride*k) ET caler l'audio dessus
    # -> evite la desync lip-sync de fin (frames generees sans audio embeds)
    stride = max(1, 81 - a.motion_frame)
    if a.num_frames > 81:
        a.num_frames = 81 + stride * math.ceil((a.num_frames - 81) / stride)
    tgt = round(a.num_frames / a.fps + 0.04, 2)
    _ain = f"{a.audio_path}/{a.audio}"; _ap = f"{a.audio_path}/_pad_{a.audio}"
    subprocess.run(["ffmpeg","-y","-loglevel","error","-i",_ain,"-af","apad","-t",str(tgt),_ap], check=False)
    a.audio = f"_pad_{a.audio}"
    print(f"[gen] num_frames aligne={a.num_frames}, audio cale a {tgt}s (lip-sync de bout en bout)")
    # VISIBILITÉ de la troncature : si l'audio SOURCE dépasse la durée générée (cap
    # max_frames atteint), le `-t` du ffmpeg apad ci-dessus le TRONQUE. Sans ce log,
    # un client dont l'audio > ~max_frames/fps recevrait une vidéo écourtée SANS aucune
    # trace. On le remonte dans le stdout (capté par worker.py → logsTail MassContent).
    _src_dur = ffprobe_duration(_ain)
    if _src_dur and tgt < _src_dur - 0.5:
        print(f"[gen] WARN AUDIO TRONQUÉ: source={_src_dur}s -> video={tgt}s "
              f"({round(_src_dur - tgt, 1)}s perdus, cap num_frames={a.num_frames}/max_frames={a.max_frames})")

    graph = build_graph(a)
    cid = uuid.uuid4().hex
    print(f"[gen] POST /prompt (image={a.image} audio={a.audio} {a.width}x{a.height} steps={a.steps} frames={a.num_frames})")
    r = post_json(f"{SERVER}/prompt", {"prompt":graph, "client_id":cid})
    if r.get("node_errors"):
        print("[gen] NODE_ERRORS:", json.dumps(r["node_errors"], indent=2)); sys.exit(2)
    pid = r["prompt_id"]; print(f"[gen] prompt_id={pid}")

    t0 = time.time()
    first_err_t = None  # début d'une série CONTINUE d'erreurs de poll (ComfyUI injoignable)
    while True:
        time.sleep(3)
        el = int(time.time() - t0)
        # 7000s = maillon le plus serré de la chaîne de timeouts, et le plus PROPRE
        # (sortie rc=3 avec logs). Doit rester > durée max réelle d'une génération :
        # ~21min pour 45s d'audio sur 5090, ~2× sur GPU lent → 7000s couvre ~85s
        # d'audio même sur le plus lent. Ordre : generate 7000 < worker kill 7200 <
        # watchdog MC 180min. VÉRIFIÉ À CHAQUE ITÉRATION (avant, ce check vivait dans
        # la branche `pid not in h` → INATTEIGNABLE si ComfyUI tombait : le poll
        # bouclait jusqu'au hard-kill worker 7200s = ~2h de GPU gaspillé par crash).
        if el > 7000:
            print("[gen] TIMEOUT"); sys.exit(3)
        try:
            h = get_json(f"{SERVER}/history/{pid}")
            first_err_t = None  # ComfyUI a répondu → reset la série d'erreurs
        except Exception as e:
            now = time.time()
            if first_err_t is None: first_err_t = now
            unreachable = int(now - first_err_t)
            print(f"[gen] poll err ({unreachable}s sans ComfyUI):", e, flush=True)
            # ComfyUI injoignable EN CONTINU > 120s = process mort (crash / OOM-kill).
            # On abandonne VITE (échec rc=5 → retry sur un autre pod) au lieu de poller
            # 2h dans le vide. Et on DUMPE la fin de comfyui.log : la VRAIE cause du
            # crash (traceback "CUDA out of memory", arrêt net = OOM-kill système
            # SIGKILL, ou erreur de node) y est — sinon INVISIBLE (le worker ne capture
            # que CE stdout, jamais les logs ComfyUI). C'est ce qui rend chaque crash
            # auto-diagnostiquable côté MassContent (logsTail du job FAILED).
            if unreachable > 120:
                print("[gen] ComfyUI INJOIGNABLE >120s -> abandon. Fin de comfyui.log :", flush=True)
                try:
                    with open("/workspace/logs/comfyui.log") as _f:
                        for _ln in _f.readlines()[-15:]:
                            print("[comfy]", _ln.rstrip())
                except Exception as _le:
                    print("[gen] comfyui.log illisible:", _le)
                sys.exit(5)
            continue
        if pid not in h:
            print(f"[gen] ... {el}s", flush=True)
            continue
        entry = h[pid]; st = entry.get("status",{})
        print(f"[gen] status={st.get('status_str')} done={st.get('completed')} ({int(time.time()-t0)}s)")
        if st.get("status_str") == "error":
            for m in st.get("messages",[]): print("[gen] MSG", json.dumps(m)[:500])
            sys.exit(4)
        outs = entry.get("outputs",{})
        vids=[]
        for nid,o in outs.items():
            for key in ("gifs","videos","images"):
                for v in o.get(key,[]):
                    vids.append(v)
        if vids:
            for v in vids: print("[gen] OUTPUT", json.dumps(v))
            v=vids[-1]; print(f"[gen] DONE in {int(time.time()-t0)}s -> /workspace/ComfyUI/output/{v.get('subfolder','')}/{v['filename']}".replace("//","/"))
            break

if __name__ == "__main__":
    main()
