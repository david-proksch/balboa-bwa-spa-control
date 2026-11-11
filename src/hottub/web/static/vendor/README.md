# Third-party files served to the browser

These are copied in, not installed, so the Pi needs neither Node nor
internet access to serve the pages.

| File | What | Version | From | SHA-384 (base64, as in a Subresource Integrity attribute) |
|------|------|---------|------|-----------------------------------------------------------|
| `htmx-2.0.4.min.js` | [HTMX](https://htmx.org), BSD Zero-Clause licence | 2.0.4 | `https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js` | `HGfztofotfshcF7+8n44JQL2oJmowVChPTg48S+jvZoztPfvwD79OC/LTtG6dMp+` |

The checksum matches the one published in HTMX's own installation
instructions for this version.

To upgrade: download the new file next to the old one under its versioned
name, check it against the published checksum, update the `<script>` tag in
`templates/base.html` and this table, and delete the old file. How the
pages use HTMX is explained at the top of `hottub/web/pages.py`.
