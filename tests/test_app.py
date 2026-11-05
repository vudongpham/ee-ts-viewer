import time

from eetsviewer import app as appmod
from eetsviewer import earthengine as eeapi


def fake_fetch_year(sensor, lng, lat, year):
    if sensor == "LND07":
        raise RuntimeError("boom")
    return [{"id": f"{sensor}_{year}", "day": (year - 1970) * 365 + 100,
             "values": {"QA_PIXEL": 1 << 6, "SCL": 4, "cs_cdf": 0.9,
                        "NIR": 21818 if sensor.startswith("LND") else 4000,
                        "RED": 10909 if sensor.startswith("LND") else 1000}}]


def test_job_flow(monkeypatch):
    monkeypatch.setattr(eeapi, "fetch_year", fake_fetch_year)
    eeapi.year_cache = eeapi.YearCache()
    client = appmod.app.test_client()

    assert client.get("/").status_code == 200
    r = client.post("/sendData", json={
        "lat": 52.5, "lng": 13.4, "startDate": "2022-01-01", "endDate": "2023-12-31",
        "sensorList": ["LND07", "LND08", "SEN2A"], "band": "NDVI", "cloudMask": 1,
    }).get_json()
    assert r["total"] == 6
    for _ in range(100):
        snap = client.get(f"/status/{r['job']}").get_json()
        if snap["status"] != "running":
            break
        time.sleep(0.02)
    assert snap["status"] == "done"
    assert snap["failed"] == 2 and "boom" in snap["error"]
    assert snap["valid"] == 4
    assert {p[1] for p in snap["points"]} == {6000}


def test_bad_request():
    client = appmod.app.test_client()
    r = client.post("/sendData", json={"lat": 100, "lng": 0, "startDate": "2020-01-01",
                                       "endDate": "2020-02-01"})
    assert r.status_code == 400
    assert client.get("/tiles?sensor=LND08&id=../x").status_code == 400
