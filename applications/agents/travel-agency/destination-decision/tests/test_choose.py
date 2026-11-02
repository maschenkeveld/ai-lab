from travel_common.contracts import Destination
from travel_destination_decision.server import choose


def d(name, code):
    return Destination(name=name, country="X", airport_iata_codes=[code])


SHORT = [d("Athens", "ATH"), d("Barcelona", "BCN"), d("Lisbon", "LIS")]


def test_roll_indexes_modulo_candidates():
    assert choose(SHORT, [], 1).name == "Athens"
    assert choose(SHORT, [], 2).name == "Barcelona"
    assert choose(SHORT, [], 20).name == "Barcelona"  # (20-1) % 3 == 1


def test_tried_airports_are_excluded():
    assert choose(SHORT, ["ATH"], 1).name == "Barcelona"


def test_all_tried_falls_back_to_full_shortlist():
    assert choose(SHORT, ["ATH", "BCN", "LIS"], 1).name == "Athens"


def test_deterministic_ignores_the_roll():
    assert choose(SHORT, ["ATH"], 3, deterministic=True).name == "Barcelona"


def test_empty_shortlist_is_none():
    assert choose([], [], 5) is None
