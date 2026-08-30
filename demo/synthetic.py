"""A fabricated company, four fabricated people, and the traps they demonstrate.

Every name, address and number here is invented. The numbers are drawn from the
ranges Ofcom reserves for drama - 07700 900xxx and 020 7946 0xxx - so nothing in
this corpus can dial a real person even if it escapes the demo. The domains sit
under `.example`, which RFC 2606 guarantees will never resolve.

The corpus is built to break things, not to flatter the pipeline:

  switchboard trap   the contact page's reception number is printed under the
                     team's own heading, where a proximity scan will attach it
                     to whichever name is nearest
  neighbour trap     Amara's directory row is immediately followed by Priya's,
                     so a text window around Amara's number reaches into
                     Priya's entry - the failure that structural binding and
                     the colleague's-address check both exist to prevent
  same-name trap     a different Amara Okafor, a ceramicist, publishes her
                     number on her own site with no link to Northwind at all
  nothing-to-find    Tobias is on the team page and publishes no number
                     anywhere, so the only honest answer is not_found
"""

from __future__ import annotations

COMPANY = "Northwind Analytics"
DOMAIN = "northwind.example"
REGION = "GB"

# --------------------------------------------------------------------- people
PEOPLE = {
    "amara": {"name": "Amara Okafor", "email": "amara.okafor@northwind.example",
              "expect": "found", "why": "directory row plus an independent speaker page"},
    "priya": {"name": "Priya Raman", "email": "priya.raman@northwind.example",
              "expect": "found", "why": "direct dial in the staff directory, landline only"},
    "tobias": {"name": "Tobias Lindqvist", "email": "t.lindqvist@northwind.example",
               "expect": "not_found", "why": "listed on the team page, publishes no number"},
}

_HOME = """<!doctype html><html><head><title>Northwind Analytics</title></head>
<body>
  <nav>
    <a href="/">Home</a>
    <a href="/platform">Platform</a>
    <a href="/team">Team</a>
    <a href="/contact">Contact</a>
    <a href="/about/people">Staff directory</a>
    <a href="/careers">Careers</a>
  </nav>
  <h1>Northwind Analytics</h1>
  <p>Measurement infrastructure for logistics operators. Founded 2019, London.</p>
</body></html>"""

# The reception number sits directly under a heading about the team. A scan that
# reads "a number near a person's name" attributes this to whoever is listed
# first; it reaches a receptionist.
_CONTACT = """<!doctype html><html><head><title>Contact - Northwind Analytics</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Organization","name":"Northwind Analytics",
 "telephone":"+44 20 7946 0800","email":"info@northwind.example",
 "address":{"@type":"PostalAddress","addressLocality":"London"}}
</script></head>
<body>
  <h1>Contact us</h1>
  <p>Reception: <a href="tel:+442079460800">020 7946 0800</a></p>
  <p>General enquiries: <a href="mailto:info@northwind.example">info@northwind.example</a></p>
  <h2>Talk to the team</h2>
  <p>Amara Okafor leads our data platform group and Priya Raman runs ingestion.
     Ask reception and we will put you through.</p>
</body></html>"""

_TEAM = """<!doctype html><html><head><title>Team - Northwind Analytics</title></head>
<body>
  <h1>Our team</h1>
  <ul>
    <li><strong>Amara Okafor</strong> - Head of Data Platform -
        <a href="mailto:amara.okafor@northwind.example">amara.okafor@northwind.example</a></li>
    <li><strong>Priya Raman</strong> - Data Engineer -
        <a href="mailto:priya.raman@northwind.example">priya.raman@northwind.example</a></li>
    <li><strong>Tobias Lindqvist</strong> - Product Manager -
        <a href="mailto:t.lindqvist@northwind.example">t.lindqvist@northwind.example</a></li>
  </ul>
</body></html>"""

