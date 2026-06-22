"""Tests unitaires du garde-fou RAM RIFE (generate.py).
Run : python3 scripts/test_generate.py  (ne necessite ni GPU ni ComfyUI ;
generate.py n'execute main() que sous __main__, l'import est sans effet de bord)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import generate as g


def test_peak_scales_with_frames():
    # Le pic RAM croit avec la duree (plus de frames) et le multiplicateur.
    p45 = g.rife_peak_gb(1105, 2, 720, 1280)
    p60 = g.rife_peak_gb(1512, 2, 720, 1280)
    assert 0 < p45 < p60, (p45, p60)
    assert g.rife_peak_gb(1512, 3, 720, 1280) > p60


def test_multiplier_1_always_fits():
    # Pas d'interpolation (multiplier<=1) -> rien a accumuler -> toujours OK.
    assert g.rife_fits_ram(10**6, 1, 720, 1280, 1.0) is True


def test_none_avail_keeps_rife():
    # RAM indeterminable (/proc illisible) -> comportement historique (on garde RIFE).
    assert g.rife_fits_ram(5000, 2, 720, 1280, None) is True


def test_skips_when_over_budget():
    # Petit node (8 Go) + longue video -> ne tient pas -> RIFE a desactiver.
    assert g.rife_fits_ram(1512, 2, 720, 1280, 8.0, 0.7) is False


def test_keeps_when_room():
    # Gros node (256 Go) -> tient large -> RIFE conservee.
    assert g.rife_fits_ram(1512, 2, 720, 1280, 256.0, 0.7) is True


def test_headroom_is_respected():
    # Au pic exact, headroom=1.0 accepte, 0.5 refuse (le seuil bouge avec headroom).
    peak = g.rife_peak_gb(1512, 2, 720, 1280)
    assert g.rife_fits_ram(1512, 2, 720, 1280, peak, 1.0) is True
    assert g.rife_fits_ram(1512, 2, 720, 1280, peak, 0.5) is False


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print(f"PASS {len(fns)} tests")
