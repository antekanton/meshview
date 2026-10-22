"""Fork release version from the container build, with local fallback."""

from meshview.__version__ import resolve_revision, resolve_version


def test_release_tag_drops_rel_prefix():
    assert resolve_version("3.0.8", {"MESHVIEW_VERSION": "rel-3.0.8.1.0"}) == "3.0.8.1.0"


def test_rc_suffix_is_kept():
    env = {"MESHVIEW_VERSION": "rel-3.0.8.1.0-rc1"}
    assert resolve_version("3.0.8", env) == "3.0.8.1.0-rc1"


def test_local_run_falls_back_to_upstream_version():
    assert resolve_version("3.0.8", {}) == "3.0.8"
    # Local `docker build` without build args leaves the variables empty.
    assert resolve_version("3.0.8", {"MESHVIEW_VERSION": "  "}) == "3.0.8"


def test_revision_from_build_or_none():
    sha = "8572f364c8a849ad7b477da8ebfd3470402b0de3"
    assert resolve_revision({"MESHVIEW_REVISION": sha}) == sha
    assert resolve_revision({"MESHVIEW_REVISION": ""}) is None
    assert resolve_revision({}) is None


def test_get_git_revision_prefers_build_revision(monkeypatch):
    from meshview import __version__ as version

    sha = "8572f364c8a849ad7b477da8ebfd3470402b0de3"
    monkeypatch.setenv("MESHVIEW_REVISION", sha)
    assert version.get_git_revision() == sha
    assert version.get_git_revision_short() == "8572f36"
