from eddn_validator.github import select_versions

EDDLITE = {
    "tag_pattern": r"^Release_(?P<v>\d+\.\d+\.\d+)$",
    "version_format": "{v}.0",
    "keep_releases": 3,
    "aliases": {"3.1.0.0": ["3.0.0.0"]},
}
EDMC = {
    "tag_pattern": r"^Release/(?P<v>\d+\.\d+\.\d+)$",
    "version_format": "{v}",
    "keep_releases": 3,
}


def release(tag, published, prerelease=False, draft=False):
    return {"tag_name": tag, "published_at": published, "prerelease": prerelease, "draft": draft}


def current(versions):
    return sorted(v.version for v in versions if v.is_current)


def test_edmc_skips_prereleases_and_keeps_newest_three():
    releases = [
        release("Release/6.1.3-beta3", "2026-05-30T17:51:00Z", prerelease=True),
        release("Release/6.1.2", "2026-01-29T03:06:00Z"),
        release("Release/6.1.2-beta1", "2026-01-27T21:48:00Z", prerelease=True),
        release("Release/6.1.1", "2026-01-25T16:57:00Z"),
        release("Release/6.1.0", "2026-01-21T22:59:00Z"),
        release("Release/6.0.2", "2025-12-26T00:00:00Z"),
    ]
    versions = select_versions(releases, EDMC)
    assert current(versions) == ["6.1.0", "6.1.1", "6.1.2"]
    assert {v.version for v in versions} == {"6.1.0", "6.1.1", "6.1.2", "6.0.2"}


def test_eddlite_aliases_and_draft():
    releases = [
        release("Release_3.4.0", "2026-10-01T00:00:00Z", draft=True),
        release("Release_3.3.0", "2025-05-01T00:00:00Z"),
        release("Release_3.2.0", "2024-12-26T00:00:00Z"),
        release("Release_3.1.0", "2024-12-09T00:00:00Z"),
        release("Release_3.0.0", "2024-11-08T00:00:00Z"),
        release("Real-Release_3_1_0", "2024-12-09T00:00:00Z"),
    ]
    versions = select_versions(releases, EDDLITE)
    # 3.0.0.0 is current through the 3.1.0 alias even though 3.0.0 itself is 4th newest.
    assert current(versions) == ["3.0.0.0", "3.1.0.0", "3.2.0.0", "3.3.0.0"]


def test_order_comes_from_publish_date_not_api_order():
    releases = [
        release("Release/6.0.0", "2025-12-13T00:00:00Z"),
        release("Release/6.1.2", "2026-01-29T00:00:00Z"),
        release("Release/6.1.1", "2026-01-25T00:00:00Z"),
        release("Release/6.1.0", "2026-01-21T00:00:00Z"),
    ]
    assert current(select_versions(releases, EDMC)) == ["6.1.0", "6.1.1", "6.1.2"]
