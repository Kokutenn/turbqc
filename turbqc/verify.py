"""Verification of the NWS Graphical Turbulence Guidance Nowcast (GTGN) against observed
turbulence reports: POD, FAR, CSI, Peirce skill and ROC by flight-level band and by
nowcast age (lag), each with bootstrap confidence intervals.

GTGN files are ~28 MB of GRIB2. They are streamed into memory, sampled at the report
positions and discarded, so nothing large is ever written to disk.
"""
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

from . import config, db, feed
from .product import FL_BAND

log = logging.getLogger("turbqc.verify")
FT_PER_M = 1 / 0.3048
MISSING = 9998.0
_trapz = getattr(np, "trapezoid", None) or np.trapz


def _now() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(timezone.utc)).tz_localize(None)


# ------------------------------------------------------------------ GRIB2 handling
def split_grib(payload: bytes) -> list[bytes]:
    """Split a concatenated GRIB file into messages using the length in section 0."""
    out, i = [], 0
    while (j := payload.find(b"GRIB", i)) >= 0:
        edition = payload[j + 7]
        length = (int.from_bytes(payload[j + 8:j + 16], "big") if edition == 2
                  else int.from_bytes(payload[j + 4:j + 7], "big"))
        out.append(payload[j:j + length])
        i = j + length
    return out


@dataclass
class LambertGrid:
    """Lambert conformal conic grid on a sphere (the HRRR/GTGN projection)."""
    nx: int
    ny: int
    lat1: float
    lon1: float
    lov: float
    latin1: float
    latin2: float
    dx: float
    dy: float
    radius: float = 6371229.0

    @classmethod
    def from_handle(cls, h) -> "LambertGrid":
        import eccodes
        g = lambda k: eccodes.codes_get(h, k)
        radius = g("radius") if eccodes.codes_is_defined(h, "radius") else 6371229.0
        return cls(g("Nx"), g("Ny"), g("latitudeOfFirstGridPointInDegrees"),
                   g("longitudeOfFirstGridPointInDegrees"), g("LoVInDegrees"),
                   g("Latin1InDegrees"), g("Latin2InDegrees"), g("DxInMetres"), g("DyInMetres"), radius)

    def _cone(self):
        p1, p2 = np.radians(self.latin1), np.radians(self.latin2)
        if abs(p1 - p2) < 1e-9:
            n = np.sin(p1)
        else:
            n = np.log(np.cos(p1) / np.cos(p2)) / np.log(np.tan(np.pi / 4 + p2 / 2) / np.tan(np.pi / 4 + p1 / 2))
        f = np.cos(p1) * np.tan(np.pi / 4 + p1 / 2) ** n / n
        return n, f

    def _xy(self, lat, lon):
        n, f = self._cone()
        lat, lon = np.radians(np.asarray(lat, float)), np.asarray(lon, float)
        dlon = (lon - self.lov + 180) % 360 - 180
        rho = self.radius * f / np.tan(np.pi / 4 + lat / 2) ** n
        theta = n * np.radians(dlon)
        return rho * np.sin(theta), -rho * np.cos(theta)

    def ij(self, lat, lon) -> tuple[np.ndarray, np.ndarray]:
        """Fractional grid indices (i east, j north) of each point."""
        x0, y0 = self._xy(self.lat1, self.lon1)
        x, y = self._xy(lat, lon)
        return (x - x0) / self.dx, (y - y0) / self.dy


