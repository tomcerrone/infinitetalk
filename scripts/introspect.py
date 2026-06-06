import json, urllib.request
CLASSES = ["WanVideoModelLoader","MultiTalkModelLoader","WanVideoLoraSelect","WanVideoVAELoader",
           "CLIPVisionLoader","WanVideoTextEncodeCached","Wav2VecModelLoader","DownloadAndLoadWav2VecModel",
           "WanVideoImageToVideoMultiTalk","MultiTalkWav2VecEmbeds","WanVideoSampler","WanVideoBlockSwap",
           "WanVideoClipVisionEncode","WanVideoDecode","ImageResizeKJv2","LoadImage","LoadAudio",
           "VHS_VideoCombine","WanVideoPassImagesFromSamples"]
for c in CLASSES:
    try:
        d = json.load(urllib.request.urlopen("http://127.0.0.1:8188/object_info/"+c, timeout=15))
        info = d[c]["input"]; print("=== %s ===" % c)
        for sect in ("required","optional"):
            for name, spec in (info.get(sect) or {}).items():
                t = spec[0]
                if isinstance(t, list):
                    opts = t if len(t) <= 14 else t[:14] + ["...(%d total)" % len(t)]
                    print("  [%s] %s: ENUM %s" % (sect, name, opts))
                else:
                    extra = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
                    print("  [%s] %s: %s default=%s" % (sect, name, t, extra.get("default","")))
    except Exception as e:
        print("=== %s === ERR %s" % (c, e))
