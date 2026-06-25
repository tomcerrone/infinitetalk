"""Tests unitaires du garde-fou anti-OOM RIFE (generate.py).
Run : python3 scripts/test_generate.py  (ni GPU ni ComfyUI requis ; generate.py
n'execute main() que sous __main__, l'import est sans effet de bord)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import generate as g


def test_rife_on_under_threshold():
    # Sous le seuil (videos courtes, plage eprouvee) -> RIFE conservee.
    assert g.rife_decision(1105, 1305) is True   # ~45s
    assert g.rife_decision(1305, 1305) is True   # ~50s (pile au seuil)


def test_rife_off_above_threshold():
    # Au-dela du seuil (videos longues) -> RIFE desactivee (anti-OOM, 25fps).
    assert g.rife_decision(1377, 1305) is False  # ~52s
    assert g.rife_decision(1500, 1305) is False  # ~60s (le cas qui OOM-killait)


def test_threshold_is_tunable():
    # Le seuil est parametrable (env IT_RIFE_MAX_FRAMES) : remonter le seuil reactive RIFE.
    assert g.rife_decision(1500, 1600) is True


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
