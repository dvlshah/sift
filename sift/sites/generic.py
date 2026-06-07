"""GenericProfile — minimal fallback for sites with no special structure.

Use as a starting point for new site profiles. Every URL becomes LIVING tier,
no facts are extracted, no special boilerplate is stripped, no excludes.
Override the methods/properties you need; leave the rest.

This is also the implicit default if no profile is configured.
"""

from . import SiteProfile


class GenericProfile(SiteProfile):
    name = "generic"
