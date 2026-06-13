"""Sending links by email.

There's no mail server wired up yet, so in dev mode we log the link to the
server output and let the caller show it on screen. To go live, flip
DEV_SHOW_LINKS to False and send a real message inside send_link (SMTP, or a
provider like Postmark or SES).
"""

import sys

# In dev we reveal the link in the UI because nothing can actually email it.
# Set to False in production so links only ever travel by email.
DEV_SHOW_LINKS = True


def send_link(to_email, subject, url):
    """'Send' an email containing a link. In dev mode this only logs it."""
    print(f"[email -> {to_email}] {subject}\n    {url}", file=sys.stderr)
    # TODO production: send a real email here, then return.
    return url
