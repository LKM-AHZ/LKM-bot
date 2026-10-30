"""LKM-specific integration boundary for this AstrBot distribution.

Keep community SSO and dashboard base-path handling here so upstream merges
can identify local integration changes without renaming the astrbot package.

Dashboard modules import this package. Avoid module-level dashboard imports
here; SSO may load dashboard implementations inside request handlers to avoid
circular imports.
"""
