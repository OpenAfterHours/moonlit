"""moonlit zipapp bootstrap (stdlib-only, D7).

Shipped verbatim into every produced .pyz; runs before staged site-packages
reaches sys.path. Implementation lands module-by-module per
specs/03-bootstrap-runtime.md.
"""
