"""Feature 9: config-driven M6 classifications (pure functions, no I/O beyond config)."""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from backend.m6_impact import exports, impact

S = impact.load_impact_settings()


def test_settings_come_from_config():
    assert S.depth_classes_m == (0.1, 0.3, 1.0, 2.0, 5.0)
    assert S.arrival_bands_min == (15, 30, 60, 120, 180, 360)
    assert (S.arrival_percentile, S.detection_delay_min, S.zone_high_p, S.zone_possible_p) == (10, 10, 0.5, 0.1)
    assert S.dv_hazard["status"] == "unverified"  # never presented as verified


@pytest.mark.parametrize("p,zone", [(0.0999, None), (0.1, "possible"), (0.4999, "possible"), (0.5, "high"), (1.0, "high")])
def test_zone_thresholds(p, zone):
    assert impact.zone_of(p, S) == zone
    arr = impact.zone_array(np.array([[p]]), np.array([[True]]), S)
    assert arr[0, 0] == {None: 0, "possible": 1, "high": 2}[zone]


@pytest.mark.parametrize("depth,label", [(0.0999, None), (0.1, "wet"), (0.3, "low"), (0.9999, "low"),
                                         (1.0, "moderate"), (2.0, "high"), (4.99, "high"), (5.0, "extreme"), (None, None)])
def test_depth_class_boundaries(depth, label):
    assert impact.depth_class_label(depth, S) == label
    assert exports.depth_class_label(depth) == label  # exports reads the same config


def test_depth_classes_follow_config_changes():
    custom = replace(S, depth_classes_m=(0.5, 3.0), depth_class_labels=("a", "b"))
    assert impact.depth_class_label(0.4, custom) is None and impact.depth_class_label(3.0, custom) == "b"


def test_arrival_bands_cover_config_edges():
    bands = impact.arrival_bands(S)
    assert [b["label"] for b in bands] == ["0-15 min", "15-30 min", "30-60 min", "60-120 min", "120-180 min",
                                           "180-360 min", "> 360 min"]
    arr = np.array([[0.0, 899.0, 900.0, 21600.0, 1e6, -9999.0]])
    valid = arr != -9999.0
    assert impact.arrival_band_array(arr, valid, S).tolist() == [[1, 1, 2, 7, 7, 0]]


def test_lead_time_subtracts_delay_and_never_goes_negative():
    assert impact.lead_time(1200.0, S) == (600.0, False)
    assert impact.lead_time(300.0, S) == (0.0, True)
    assert impact.lead_time(None, S) == (None, False)


@pytest.mark.parametrize("n,shown", [(12.0, "< 50"), (49.9, "< 50"), (50.0, "50"), (1234.0, "1,200"), (3456.0, "3,500")])
def test_population_display(n, shown):
    assert impact.population_display(n, S) == shown
