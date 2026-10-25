"""GTG Nowcast verification: projection, sampling, scores, acceptance and API backfill mapping."""
import numpy as np
import pandas as pd
import pytest

import test_feed
from conftest import calm_cluster, make_track
from turbqc import config, db, feed, pipeline, synthetic, verify

# Reference points read with eccodes from gtgn.t1200z.3km.grib2 (24 Oct 2026): (i, j, lat, lon)
GTGN_GRID = verify.LambertGrid(1799, 1059, 21.138124, 237.28048, 262.5, 38.5, 38.5, 3000.0, 3000.0)
ECCODES_POINTS = [(0, 0, 21.13812, 237.28048), (1798, 0, 21.14054, 287.71029), (0, 1058, 47.83863, 225.90453),
                  (1798, 1058, 47.84219, 299.08282), (899, 529, 38.49725, 262.49403),
                  (1500, 300, 30.68082, 281.30905), (200, 800, 43.11071, 236.39886)]


@pytest.mark.parametrize("i,j,lat,lon", ECCODES_POINTS)
def test_lambert_projection_matches_eccodes(i, j, lat, lon):
    fi, fj = GTGN_GRID.ij(lat, lon - 360)  # reports use -180..180
    assert abs(fi - i) < 0.01 and abs(fj - j) < 0.01


def test_split_grib_uses_section0_length():
    def msg(body: bytes) -> bytes:
        total = 16 + len(body) + 4
        return b"GRIB" + b"\0\0" + b"\0" + b"\x02" + total.to_bytes(8, "big") + body + b"7777"
    payload = msg(b"abc") + msg(b"GRIBinside") + msg(b"")
    parts = verify.split_grib(payload)
    assert len(parts) == 3 and parts[1].endswith(b"7777") and b"GRIBinside" in parts[1]


def _toy_field():
    grid = verify.LambertGrid(200, 200, 30.0, -110.0, -100.0, 38.5, 38.5, 3000.0, 3000.0)
    levels = np.array([30000.0, 31000.0, 32000.0])
    fields = [np.full((200, 200), 0.05) for _ in levels]
    fi, fj = grid.ij(31.0, -109.0)
    i, j = int(round(float(fi))), int(round(float(fj)))
    fields[1][j, i] = 0.30          # turbulence exactly at the report
    fields[2][j + 5, i + 5] = 0.50  # stronger, 15 km away and 1,000 ft higher
    return grid, levels, fields


def test_sampling_point_and_neighbourhood():
    grid, levels, fields = _toy_field()
    obs = pd.DataFrame({"report_id": ["a", "far"], "lat": [31.0, 45.0], "lon": [-109.0, -60.0],
                        "alt_ft": [31000, 31000]})
    out = verify.sample_levels(levels, lambda k: fields[k], grid, obs, nbhd_km=20, nbhd_levels=1).set_index("report_id")
    assert out.loc["a", "edr_point"] == pytest.approx(0.30)
    assert out.loc["a", "edr_nbhd"] == pytest.approx(0.50)
    assert np.isnan(out.loc["far", "edr_point"])        # outside the grid


def test_neighbourhood_respects_vertical_window():
    grid, levels, fields = _toy_field()
    obs = pd.DataFrame({"report_id": ["a"], "lat": [31.0], "lon": [-109.0], "alt_ft": [31000]})
    out = verify.sample_levels(levels, lambda k: fields[k], grid, obs, nbhd_km=20, nbhd_levels=0)
    assert out.loc[0, "edr_nbhd"] == pytest.approx(0.30)


def test_scores_on_known_table():
    s = verify.scores(a=30, b=20, c=10, d=40)
    assert s["pod"] == pytest.approx(0.75) and s["far"] == pytest.approx(0.4)
    assert s["csi"] == pytest.approx(0.5) and s["pofd"] == pytest.approx(1 / 3)
    assert s["bias"] == pytest.approx(1.25) and s["pss"] == pytest.approx(0.75 - 1 / 3)


def test_roc_area_perfect_and_random():
    rng = np.random.default_rng(0)
    obs = rng.random(4000) < 0.2
    perfect = np.where(obs, 0.4, 0.05)
    noise = rng.random(4000)
    thr = np.linspace(0.01, 0.99, 50)
    assert verify.roc(obs, perfect, thr)[1] == pytest.approx(1.0)
    assert verify.roc(obs, noise, thr)[1] == pytest.approx(0.5, abs=0.05)


def _pairs_con():
    df = pd.concat([make_track(), calm_cluster("MOD")], ignore_index=True)
    con = db.load_frame(df)
    pipeline.refresh(con)
    return con


def test_synthetic_pairs_score_and_decay():
    con = db.load_frame(synthetic.generate(hours=24, seed=5, end=pd.Timestamp("2026-01-20")))
    pipeline.refresh(con, with_product=False)
    verify.synthetic_pairs(con)
    t = verify.score_table(verify.pairs_frame(con), ["lag_min"]).set_index("lag_min")
    assert t.loc[0, "pairs"] > 500
    assert t.loc[0, "auc"] > t.loc[120, "auc"] > 0.5        # skill decays with nowcast age
    assert t.loc[0, "pod_lo"] <= t.loc[0, "pod"] <= t.loc[0, "pod_hi"]
    acc = verify.acceptance(verify.pairs_frame(con))
    assert set(acc.columns) == {"fl_band", "criterion", "passed"} and len(acc) > 0


def test_qc_subsets_differ_only_by_rejected_reports():
    con = _pairs_con()
    rid = con.execute("SELECT report_id FROM v_reports WHERE stream = 'pirep' LIMIT 3").df()["report_id"]
    con.register("p", pd.DataFrame({"report_id": rid, "lag_min": 0, "valid_time": pd.NaT,
                                    "edr_point": [0.3, 0.05, 0.05], "edr_nbhd": [0.3, 0.05, 0.05], "source": "t"}))
    con.execute("INSERT INTO verif_pairs SELECT * FROM p")
    con.execute(f"UPDATE report_confidence SET confidence = 0 WHERE report_id = '{rid[1]}'")
    pf = verify.pairs_frame(con)
    all_ = verify.score_table(pf, [], mode=None).iloc[0]
    qc = verify.score_table(pf, [], mode="qc").iloc[0]
    assert (all_["pairs"], qc["pairs"]) == (3, 2)


def test_api_json_maps_to_same_report_as_bulk_csv():
    rec = {"receiptTime": "2026-10-24T15:25:03.104Z", "obsTime": 1792855320, "acType": "E75L",
           "lat": 39.8125, "lon": -104.6607, "fltLvl": 60, "tbInt1": "LGT", "tbType1": "CHOP", "tbInt2": "",
           "pirepType": "PIREP", "rawOb": "DEN UA /OV DEN/TM 1522/FL060/TP E75L/TB LGT CHOP/RM DURD RY34R DEN"}
    df = feed.normalise(verify.json_to_feed_frame([rec]), "bf", pd.Timestamp("2026-10-24 16:00"), config.settings())
    r = df.iloc[0]
    assert r["obs_time"] == pd.Timestamp("2026-10-24 15:22:00") and r["alt_ft"] == 6000
    assert r["stream"] == "pirep" and r["edr_band"] == "LGT" and r["source_id"] == "PIREP"
    # the same report parsed from the bulk CSV gets the same identity, so backfill cannot duplicate it
    bulk = feed.parse(test_feed.TEXT, "bulk", test_feed.NOW).frame
    assert r["report_id"] in set(bulk["report_id"])
