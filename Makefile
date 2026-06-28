# Convenience targets for the meeting-brief agent. The scheduled job and the manual
# run are the SAME command (scheduled_run.py); these are just shortcuts.

PYTHON ?= python

.PHONY: brief brief-local brief-date test help

help:
	@echo "make brief                 # today's packet (Europe/Paris), email the PDF (what cron runs)"
	@echo "make brief-local           # build + save the PDF under out/, do NOT send"
	@echo "make brief-date DATE=2026-06-29   # run for a specific day, email the PDF"
	@echo "make test                  # offline unit tests"

# Run now and email — identical to what the scheduler executes.
brief:
	$(PYTHON) scheduled_run.py

# Run now, save the PDF locally, skip sending.
brief-local:
	$(PYTHON) scheduled_run.py --no-email

# Run for a specific day: make brief-date DATE=2026-06-29
brief-date:
	$(PYTHON) scheduled_run.py --date $(DATE)

test:
	$(PYTHON) -m pytest -q tests/test_pdf_unit.py tests/test_mailer_unit.py
