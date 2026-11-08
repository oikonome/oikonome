"""Every Settings section the server links into, in one place.

Alerts, emails and pushes deep-link into the web app's Settings page by
section id (`/settings/<id>`). A link to an id the page does not know
renders an empty pane: the owner lands nowhere with the alert already
dismissed. Build those links with `settings_link`, which accepts only the
ids listed here; tests/test_settings_deep_links_resolve.py checks this
table against the sections webapp/src/pages/Settings.tsx accepts and the
mobile link mapping, so a renamed section fails a test instead of a
household."""

# id -> what lives there (for the reader; the id is the contract)
SECTIONS = {
    "connections": "bank connections and collector script tokens",
    "data": "export, import and maintenance",
    # only present when the installed add-on has an account section
    "billing": "the add-on's account section",
}


def settings_link(section: str) -> str:
    """The web path of one Settings section."""
    if section not in SECTIONS:
        raise KeyError(f"unknown Settings section: {section}")
    return f"/settings/{section}"
