"""Story 2.2 item 4: rates between consecutive counters rows, and reset detection.

Pinned to story 2.1's capture pairs. dt is the manifest's captured_at difference:
15:36:44Z - 15:34:25Z = 2 min 19 s = 139 s for every pair. The counters are the rows
``rows.probe_to_rows`` builds from each capture (NULL columns included), as the collector
stores them.
"""
import copy

import pytest

from localagents.monitor import rates, rows

from monitor_fixtures import doc, dt, raw


def _counters(path):
    body = doc(path) if path.endswith(".json") else raw(path).decode()
    kind = "strata" if path.endswith(".json") else "prometheus"
    return rows.probe_to_rows(1, None, {"up": True, "host": "up"}, (kind, body), 0.0).counters


def _since(path):
    return doc(path)["totals"]["since"]


def test_the_manifest_gives_139_seconds_between_passes():
    for a, b in [("strata/ai2_metrics_1.json", "strata/ai2_metrics_2.json"),
                 ("strata/ai1_metrics_1.json", "strata/ai1_metrics_2.json"),
                 ("vllm/dgx1-a_metrics_1.txt", "vllm/dgx1-a_metrics_2.txt"),
                 ("llamacpp/llamacpp_metrics_1.txt", "llamacpp/llamacpp_metrics_2.txt")]:
        assert dt(a, b) == 139.0


def test_ai2_pair_rates_by_hand():
    """ai2 (strata) totals, pass 1 -> pass 2:
    prompt_tokens 15631377 -> 16120787 (+489410), reused 15243241 -> 15727997 (+484756), so
    processed = 489410 - 484756 = 4654; output_tokens 220395 -> 222979 (+2584);
    drafts_offered 184778 -> 186884 (+2106), drafts_accepted 132173 -> 133904 (+1731).
    prompt_tps = 4654 / 139 = 33.482; gen_tps = 2584 / 139 = 18.590;
    cached_tps = 484756 / 139 = 3487.453; spec_acceptance = round(1731 / 2106, 3) = 0.822."""
    a, b = "strata/ai2_metrics_1.json", "strata/ai2_metrics_2.json"
    r = rates.rates(_counters(a), _counters(b), dt(a, b), "strata", _since(a), _since(b))
    assert r.reset is False and r.reason is None
    assert r.prompt_tps == pytest.approx(4654 / 139) == pytest.approx(33.482, abs=1e-3)
    assert r.gen_tps == pytest.approx(2584 / 139) == pytest.approx(18.590, abs=1e-3)
    assert r.cached_tps == pytest.approx(484756 / 139) == pytest.approx(3487.453, abs=1e-3)
    assert r.spec_acceptance == 0.822


def test_dgx1a_pair_rates_by_hand():
    """dgx1-a (vLLM), pass 1 -> pass 2: prompt_tokens 17994028 -> 18361898 (+367870, counts
    cached tokens too), prefix_cache_hits 17176832 -> 17534720 (+357888), so processed =
    367870 - 357888 = 9982; generation 293517 -> 299686 (+6169); draft tokens 428005 -> 437219
    (+9214), accepted 212613 -> 217046 (+4433).
    prompt_tps = 9982 / 139 = 71.813; gen_tps = 6169 / 139 = 44.381;
    cached_tps = 357888 / 139 = 2574.734; spec_acceptance = round(4433 / 9214, 3) = 0.481."""
    a, b = "vllm/dgx1-a_metrics_1.txt", "vllm/dgx1-a_metrics_2.txt"
    r = rates.rates(_counters(a), _counters(b), dt(a, b), "vllm", None, None)
    assert r.reset is False
    assert r.prompt_tps == pytest.approx(9982 / 139) == pytest.approx(71.813, abs=1e-3)
    assert r.gen_tps == pytest.approx(6169 / 139) == pytest.approx(44.381, abs=1e-3)
    assert r.cached_tps == pytest.approx(357888 / 139) == pytest.approx(2574.734, abs=1e-3)
    assert r.spec_acceptance == 0.481


