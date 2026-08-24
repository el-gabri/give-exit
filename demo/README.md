# Demo data governance

`manifest.json` is the inventory and integrity record for every PDF committed
under this directory. A PDF addition or replacement must update its SHA-256,
classification, provenance, and review metadata in the same change.

`contains_personal_data` means the file is known or reasonably expected to
contain data about a real identifiable person. Synthetic fixtures can still
contain names and identifier-shaped placeholders; those are marked separately
as `contains_personal_data_like_content` and should be handled as sensitive in
tests and telemetry.

The real filing is retained because it was reported as publicly accessible.
Public accessibility does not by itself establish unrestricted redistribution,
waive LGPD duties, or document compliance with court terms and other applicable
rights. Its exact source URL and redistribution basis are not recorded here, so
the manifest deliberately marks redistribution as `not_established`. Record and
review those facts before publishing or redistributing the fixture elsewhere.