def sample_levels(levels_ft: np.ndarray, get_level, grid: LambertGrid, obs: pd.DataFrame,
                  nbhd_km: float, nbhd_levels: int) -> pd.DataFrame:
    """Point and neighbourhood-max EDR at each observation.

    get_level(idx) returns the 2-D (ny, nx) field of level idx, NaN where missing.
    Only levels that some observation needs are decoded.
    """
    k = np.abs(obs["alt_ft"].to_numpy(float)[:, None] - levels_ft[None, :]).argmin(axis=1)
    fi, fj = grid.ij(obs["lat"].to_numpy(float), obs["lon"].to_numpy(float))
    ii, jj = np.rint(fi).astype(int), np.rint(fj).astype(int)
    inside = (ii >= 0) & (ii < grid.nx) & (jj >= 0) & (jj < grid.ny)
    r = max(int(round(nbhd_km * 1000 / grid.dx)), 0)
    point = np.full(len(obs), np.nan)
    nbhd = np.full(len(obs), np.nan)
    for lev in range(len(levels_ft)):
        sel = np.flatnonzero(inside & (np.abs(k - lev) <= nbhd_levels))
        if not len(sel):
            continue
        field = get_level(lev)
        for s in sel:
            win = field[max(jj[s] - r, 0):jj[s] + r + 1, max(ii[s] - r, 0):ii[s] + r + 1]
            if np.isfinite(win).any():
                nbhd[s] = np.fmax(nbhd[s], np.nanmax(win))
            if k[s] == lev:
                point[s] = field[jj[s], ii[s]]
    return pd.DataFrame({"report_id": obs["report_id"].to_numpy(), "edr_point": point, "edr_nbhd": nbhd})


