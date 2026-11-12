"""Unit tests: URL normalization, robots subset, adapters, content digests.

Run: ``python -m unittest discover -s tests -v`` from the project root.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from archive_pipeline.engine import business_digest, paragraph_diff
from archive_pipeline.htmlparse import ADAPTERS, parse_html
from archive_pipeline.robots import RobotsPolicy
from archive_pipeline.urlnorm import InvalidURL, normalize_url

TRACKING = frozenset({"utm_source", "utm_medium", "utm_campaign", "gclid", "ref"})


class UrlNormalizationTests(unittest.TestCase):
    def test_fragment_and_tracking_removed(self):
        raw = ("http://127.0.0.1:8811/notices/a/A-1?utm_source=x&view=full"
               "&gclid=abc#frag")
        self.assertEqual(
            normalize_url(raw, TRACKING),
            "http://127.0.0.1:8811/notices/a/A-1?view=full")

    def test_query_order_irrelevant(self):
        a = normalize_url("http://h/x?b=2&a=1", TRACKING)
        b = normalize_url("http://h/x?a=1&b=2", TRACKING)
        self.assertEqual(a, b)

    def test_business_parameters_not_merged(self):
        plain = normalize_url("http://h/x?a=1", TRACKING)
        edition = normalize_url("http://h/x?a=1&edition=public", TRACKING)
        self.assertNotEqual(plain, edition)

    def test_tracking_keys_case_insensitive_and_value_preserved(self):
        self.assertEqual(
            normalize_url("http://h/x?UTM_Source=a&z=9", TRACKING),
            "http://h/x?z=9")

    def test_empty_query_and_trailing_slash(self):
        self.assertEqual(normalize_url("http://h/x?utm_source=1", TRACKING),
                         "http://h/x")
        self.assertEqual(normalize_url("http://h/x/", TRACKING), "http://h/x")

    def test_default_port_removed_non_default_kept(self):
        self.assertEqual(normalize_url("http://h:80/x", TRACKING), "http://h/x")
        self.assertEqual(normalize_url("http://h:8811/x", TRACKING),
                         "http://h:8811/x")

    def test_rejects_non_http_and_userinfo(self):
        for bad in ("ftp://h/x", "file:///x", "http://user:pw@h/x", "/relative"):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidURL):
                    normalize_url(bad, TRACKING)


class RobotsTests(unittest.TestCase):
    UA = "archive-bot/0.1 (+research-archive)"

    def test_specific_group_overrides_wildcard(self):
        text = (
            "User-agent: *\nDisallow: /\n\n"
            "User-agent: archive-bot\nDisallow: /internal/\nAllow: /n/\n"
        )
        policy = RobotsPolicy.from_text(text)
        self.assertTrue(policy.allowed("http://h/n/x", self.UA))
        self.assertFalse(policy.allowed("http://h/internal/x", self.UA))
        self.assertTrue(policy.allowed("http://h/feed", self.UA))

    def test_unlisted_wildcard_agent_still_uses_star(self):
        policy = RobotsPolicy.from_text("User-agent: other\nDisallow: /\n")
        self.assertTrue(policy.allowed("http://h/anything", self.UA))

    def test_longest_match_wins_and_allow_tiebreaks(self):
        policy = RobotsPolicy.from_text(
            "User-agent: *\nDisallow: /notices/page/\nAllow: /notices/page/2\n")
        self.assertTrue(policy.allowed("http://h/notices/page/2", self.UA))
        self.assertFalse(policy.allowed("http://h/notices/page/3", self.UA))

    def test_wildcard_and_anchor(self):
        policy = RobotsPolicy.from_text(
            "User-agent: archive-bot\nDisallow: /feed*sort=desc\n"
            "Disallow: /exact$\n")
        self.assertFalse(policy.allowed("http://h/feed?page=1&sort=desc", self.UA))
        self.assertTrue(policy.allowed("http://h/feed?page=1&sort=asc", self.UA))
        self.assertFalse(policy.allowed("http://h/exact", self.UA))
        self.assertTrue(policy.allowed("http://h/exact/more", self.UA))

    def test_query_part_is_matched(self):
        policy = RobotsPolicy.from_text(
            "User-agent: *\nDisallow: /x?private=1\n")
        self.assertFalse(policy.allowed("http://h/x?private=1", self.UA))
        self.assertTrue(policy.allowed("http://h/x?a=1", self.UA))

    def test_unavailable_policies(self):
        deny = RobotsPolicy.from_unavailable("http_404", "disallow_all")
        allow = RobotsPolicy.from_unavailable("http_404", "allow_all")
        self.assertFalse(deny.allowed("http://h/x", self.UA))
        self.assertTrue(allow.allowed("http://h/x", self.UA))

    def test_crawl_delay_is_reported_not_used(self):
        policy = RobotsPolicy.from_text("User-agent: *\nCrawl-delay: 5\n")
        self.assertEqual(policy.crawl_delay, "5")
        self.assertTrue(policy.allowed("http://h/x", self.UA))


LAYOUT_A_PAGE = """
<html><body><nav><a href='/x'>nav</a></nav>
<article class="notice" data-key="A-1">
  <h1 class="notice-title">Title one</h1>
  <dl class="facts"><dt>编号</dt><dd>A-1</dd>
    <dt>发布日期</dt><dd>2026-03-02</dd></dl>
  <div class="body"><p>First paragraph.</p><p>Second  paragraph.</p></div>
