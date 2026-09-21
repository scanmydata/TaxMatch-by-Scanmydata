from taxmatch.ingestion import rss_fetch, sources
from tests.fakes import FakeResponse, FakeSession

TAXHEAVEN_CAL = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Taxheaven - Ημερολόγιο</title>
<item><title>Υποβολή δήλωσης ΦΠΑ μηνός Αυγούστου</title>
<description><![CDATA[<p align="justify"><b>Υποβολή δήλωσης...]]></description>
<pubDate>Wed, 30 Sep 2026 00:00:00 +0300</pubDate>
<guid isPermaLink="true">https://www.taxheaven.gr/calendar/event/11500</guid>
<link>https://www.taxheaven.gr/calendar/event/11500</link></item>
</channel></rss>"""

EFOROLOGIA = """﻿<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title><![CDATA[e-forologia.gr - Τρέχοντα Φορολογικά]]></title>
<item><title><![CDATA[Νέα εγκύκλιος για το myDATA]]></title><description><![CDATA[Νέα εγκύκλιος για το myDATA]]></description>
<link><![CDATA[https://www.e-forologia.gr/lawbank/document.aspx?Digest=ABC123]]></link>
<guid>https://www.e-forologia.gr/lawbank/document.aspx?Digest=ABC123</guid>
<pubDate>Fri, 18 Sep 2026 07:27:00 GMT</pubDate><category><![CDATA[ΦΠΑ]]></category></item></channel></rss>"""


def test_calendar_date_is_not_shifted_by_timezone():
    """00:00 +03:00 σε UTC θα ήταν η προηγούμενη μέρα — η λήξη πρέπει να μείνει 30/9."""
    items = rss_fetch.parse_feed(TAXHEAVEN_CAL.encode(), calendar=True)
    assert items[0].due_date == "2026-09-30"
    assert items[0].title.startswith("Υποβολή δήλωσης ΦΠΑ")


def test_parse_eforologia_bom_and_cdata_link():
    items = rss_fetch.parse_feed(EFOROLOGIA.encode("utf-8"))
    assert len(items) == 1
    assert items[0].url == "https://www.e-forologia.gr/lawbank/document.aspx?Digest=ABC123"
    assert items[0].category == "ΦΠΑ"
    assert items[0].published_at == "2026-09-18T07:27:00Z"


def test_store_articles_dedups_by_url(conn):
    items = rss_fetch.parse_feed(EFOROLOGIA.encode("utf-8"))
    assert rss_fetch.store_articles(conn, "eforologia_7", items) == 1
    assert rss_fetch.store_articles(conn, "eforologia_7", items) == 0
    assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 1


def test_url_hash_ignores_trailing_slash_fragment_and_host_case():
    a = rss_fetch.url_hash("https://WWW.taxheaven.gr/circulars/1/x/#top")
    b = rss_fetch.url_hash("https://www.taxheaven.gr/circulars/1/x")
    assert a == b


def test_obligations_upsert_updates_changed_date(conn):
    it = rss_fetch.parse_feed(TAXHEAVEN_CAL.encode(), calendar=True)
    assert rss_fetch.store_obligations(conn, it) == 1
    it[0].due_date = "2026-10-05"                     # παράταση
    assert rss_fetch.store_obligations(conn, it) == 0
    rows = conn.execute("SELECT due_date FROM obligations_general").fetchall()
    assert [r[0] for r in rows] == ["2026-10-05"]


def test_one_failing_source_does_not_stop_others(conn):
    s = FakeSession()
    s.route("soft_dat", FakeResponse(TAXHEAVEN_CAL))
    s.route("rss_id7", FakeResponse(b"", 500))
    srcs = [sources.BY_ID["eforologia_7"], sources.BY_ID["taxheaven_dat"]]
    stats = rss_fetch.ingest(conn, srcs, s)
    assert "error" in stats["eforologia_7"]
    assert stats["taxheaven_dat"]["new"] == 1
