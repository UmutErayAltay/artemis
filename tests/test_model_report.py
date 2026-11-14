from bench import model_testi


def test_ui_scenario_catalog_matches_real_scenarios() -> None:
    # Arayüzün elle tuttuğu kimlik listesi (artemis.brain.model_report) gerçek senaryolardan
    # ayrışırsa bu test kırılır; bench'in içe aktarımı kırılmaz.
    model_testi._check_catalog()
