# Add or change a doc test on one page, without touching the content repo.
#
#   make annotate PRODUCT=agentgateway PAGE=assets/agw-docs/pages/traffic-management/direct-response.md
#   ...edit out/annotate/<PAGE>: add paths= tags and {{< doc-test >}} checks...
#   make save PRODUCT=agentgateway PAGE=<same page>
#
# CONTENT_REPO is a sibling clone of the content repo.

PRODUCT ?= agentgateway
CONTENT_REPO ?= ../agentgateway-oss-website
ANNOTATIONS = products/$(PRODUCT)/annotations
EDITED = out/annotate/$(PAGE)

.PHONY: annotate save unit-test roundtrip

annotate:
	@test -n "$(PAGE)" || (echo "set PAGE=<repo-relative page path>" && exit 1)
	python3 scripts/annotations.py annotate --repo $(CONTENT_REPO) --annotations $(ANNOTATIONS) --page $(PAGE) --out $(EDITED)

save:
	@test -n "$(PAGE)" || (echo "set PAGE=<repo-relative page path>" && exit 1)
	python3 scripts/annotations.py save --repo $(CONTENT_REPO) --annotations $(ANNOTATIONS) --page $(PAGE) --edited $(EDITED)

unit-test:
	DOCS_REPO_ROOT=$(abspath $(CONTENT_REPO)) python3 -m unittest discover -s scripts -p 'test_*.py'

roundtrip:
	python3 scripts/annotations.py roundtrip --repo $(CONTENT_REPO)
