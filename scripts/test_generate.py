"""Tests unitaires du garde-fou anti-OOM RIFE (generate.py).
Run : python3 scripts/test_generate.py  (ni GPU ni ComfyUI requis ; generate.py
n'execute main() que sous __main__, l'import est sans effet de bord)."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import generate as g


def test_rife_off_above_threshold():
    # Au-dela du seuil de repli -> RIFE desactivee, quelle que soit la RAM annoncee.
    assert g.rife_decision(1377, 750) is False   # ~55s
    assert g.rife_decision(1500, 750, 999.0) is False  # RAM enorme mais > seuil


def test_rife_ram_aware_sous_le_seuil():
    # Sous le seuil, c'est la RAM REELLE qui tranche (la lecon du 07/08 : un seuil de
    # frames seul designait la zone de mort au lieu de la protéger).
    besoin = g.rife_ram_need_gb(750, 720, 1280, with_rife=True)
    assert g.rife_decision(750, 750, besoin + 1.0) is True    # marge -> on garde RIFE
    assert g.rife_decision(750, 750, besoin - 1.0) is False   # trop juste -> on coupe


def test_rife_ram_illisible_retombe_sur_le_seuil():
    # RAM illisible (hors conteneur / cgroup masque) : seul le seuil prudent decide.
    assert g.rife_decision(700, 750, None) is True
    assert g.rife_decision(800, 750, None) is False


def test_rife_ram_need_croit_avec_les_frames_et_rife():
    # Le besoin est proportionnel aux frames, et RIFE le double (entree + sortie).
    a = g.rife_ram_need_gb(1000, 720, 1280, with_rife=False)
    b = g.rife_ram_need_gb(2000, 720, 1280, with_rife=False)
    c = g.rife_ram_need_gb(1000, 720, 1280, with_rife=True)
    assert abs(b - 2 * a) < 1e-9
    assert abs(c - 2 * a) < 1e-9
    # Ordre de grandeur mesure en prod : ~11 Mo par frame 720x1280 en float32.
    assert 15.0 < a < 20.0  # 1000 frames sans RIFE, marge 1.6 comprise


def test_rife_cas_reel_du_07_08_est_refuse():
    # Le job qui a OOM-kill ComfyUI le 07/08 : 1305 frames, RIFE active, ~41 Go de RAM
    # conteneur. L'ancienne regle disait OUI (1305 <= 1305). La nouvelle dit NON.
    assert g.rife_decision(1305, 1305, 41.0) is False


def test_avail_ram_gb_runs():
    # Ne doit jamais lever (None acceptable hors Linux / hors conteneur).
    v = g.avail_ram_gb()
    assert v is None or (isinstance(v, float) and v > 0)


GO = 1024 ** 3


def test_ram_machine_chaude_compte_le_cache_comme_rendable():
    # Le cas mesure en production : au 2e travail, limite 48 Go, `memory.current` 47,3 Go
    # (dont 30 Go de cache des fichiers de modeles). L'ancien calcul (limite - usage)
    # rendait 0,7 Go -> refus « MACHINE TROP PETITE ». Le cache est rendable : 30,7 Go.
    stat_v2 = {"anon": int(17.3 * GO), "file": 30 * GO, "active_file": 20 * GO,
               "inactive_file": 10 * GO, "file_dirty": 0, "file_writeback": 0, "shmem": 0}
    dispo = g.ram_disponible_octets(48 * GO, int(47.3 * GO), stat_v2)
    assert abs(dispo / GO - 30.7) < 0.01


def test_ram_shmem_et_cache_sale_ne_sont_pas_rendables():
    # `file` contient tmpfs / memoire partagee (pas rendable sans swap) : on ne lit QUE les
    # listes de fichiers ; et le cache modifie pas encore ecrit n'est pas rendable tout de suite.
    stat = {"file": 12 * GO, "shmem": 4 * GO, "active_file": 6 * GO, "inactive_file": 2 * GO,
            "file_dirty": 1 * GO, "file_writeback": 1 * GO}
    assert g.cache_fichiers_recuperable(stat) == 6 * GO


def test_ram_cgroup_v1_lit_les_cles_hierarchiques():
    stat_v1 = {"total_active_file": 3 * GO, "total_inactive_file": 5 * GO, "total_dirty": 1 * GO,
               "active_file": 99 * GO}
    assert g.cache_fichiers_recuperable(stat_v1) == 7 * GO


def test_ram_illisible_ne_conclut_rien():
    assert g.ram_disponible_octets(None, 10 * GO, {}) is None
    assert g.ram_disponible_octets(10 * GO, None, {}) is None
    # Sans memory.stat lisible, on retombe sur limite - usage (jamais plus optimiste).
    assert g.ram_disponible_octets(10 * GO, 4 * GO, {}) == 6 * GO
    # Jamais negatif.
    assert g.cache_fichiers_recuperable({"active_file": 1, "file_dirty": 5}) == 0


def test_detail_ram_lisible():
    txt = g.detail_ram({"dispo": 30 * GO, "limite": 48 * GO, "utilise": 47 * GO,
                        "cache": 29 * GO, "hote": None})
    assert "limite=48.0Go" in txt and "cache rendable=29.0Go" in txt and "hote=?" in txt


def _worker():
    os.environ.setdefault("MASSCONTENT_BASE_URL", "http://mc.invalid")
    os.environ.setdefault("PIPELINE_SECRET", "test")
    import worker as w
    return w


def test_liberer_comfy_appelle_la_route_officielle():
    # POST /free {"unload_models": true, "free_memory": true} (ComfyUI server.py).
    import http.server, threading, json as _json
    recu = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            recu["path"] = self.path
            recu["body"] = _json.loads(self.rfile.read(n) or b"{}")
            self.send_response(200); self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    w = _worker()
    ancien_comfy, ancien_sleep = w.COMFY, w.time.sleep
    w.COMFY = f"http://127.0.0.1:{srv.server_address[1]}"
    w.time.sleep = lambda s: None
    try:
        assert w.liberer_comfy() is True
    finally:
        w.COMFY, w.time.sleep = ancien_comfy, ancien_sleep
        srv.server_close()
    assert recu["path"] == "/free"
    assert recu["body"] == {"unload_models": True, "free_memory": True}


def test_liberer_comfy_injoignable_ne_bloque_rien():
    w = _worker()
    ancien = w.COMFY
    w.COMFY = "http://127.0.0.1:9"  # port ferme
    try:
        assert w.liberer_comfy() is False
    finally:
        w.COMFY = ancien


def test_chaque_travail_libere_la_memoire_succes_ou_echec():
    # La boucle libere ComfyUI apres CHAQUE travail, meme en echec : c'est ce qui permet a
    # une machine deja allumee d'enchainer les videos.
    w = _worker()
    appels = {"free": 0, "claims": 0}
    sauvegarde = {k: getattr(w, k) for k in ("wait_comfy", "comfy_alive", "claim", "process",
                                             "complete", "liberer_comfy")}

    class Fin(BaseException):  # la boucle avale les Exception de la reclamation
        pass

    def claim():
        appels["claims"] += 1
        if appels["claims"] > 2:
            raise Fin()
        return {"job": {"jobId": f"j{appels['claims']}", "videoId": f"v{appels['claims']}"}}

    def process(job):
        if job["jobId"] == "j1":
            raise RuntimeError("echec simule")
        return ("k", "u", 1, 1.0)

    w.wait_comfy = lambda max_s=900: True
    w.comfy_alive = lambda timeout=5: True
    w.claim = claim
    w.process = process
    w.complete = lambda payload: {}
    w.liberer_comfy = lambda: appels.__setitem__("free", appels["free"] + 1) or True
    try:
        try:
            w.main()
        except Fin:
            pass
    finally:
        for k, v in sauvegarde.items():
            setattr(w, k, v)
    assert appels["free"] == 2


def test_progression_images_lit_la_derniere_fenetre():
    # Journal reel de ComfyUI : des barres tqdm separees par des retours chariot, une par fenetre.
    journal = ("Sampling 1233 frames in 17 windows, at 720x1280 with 4 steps\n"
               "Sampling audio indices 0-81:  25%|##5       | 1/4 [00:20<01:00]\r"
               "Sampling audio indices 0-81: 100%|##########| 4/4 [01:20<00:00]\n"
               "Sampling audio indices 72-153:  50%|#####     | 2/4 [00:40<00:40]\r")
    assert g.progression_images(journal) == 72
    assert g.progression_images("rien encore") is None
    assert g.progression_images("") is None


def test_fin_du_journal_illisible():
    assert g.fin_du_journal("/chemin/qui/n/existe/pas.log") == ""


def test_fin_du_journal_ignore_le_travail_precedent():
    # ComfyUI ecrit a la suite d'un travail a l'autre : seul ce qui suit le debut de CE travail compte.
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False, encoding="utf-8") as f:
        f.write("Sampling audio indices 1152-1233: 100%\n")
        chemin = f.name
    try:
        debut = g.taille_du_journal(chemin)
        assert g.progression_images(g.fin_du_journal(chemin, depuis=debut)) is None
        with open(chemin, "a", encoding="utf-8") as f:
            f.write("Sampling audio indices 0-81:  25%\r")
        assert g.progression_images(g.fin_du_journal(chemin, depuis=debut)) == 0
        # Journal recree plus court (ComfyUI relance) : relu depuis le debut.
        assert g.progression_images(g.fin_du_journal(chemin, depuis=10 ** 9)) == 0
    finally:
        os.remove(chemin)


def test_worker_recolle_l_avancement_dans_la_note():
    # Le worker capte la DERNIERE progression de generate.py et la recolle a chaque battement.
    w = _worker()
    notes = []
    ancien_progress, ancien_hb = w.progress, w.HEARTBEAT_S
    w.progress = lambda jid, **kw: notes.append(kw.get("note", ""))
    w.HEARTBEAT_S = 0.2
    try:
        code = ("import time;print('[gen] num_frames aligne=1233, audio cale', flush=True);"
                "print('[gen] PROGRESSION images=72/1233 (30s)', flush=True);time.sleep(1.2)")
        rc, _ = w.run_generate([sys.executable, "-c", code], "job", time.time())
    finally:
        w.progress, w.HEARTBEAT_S = ancien_progress, ancien_hb
    assert rc == 0
    assert any("avancement 72/1233 images" in n for n in notes), notes


def test_audio_usable_rejects_illisible_et_vide():
    # None (ffprobe KO) ou <=0 -> rejete (rc=6), comportement historique conserve.
    assert g.audio_is_usable(None, 1.0) is False
    assert g.audio_is_usable(0, 1.0) is False
    assert g.audio_is_usable(-1, 1.0) is False


def test_audio_usable_rejects_trop_court():
    # Audio valide mais minuscule (tronque/corrompu, ex 0,6s) -> rejete (le cas confirme).
    assert g.audio_is_usable(0.6, 1.0) is False
    assert g.audio_is_usable(0.99, 1.0) is False


def test_audio_usable_accepts_canary_et_prod():
    # Canary 6s + prod 30-60s = exploitables (zero regression sur les durees legitimes).
    assert g.audio_is_usable(1.0, 1.0) is True   # pile au seuil
    assert g.audio_is_usable(6.0, 1.0) is True    # canary
    assert g.audio_is_usable(45.0, 1.0) is True   # mediane prod
    assert g.audio_is_usable(60.0, 1.0) is True   # max prod


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print(f"PASS {len(fns)} tests")
