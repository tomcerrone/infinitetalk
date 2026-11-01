"""Tests du dépôt S3 rejoué (worker.py, 2026-11-01).
Run : python3 scripts/test_worker_upload.py  (ni GPU ni réseau : `lancer`, `dormir`, `horloge` sont injectés ;
worker.py n'exécute main() que sous __main__)."""
import os, sys, subprocess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import worker as w


class Faux:
    """Rejoue une suite de réponses de curl (returncode, http_code, stderr)."""
    def __init__(self, reponses):
        self.reponses = list(reponses)
        self.appels = 0

    def __call__(self, cmd, **kw):
        self.appels += 1
        rc, code, err = self.reponses.pop(0)
        return subprocess.CompletedProcess(cmd, rc, stdout=str(code), stderr=err)


def horloge_fixe():
    return 0.0


def test_panne_dns_passagere_puis_succes():
    f = Faux([(6, 0, "curl: (6) Could not resolve host"), (0, 200, "")])
    attentes = []
    assert w.deposer_la_video("v.mp4", "https://s3/x", lancer=f, dormir=attentes.append, horloge=horloge_fixe) == 2
    assert f.appels == 2 and attentes == [5]


def test_refus_net_jamais_rejoue():
    f = Faux([(22, 403, "curl: (22) The requested URL returned error: 403")])
    try:
        w.deposer_la_video("v.mp4", "https://s3/x", lancer=f, dormir=lambda s: None, horloge=horloge_fixe)
        assert False, "devait lever"
    except RuntimeError as e:
        # Le format relu par MassContent (summarizeGpuError) : « upload S3 echec rc=N: … ».
        assert str(e).startswith("upload S3 echec rc=22:")
    assert f.appels == 1


def test_5xx_rejoue_et_borne():
    f = Faux([(22, 503, "e")] * w.UPLOAD_ESSAIS)
    try:
        w.deposer_la_video("v.mp4", "https://s3/x", lancer=f, dormir=lambda s: None, horloge=horloge_fixe)
        assert False, "devait lever"
    except RuntimeError as e:
        assert "rc=22" in str(e) and f"essai {w.UPLOAD_ESSAIS}/{w.UPLOAD_ESSAIS}" in str(e)
    assert f.appels == w.UPLOAD_ESSAIS


def test_budget_de_temps_respecte():
    t = [0.0]
    def horloge():
        return t[0]
    def dormir(s):
        t[0] += 1000  # le budget de 960 s est dépassé après le premier essai
    f = Faux([(6, 0, "e"), (0, 200, "")])
    try:
        w.deposer_la_video("v.mp4", "https://s3/x", lancer=f, dormir=dormir, horloge=horloge)
        assert False, "devait lever"
    except RuntimeError as e:
        assert str(e).startswith("upload S3 echec rc=6:")
    assert f.appels == 1


def test_depot_a_reessayer():
    assert w.depot_a_reessayer(6, 0) and w.depot_a_reessayer(28, 0) and w.depot_a_reessayer(22, 429)
    assert not w.depot_a_reessayer(0, 200) and not w.depot_a_reessayer(22, 403) and not w.depot_a_reessayer(22, 400)


if __name__ == "__main__":
    for nom, f in list(globals().items()):
        if nom.startswith("test_"):
            f()
            print("ok", nom)
