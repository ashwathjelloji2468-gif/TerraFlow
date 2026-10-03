"""Feature 16 P1: an event imagery `source` (client-supplied via POST /sites) can never make M7 read
or write outside `<repo_root>/cache/gee/<site_id>/`. Synthetic paths only; nothing is fetched."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.m7_gee import imagery
from tests.m7_gee.test_imagery import _Cfg, _Event, _SourcedValue, _write_rgb_tif

UNSAFE = [
    "../../etc/passwd.tif",
    "cache/gee/teesta/../../../outside.tif",
    "cache/gee/../gee/teesta/x.tif",
    "/etc/cron.d/x.tif",
    "/tmp/cache/gee/teesta/x.tif",
    "C:/Windows/x.tif",
    "cache\\gee\\teesta\\x.tif",
    "cache/gee/teesta/x.tif\x00.png",
    "data/teesta/config/teesta.tif",   # outside cache/gee
    "cache/teesta/x.tif",              # not under cache/gee
    "cache/gee/x.tif",                 # no site folder
    "cache/gee/teesta/x.py",           # not a GeoTIFF
    "cache/gee/teesta/x",              # no suffix
]


@pytest.mark.parametrize("source", UNSAFE)
def test_unsafe_sources_rejected_before_any_io(tmp_path, source):
    with pytest.raises(imagery.UnsafeImageryPath):
        imagery.raw_rgb_path(source, tmp_path)
    assert list(tmp_path.iterdir()) == []  # nothing created


def test_unsafe_is_a_value_error(tmp_path):
    assert issubclass(imagery.UnsafeImageryPath, ValueError)


@pytest.mark.parametrize("source,expected", [
    ("cache/gee/teesta/teesta_pre_event.tif", "cache/gee/teesta/teesta_pre_event_rgb.tif"),
    ("cache/gee/teesta/sub/a.TIFF", "cache/gee/teesta/sub/a_rgb.TIFF"),
    ("./cache/gee/teesta/a.tif", "cache/gee/teesta/a_rgb.tif"),
])
def test_valid_sources_unchanged(tmp_path, source, expected):
    assert imagery.raw_rgb_path(source, tmp_path) == tmp_path / expected
    assert imagery.raw_rgb_path(source, tmp_path, "teesta") == tmp_path / expected


def test_other_sites_folder_rejected_when_site_given(tmp_path):
    with pytest.raises(imagery.UnsafeImageryPath, match="cache/gee/demo_site/"):
        imagery.raw_rgb_path("cache/gee/teesta/teesta_pre_event.tif", tmp_path, "demo_site")


@pytest.mark.skipif(os.name == "nt", reason="symlinks")
def test_symlink_escape_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "repo" / "cache" / "gee").mkdir(parents=True)
    (tmp_path / "repo" / "cache" / "gee" / "evil").symlink_to(outside, target_is_directory=True)
    with pytest.raises(imagery.UnsafeImageryPath, match="symlink"):
        imagery.raw_rgb_path("cache/gee/evil/x.tif", tmp_path / "repo")


def test_teesta_config_source_still_valid():
    import warnings

    from backend.shared.site_config import PlaceholderWarning, load_site_config
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PlaceholderWarning)
        cfg = load_site_config("teesta")
    for ev in cfg.events:
        for sv in (ev.imagery_pre_event, ev.imagery_post_event):
            if sv.source:
                imagery.raw_rgb_path(sv.source, Path("/repo"), cfg.site.id)  # does not raise


def _cfg(pre: str, post: str):
    return _Cfg(events=[_Event(id="synth_event_2020", imagery_pre_event=_SourcedValue("2020-01-01", pre),
                               imagery_post_event=_SourcedValue("2020-01-10", post))])


def test_convert_refuses_traversal_and_writes_nothing(tmp_path):
    secret = tmp_path / "secret_rgb.tif"
    _write_rgb_tif(secret)  # a real GeoTIFF the traversal would otherwise read
    data_dir = tmp_path / "data"
    with pytest.raises(imagery.UnsafeImageryPath):
        imagery.convert("testsite", cfg=_cfg("../secret.tif", "../secret.tif"), data_dir=data_dir,
                        repo_root=tmp_path / "repo")
    assert not data_dir.exists()


def test_convert_refuses_another_sites_imagery(tmp_path):
    repo = tmp_path / "repo"
    _write_rgb_tif(repo / "cache/gee/teesta/t_pre_rgb.tif")
    _write_rgb_tif(repo / "cache/gee/teesta/t_post_rgb.tif")
    with pytest.raises(imagery.UnsafeImageryPath):
        imagery.convert("testsite", cfg=_cfg("cache/gee/teesta/t_pre.tif", "cache/gee/teesta/t_post.tif"),
                        data_dir=tmp_path / "data", repo_root=repo)


def test_refresh_never_calls_live_render_for_unsafe_path(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr("backend.m7_gee.live_render.render_event_rgb",
                        lambda *a, **k: called.append(1) or {})
    victim = tmp_path / "victim_rgb.tif"
    victim.write_bytes(b"keep")
    result = imagery.refresh("testsite", cfg=_cfg("../victim.tif", "../victim.tif"), data_dir=tmp_path / "data",
                             repo_root=tmp_path / "repo")  # refresh never raises: it falls back (existing contract)
    assert result.manifest_path is None and any("UnsafeImageryPath" in e for e in result.errors)
    assert called == [] and victim.read_bytes() == b"keep"
    assert not (tmp_path / "victim_rgb.staging.tif").exists()
