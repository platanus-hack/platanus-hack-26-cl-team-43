"""El experimento de línea base contra el modelo de juguete (sin red ni claves)."""
from cachai import experiment, suite


def _cells(res):
    return {(c["tramposo"], c["juego"], c["proveedor"]): c for c in res["resumen"]}


def test_fixed_benchmark_misses_an_evading_provider_and_blind_game_catches_it():
    res = experiment.run(experiment.toy_upstream(), reps=2, log=lambda m: None)
    s = _cells(res)
    simple = "reconoce el juego fijo"
    for name in ("prompt-oculto", "desvia-100", "oculto+desvia-30"):
        assert s[(simple, "fijo", name)]["detectados"] == 0
        assert s[(simple, "ciego", name)]["detectados"] == 2
    # el control honesto nunca se marca
    for game in ("fijo", "ciego"):
        assert s[(simple, game, "honesto")]["detectados"] == 0


def test_template_aware_cheater_evades_the_blind_game_too():
    res = experiment.run(experiment.toy_upstream(), reps=1, log=lambda m: None)
    s = _cells(res)
    for name, _ in experiment.CONDITIONS:
        assert s[("reconoce las plantillas", "ciego", name)]["detectados"] == 0


def test_template_detector_recognizes_every_blind_probe_and_no_ordinary_prompt():
    for p in suite.build(123):
        assert experiment.knows_templates(p.as_messages()), p.id
    for text in ("What is the capital of Chile?", "Resume este contrato en tres puntos.",
                 "Write a haiku about the sea."):
        assert not experiment.knows_templates([{"role": "user", "content": text}])


def test_markdown_has_one_row_per_provider():
    res = experiment.run(experiment.toy_upstream(), reps=1, log=lambda m: None)
    md = experiment.markdown(res)
    for name, _ in experiment.CONDITIONS:
        assert f"| {name}" in md


def test_evading_cheater_memorizes_the_whole_fixed_game_at_any_size():
    res = experiment.run(experiment.toy_upstream(), reps=1, n_tasks=40, log=lambda m: None)
    s = _cells(res)
    for name, _ in experiment.CONDITIONS:
        assert s[("reconoce el juego fijo", "fijo", name)]["detectados"] == 0
