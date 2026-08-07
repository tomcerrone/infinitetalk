"""Tests unitaires du garde-fou anti-OOM RIFE (generate.py).
Run : python3 scripts/test_generate.py  (ni GPU ni ComfyUI requis ; generate.py
n'execute main() que sous __main__, l'import est sans effet de bord)."""
import os, sys
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
