# Install the Solo Enterprise for agentgateway binary for this version.
# Test-only: the consuming site's workflow copies this file over
# assets/agw-docs/snippets/install-agentgateway-binary.md, which shadows the
# open source snippet the upstream annotations reuse. Same commands as the
# site's install guide (assets/agw-docs/standalone/binary-install.md), pinned
# to the version that guide documents. The workflow sets
# ENTERPRISE_AGENTGATEWAY_LICENSE_KEY; the binary refuses to start without it.
if [ -z "${ENTERPRISE_AGENTGATEWAY_LICENSE_KEY:-}" ]; then
  echo "ENTERPRISE_AGENTGATEWAY_LICENSE_KEY is not set" >&2
  exit 1
fi
export PATH="$HOME/.agentgateway/bin:$PATH"
if ! command -v agentgateway >/dev/null 2>&1 || ! agentgateway --version 2>/dev/null | grep -q '{{< reuse "agw-docs/versions/n-patch.md" >}}'; then
  curl -fsSL https://run.solo.io/agentgateway/install | AGENTGATEWAY_VERSION=v{{< reuse "agw-docs/versions/n-patch.md" >}} sh
fi
