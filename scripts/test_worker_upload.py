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


def test_repli_sans_rife_lit_le_fait_capte_pas_la_fin_du_journal():
    # Une generation de plus de 25 minutes : la ligne « [gen] RIFE active » du debut est suivie de plus de 500
    # lignes ; `run_generate` n'en garde que 500. Le repli doit quand meme savoir que l'interpolation etait active.
    script = ("import sys\n"
              "print('[gen] RIFE active (frames=729, besoin~24.0Go, RAM conteneur~26.1Go, seuil=750)')\n"
              "for i in range(700): print(f'[gen] ... {i*3}s')\n"
              "sys.exit(5)\n")
    rc, lignes, rife_active = w.run_generate([sys.executable, "-c", script], "test", 0.0 + __import__("time").time())
    assert rc == 5
    assert len(lignes) <= 500 and not any("RIFE active" in l for l in lignes)  # le journal garde ne la porte plus
    assert rife_active is True
    assert w.repli_sans_rife(rc, rife_active) is True
    # Interpolation deja coupee, ou autre code de sortie : pas de rejeu.
    script_off = "import sys\nprint('[gen] RIFE DESACTIVEE: frames=1017')\nsys.exit(5)\n"
    rc2, _, rife2 = w.run_generate([sys.executable, "-c", script_off], "test", __import__("time").time())
    assert rc2 == 5 and rife2 is False and w.repli_sans_rife(rc2, rife2) is False
    assert w.repli_sans_rife(3, True) is False and w.repli_sans_rife(0, True) is False


def test_rejeu_apres_liberation_seulement_sur_une_machine_qui_a_deja_travaille():
    refus = ["[gen] RAM detail: limite=57.7Go utilise=46.2Go dont cache rendable=16.0Go -> dispo=11.5Go",
             "[gen] MACHINE TROP PETITE: 11.5Go de RAM pour un travail qui en exige au moins 13.7Go (945 images)"]
    # Le cas releve le 18/11 : 4e travail d'une machine qui en avait livre trois.
    assert w.rejeu_apres_liberation(6, refus, 3) is True
    # Machine neuve : le refus est vrai, rien a rejouer.
    assert w.rejeu_apres_liberation(6, refus, 0) is False
    # rc=6 pour une autre raison (audio trop court), ou un autre code : pas de rejeu.
    assert w.rejeu_apres_liberation(6, ["[gen] audio trop court -> abandon (rc=6)"], 3) is False
    assert w.rejeu_apres_liberation(5, refus, 3) is False and w.rejeu_apres_liberation(0, refus, 3) is False


def test_la_phrase_du_refus_est_celle_que_generate_imprime():
    # Les deux fichiers changent ensemble : sans la phrase dans generate.py, le rejeu ne se declencherait plus jamais.
    source = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "generate.py"), encoding="utf-8").read()
    assert ("[gen] " + w.REFUS_MEMOIRE + ":") in source
    assert "sys.exit(6)" in source


def test_rejouer_apres_liberation_relance_puis_rejoue_une_seule_fois():
    refus = ["[gen] MACHINE TROP PETITE: 11.5Go de RAM pour un travail qui en exige au moins 13.7Go"]
    appels = {"relance": 0, "lance": 0, "notes": []}

    def relancer():
        appels["relance"] += 1
        return True

    def lancer(cmd, jid, t0):
        appels["lance"] += 1
        return 0, ["[gen] DONE in 12s -> /tmp/x.mp4"], False

    def noter(jid, **kw):
        appels["notes"].append(kw.get("note", ""))

    rc, lignes, rife = w.rejouer_apres_liberation(["x"], "j1", 0.0, 6, refus, False, 3, lancer=lancer, relancer=relancer, noter=noter)
    assert (rc, rife) == (0, False) and "DONE in" in lignes[0]
    assert appels["relance"] == 1 and appels["lance"] == 1
    assert "memoire non rendue" in appels["notes"][0]

    # ComfyUI ne repart pas : aucun rejeu, le refus est rendu tel quel.
    appels.update(relance=0, lance=0)
    rc, lignes, _ = w.rejouer_apres_liberation(["x"], "j1", 0.0, 6, refus, False, 3, lancer=lancer, relancer=lambda: False, noter=noter)
    assert rc == 6 and lignes is refus and appels["lance"] == 0

    # Machine neuve, ou travail reussi : rien n'est relance ni rejoue.
    appels.update(relance=0, lance=0)
    assert w.rejouer_apres_liberation(["x"], "j1", 0.0, 6, refus, False, 0, lancer=lancer, relancer=relancer, noter=noter)[0] == 6
    assert w.rejouer_apres_liberation(["x"], "j1", 0.0, 0, ["ok"], True, 3, lancer=lancer, relancer=relancer, noter=noter) == (0, ["ok"], True)
    assert appels["relance"] == 0 and appels["lance"] == 0

    # Une note impossible (reseau) n'empeche pas le rejeu.
    def noter_ko(jid, **kw):
        raise RuntimeError("reseau")
    assert w.rejouer_apres_liberation(["x"], "j1", 0.0, 6, refus, False, 2, lancer=lancer, relancer=relancer, noter=noter_ko)[0] == 0


if __name__ == "__main__":
    for nom, f in list(globals().items()):
        if nom.startswith("test_"):
            f()
            print("ok", nom)