def test_llamacpp_pair_is_a_reset_with_no_rates():
    """The llama.cpp server restarted between the passes: every counter is 0 in pass 2
    (prompt_tokens 4705 -> 0, prompt_tokens_cached 18341 -> 0, tokens_predicted 3822 -> 0, ...)."""
    a, b = "llamacpp/llamacpp_metrics_1.txt", "llamacpp/llamacpp_metrics_2.txt"
    r = rates.rates(_counters(a), _counters(b), dt(a, b), "llamacpp", None, None)
    assert r.reset is True
    assert (r.prompt_tps, r.gen_tps, r.cached_tps, r.spec_acceptance) == (None, None, None, None)
    assert r.reason.startswith("counters went backwards: prompt_tokens 4705 → 0, prompt_tokens_cached 18341 → 0,")
    assert "generated_tokens 3822 → 0" in r.reason and "spec_accepted_tokens 2105 → 0" in r.reason


def test_reset_reason_rounds_the_figures_it_names():
    """Review of 3.3 (its third server seam): the stored restart detail carried the raw float
    repr — ``prompt_tokens 392777.0722222222 → 388136``. The detail is an event-list sentence,
    so the readings it names read rounded: whole numbers plain, fractions to two decimals."""
    prev = {"prompt_tokens": 392777.0722222222, "generated_tokens": 222971.82222222222}
    cur = {"prompt_tokens": 388136, "generated_tokens": 220395}
    assert rates.reset_reason(prev, cur, None, None) == (
        "counters went backwards: prompt_tokens 392777.07 → 388136,"
        " generated_tokens 222971.82 → 220395")


def test_ai1_idle_pair_rates_are_zero_not_none():
    """ai1 sat idle across both passes: every total identical, so every token rate is 0.0.
    No drafts were offered in the interval, so the acceptance ratio has no value (None)."""
    a, b = "strata/ai1_metrics_1.json", "strata/ai1_metrics_2.json"
    assert doc(a)["totals"] == doc(b)["totals"]
    r = rates.rates(_counters(a), _counters(b), dt(a, b), "strata", _since(a), _since(b))
    assert r.reset is False
    assert (r.prompt_tps, r.gen_tps, r.cached_tps) == (0.0, 0.0, 0.0)
    assert r.spec_acceptance is None


def test_since_changing_with_counters_that_only_grew_is_a_reset():
    """A restart can outrun the old counters before the next probe: ai2's pass 2 with its since
    moved later (the one field changed) is a reset though every counter grew."""
    a = "strata/ai2_metrics_1.json"
    restarted = copy.deepcopy(doc("strata/ai2_metrics_2.json"))
    restarted["totals"]["since"] = 1793288100.0
    cur = rows.probe_to_rows(1, None, {"up": True}, ("strata", restarted), 0.0).counters
    r = rates.rates(_counters(a), cur, 139.0, "strata", _since(a), 1793288100.0)
    assert r.reset is True and r.reason == "since changed (1793229893.2645304 → 1793288100.0)"
    assert (r.prompt_tps, r.gen_tps, r.cached_tps, r.spec_acceptance) == (None, None, None, None)


def test_unknown_since_on_either_side_is_not_a_reset():
    a, b = "strata/ai2_metrics_1.json", "strata/ai2_metrics_2.json"
    for ps, cs in ((None, _since(b)), (_since(a), None)):
        assert rates.rates(_counters(a), _counters(b), 139.0, "strata", ps, cs).reset is False


def test_no_previous_counters_or_no_interval_gives_no_rates():
    b = _counters("strata/ai2_metrics_2.json")
    for prev, d in ((None, 139.0), ({}, 139.0), (_counters("strata/ai2_metrics_1.json"), 0.0)):
        r = rates.rates(prev, b, d, "strata", None, None)
        assert r.reset is False and (r.prompt_tps, r.gen_tps, r.cached_tps) == (None, None, None)


def test_a_counter_absent_on_one_side_is_not_a_reset():
    """A NULL column (a series the backend stopped reporting) is never read as going backwards."""
    a, b = _counters("vllm/dgx1-a_metrics_1.txt"), _counters("vllm/dgx1-a_metrics_2.txt")
    b["prefix_queries"] = b["prefix_hits"] = None  # pass 2 without the prefix-cache series
    r = rates.rates(a, b, 139.0, "vllm", None, None)
    assert r.reset is False
    assert r.prompt_tps == pytest.approx(367870 / 139)  # no prefix series in both: prompt is all processed
    assert r.cached_tps is None