# Stacked rows, each with a name, an address and a direct dial. Read as flat
# text this is a minefield; read as markup it is the cleanest source there is.
_DIRECTORY = """<!doctype html><html><head><title>Staff directory - Northwind</title></head>
<body>
  <h1>Staff directory</h1>
  <table>
    <tr><th>Name</th><th>Role</th><th>Email</th><th>Direct line</th></tr>
    <tr>
      <td>Amara Okafor</td><td>Head of Data Platform</td>
      <td>amara.okafor@northwind.example</td>
      <td><a href="tel:+442079460812">020 7946 0812</a></td>
    </tr>
    <tr>
      <td>Priya Raman</td><td>Data Engineer</td>
      <td>priya.raman@northwind.example</td>
      <td><a href="tel:+442079460813">020 7946 0813</a></td>
    </tr>
    <tr>
      <td>Tobias Lindqvist</td><td>Product Manager</td>
      <td>t.lindqvist@northwind.example</td>
      <td>via reception</td>
    </tr>
  </table>
</body></html>"""

# A second, independent host. Amara published a mobile here herself, alongside
# the work address - which is what lets the pipeline tie it to her rather than
# to the several thousand other people with her name.
_SPEAKER = """<!doctype html><html><head><title>Speakers - DataDay 2026</title></head>
<body>
  <h1>DataDay 2026 speakers</h1>
  <article>
    <h2>Amara Okafor</h2>
    <p>Head of Data Platform, Northwind Analytics.</p>
    <p>Amara is happy to be contacted about the talk:
       amara.okafor@northwind.example, mobile 07700 900412.</p>
  </article>
  <article>
    <h2>Ravi Shanmugam</h2>
    <p>Principal Engineer, Fernbank Systems. ravi@fernbank.example, 07700 900555.</p>
  </article>
</body></html>"""

# Same name, different person, no connection to Northwind anywhere on the page.
_NAMESAKE = """<!doctype html><html><head><title>Amara Okafor - Ceramics</title></head>
<body>
  <h1>Amara Okafor</h1>
  <p>Hand-thrown stoneware from a small studio in Leeds. Commissions welcome.</p>
  <p>Call the studio on 07700 900654 or come to an open day.</p>
</body></html>"""

PAGES = {
    "https://northwind.example": _HOME,
    "https://northwind.example/": _HOME,
    "https://northwind.example/contact": _CONTACT,
    "https://northwind.example/team": _TEAM,
    "https://northwind.example/about/people": _DIRECTORY,
    "https://dataday.example/speakers/2026": _SPEAKER,
    "https://amara-okafor.example": _NAMESAKE,
    "https://amara-okafor.example/": _NAMESAKE,
}

# Canned search results, keyed by a substring of the query. Two entries matter:
# a query quoting the *work address* finds only pages already about that person,
# while a query on the bare name also drags in the ceramicist.
RESULTS = {
    '"amara.okafor@northwind.example"': [
        ("Speakers - DataDay 2026", "https://dataday.example/speakers/2026",
         "Amara Okafor, Head of Data Platform, Northwind Analytics. "
         "amara.okafor@northwind.example, mobile 07700 900412."),
    ],
    '"Amara Okafor"': [
        ("Speakers - DataDay 2026", "https://dataday.example/speakers/2026",
         "Amara Okafor, Head of Data Platform, Northwind Analytics. "
         "amara.okafor@northwind.example, mobile 07700 900412."),
        ("Amara Okafor - Ceramics", "https://amara-okafor.example",
         "Hand-thrown stoneware from a small studio in Leeds. "
         "Call the studio on 07700 900654."),
    ],
    "site:northwind.example": [
        ("Staff directory - Northwind", "https://northwind.example/about/people",
         "Name, role, email and direct line for everyone at Northwind Analytics."),
    ],
}


def web():
    """A FakeWeb loaded with this corpus."""
    from .fake_web import FakeWeb
    return FakeWeb(PAGES, RESULTS)
