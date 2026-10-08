"""First-run setup core: discover what a host is, plan what Planet Express would change, apply it.

Headless and JSON in/out so a terminal wizard and the browser wizard are both thin clients
(docs/designs/installer-brief.md, section 6). This package runs BEFORE any Planet Express config
exists, so it must never import `config` or anything that loads it.
"""
