from datetime import datetime, timezone

from fastapi.testclient import TestClient

from eddn_validator import web
from eddn_validator.web import breakable

client = TestClient(web.app)


def test_security_headers_and_health():
    r = client.get("/health")
    assert r.status_code == 200 and r.text == "ok"
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-content-type-options"] == "nosniff"


def test_only_short_versions_reach_the_database(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("database must not be queried")
    monkeypatch.setattr(web, "load_card", boom)
    assert client.get("/card/1?v=" + "x" * 65).status_code == 404
    assert client.get("/card/abc?v=1.0").status_code == 404


def test_old_range_addresses_redirect_to_the_version_card():
    r = client.get("/card/5/30?v=0.1 beta", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/card/5?v=0.1%20beta"
    assert client.get("/card/5/7?v=" + "x" * 65, follow_redirects=False).status_code == 404


def test_unknown_card_is_404(monkeypatch):
    monkeypatch.setattr(web, "load_card", lambda *a: None)
    r = client.get("/card/1?v=1.0")
    assert r.status_code == 404 and "Not found" in r.text


def test_breakable_escapes_before_adding_breaks():
    out = str(breakable("journal/1:<script>"))
    assert "<script>" not in out and "&lt;script&gt;" in out and "/<wbr>" in out


def test_front_page_lists_new_apps(monkeypatch):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(web, "load_apps", lambda: [])
    monkeypatch.setattr(web, "load_new_apps", lambda: {"data_since": datetime(2026, 4, 16, tzinfo=timezone.utc),
                                                       "since": datetime(2026, 9, 14, 5, 0, tzinfo=timezone.utc), "apps": [
        {"id": 5, "name": "<b>Tool</b>", "first_seen": now, "last_seen": now, "versions": 1,
         "messages": 12, "latest": "0.1 beta", "newest": "0.2", "has_card": True, "grade": "B", "score": 85.0},
        {"id": 6, "name": "Fresh", "first_seen": now, "last_seen": now, "versions": 1,
         "messages": 2, "latest": "1.0", "newest": "1.0", "has_card": False, "grade": None, "score": None}]})
    web.cache._data.clear(); web.card_cache._data.clear()
    r = client.get("/")
    assert r.status_code == 200
    assert "&lt;b&gt;Tool&lt;/b&gt;" in r.text and "<b>Tool</b>" not in r.text
    assert "/card/5?v=0.1%20beta" in r.text and "scorecard pending" in r.text
    assert "First message on or after 2026-09-14, not counting" in r.text and "(Oldest records in database: 2026-04-16)" in r.text
    assert '<span class="ver">0.1 beta</span>' in r.text and '<span class="ver">1.0</span>' in r.text
    assert "B · 85.0</span>" in r.text
    assert "newest 0.2 not graded yet" in r.text and "newest 1.0 not graded" not in r.text


def test_nul_in_version_is_404_without_database(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("database must not be queried")
    monkeypatch.setattr(web, "load_card", boom)
    assert client.get("/card/1?v=a%00b").status_code == 404


def test_all_apps_page(monkeypatch):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(web, "load_all_apps", lambda: [
        {"id": 1, "name": "Alpha", "ref": None, "last_seen": now, "version": "1.0",
         "grade": "B", "score": 84.66, "recent": True, "current": "1.1",
         "current_on_file": True},
        {"id": 2, "name": "<i>Beta</i>", "ref": "EDMC", "last_seen": now, "version": None,
         "grade": None, "score": None, "recent": True, "current": None}])
    web.cache._data.clear(); web.card_cache._data.clear()
    r = client.get("/apps")
    assert r.status_code == 200
    assert "/card/1?v=1.0" in r.text and "&lt;i&gt;Beta&lt;/i&gt;" in r.text
    assert "B · 84.7" in r.text
    assert '<span class="ver">1.0</span>' in r.text and "newest 1.1 not graded yet" in r.text


CARD = {
    "app": "Tool", "app_id": 5, "version": "1.0", "range_days": 3650, "all_messages": True,
    "oldest": "2026-09-01T00:00:00+00:00", "is_reference": False, "ref_group": None,
    "computed_at": "2026-10-14T00:00:00+00:00", "messages": 120, "score": 88.5, "grade": "B",
    "weights": {"rules": 30, "structure": 20, "values": 20, "pairing": 20, "behaviour": 10},
    "reference_versions": {"EDMC": ["6.1.2"]},
    "gameversions": {"classes": {"Live 4.x": 100, "absent": 20}, "values": {"4.4.1.1": 100}},
    "branches": {"live": 120}, "schemas": {"live": 120}, "unrecognised": [],
    "not_sent": [{"event_type": "navroute/1", "reference": {"EDMC": 50}}],
    "rows": [{
        "event_type": "journal/1:FSDJump", "branch": "live", "messages": 120, "sampled": 120,
        "confidence": "ok", "recognised": True, "score": 88.5,
        "layers": {"rules": 80.0, "structure": 100.0, "values": 95.0, "pairing": 90.0, "behaviour": None},
        "rules": [{"id": "GEN-033", "severity": "error", "title": "name vs address", "source": "Dev",
                   "count": 3, "rate": 0.025, "detail": "<b>x</b>", "reference": {"EDMC": 0.0}, "examples": []}],
        "fields": {"subtypes": [{"name": "populated", "messages": 120, "compared_with": "populated"}],
                   "missing_expected": [], "unseen": [], "differing": []},
        "values": [{"path": "StarPos[]", "kind": "type", "severity": "error", "count": 1, "rate": 0.01,
                    "app": [["str"]], "reference": ["number"]}],
        "pairing": {"kinds": {"system": {"messages": 100, "mismatched_messages": 4,
                                         "fields_compared": 200, "fields_mismatched": 4}},
                    "fields": {"StarPos": 4}, "examples": []},
        "behaviour": {"messages": 120, "duplicate": 0.0, "stale": 0.0, "future": 0.0, "burst": 0.0,
                      "resend": 0.0, "baseline": {}, "excess": {"duplicate": 0, "stale": 0, "future": 0, "burst": 0}},
    }],
}


def test_card_has_hover_breakdowns(monkeypatch):
    import copy
    monkeypatch.setattr(web, "load_card", lambda *a: copy.deepcopy(CARD))
    seen = datetime(2026, 10, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(web, "load_versions", lambda *a: [
        {"version": "1.1", "first_seen": seen, "last_seen": seen, "messages": 12, "has_card": True,
         "grade": "B", "score": 84.25},
        {"version": "1.0", "first_seen": seen, "last_seen": seen, "messages": 900, "has_card": True,
         "grade": "A", "score": 91.0},
        {"version": "0.9<b>", "first_seen": seen, "last_seen": seen, "messages": 5, "has_card": False,
         "grade": None, "score": None}])
    web.cache._data.clear(); web.card_cache._data.clear()
    r = client.get("/card/5?v=1.0")
    assert r.status_code == 200
    versions = r.text[r.text.index(">Versions<"):]
    assert r.text.index(">Basis<") < r.text.index(">Versions<")      # at the bottom
    assert '<a href="/card/5?v=1.1">1.1</a>' in versions and "84.2" in versions
    assert "2026-10-01</td>" in versions and "UTC" not in versions   # dates only
    assert "all messages on file (since 2026-09-01)" in r.text
    assert "<strong>1.0</strong>" in versions and "(this card)" in versions
    assert "no scorecard" in versions and "0.9&lt;b&gt;" in versions
    assert r.text.count("data-tip=") > 40
    assert "GEN-033 error 2.5%" in r.text                       # rules breakdown
    assert "System name and position: 100 matched, 4 disagree" in r.text   # pairing breakdown
    assert "Rules 80 × 30%" in r.text                           # score composition
    assert "<b>x</b>" not in r.text                              # sender text still escaped


def test_about_layer_pages():
    for layer in ("rules", "fields", "values", "pairing", "behaviour"):
        r = client.get(f"/about/{layer}")
        assert r.status_code == 200, layer
    rules = client.get("/about/rules").text
    assert "GEN-033" in rules and "Switched off" in rules and "Also checked" not in rules
    assert "length of year" in client.get("/about/pairing").text
    assert client.get("/about/nope").status_code == 404


def test_logo_and_favicon_are_served():
    logo = client.get("/static/logo.svg")
    assert logo.status_code == 200 and logo.headers["content-type"].startswith("image/svg+xml")
    icon = client.get("/favicon.ico")
    assert icon.status_code == 200 and icon.content[:8] == b"\x89PNG\r\n\x1a\n"
    page = client.get("/about")
    assert 'href="/static/logo.svg"' in page.text and 'src="/static/logo.svg"' in page.text


def test_rule_sources_link_to_eddn_documentation():
    from eddn_validator.web import source_links
    out = str(source_links("Dev:255-257, 273-275"))
    assert out.count("<a ") == 2
    assert f"/blob/{web.EDDN_LIVE}/docs/Developers.md?plain=1#L255-L257" in out and ">Dev:255-257</a>" in out
    assert "#L273-L275" in out and ">273-275</a>" in out
    out = str(source_links("outfitting-README:50"))
    assert "schemas/outfitting-README.md?plain=1#L50\"" in out and "line 50" in out
    assert web.EDDN_DEVELOP in str(source_links("scanorganic-README:60-62"))
    out = str(source_links('implied (mirrors codexentry-README:80-81) <b>'))
    assert out.startswith("implied (mirrors <a ") and "&lt;b&gt;" in out
    assert str(source_links("game data quirk")) == "game data quirk"


def test_versions_sort_by_number_not_activity():
    from datetime import datetime, timezone
    t = datetime(2026, 10, 1, tzinfo=timezone.utc)
    vs = [{"version": v, "first_seen": t} for v in
          ("2026.10.21", "2026.10.22", "2026.10.19.1", "2026.10.19", "5.4.3 beta 1", "5.4.3",
           "5.4.2", "5.10.0", "3.3.0.0", "dev-build", "v0.9")]
    order = [v["version"] for v in web.newest_first(vs)]
    assert order[:4] == ["2026.10.22", "2026.10.21", "2026.10.19.1", "2026.10.19"]
    assert order.index("5.10.0") < order.index("5.4.3") < order.index("5.4.3 beta 1") < order.index("5.4.2")
    assert order[-2:] == ["v0.9", "dev-build"]
    assert web.version_key("3.3.0.0") == web.version_key("3.3.0")


def test_version_suffixes_rank_sensibly():
    from datetime import datetime, timezone
    early, late = datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 10, 1, tzinfo=timezone.utc)
    order = [v["version"] for v in web.newest_first([
        {"version": "2.0", "first_seen": early}, {"version": "2.0 March", "first_seen": late}])]
    assert order == ["2.0 March", "2.0"]          # not a pre-release ("rc" inside a word)
    order = [v["version"] for v in web.newest_first([
        {"version": "1.2.3", "first_seen": early}, {"version": "1.2.3b4", "first_seen": late},
        {"version": "5.4.3 alpha 2", "first_seen": early}, {"version": "5.4.3 beta 1", "first_seen": early}])]
    assert order.index("1.2.3") < order.index("1.2.3b4")
    assert order.index("5.4.3 beta 1") < order.index("5.4.3 alpha 2")


def test_malformed_card_addresses_get_the_404_page():
    r = client.get("/card/5")
    assert r.status_code == 404 and "Not found" in r.text


def test_lists_show_the_newest_graded_version():
    from datetime import datetime, timezone
    t = datetime(2026, 10, 1, tzinfo=timezone.utc)
    def v(version, grade, has_card=True):
        return {"version": version, "first_seen": t, "has_card": has_card, "grade": grade, "score": 90.0 if grade else None}
    assert web.shown_version([v("1.2", None), v("1.1", "A"), v("1.0", "B")])["version"] == "1.1"
    assert web.shown_version([v("1.2", None), v("1.1", None)])["version"] == "1.2"
    assert web.shown_version([v("1.2", None, False)])["version"] == "1.2"
    assert web.shown_version([]) is None


def test_build_tag_letters_are_not_prereleases():
    assert web.version_key("1.2.3 build 7b") == web.version_key("1.2.3")
    assert web.version_key("1.2.3b4") < web.version_key("1.2.3")
    assert web.version_key("1.2.3 b 4") < web.version_key("1.2.3")


def test_rules_page_links_rule_ids_to_eddn_docs():
    page = client.get("/about/rules").text
    assert 'href="https://github.com/EDCD/EDDN/blob/' in page and "<code>FCJ-001</code></a>" in page
    assert web.source_link("implied") is None
    link = web.source_link("fcmaterials_journal-README:19-22")
    assert link["url"].endswith("schemas/fcmaterials_journal-README.md?plain=1#L19-L22")


def test_technical_reference_lists_every_rule():
    from eddn_validator.config import Settings
    page = client.get("/about/technical")
    assert page.status_code == 200
    rules = Settings.from_env().load_yaml("rules.yaml")["rules"]
    for rule_id in rules:
        assert f"<code>{rule_id}</code>" in page.text, rule_id
    assert "Switched off" in page.text and "COM-005" in page.text
    assert "w = 0.5" in page.text and "duplicate_window_hours" in page.text