</article>
<footer><p>footer v1</p></footer></body></html>
"""

LAYOUT_B_PAGE = """
<html><body><section class="topbar">nav</section>
<section class="entry" data-ref="B-1">
  <h2>Title two</h2>
  <div class="meta">
    <div class="row"><span class="label">Ref</span><span class="value">B-1</span></div>
    <div class="row"><span class="label">Issued</span><span class="value">2026-13-45</span></div>
  </div>
  <div class="content"><p>Alpha.</p><p>Beta <em>emphasised</em>.</p></div>
</section></body></html>
"""


class AdapterTests(unittest.TestCase):
    def test_layout_a_fields(self):
        root, _ = parse_html(LAYOUT_A_PAGE)
        record = ADAPTERS["layout_a"].parse_detail(root, "http://h/x")
        self.assertEqual(record.document_key, "A-1")
        self.assertEqual(record.title, "Title one")
        self.assertEqual(record.issued_on, "2026-03-02")
        self.assertEqual(record.body, "First paragraph.\n\nSecond paragraph.")

    def test_layout_b_fields_and_bad_date(self):
        root, _ = parse_html(LAYOUT_B_PAGE)
        record = ADAPTERS["layout_b"].parse_detail(root, "http://h/y")
        self.assertEqual(record.document_key, "B-1")
        self.assertEqual(record.issued_on, "")
        self.assertTrue(any("Issued" in issue for issue in record.issues))
        self.assertEqual(record.body, "Alpha.\n\nBeta emphasised.")

    def test_layout_a_adapter_rejects_layout_b_page(self):
        root, _ = parse_html(LAYOUT_B_PAGE)
        with self.assertRaises(Exception):
            ADAPTERS["layout_a"].parse_detail(root, "http://h/y")

    def test_content_normal_form_ignores_markup_churn(self):
        changed = LAYOUT_A_PAGE.replace(
            "<p>Second  paragraph.</p>",
            "<p>Second <strong>paragraph</strong>.</p>")
        a, _ = parse_html(LAYOUT_A_PAGE)
        b, _ = parse_html(changed)
        ra = ADAPTERS["layout_a"].parse_detail(a, "http://h/x")
        rb = ADAPTERS["layout_a"].parse_detail(b, "http://h/x")
        self.assertEqual(ra.body, rb.body)
        self.assertEqual(
            business_digest(ra.title, ra.issued_on, ra.body),
            business_digest(rb.title, rb.issued_on, rb.body))

    def test_footer_change_does_not_affect_business_fields(self):
        changed = LAYOUT_A_PAGE.replace("footer v1", "footer v2")
        a, _ = parse_html(LAYOUT_A_PAGE)
        b, _ = parse_html(changed)
        ra = ADAPTERS["layout_a"].parse_detail(a, "http://h/x")
        rb = ADAPTERS["layout_a"].parse_detail(b, "http://h/x")
        self.assertNotEqual(LAYOUT_A_PAGE, changed)  # raw really differs
        self.assertEqual(ra.body, rb.body)
        self.assertEqual(ra.title, rb.title)

    def test_body_change_does_change_digest(self):
        changed = LAYOUT_A_PAGE.replace("Second  paragraph.", "Second paragraph changed.")
        a, _ = parse_html(LAYOUT_A_PAGE)
        b, _ = parse_html(changed)
        ra = ADAPTERS["layout_a"].parse_detail(a, "http://h/x")
        rb = ADAPTERS["layout_a"].parse_detail(b, "http://h/x")
        self.assertNotEqual(
            business_digest(ra.title, ra.issued_on, ra.body),
            business_digest(rb.title, rb.issued_on, rb.body))
        diff = paragraph_diff(ra.body, rb.body)
        self.assertIn("Second paragraph changed.", diff["paragraphs_added"])
        self.assertIn("Second paragraph.", diff["paragraphs_removed"])

    def test_parser_tolerates_malformed_markup(self):
        root, notes = parse_html("<p>ok<div>unclosed</p></div></span>")
        self.assertTrue(root is not None)


if __name__ == "__main__":
    unittest.main()