def sample_gtgn(payload: bytes, obs: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    import eccodes
    msgs = []
    for m in split_grib(payload):
        h = eccodes.codes_new_from_message(m)
        msgs.append((round(eccodes.codes_get(h, "level") * FT_PER_M / 100) * 100, m))
        if len(msgs) == 1:
            grid = LambertGrid.from_handle(h)
        eccodes.codes_release(h)
    msgs.sort(key=lambda t: t[0])
    levels_ft = np.array([lv for lv, _ in msgs], float)

    def get_level(idx):
        h = eccodes.codes_new_from_message(msgs[idx][1])
        vals = eccodes.codes_get_values(h).reshape(grid.ny, grid.nx)
        eccodes.codes_release(h)
        return np.where(vals >= MISSING, np.nan, vals)

    v = cfg["verification"]
    return sample_levels(levels_ft, get_level, grid, obs, v["neighbourhood_km"], v["neighbourhood_levels"])


# ------------------------------------------------------------------ collection
def gtgn_url(t: pd.Timestamp, cfg: dict) -> str:
    return cfg["verification"]["gtgn_url"].format(t=t)


def fetch_gtgn(t: pd.Timestamp, cfg: dict) -> tuple[int, bytes | None]:
    r = requests.get(gtgn_url(t, cfg), timeout=120, headers={"User-Agent": cfg["feed"]["user_agent"]})
    return r.status_code, (r.content if r.status_code == 200 else None)


def candidates(con, hours: float, cfg: dict, now: pd.Timestamp | None = None) -> pd.DataFrame:
    """Turbulence reports (any QC outcome) still lacking a GTGN match for some lag."""
    v = cfg["verification"]
    now = now or _now()
    obs = con.execute(f"""
        SELECT report_id, lat, lon, alt_ft, obs_time FROM v_reports
        WHERE severity IS NOT NULL AND lat BETWEEN 15 AND 60 AND lon BETWEEN -140 AND -55
          AND alt_ft IS NOT NULL AND obs_time >= TIMESTAMP '{now - pd.Timedelta(hours=hours)}'""").df()
    if obs.empty:
        return obs
    slot = pd.Timedelta(minutes=v["slot_minutes"])
    obs["slot"] = (obs["obs_time"] + slot / 2).dt.floor(f"{v['slot_minutes']}min")
    done = con.execute("SELECT report_id, lag_min FROM verif_pairs").df()
    done_keys = set(zip(done["report_id"], done["lag_min"]))
    rows = []
    for lag in v["lags_min"]:
        c = obs.assign(lag_min=lag, valid_time=obs["slot"] - pd.Timedelta(minutes=lag))
        rows.append(c[[(r, lag) not in done_keys for r in c["report_id"]]])
    out = pd.concat(rows, ignore_index=True)
    # NOMADS keeps today's and yesterday's directories only
    oldest = now.floor("D") - pd.Timedelta(days=1)
    return out[(out["valid_time"] >= oldest) & (out["valid_time"] <= now - pd.Timedelta(minutes=5))]


def run(con, hours: float = 48, cfg: dict | None = None, max_files: int | None = None) -> dict:
    """Fetch every GTGN analysis needed to match recent reports and store the pairs."""
    cfg = cfg or config.settings()
    cand = candidates(con, hours, cfg)
    stats = dict(files=0, pairs=0, unavailable=0)
    for valid, grp in sorted(cand.groupby("valid_time"), key=lambda t: t[0], reverse=True):
        if max_files is not None and stats["files"] >= max_files:
            break
        t0 = time.perf_counter()
        try:
            status, payload = fetch_gtgn(valid, cfg)
        except requests.RequestException as e:
            status, payload = None, None
            msg = str(e)[:300]
        else:
            msg = None
        if payload is None:
            stats["unavailable"] += 1
            _log_file(con, valid, f"http_{status}", 0, time.perf_counter() - t0, 0, msg)
            continue
        obs = grp.drop_duplicates("report_id")
        sampled = sample_gtgn(payload, obs, cfg)
        pairs = grp[["report_id", "lag_min", "valid_time"]].merge(sampled, on="report_id")
        pairs["source"] = "gtgn"
        con.register("pairs_df", pairs)
        con.execute("""INSERT OR IGNORE INTO verif_pairs
                       SELECT report_id, lag_min, valid_time, edr_point, edr_nbhd, source FROM pairs_df""")
        con.unregister("pairs_df")
        stats["files"] += 1
        stats["pairs"] += len(pairs)
        _log_file(con, valid, "ok", len(payload), time.perf_counter() - t0, len(pairs), None)
        log.info("GTGN %s: %d pairs", valid, len(pairs))
        del payload
    return stats


def _log_file(con, valid, status, nbytes, seconds, pairs, message):
    con.execute("INSERT INTO verif_files VALUES (?, ?, ?, ?, ?, ?, ?)",
                [valid, _now(), status, nbytes, round(seconds, 2), pairs, message])


# ------------------------------------------------------------------ PIREP backfill
JSON_TO_FEED = {"receiptTime": "receipt_time", "acType": "aircraft_ref", "pirepType": "report_type",
                "rawOb": "raw_text", "tbInt1": "turbulence_intensity", "tbInt2": "turbulence_intensity_2",
                "tbType1": "turbulence_type", "tbFreq1": "turbulence_freq", "temp": "temp_c",
                "wdir": "wind_dir_degrees", "wspd": "wind_speed_kt", "vertGust": "vert_gust_kt"}


def json_to_feed_frame(records: list[dict]) -> pd.DataFrame:
    """Data API JSON records -> the column layout of the bulk CSV, so one normaliser serves both."""
    rows = []
    for r in records:
        row = {dst: (None if r.get(src) in (None, "") else str(r[src])) for src, dst in JSON_TO_FEED.items()}
        obs = datetime.fromtimestamp(r["obsTime"], timezone.utc) if r.get("obsTime") is not None else None
        row["observation_time"] = obs.strftime("%Y-%m-%dT%H:%M:%S.000Z") if obs else None
        row["latitude"] = None if r.get("lat") is None else f"{r['lat']:.4f}"
        row["longitude"] = None if r.get("lon") is None else f"{r['lon']:.4f}"
        row["altitude_ft_msl"] = None if r.get("fltLvl") is None else str(int(r["fltLvl"]) * 100)
        for src, dst in (("tbBas1", "turbulence_base_ft_msl"), ("tbTop1", "turbulence_top_ft_msl")):
            row[dst] = None if r.get(src) is None else str(int(r[src]) * 100)
        rows.append(row)
    return pd.DataFrame(rows)


def backfill_pireps(con, hours: int = 48, cfg: dict | None = None) -> dict:
    """Pull past reports hour by hour from the Data API (the bulk file only holds ~90 min)."""
    cfg = cfg or config.settings()
    v = cfg["verification"]
    now = _now()
    end = now.floor("h")
    records = []
    for h in range(hours):
        t = end - pd.Timedelta(hours=h)
        r = requests.get(v["pirep_api"], timeout=60, headers={"User-Agent": cfg["feed"]["user_agent"]},
                         params={"format": "json", "bbox": v["backfill_bbox"], "age": 1,
                                 "date": t.strftime("%Y%m%d_%H%M")})
        if r.status_code == 200:
            records += r.json()
        time.sleep(0.7)  # stay far below the 100 requests/minute limit
    run_id = f"{now:%Y%m%dT%H%M%S}-backfill"
    df = feed.normalise(json_to_feed_frame(records), run_id, now, cfg) if records else pd.DataFrame()
    new = 0
    if not df.empty:
        # The same report may already have arrived through the bulk file: match on content.
        con.register("bf", df)
        df = con.execute("""
            SELECT b.* FROM bf b WHERE NOT EXISTS (
                SELECT 1 FROM reports r WHERE r.obs_time = b.obs_time AND r.aircraft_ref = b.aircraft_ref
                  AND trim(r.raw_text) = trim(b.raw_text))""").df()
        con.unregister("bf")
        new = db.insert_reports(con, df)
    con.execute("""INSERT INTO ingest_runs (run_id, started_at, finished_at, source_url, rows_in_file,
                   rows_new, rows_already_seen, status, message) VALUES (?, ?, ?, ?, ?, ?, ?, 'backfill', ?)""",
                [run_id, now, _now(), v["pirep_api"], len(records), new, len(records) - new, f"{hours} h"])
    return dict(records=len(records), new=new)


# ------------------------------------------------------------------ scores
def pairs_frame(con, cfg: dict | None = None) -> pd.DataFrame:
    cfg = cfg or config.settings()
    df = con.execute(f"""
        SELECT p.*, v.obs_time, v.alt_ft, v.stream, v.severity, v.tb_intensity, v.lat, v.lon,
               {FL_BAND} AS fl_band, coalesce(c.confidence, 1.0) AS confidence
        FROM verif_pairs p JOIN v_reports v USING (report_id)
        LEFT JOIN report_confidence c USING (report_id)""").df()
    df["obs_event"] = df["severity"] >= cfg["verification"]["event_severity"]
    return df


def contingency(obs_event, fc_event, weight=None) -> tuple[float, float, float, float]:
    o, f = np.asarray(obs_event, bool), np.asarray(fc_event, bool)
    w = np.ones(len(o)) if weight is None else np.asarray(weight, float)
    return (float(w[f & o].sum()), float(w[f & ~o].sum()), float(w[~f & o].sum()), float(w[~f & ~o].sum()))


def scores(a, b, c, d) -> dict:
    div = lambda x, y: x / y if y else np.nan
    pod, pofd = div(a, a + c), div(b, b + d)
    return dict(pod=pod, far=div(b, a + b), csi=div(a, a + b + c), pofd=pofd,
                bias=div(a + b, a + c), pss=pod - pofd if not (np.isnan(pod) or np.isnan(pofd)) else np.nan)


def roc(obs_event, edr, thresholds, weight=None) -> tuple[pd.DataFrame, float]:
    pts = []
    for t in thresholds:
        s = scores(*contingency(obs_event, edr >= t, weight))
        pts.append(dict(threshold=t, pod=s["pod"], pofd=s["pofd"], csi=s["csi"], pss=s["pss"]))
    curve = pd.DataFrame(pts)
    xy = pd.concat([pd.DataFrame({"pofd": [0.0, 1.0], "pod": [0.0, 1.0]}), curve[["pofd", "pod"]]]) \
        .dropna().sort_values(["pofd", "pod"])
    auc = float(_trapz(xy["pod"], xy["pofd"])) if len(xy) > 2 else np.nan
    return curve, auc


SUBSETS = {"all reports": None, "QC-passed": "qc", "confidence-weighted": "weighted"}


def subset(df, mode):
    if mode == "qc":
        return df[df["confidence"] > 0], None
    if mode == "weighted":
        return df, df["confidence"].to_numpy()
    return df, None


def score_table(df: pd.DataFrame, by: list[str], field: str = "edr_nbhd", mode: str | None = "qc",
                threshold: float | None = None, cfg: dict | None = None, seed: int = 1) -> pd.DataFrame:
    """Scores per group with 90% bootstrap intervals for POD, FAR and CSI, plus ROC area."""
    cfg = cfg or config.settings()
    v = cfg["verification"]
    threshold = v["edr_threshold"] if threshold is None else threshold
    df = df[df[field].notna()]
    rng = np.random.default_rng(seed)
    rows = []
    for key, g in df.groupby(by) if by else [((), df)]:
        g, w = subset(g, mode)
        if g.empty:
            continue
        o, e = g["obs_event"].to_numpy(), g[field].to_numpy()
        a, b, c, d = contingency(o, e >= threshold, w)
        row = dict(zip(by, key if isinstance(key, tuple) else (key,)))
        row.update(pairs=len(g), events=int(o.sum()), hits=a, false_alarms=b, misses=c, correct_neg=d,
                   **scores(a, b, c, d))
        boot = []
        for _ in range(v["bootstrap"]):
            idx = rng.integers(0, len(g), len(g))
            boot.append(scores(*contingency(o[idx], e[idx] >= threshold, None if w is None else w[idx])))
        bt = pd.DataFrame(boot)
        for m in ("pod", "far", "csi"):
            row[f"{m}_lo"], row[f"{m}_hi"] = bt[m].quantile(0.05), bt[m].quantile(0.95)
        row["auc"] = roc(o, e, v["sweep"], w)[1]
        rows.append(row)
    return pd.DataFrame(rows)


def acceptance(df: pd.DataFrame, cfg: dict | None = None) -> pd.DataFrame:
    """Evaluate the (illustrative) nowcast acceptance criteria per flight-level band."""
    cfg = cfg or config.settings()
    acc = cfg["verification"]["acceptance"]
    t = score_table(df, ["fl_band", "lag_min"], cfg=cfg)
    out = []
    for band, g in t.groupby("fl_band"):
        g = g.set_index("lag_min")
        if 0 not in g.index:
            continue
        r0 = g.loc[0]
        drop = (1 - g.loc[60, "csi"] / r0["csi"]) if 60 in g.index and r0["csi"] else np.nan
        checks = {
            f"pairs >= {acc['min_pairs']}": r0["pairs"] >= acc["min_pairs"],
            f"POD >= {acc['pod_min']}": r0["pod"] >= acc["pod_min"],
            f"FAR <= {acc['far_max']}": r0["far"] <= acc["far_max"],
            f"ROC area >= {acc['auc_min']}": r0["auc"] >= acc["auc_min"],
            f"CSI drop at 60 min <= {acc['max_relative_csi_drop_60min']:.0%}":
                bool(drop <= acc["max_relative_csi_drop_60min"]) if not np.isnan(drop) else False,
        }
        for name, ok in checks.items():
            out.append(dict(fl_band=band, criterion=name, passed=bool(ok)))
    return pd.DataFrame(out)


# ------------------------------------------------------------------ demo
def synthetic_pairs(con, seed: int = 3, cfg: dict | None = None) -> int:
    """Demo only: a made-up forecast with skill that decays with lag, so the verification
    views can be explored offline. Stored with source='synthetic' and labelled as such."""
    cfg = cfg or config.settings()
    rng = np.random.default_rng(seed)
    obs = con.execute("""SELECT report_id, severity FROM v_reports
                         WHERE severity IS NOT NULL AND stream = 'pirep'""").df()
    rows = []
    for lag in cfg["verification"]["lags_min"]:
        noise = rng.normal(0, 0.035 + lag * 0.0006, len(obs))
        point = np.clip(0.07 + 0.065 * obs["severity"] + noise, 0, 0.7)
        rows.append(pd.DataFrame({"report_id": obs["report_id"], "lag_min": lag, "valid_time": pd.NaT,
                                  "edr_point": point, "edr_nbhd": point + np.abs(rng.normal(0, 0.02, len(obs))),
                                  "source": "synthetic"}))
    pairs = pd.concat(rows, ignore_index=True)
    con.execute("DELETE FROM verif_pairs")
    con.register("pairs_df", pairs)
    con.execute("INSERT INTO verif_pairs SELECT * FROM pairs_df")
    con.unregister("pairs_df")
    return len(pairs)
