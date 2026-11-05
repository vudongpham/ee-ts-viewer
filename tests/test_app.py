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


def test_empty_collection_is_not_an_error(monkeypatch):
    import ee

    class FakeCol:
        def getRegion(self, *a, **k):
            return self

    def raise_empty(obj, retries=4):
        raise ee.EEException("ImageCollection.getRegion: No bands in collection.")

    monkeypatch.setattr(eeapi.ee.Geometry, "Point", lambda *a, **k: None)
    monkeypatch.setattr(eeapi, "build_collection", lambda *a, **k: FakeCol())
    monkeypatch.setattr(eeapi, "_get_info", raise_empty)
    assert eeapi.fetch_year("SEN2C", 13.4, 52.5, 2024) == []


def test_groups_cannot_be_mixed():
    client = appmod.app.test_client()
    r = client.post("/sendData", json={"lat": 52.5, "lng": 13.4, "startDate": "2020-01-01",
                                       "endDate": "2020-02-01", "sensorList": ["LND08", "HLSL30"]})
    assert r.status_code == 400
    page = client.get("/").get_data(as_text=True)
    assert "Individual Sensors" in page and "Harmonized Sensors" in page and 'value="HLSL30"' in page and 'value="HLSS30"' in page
